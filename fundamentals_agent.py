"""
Fundamentals agent: value-investing score from primary-source XBRL filings.

Deterministic, no scraping, no LLM -- cheap enough to run across the entire
F&O universe (~210 names) on every screen.

Banks and NBFCs file under structurally different XBRL taxonomies (no
Revenue/CurrentAssets tags at all for banks; no NPA/NIM data for the general
taxonomy) -- value_score() would silently under-score them, so fno_value_scan()
routes each symbol to the rubric that matches what its filings actually
contain: xbrl_parser.value_score / bank_score / nbfc_score / etc.
"""

from __future__ import annotations

import time

import pandas as pd

import config
import nse_api
import xbrl_parser


def fno_value_scan(symbols: list[str] | None = None, n_years: int = 3,
                   use_live_price: bool = True, pause: float = 0.3,
                   progress_cb=None) -> pd.DataFrame:
    """Run the sector-appropriate xbrl_parser score across the F&O universe
    (or a given symbol list).

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


def build_fundamentals_history(symbols: list[str] | None = None, n_years: int = 5,
                               pause: float = 0.3, progress_cb=None) -> dict:
    """Fetches each symbol's FULL annual history once (same underlying calls
    as fno_value_scan) and keeps the raw bs_years rows -- each already tagged
    with known_as_of by xbrl_parser -- instead of collapsing to a single
    current score. This is the one-time (or periodically refreshed) batch
    step for point-in-time backtesting: score_asof() then does the actual
    per-date scoring purely in memory against this, with zero network calls.

    Returns {symbol: {"taxonomy": str, "bs_years": list[dict]}}.
    """
    symbols = symbols if symbols is not None else config.UNIVERSE
    history: dict = {}
    for i, sym in enumerate(symbols):
        if progress_cb:
            progress_cb(f"{sym} ({i + 1}/{len(symbols)})...", (i + 1) / len(symbols))
        try:
            taxonomy = nse_api.filing_taxonomy(sym)
            bs = xbrl_parser.annual_balance_sheet(sym, n_years=n_years)
        except Exception as e:
            print(f"[build_fundamentals_history] {sym}: failed: {e}", flush=True)
            taxonomy, bs = "error", []
        history[sym] = {"taxonomy": taxonomy, "bs_years": bs}
        time.sleep(pause)  # be polite to NSE
    return history


_SCORERS = {
    "banking": xbrl_parser.bank_score,
    "nbfc": xbrl_parser.nbfc_score,
    "general_insurance": xbrl_parser.general_insurance_score,
    "life_insurance": xbrl_parser.life_insurance_score,
}


def score_asof(history: dict, date, score_cache: dict | None = None) -> pd.DataFrame:
    """Point-in-time fundamental scores for the whole universe, as of `date`
    -- the backtest-facing counterpart to fno_value_scan(). Pure in-memory:
    filters each symbol's pre-fetched bs_years down to what was knowable by
    `date` (xbrl_parser.fundamentals_asof), then runs it through the SAME
    scoring functions fno_value_scan uses live.

    PEG is never computed here (general rubric's value_score only, and it
    needs a live market price which doesn't exist for a historical date) --
    it will be systematically unavailable for every backtest-computed score,
    unlike the live Fundamentals page.

    score_cache: pass the SAME dict across repeated calls within one backtest
    run (not shared across separate runs) to skip rescoring a symbol whose
    knowable filing set hasn't changed between two rebalance dates -- keyed
    on (symbol, tuple of qe_dates actually included), which is safe by
    construction: identical included rows always produce an identical score,
    independent of any assumption about filing cadence or ordering.
    """
    if score_cache is None:
        score_cache = {}

    rows = []
    for sym, entry in history.items():
        filtered = xbrl_parser.fundamentals_asof(entry["bs_years"], date)
        key = (sym, tuple(r["qe_date"] for r in filtered))
        if key in score_cache:
            score = score_cache[key]
        else:
            taxonomy = entry["taxonomy"]
            try:
                if taxonomy in _SCORERS:
                    score = _SCORERS[taxonomy](filtered)
                elif taxonomy == "general":
                    score = xbrl_parser.value_score(filtered)
                else:
                    score = {"total_score": None, "rubric": taxonomy,
                            "missing_pillars": ["unsupported_taxonomy"]}
            except Exception as e:
                print(f"[score_asof] {sym}: failed: {e}", flush=True)
                score = {"total_score": None, "rubric": "error",
                        "missing_pillars": ["error"]}
            score_cache[key] = score
        row = dict(score)
        row["symbol"] = sym
        rows.append(row)

    df = pd.DataFrame(rows).set_index("symbol")
    cols = ["total_score", "rubric", "roe", "roa", "debt_to_equity",
            "current_ratio", "revenue_cagr_pct", "fcf_yoy_pct",
            "gross_npa_pct", "net_npa_pct", "nim_proxy_pct",
            "combined_ratio_pct", "incurred_claim_ratio_pct",
            "premium_yoy_pct", "pat_yoy_pct", "loan_yoy_pct", "advances_yoy_pct",
            "fiscal_year_end", "missing_pillars", "pillar_scores", "sub_scores"]
    return df[[c for c in cols if c in df.columns]]


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
