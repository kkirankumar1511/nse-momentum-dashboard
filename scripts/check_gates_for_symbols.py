"""
Ad-hoc, read-only lookup: for a given date and a list of symbols, print
each symbol's full watchlist gate breakdown (rank_universe_asof). Not a
strategy change.

Run with: python scripts/check_gates_for_symbols.py 2026-07-30 COLPAL BHARATFORG ...
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

BASE_CFG = {**config.STRATEGY, **FILTER_CFG_OVERRIDE, "watchlist_size": 999}


def main():
    target = dt.datetime.strptime(sys.argv[1], "%Y-%m-%d")
    syms = sys.argv[2:]

    fundamentals_history = None
    if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
        fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]
    sector_membership = sector_candles = None
    if os.path.exists(SECTOR_DATA_CACHE):
        _sd = pd.read_pickle(SECTOR_DATA_CACHE)
        sector_membership, sector_candles = _sd["sector_membership"], _sd["sector_candles"]

    bench = _tz_naive(pd.read_csv(os.path.join(LONG_CACHE_DIR, "_NIFTY.csv"),
                                  index_col=0, parse_dates=True))
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

    gate_cols = [c for c in ranked.columns if c.endswith("_ok") or c == "all_gates"]
    passers = ranked[ranked["all_gates"]].sort_values("score", ascending=False)
    show_cols = ["score"] + gate_cols

    print(f"=== Gate breakdown as of {target.date()} ({len(passers)} total gate-passers, "
         f"top {BASE_CFG['watchlist_size']} eligible) ===")
    with pd.option_context("display.max_rows", None, "display.max_columns", None,
                           "display.width", 220):
        rows = []
        for sym in syms:
            if sym not in ranked.index:
                print(f"{sym}: not ranked that day (missing history?)")
                continue
            in_gate_passers = sym in passers.index
            rank_pos = passers.index.get_loc(sym) + 1 if in_gate_passers else None
            in_top = rank_pos is not None and rank_pos <= BASE_CFG["watchlist_size"]
            row = ranked.loc[sym, show_cols].copy()
            row["gate_rank"] = rank_pos
            row["in_watchlist"] = in_top
            vol_sma50 = candles[sym]["volume"].rolling(50).mean().loc[:target].iloc[-1]
            vol_today = float(candles[sym]["volume"].loc[:target].iloc[-1])
            row["vol_above_50sma"] = (not pd.isna(vol_sma50)) and vol_today > vol_sma50
            row["all_gates_and_vol"] = bool(row["all_gates"]) and bool(row["vol_above_50sma"])
            rows.append(row.rename(sym))
        if rows:
            print(pd.DataFrame(rows).round(3))
            n_pass = sum(1 for r in rows if r["all_gates_and_vol"])
            print(f"\n{n_pass} of {len(rows)} pass ALL gates (no top-N cap) "
                 f"AND volume-above-50SMA.")


if __name__ == "__main__":
    main()
