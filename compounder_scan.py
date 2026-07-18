"""
Compounder ("multibagger candidate") scanner.

=== READ THIS BEFORE USING ===

This module does NOT predict multibaggers. Nothing can. What it does is
score stocks on the *characteristics that multibaggers demonstrably had
before they ran*, so you get a research-grounded watchlist instead of a
tip-sheet.

Three things you must hold in mind:

1. TIME HORIZON. A multibagger (2x-10x) is a 3-7 year event, not a 3-6 month
   one. Marcellus's "Coffee Can" work on Indian equities, and Lynch's
   original framing, both rest on holding periods measured in years. This
   scanner is therefore DELIBERATELY SEPARATE from the momentum book: that
   one is a 3-6 month trading strategy with stops; this one is a long-term
   watchlist with no stops and no timing claim. Do not mix the two sleeves.

2. THE F&O UNIVERSE IS A HANDICAP HERE. F&O eligibility requires large market
   cap and heavy liquidity — the exact opposite of where multibaggers are
   usually found. Base rates matter: a Rs 5,000cr company 10x-ing to
   Rs 50,000cr has happened many times; a Rs 5,00,000cr company 10x-ing to
   Rs 50,00,000cr would exceed India's entire current market cap. So within
   F&O, this scanner tilts toward the smallest, fastest-growing names, and
   `--all-nse` lets you widen beyond F&O where the real hunting ground is.

3. SURVIVORSHIP. Every "multibagger trait" study looks at winners in
   hindsight. Thousands of stocks had identical traits and went nowhere or
   to zero. Treat a high score as "worth reading the annual report", never
   as a buy signal.

What the score actually measures (Lynch; Marcellus Coffee Can; Greenblatt's
Magic Formula; Fama-French RMW profitability factor):
  * Earnings growth, and whether it is ACCELERATING (the single best marker)
  * ROCE sustained above ~15-18% (cost of capital) — proves reinvestment works
  * Sales growth (real growth, not just margin one-offs)
  * Margin expansion (operating leverage kicking in)
  * Low debt (survives downturns without dilution)
  * High/stable promoter holding, no pledging (skin in the game, no red flag)
  * Smaller market cap (room to compound — the runway)
  * Reasonable PEG (not paying away the next 5 years of growth today)
"""

from __future__ import annotations

import re
import time

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

import config

HEADERS = {"User-Agent": "Mozilla/5.0 (personal research dashboard)"}

# Weights for the composite compounder score (sum = 1.0)
WEIGHTS = {
    "growth_accel": 0.22,
    "profit_growth": 0.18,
    "roce": 0.18,
    "sales_growth": 0.12,
    "margin_trend": 0.10,
    "low_debt": 0.08,
    "promoter": 0.06,
    "runway": 0.06,
}

THRESHOLDS = {
    "min_roce": 18.0,           # above cost of capital, sustained
    "min_profit_growth_3y": 15.0,
    "min_sales_growth_3y": 10.0,
    "max_debt_to_equity": 0.5,
    "min_promoter_holding": 40.0,
    "max_pledge": 5.0,
    "max_peg": 2.0,
    "max_mcap_cr": 100_000,     # runway filter: prefer < Rs 1 lakh cr
}


# ---------------------------------------------------------------------------
# Data extraction from screener.in
# ---------------------------------------------------------------------------

def _to_float(s: str):
    if not s:
        return None
    s = s.replace(",", "").replace("%", "").strip()
    m = re.search(r"-?\d+\.?\d*", s)
    return float(m.group()) if m else None


def _parse_growth_table(soup: BeautifulSoup, label: str) -> dict:
    """Screener.in growth tables: 'Compounded Sales Growth',
    'Compounded Profit Growth' with 10Y/5Y/3Y/TTM rows."""
    out = {}
    for table in soup.select("table.ranges-table"):
        head = table.find("th")
        if not head or label.lower() not in head.get_text(strip=True).lower():
            continue
        for row in table.select("tr")[1:]:
            cells = row.select("td")
            if len(cells) >= 2:
                period = cells[0].get_text(strip=True).rstrip(":").lower()
                out[period] = _to_float(cells[1].get_text(strip=True))
    return out


def _parse_quarterly_profit(soup: BeautifulSoup) -> list[float]:
    """Net profit row from the quarterly results table (oldest -> newest)."""
    section = soup.select_one("#quarters table")
    if not section:
        return []
    for row in section.select("tr"):
        label = row.select_one("td")
        if label and "net profit" in label.get_text(strip=True).lower():
            vals = [_to_float(td.get_text(strip=True))
                    for td in row.select("td")[1:]]
            return [v for v in vals if v is not None]
    return []


def _parse_opm(soup: BeautifulSoup) -> list[float]:
    """OPM % row from the annual P&L table (oldest -> newest)."""
    section = soup.select_one("#profit-loss table")
    if not section:
        return []
    for row in section.select("tr"):
        label = row.select_one("td")
        if label and "opm" in label.get_text(strip=True).lower():
            vals = [_to_float(td.get_text(strip=True))
                    for td in row.select("td")[1:]]
            return [v for v in vals if v is not None]
    return []


def fetch_company_data(symbol: str) -> dict:
    """Full fundamental pull for one symbol from screener.in."""
    for suffix in ("consolidated/", ""):
        try:
            r = requests.get(f"https://www.screener.in/company/{symbol}/{suffix}",
                             headers=HEADERS, timeout=15)
            if r.status_code == 200:
                break
        except requests.RequestException:
            return {}
    else:
        return {}

    soup = BeautifulSoup(r.text, "lxml")
    d: dict = {"symbol": symbol}

    # top ratios
    for li in soup.select("#top-ratios li"):
        name_el, val_el = li.select_one(".name"), li.select_one(".value")
        if not name_el or not val_el:
            continue
        name = name_el.get_text(strip=True).lower()
        val = _to_float(val_el.get_text(" ", strip=True))
        if "market cap" in name:
            d["mcap_cr"] = val
        elif "stock p/e" in name:
            d["pe"] = val
        elif "roce" in name:
            d["roce"] = val
        elif "roe" in name:
            d["roe"] = val
        elif "debt to equity" in name:
            d["debt_to_equity"] = val
        elif "promoter holding" in name:
            d["promoter_holding"] = val
        elif "pledged" in name:
            d["pledged_pct"] = val
        elif "dividend yield" in name:
            d["div_yield"] = val

    profit_growth = _parse_growth_table(soup, "Compounded Profit Growth")
    sales_growth = _parse_growth_table(soup, "Compounded Sales Growth")
    d["profit_growth_3y"] = profit_growth.get("3 years")
    d["profit_growth_5y"] = profit_growth.get("5 years")
    d["profit_growth_ttm"] = profit_growth.get("ttm")
    d["sales_growth_3y"] = sales_growth.get("3 years")
    d["sales_growth_5y"] = sales_growth.get("5 years")
    d["sales_growth_ttm"] = sales_growth.get("ttm")

    d["quarterly_profit"] = _parse_quarterly_profit(soup)
    d["opm_history"] = _parse_opm(soup)
    return d


# ---------------------------------------------------------------------------
# Derived signals
# ---------------------------------------------------------------------------

def growth_acceleration(d: dict) -> float | None:
    """TTM profit growth minus 3Y profit growth. Positive = accelerating.
    Lynch's core insight: the market re-rates *acceleration*, and that
    re-rating (PE expansion x earnings growth) is what produces multibaggers."""
    ttm, three = d.get("profit_growth_ttm"), d.get("profit_growth_3y")
    if ttm is None or three is None:
        return None
    return ttm - three


def margin_trend(d: dict) -> float | None:
    """Recent OPM vs 5-year-ago OPM (pct points). Expanding = operating
    leverage, a hallmark of scaling compounders."""
    opm = d.get("opm_history") or []
    if len(opm) < 4:
        return None
    recent = np.mean(opm[-2:])
    older = np.mean(opm[-6:-4]) if len(opm) >= 6 else opm[0]
    return float(recent - older)


def peg(d: dict) -> float | None:
    pe, g = d.get("pe"), d.get("profit_growth_3y")
    if not pe or not g or g <= 0:
        return None
    return pe / g


def quarterly_consistency(d: dict) -> float | None:
    """Share of the last 8 quarters where net profit grew YoY. Consistency
    separates real compounders from cyclical one-offs."""
    q = d.get("quarterly_profit") or []
    if len(q) < 8:
        return None
    wins = sum(1 for i in range(4, len(q)) if q[i] > q[i - 4])
    return wins / max(len(q) - 4, 1) * 100


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _clip_score(val, lo, hi):
    """Map val into 0..1 across [lo, hi]."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return np.nan
    return float(np.clip((val - lo) / (hi - lo), 0, 1))


def score_company(d: dict) -> dict:
    """0-100 compounder score + the reasons behind it."""
    accel = growth_acceleration(d)
    mtrend = margin_trend(d)
    peg_val = peg(d)

    parts = {
        "growth_accel": _clip_score(accel, -10, 30),
        "profit_growth": _clip_score(d.get("profit_growth_3y"), 0, 40),
        "roce": _clip_score(d.get("roce"), 10, 40),
        "sales_growth": _clip_score(d.get("sales_growth_3y"), 0, 30),
        "margin_trend": _clip_score(mtrend, -5, 10),
        "low_debt": _clip_score(-(d.get("debt_to_equity") or np.nan), -1.5, 0),
        "promoter": _clip_score(d.get("promoter_holding"), 30, 75),
        # runway: smaller = better (log scale, 2k cr -> 2 lakh cr)
        "runway": _clip_score(
            -np.log10(max(d.get("mcap_cr") or np.nan, 1)), -5.3, -3.3),
    }

    usable = {k: v for k, v in parts.items() if not np.isnan(v)}
    wsum = sum(WEIGHTS[k] for k in usable) or 1
    score = sum(WEIGHTS[k] * v for k, v in usable.items()) / wsum * 100

    # Hard red flags: these disqualify regardless of score
    flags = []
    if (d.get("pledged_pct") or 0) > THRESHOLDS["max_pledge"]:
        flags.append(f"promoter pledge {d['pledged_pct']:.1f}%")
    if (d.get("debt_to_equity") or 0) > 1.5:
        flags.append(f"D/E {d['debt_to_equity']:.2f}")
    if (d.get("roce") or 99) < 10:
        flags.append(f"ROCE {d['roce']:.1f}% below cost of capital")
    if peg_val and peg_val > THRESHOLDS["max_peg"]:
        flags.append(f"PEG {peg_val:.1f} — growth already priced in")
    if (d.get("promoter_holding") or 99) < 25:
        flags.append(f"low promoter holding {d['promoter_holding']:.1f}%")

    return {
        "symbol": d.get("symbol"),
        "compounder_score": round(score, 1) if usable else np.nan,
        "mcap_cr": d.get("mcap_cr"),
        "roce": d.get("roce"),
        "roe": d.get("roe"),
        "profit_growth_3y": d.get("profit_growth_3y"),
        "profit_growth_ttm": d.get("profit_growth_ttm"),
        "growth_accel": round(accel, 1) if accel is not None else np.nan,
        "sales_growth_3y": d.get("sales_growth_3y"),
        "margin_trend_pp": round(mtrend, 1) if mtrend is not None else np.nan,
        "debt_to_equity": d.get("debt_to_equity"),
        "promoter_holding": d.get("promoter_holding"),
        "pledged_pct": d.get("pledged_pct"),
        "pe": d.get("pe"),
        "peg": round(peg_val, 2) if peg_val else np.nan,
        "qtr_consistency_pct": quarterly_consistency(d),
        "red_flags": "; ".join(flags),
        "clean": len(flags) == 0,
    }


def scan(symbols: list[str], pause: float = 1.5,
         progress_cb=None) -> pd.DataFrame:
    """Scan a symbol list for compounder characteristics."""
    rows = []
    for i, sym in enumerate(symbols):
        d = fetch_company_data(sym)
        if d:
            rows.append(score_company(d))
        if progress_cb:
            progress_cb(f"Scanning {sym}...", (i + 1) / len(symbols))
        time.sleep(pause)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("symbol")
    return df.sort_values("compounder_score", ascending=False)


def shortlist(df: pd.DataFrame, min_score: float = 60.0) -> pd.DataFrame:
    """Clean names above the score bar that also clear the hard thresholds."""
    t = THRESHOLDS
    m = (
        (df["compounder_score"] >= min_score)
        & df["clean"]
        & (df["roce"].fillna(0) >= t["min_roce"])
        & (df["profit_growth_3y"].fillna(-99) >= t["min_profit_growth_3y"])
        & (df["sales_growth_3y"].fillna(-99) >= t["min_sales_growth_3y"])
        & (df["debt_to_equity"].fillna(9) <= t["max_debt_to_equity"])
    )
    return df[m]


if __name__ == "__main__":
    import sys
    import fno_universe

    universe = fno_universe.get_fno_universe()
    if "--top" in sys.argv:
        universe = universe[:int(sys.argv[sys.argv.index("--top") + 1])]
    print(f"Scanning {len(universe)} symbols (this takes a while)...")
    df = scan(universe)
    df.to_csv("compounder_scan.csv")
    print(shortlist(df).to_string())
    print("\nSaved compounder_scan.csv")
    print("\nReminder: these are candidates for RESEARCH over 3-7 years, "
          "not signals. High score != buy.")
