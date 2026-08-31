"""
Ad-hoc: prints how many stocks pass ALL 5 selection gates (trend, near-
52wk-high, RSI, fundamental, weekly/monthly) each trading day over a
window -- answers "how big is the actual eligible universe on a given
day" when watchlist_size is uncapped (999). Local-only, reuses the same
deep-cache data loading as run_ema21touch_backtest_local.py. Not wired
into anything else.

Run with: python scripts/count_gate_passers_daily.py --years 0.6
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached, rank_universe_asof
import indicators
import sector_universe

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")

FILTER_CFG_OVERRIDE: dict = {
    "rsi_min": 60, "rsi_max": 100, "ema_fast": 50, "ema_slow": 200,
    "mom_lookback_days_short": 63, "mom_lookback_days_long": 126, "skip_recent_days": 5,
    "rsi_exit_gate_enabled": False, "weekly_monthly_gate_enabled": True,
    "advanced_equal_weight_sizing": False, "equal_weight_tolerance_pct": 0.20,
    "near_high_threshold": 0.85, "fundamental_gate_enabled": True,
    "fundamental_bonus_weight": 0.50, "min_fundamental_score": 50.0,
    "sector_bonus_weight": 1.00, "sector_diversification_enabled": False,
    "sector_composite_score_enabled": True, "history_days": 1200,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=0.6)
    ap.add_argument("--start-date", type=str, default=None)
    ap.add_argument("--end-date", type=str, default=None)
    args = ap.parse_args()

    fundamentals_history = None
    if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
        fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]

    sector_membership = sector_candles = None
    if os.path.exists(SECTOR_DATA_CACHE):
        _sd = pd.read_pickle(SECTOR_DATA_CACHE)
        sector_membership, sector_candles = _sd["sector_membership"], _sd["sector_candles"]

    # Pin end_date to the cache's OWN known-fresh date, not today -- this
    # machine has no valid Kite access_token, so requesting anything past
    # what's already cached makes load_long_history_cached attempt (and
    # fail) a live fetch for all 202 symbols before falling back anyway.
    # Matches run_triggered_backtest_local.py's own documented reasoning.
    bench = _tz_naive(pd.read_csv(os.path.join(LONG_CACHE_DIR, "_NIFTY.csv"),
                                  index_col=0, parse_dates=True))
    cache_fresh_date = bench.index.max().date()
    long_candles = load_long_history_cached(config.UNIVERSE, end_date=cache_fresh_date)
    candles = long_candles

    if args.start_date:
        requested_start = dt.datetime.strptime(args.start_date, "%Y-%m-%d").date()
        window_end = (dt.datetime.strptime(args.end_date, "%Y-%m-%d").date()
                      if args.end_date else dt.date.today())
    else:
        requested_start = dt.date.today() - dt.timedelta(days=int(args.years * 365))
        window_end = dt.date.today()

    all_dates = bench.index.sort_values()
    dates = all_dates[(all_dates >= pd.Timestamp(requested_start))
                      & (all_dates <= pd.Timestamp(window_end))]
    print(f"Counting gate-passers for {len(dates)} trading days, "
         f"{dates[0].date()} to {dates[-1].date()}.\n")

    full_cfg = {**config.STRATEGY, **FILTER_CFG_OVERRIDE}

    # Precompute ONCE, matching run_backtest/run_triggered_backtest's own
    # optimization (see backtest.py's rank_universe_asof docstring) --
    # without this, every day redoes a full O(symbols x full-history)
    # indicator computation AND a full weekly/monthly resample from
    # scratch, ~45% of runtime by backtest.py's own profiling comment.
    # Precomputing once turns 145 days of that into 1.
    print("Precomputing daily indicators + weekly/monthly bars once...")
    precomputed_daily: dict = {}
    for sym, df in candles.items():
        if not df.empty and len(df) >= full_cfg["ema_slow"]:
            precomputed_daily[sym] = indicators.precompute_daily_series(df, full_cfg)
    precomputed_weekly_monthly: dict = {}
    for sym, df in long_candles.items():
        if not df.empty:
            precomputed_weekly_monthly[sym] = indicators.precompute_weekly_monthly_bars(df["close"])
    print("Done precomputing.\n")

    fundamentals_score_cache: dict = {}
    print(f"{'date':12s} {'gate-passers':>12s}")
    for date in dates:
        precomputed_rows = {s: precomputed_daily[s].loc[date] for s in candles
                            if s in precomputed_daily and date in precomputed_daily[s].index}
        ranked = rank_universe_asof(
            candles, bench, date, full_cfg,
            fundamentals_history=fundamentals_history, score_cache=fundamentals_score_cache,
            sector_candles=sector_candles, sector_membership=sector_membership,
            long_candles=long_candles, precomputed=precomputed_rows,
            precomputed_weekly_monthly=precomputed_weekly_monthly)
        n_pass = int(ranked["all_gates"].sum()) if not ranked.empty and "all_gates" in ranked else 0
        print(f"{date.date()!s:12s} {n_pass:12d}")


if __name__ == "__main__":
    main()
