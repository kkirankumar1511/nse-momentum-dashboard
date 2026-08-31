"""
Ad-hoc, read-only lookup: print the top-N ranked, gate-passing watchlist
for one specific date, using the same stock-selection pipeline
(FILTER_CFG_OVERRIDE + rank_universe_asof) as the live paper-trade
strategy and the EMA21-touch backtests. Not a strategy change.

Run with: python scripts/show_watchlist_asof.py 2026-07-31 [rsi_min]
(rsi_min optional, overrides FILTER_CFG_OVERRIDE's rsi_min=60 for
ad-hoc "what if the watchlist gate were looser" checks.)
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
import indicators
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached, rank_universe_asof
from scripts.run_ema21touch_backtest_local import (
    FILTER_CFG_OVERRIDE, FUNDAMENTALS_HISTORY_CACHE, SECTOR_DATA_CACHE,
)

BASE_CFG = {**config.STRATEGY, **FILTER_CFG_OVERRIDE, "watchlist_size": 20}


def main():
    target = dt.datetime.strptime(sys.argv[1], "%Y-%m-%d") if len(sys.argv) > 1 \
        else dt.datetime.today()
    if len(sys.argv) > 2:
        BASE_CFG["rsi_min"] = float(sys.argv[2])
        print(f"Overriding watchlist rsi_min -> {BASE_CFG['rsi_min']} (from CLI arg).")

    fundamentals_history = None
    if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
        fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]
    sector_membership = sector_candles = None
    if os.path.exists(SECTOR_DATA_CACHE):
        _sd = pd.read_pickle(SECTOR_DATA_CACHE)
        sector_membership, sector_candles = _sd["sector_membership"], _sd["sector_candles"]

    bench = _tz_naive(pd.read_csv(os.path.join(LONG_CACHE_DIR, "_NIFTY.csv"),
                                  index_col=0, parse_dates=True))
    # Pin end_date to the cache's own last date, not today-1 -- otherwise
    # load_long_history_cached retries a live Kite fetch (and fails) for
    # every one of 202 symbols before falling back to stale cache, which
    # is what made this take minutes instead of seconds (same bug fixed
    # earlier in scripts/count_gate_passers_daily.py).
    cache_end_date = bench.index.max().date()
    long_candles = load_long_history_cached(config.UNIVERSE, end_date=cache_end_date)
    candles = long_candles

    precomputed_daily = {}
    for sym, df in candles.items():
        if not df.empty and len(df) >= BASE_CFG["ema_slow"]:
            precomputed_daily[sym] = indicators.precompute_daily_series(df, BASE_CFG)

    precomputed_weekly_monthly = {}
    if BASE_CFG.get("weekly_monthly_gate_enabled", False):
        for sym, df in long_candles.items():
            if not df.empty:
                precomputed_weekly_monthly[sym] = indicators.precompute_weekly_monthly_bars(df["close"])

    ranked = rank_universe_asof(candles, bench, pd.Timestamp(target), BASE_CFG,
                                fundamentals_history, {}, sector_candles, sector_membership,
                                long_candles, precomputed_daily, None, precomputed_weekly_monthly)

    if ranked.empty:
        print(f"No ranking data available for {target.date()}.")
        return

    gate_cols = [c for c in ranked.columns if c.endswith("_ok") or c == "all_gates"]
    passers = ranked[ranked["all_gates"]].sort_values("score", ascending=False)
    top20 = passers.head(20)

    print(f"=== Top {len(top20)} watchlist as of {target.date()} "
         f"({len(passers)} total gate-passers out of {len(ranked)} ranked) ===")
    show_cols = ["score"] + [c for c in gate_cols if c != "all_gates"]
    with pd.option_context("display.max_rows", None, "display.max_columns", None,
                           "display.width", 220):
        print(top20[show_cols].round(3))

        if "SIEMENS" in ranked.index:
            print("\nSIEMENS row (for reference, ALL gate columns):")
            rank_pos = ranked.sort_values("score", ascending=False).index.get_loc("SIEMENS")
            print(f"Rank #{rank_pos + 1} of {len(ranked)}")
            print(ranked.loc[["SIEMENS"], show_cols].round(3).T)


if __name__ == "__main__":
    main()
