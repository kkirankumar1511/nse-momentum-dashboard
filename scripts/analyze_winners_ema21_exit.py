"""
Ad-hoc, read-only analysis: for every WINNING (target-hit) trade in a
saved backtest run, walk forward from entry looking for the first day
HA_close drops below HA EMA21 (the same trend-break condition the
strategy's own state machine already uses to invalidate a PENDING
signal) -- a hypothetical "let it run until trend breaks" exit instead
of the fixed 2R target -- and report the R-multiple return at that
point, plus the max R reached anywhere along the way. Not a strategy
change, pure what-if analysis.

Run with: python scripts/analyze_winners_ema21_exit.py <trades.csv>
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
import trigger_indicators as ti
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached
from indicators import ema


def main():
    trades_path = sys.argv[1]
    trades = pd.read_csv(trades_path, parse_dates=["entry_date", "exit_date"])
    winners = trades[trades["reason"] == "target"].copy()
    print(f"{len(winners)} winning (target-hit) trades of {len(trades)} total.")

    bench = _tz_naive(pd.read_csv(os.path.join(LONG_CACHE_DIR, "_NIFTY.csv"),
                                  index_col=0, parse_dates=True))
    cache_end_date = bench.index.max().date()
    symbols = sorted(winners["symbol"].unique())
    long_candles = load_long_history_cached(symbols, end_date=cache_end_date)

    rows = []
    for _, t in winners.iterrows():
        sym = t["symbol"]
        if sym not in long_candles or long_candles[sym].empty:
            continue
        df = long_candles[sym]
        ha = ti.precompute_heikin_ashi(df)
        ema21 = ema(ha["ha_close"], 21)

        risk = t["entry_price"] - t["exit_stop_price"]
        if risk <= 0:
            continue

        future = df.loc[t["entry_date"]:]
        future_ha_close = ha["ha_close"].loc[t["entry_date"]:]
        future_ema21 = ema21.loc[t["entry_date"]:]
        if future.empty:
            continue

        below = future_ha_close < future_ema21
        # Skip entry day itself (index 0) -- the trend-break check applies
        # to days AFTER entry, matching the state machine's own "day
        # after" convention elsewhere in this pattern.
        below_after_entry = below.iloc[1:]
        break_dates = below_after_entry[below_after_entry].index

        max_high_so_far = future["high"].cummax()

        if len(break_dates) == 0:
            trend_break_date = None
            exit_close = float(future["close"].iloc[-1])
            r_at_exit = (exit_close - t["entry_price"]) / risk
            max_r = (float(max_high_so_far.iloc[-1]) - t["entry_price"]) / risk
            status = "still above EMA21 at end of data"
        else:
            trend_break_date = break_dates[0]
            exit_close = float(future.loc[trend_break_date, "close"])
            r_at_exit = (exit_close - t["entry_price"]) / risk
            pre_break_high = float(max_high_so_far.loc[:trend_break_date].iloc[-1])
            max_r = (pre_break_high - t["entry_price"]) / risk
            status = "trend broke"

        rows.append({
            "symbol": sym, "entry_date": t["entry_date"],
            "actual_exit_date": t["exit_date"], "actual_ret_pct": t["ret_pct"],
            "trend_break_date": trend_break_date.date() if trend_break_date is not None else None,
            "status": status,
            "r_at_trend_break_exit": round(r_at_exit, 2),
            "max_r_before_break": round(max_r, 2),
            "actual_target_r": round((t["exit_price"] - t["entry_price"]) / risk, 2),
        })

    feat = pd.DataFrame(rows)
    print(f"\n=== Per-trade: R-multiple at hypothetical close<EMA21 exit vs actual 2R target ===")
    with pd.option_context("display.max_rows", None, "display.width", 220):
        print(feat.to_string(index=False))

    print(f"\nSummary:")
    print(f"  Actual target R (fixed, all should be ~2.0): "
         f"mean={feat['actual_target_r'].mean():.2f}")
    print(f"  R at close<EMA21 exit: mean={feat['r_at_trend_break_exit'].mean():.2f}, "
         f"median={feat['r_at_trend_break_exit'].median():.2f}")
    print(f"  Max R reached before trend break: mean={feat['max_r_before_break'].mean():.2f}, "
         f"median={feat['max_r_before_break'].median():.2f}")
    beat_target = (feat["r_at_trend_break_exit"] > feat["actual_target_r"]).sum()
    print(f"  Trades where close<EMA21 exit R > actual 2R target: {beat_target}/{len(feat)}")


if __name__ == "__main__":
    main()
