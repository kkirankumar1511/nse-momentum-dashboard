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
