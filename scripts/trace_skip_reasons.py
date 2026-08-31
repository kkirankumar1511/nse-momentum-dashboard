"""
Ad-hoc, read-only diagnostic: runs the EMA21-touch backtest with
debug_log wired in (see backtest_triggered.run_triggered_backtest's
2026-08-23 addition) and summarizes exactly why each confirmed signal
did or didn't become a real trade -- ground truth, not inference. Not a
strategy change (debug_log is a pure logging hook, zero behavior change).

Run with: python scripts/trace_skip_reasons.py --years 0.6 --rsi-min 50 --close-above-ema21 --max-positions 5
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
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached
from backtest_triggered import run_triggered_backtest
from scripts.run_ema21touch_backtest_local import (
    FILTER_CFG_OVERRIDE, NEW_EMA21_TOUCH_LATEST_LOW_CFG,
    FUNDAMENTALS_HISTORY_CACHE, SECTOR_DATA_CACHE,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=0.6)
    ap.add_argument("--rsi-min", type=float, default=50.0)
    ap.add_argument("--close-above-ema21", action="store_true")
    ap.add_argument("--max-positions", type=int, default=None)
    args = ap.parse_args()

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

    TRIG_WARMUP_DAYS = 780
    all_dates = bench.index.sort_values()
    data_floor = all_dates[TRIG_WARMUP_DAYS].date()
    requested_start = dt.date.today() - dt.timedelta(days=int(args.years * 365))
    sim_start_date = max(data_floor, requested_start)

    cfg = {**FILTER_CFG_OVERRIDE, **NEW_EMA21_TOUCH_LATEST_LOW_CFG, "rsi_min": args.rsi_min}
    if args.close_above_ema21:
        cfg["ha_ema21_touch_signal_close_above_ema13"] = False
    if args.max_positions is not None:
        cfg["max_positions"] = args.max_positions
    print(f"Config: rsi_min={cfg['rsi_min']} max_positions={cfg.get('max_positions')} "
         f"close_above_ema13={cfg['ha_ema21_touch_signal_close_above_ema13']}")

    debug_log: list = []
    result = run_triggered_backtest(
        candles, bench, cfg, initial_capital=1_000_000, cost_bps=0.0,
        warmup_days=TRIG_WARMUP_DAYS, fundamentals_history=fundamentals_history,
        sector_candles=sector_candles, sector_membership=sector_membership,
        long_candles=long_candles, start_date=sim_start_date, debug_log=debug_log)

    print(f"\nMetrics: CAGR={result['metrics'].get('CAGR %')} "
         f"Trades={result['metrics'].get('Trades')} "
         f"WinRate={result['metrics'].get('Win rate %')}")

    log = pd.DataFrame(debug_log)
    print(f"\n{len(log)} confirmed-signal-days logged.")
    print("\n=== Reason breakdown ===")
    print(log["reason"].value_counts())

    for reason in log["reason"].unique():
        if reason == "entered":
            continue
        sub = log[log["reason"] == reason]
        print(f"\n--- Sample of '{reason}' ({len(sub)} total) ---")
        for _, r in sub.head(10).iterrows():
            print(f"  {r['symbol']:16s} {r['date'].date()}")


if __name__ == "__main__":
    main()
