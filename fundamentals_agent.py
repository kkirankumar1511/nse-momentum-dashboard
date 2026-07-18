"""
Fundamentals agent.

Two layers:
  1. Deterministic scraper: pulls key ratios (ROCE, ROE, D/E, profit growth,
     promoter holding) from screener.in's public pages.
  2. Optional AI layer: if ANTHROPIC_API_KEY is set, Claude (with web search)
     synthesizes a short qualitative brief per stock — recent results, order
     book, red flags — and returns a structured verdict.

Note on data terms: screener.in and NSE pages are for personal use; respect
their rate limits (this module sleeps between requests) and terms of service.
"""

from __future__ import annotations

import json
import re
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

import config
import nse_api
import xbrl_parser

HEADERS = {"User-Agent": "Mozilla/5.0 (personal research dashboard)"}


# ---------------------------------------------------------------------------
# Layer 1: deterministic ratio scraping
# ---------------------------------------------------------------------------

def _num(text: str) -> float | None:
    m = re.search(r"-?[\d,]+\.?\d*", text.replace(",", ""))
    return float(m.group()) if m else None


def fetch_ratios(symbol: str) -> dict:
    """Key ratios from screener.in. Returns {} on failure."""
    url = f"https://www.screener.in/company/{symbol}/consolidated/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            r = requests.get(f"https://www.screener.in/company/{symbol}/",
                             headers=HEADERS, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return {}

    soup = BeautifulSoup(r.text, "lxml")
    ratios: dict[str, float | None] = {}
    for li in soup.select("#top-ratios li"):
        name_el = li.select_one(".name")
        val_el = li.select_one(".value") or li.select_one(".number")
        if not name_el or not val_el:
            continue
        name = name_el.get_text(strip=True).lower()
        val = _num(val_el.get_text(" ", strip=True))
        if "roce" in name:
            ratios["roce"] = val
        elif "roe" in name:
            ratios["roe"] = val
        elif "market cap" in name:
            ratios["market_cap_cr"] = val
        elif "stock p/e" in name or name == "p/e":
            ratios["pe"] = val
        elif "debt to equity" in name:
            ratios["debt_to_equity"] = val

    # Compounded profit growth table (TTM / 3Y)
    for section in soup.select("table.ranges-table"):
        header = section.find_previous(["th", "h3", "p"])
        text = section.get_text(" ", strip=True).lower()
        if "profit growth" in (header.get_text(strip=True).lower() if header else "") \
                or "compounded profit growth" in text:
            m = re.search(r"ttm[:\s]+(-?[\d.]+)", text)
            if m:
                ratios["profit_growth_ttm"] = float(m.group(1))
    return ratios


def passes_quality_gate(ratios: dict, cfg: dict = config.STRATEGY) -> tuple[bool, list[str]]:
    """Quality-Minus-Junk style filter. Returns (passed, reasons_failed)."""
    fails = []
    roce = ratios.get("roce")
    de = ratios.get("debt_to_equity")
    growth = ratios.get("profit_growth_ttm")

    if roce is not None and roce < cfg["min_roce"]:
        fails.append(f"ROCE {roce:.1f}% < {cfg['min_roce']}%")
    if de is not None and de > cfg["max_debt_to_equity"]:
        fails.append(f"D/E {de:.2f} > {cfg['max_debt_to_equity']}")
    if growth is not None and growth < cfg["min_profit_growth_yoy"]:
        fails.append(f"profit growth {growth:.1f}% < {cfg['min_profit_growth_yoy']}%")
    return (len(fails) == 0, fails)


def fetch_universe_fundamentals(symbols: list[str], pause: float = 1.5) -> dict[str, dict]:
    out = {}
    for sym in symbols:
        out[sym] = fetch_ratios(sym)
        time.sleep(pause)  # be polite to the source
    return out


# ---------------------------------------------------------------------------
# Layer 1b: value-investing score from primary XBRL — no scraping, no LLM.
# Deterministic, so it's cheap enough to run across the entire F&O universe
# (~210 names) rather than just the LLM-shortlisted few.
#
# Banks and NBFCs file under structurally different XBRL taxonomies (no
# Revenue/CurrentAssets tags at all for banks; no NPA/NIM data for the
# general taxonomy) — value_score() would silently under-score them, so
# fno_value_scan() routes each symbol to the rubric that matches what its
# filings actually contain: xbrl_parser.value_score / bank_score / nbfc_score.
# Insurers (LI taxonomy) aren't covered yet — their key metrics (persistency,
# embedded value, solvency ratio) aren't reliably XBRL-tagged, unverified
# rather than guessed at.
# ---------------------------------------------------------------------------

def fno_value_scan(symbols: list[str] | None = None, n_years: int = 3,
                   use_live_price: bool = True, pause: float = 0.3,
                   progress_cb=None) -> pd.DataFrame:
    """Run the sector-appropriate xbrl_parser score across the F&O universe
    (or a given symbol list). Purely deterministic — safe to run on all ~210
    F&O names, unlike the AI deep-read stage which is scoped to a shortlist.

    use_live_price: fetch LTPs via kite_client (one batched call) to enable
    the PEG sub-score (general rubric only). Needs an active Kite session;
    falls back to leaving PEG unavailable (not faked) if that fails.
    """
    symbols = symbols if symbols is not None else config.UNIVERSE

    prices: dict[str, float] = {}
    if use_live_price:
        try:
            import kite_client
            prices = kite_client.get_ltp(symbols)
        except Exception as e:
            print(f"[fno_value_scan] live price fetch failed, PEG will be "
                  f"unavailable for all symbols: {e}", flush=True)

    rows = []
    for i, sym in enumerate(symbols):
        if progress_cb:
            progress_cb(f"{sym} ({i + 1}/{len(symbols)})...", (i + 1) / len(symbols))
        try:
            taxonomy = nse_api.filing_taxonomy(sym)
            bs = xbrl_parser.annual_balance_sheet(sym, n_years=n_years)
            if taxonomy == "banking":
                score = xbrl_parser.bank_score(bs)
            elif taxonomy == "nbfc":
                score = xbrl_parser.nbfc_score(bs)
            elif taxonomy == "general_insurance":
                score = xbrl_parser.general_insurance_score(bs)
            elif taxonomy == "life_insurance":
                score = xbrl_parser.life_insurance_score(bs)
            elif taxonomy == "general":
                score = xbrl_parser.value_score(bs, market_price=prices.get(sym))
            else:
                score = {"total_score": None, "rubric": taxonomy,
                        "missing_pillars": ["unsupported_taxonomy"]}
        except Exception as e:
            print(f"[fno_value_scan] {sym}: failed: {e}", flush=True)
            score = {"total_score": None, "rubric": "error",
                    "missing_pillars": ["error"]}
        score["symbol"] = sym
        rows.append(score)
        time.sleep(pause)  # be polite to NSE

    df = pd.DataFrame(rows).set_index("symbol")
    cols = ["total_score", "rubric", "roe", "roa", "debt_to_equity",
            "current_ratio", "revenue_cagr_pct", "fcf_yoy_pct", "peg",
            "gross_npa_pct", "net_npa_pct", "nim_proxy_pct",
            "combined_ratio_pct", "incurred_claim_ratio_pct",
            "premium_yoy_pct", "pat_yoy_pct", "loan_yoy_pct", "advances_yoy_pct",
            "solvency_ratio_UNVERIFIED", "persistency_13m_UNVERIFIED",
            "fiscal_year_end", "missing_pillars", "pillar_scores", "sub_scores"]
    return df[[c for c in cols if c in df.columns]].sort_values(
        "total_score", ascending=False)


def flatten_for_export(df: pd.DataFrame) -> pd.DataFrame:
    """fno_value_scan() keeps pillar_scores/sub_scores as dict-valued cells,
    which is convenient for the dashboard (st.dataframe expands them via
    pd.Series) but renders as unwieldy dict-text blobs in a plain CSV viewer
    or spreadsheet. This expands them into individual numeric columns and
    turns missing_pillars (a list) into a plain comma-joined string, so the
    exported file is flat and viewer-agnostic."""
    out = df.drop(columns=["pillar_scores", "sub_scores"], errors="ignore").copy()
    if "pillar_scores" in df.columns:
        pillars = df["pillar_scores"].apply(lambda d: d if isinstance(d, dict) else {})
        out = out.join(pillars.apply(pd.Series).add_prefix("pillar_"))
    if "sub_scores" in df.columns:
        subs = df["sub_scores"].apply(lambda d: d if isinstance(d, dict) else {})
        out = out.join(subs.apply(pd.Series).add_prefix("sub_"))
    if "missing_pillars" in out.columns:
        out["missing_pillars"] = out["missing_pillars"].apply(
            lambda v: ", ".join(v) if isinstance(v, list) else v)
    return out


# ---------------------------------------------------------------------------
# Layer 2: AI qualitative brief (optional)
# ---------------------------------------------------------------------------

AI_SYSTEM = """You are an equity research assistant for Indian (NSE) stocks.
You are given a company's ratios and recent NSE announcements. Judge whether
this looks attractive for a 3-6 month momentum position. Base every claim on
the data provided — you have no web access, so do NOT invent news, prices,
or results you were not given. If the evidence is thin, say so.
Respond ONLY with JSON, no markdown fences:
{"symbol": str, "verdict": "positive"|"neutral"|"negative",
 "summary": "<=60 words", "red_flags": [str], "catalysts": [str]}"""

BRIEF_SCHEMA = {
    "symbol": {"type": "str", "default": ""},
    "verdict": {"type": "enum", "values": ["positive", "neutral", "negative"],
                "default": "neutral"},
    "summary": {"type": "str", "default": ""},
    "red_flags": {"type": "list"},
    "catalysts": {"type": "list"},
}


def ai_brief(symbol: str) -> dict:
    """Qualitative brief via the configured open-source LLM (see llm.py).

    NOTE: unlike the old paid-API version, local models have NO web search.
    We therefore feed them NSE announcements as the evidence base rather than
    letting them free-associate about recent news — which is the right call
    anyway: a model recalling half-remembered news from training data is worse
    than one reading the company's actual filings.
    """
    import llm
    import nse_api

    t0 = time.time()
    ok, _ = llm.is_available()
    if not ok:
        print(f"[ai_brief] {symbol}: LLM not available, skipping", flush=True)
        return {}

    print(f"[ai_brief] {symbol}: fetching ratios...", flush=True)
    ratios = fetch_ratios(symbol)

    print(f"[ai_brief] {symbol}: fetching NSE announcements...", flush=True)
    try:
        ann = nse_api.classify_announcements(symbol, days=180)
    except Exception as e:
        print(f"[ai_brief] {symbol}: announcements fetch failed: {e}", flush=True)
        ann = {"red_flags": [], "catalysts": [], "recent_announcements": []}

    evidence = [f"SYMBOL: {symbol}", f"RATIOS: {json.dumps(ratios)}"]
    if ann.get("red_flags"):
        evidence.append(f"DETECTED RED FLAGS: {json.dumps(ann['red_flags'])}")
    if ann.get("catalysts"):
        evidence.append(f"DETECTED CATALYSTS: {json.dumps(ann['catalysts'])}")
    rows = [f"{a['date']} [{a['desc']}] {a['text'][:150]}"
            for a in ann.get("recent_announcements", [])[:10]]
    if rows:
        evidence.append("RECENT NSE ANNOUNCEMENTS:\n" + "\n".join(rows))

    slow_note = (" (local CPU inference can take a few minutes, and the "
                 "first call also loads the model into RAM)"
                 if "local" in llm.describe() else "")
    print(f"[ai_brief] {symbol}: calling {llm.describe()}{slow_note}...",
          flush=True)
    try:
        out = llm.chat_json(AI_SYSTEM, "\n\n".join(evidence),
                            schema=BRIEF_SCHEMA, max_tokens=800)
        out["symbol"] = symbol
        print(f"[ai_brief] {symbol}: done in {time.time() - t0:.1f}s "
              f"-> verdict={out.get('verdict')}", flush=True)
        return out
    except Exception as e:
        print(f"[ai_brief] {symbol}: LLM call failed after "
              f"{time.time() - t0:.1f}s: {e}", flush=True)
        return {"symbol": symbol, "verdict": "neutral",
                "summary": f"AI brief unavailable: {e}",
                "red_flags": [], "catalysts": []}
