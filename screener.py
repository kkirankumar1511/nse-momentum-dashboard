"""
Composite screener: ranks the universe by a research-backed momentum score,
gated by trend-structure rules and fundamental quality filters.

Score construction (all cross-sectional z-scores, then weighted):
  40%  6-month relative strength vs NIFTY   (Jegadeesh & Titman)
  25%  3-month relative strength vs NIFTY   (intermediate momentum)
  20%  proximity to 52-week high            (George & Hwang)
  15%  volume expansion                     (confirmation, NSE studies)

Hard gates (a stock must pass ALL to appear as a candidate):
  - price above 50 EMA and 200 EMA, 50 EMA rising   (trend structure)
  - price >= 85% of 52-week high
  - RSI within [45, 78]  (in momentum regime, not parabolic)
  - fundamental quality gate (ROCE, D/E, profit growth) when data available
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

import config
import indicators
import kite_client
import fundamentals_agent as fa


def _zscore(s: pd.Series) -> pd.Series:
    return (s - s.mean()) / s.std(ddof=0) if s.std(ddof=0) else s * 0


def build_technical_table(candles: dict[str, pd.DataFrame],
                          bench: pd.DataFrame) -> pd.DataFrame:
    cfg = config.STRATEGY
    rows = {}
    for sym, df in candles.items():
        snap = indicators.compute_snapshot(df, bench, cfg)
        if snap:
            rows[sym] = snap
    return pd.DataFrame(rows).T


def apply_gates(tech: pd.DataFrame,
                fundamentals: pd.DataFrame | None = None) -> pd.DataFrame:
    """fundamentals: fundamentals_agent.fno_value_scan() output — indexed by
    symbol, with a 'total_score' column (0-100, sector-aware: value_score/
    bank_score/nbfc_score/etc, computed from primary-source XBRL — see
    xbrl_parser.py). A symbol with no fundamental score yet (NaN) passes the
    quality gate rather than being excluded, matching this gate's behavior
    before it switched away from screener.in scraping — no data blocking a
    stock from an otherwise-valid technical setup was never the intent.
    """
    cfg = config.STRATEGY
    t = tech.copy()

    t["trend_ok"] = t["above_ema50"] & t["above_ema200"] & t["ema50_rising"]
    t["near_high_ok"] = t["pct_52w_high"] >= cfg["near_high_threshold"]
    t["rsi_ok"] = t["rsi"].between(cfg["rsi_min"], cfg["rsi_max"])

    if fundamentals is not None and not fundamentals.empty:
        fund = fundamentals.reindex(t.index)
        t["fundamental_score"] = fund["total_score"]
        t["fundamental_rubric"] = fund.get("rubric")
        min_score = cfg["min_fundamental_score"]
        t["quality_ok"] = (t["fundamental_score"].isna()
                           | (t["fundamental_score"] >= min_score))
        t["quality_fails"] = t.apply(
            lambda r: "" if r["quality_ok"] else
            f"fundamental score {r['fundamental_score']:.0f} < {min_score:.0f}",
            axis=1)
    else:
        t["quality_ok"] = True
        t["quality_fails"] = ""

    t["all_gates"] = t["trend_ok"] & t["near_high_ok"] & t["rsi_ok"] & t["quality_ok"]
    return t


def score(t: pd.DataFrame, cfg: dict = config.STRATEGY) -> pd.DataFrame:
    t = t.copy()
    t["score"] = (
        0.40 * _zscore(t["rs_6m"].astype(float))
        + 0.25 * _zscore(t["rs_3m"].astype(float))
        + 0.20 * _zscore(t["pct_52w_high"].astype(float))
        + 0.15 * _zscore(t["vol_expansion"].astype(float))
    ).round(3)

    # Optional sector relative-strength tilt (see sector_universe.py). Off
    # by default (sector_bonus_weight=0) -- only present when the caller
    # attached a "sector_rs" column (backtest.rank_universe_asof / live
    # screener.run_screen, both opt-in). Not renormalized against the 4
    # terms above: only relative ranking (sort_values) matters, and this
    # keeps weight=0 byte-identical to today's score.
    if cfg.get("sector_bonus_weight", 0.0) and "sector_rs" in t.columns:
        t["score"] += (cfg["sector_bonus_weight"]
                       * _zscore(t["sector_rs"].astype(float)).fillna(0))

    return t.sort_values("score", ascending=False)


def position_size(capital: float, price: float, stop: float,
                  cfg: dict = config.STRATEGY) -> int:
    """Volatility-based sizing: risk `risk_per_trade_pct` of capital between
    entry and ATR stop."""
    risk_amount = capital * cfg["risk_per_trade_pct"] / 100
    per_share_risk = max(price - stop, 0.01)
    return max(int(risk_amount / per_share_risk), 0)


def run_screen(with_fundamentals: bool = True,
               fundamentals: pd.DataFrame | None = None,
               progress_cb=None) -> pd.DataFrame:
    """Full pipeline. progress_cb(stage:str, frac:float) for UI updates.

    fundamentals: pre-computed fundamentals_agent.fno_value_scan() output —
    pass this in (e.g. the dashboard's already-cached Value Score results)
    to avoid re-running a full 210-stock NSE scan on every screen. If not
    given and with_fundamentals=True, falls back to the on-disk Value Score
    cache if present, then to a fresh fno_value_scan() only as a last resort.
    """
    def report(stage, frac):
        if progress_cb:
            progress_cb(stage, frac)

    report("Fetching benchmark (NIFTY 50)...", 0.05)
    days = config.STRATEGY["history_days"]
    bench = kite_client.benchmark_candles(days)

    report("Fetching universe candles from Kite (~3 years each)...", 0.15)
    candles = kite_client.fetch_universe_candles(config.UNIVERSE, days)

    report("Computing technicals...", 0.60)
    tech = build_technical_table(candles, bench)

    if with_fundamentals and fundamentals is None:
        cache_path = os.path.join("cache", "fno_value_scores.pkl")
        if os.path.exists(cache_path):
            report("Loading cached fundamental scores...", 0.65)
            fundamentals = pd.read_pickle(cache_path)
        else:
            report("Fetching fundamentals (primary XBRL)...", 0.70)
            fundamentals = fa.fno_value_scan(list(tech.index))
    elif not with_fundamentals:
        fundamentals = None

    report("Scoring & gating...", 0.95)
    t = apply_gates(tech, fundamentals)
    t = score(t)
    report("Done", 1.0)
    return t
