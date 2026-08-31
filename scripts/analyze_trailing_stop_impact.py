"""
Ad-hoc, read-only analysis: for every trade in a saved EMA21-touch
backtest run (which currently uses a FIXED stop -- signal candle's HA
low -- and a fixed profit target, trailing_stop_enabled=False), replay
the SAME entries against a trailing ATR-chandelier stop instead, using
the identical mechanic backtest_triggered.py's own step 1b already runs
for other trigger types: new_stop = highest_close - trailing_atr_
multiple * ATR(atr_period), ratchet UP only, exit when the real day's
low <= stop (fill = min(stop, high), gap-down adjusted if open < stop).
The fixed profit target is REMOVED for this simulation -- the whole
point is "what if a trailing stop replaced the fixed target," not "what
if a trailing stop is added on top of it" (a trailing stop that's
always tighter than a still-active fixed target would never do
anything differently).

Not a strategy change -- pure post-hoc simulation for comparison.

Run with: python scripts/analyze_trailing_stop_impact.py <trades.csv> [--atr-mult 2.0] [--atr-period 14] [--max-hold-days 250]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import indicators
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached


def simulate_trailing_exit(df: pd.DataFrame, entry_date: pd.Timestamp, entry_price: float,
                            initial_stop: float, atr_mult: float, atr_period: int,
                            max_hold_days: int):
    """Walks forward from entry_date using the SAME day-loop convention as
    backtest_triggered.py's step 1/1b: stop-check first (using the stop as
    of the START of the day), THEN ratchet the stop up for the next day
    using today's close. Returns (exit_date, exit_price, exit_reason,
    holding_days, still_open: bool)."""
    idx = df.index
    if entry_date not in idx:
        return None
    pos = idx.get_loc(entry_date)
    stop = initial_stop
    highest_close = max(entry_price, float(df["close"].iloc[pos]))
    end_pos = min(pos + max_hold_days, len(idx) - 1)

    for i in range(pos + 1, end_pos + 1):
        bar = df.iloc[i]
        if bar["low"] <= stop:
            fill = min(stop, bar["high"])
            if bar["open"] < stop:
                fill = bar["open"]
            holding_days = (idx[i] - entry_date).days
            return idx[i], fill, "trailing_stop", holding_days, False
        highest_close = max(highest_close, float(bar["close"]))
        df_upto = df.iloc[:i + 1]
        atr_now = float(indicators.atr(df_upto, atr_period).iloc[-1])
        if pd.isna(atr_now):
            continue
        new_stop = highest_close - atr_mult * atr_now
        if new_stop > stop:
            stop = new_stop

    # Ran out of simulated days (either max_hold_days or data) without a
    # trailing-stop hit -- mark still open, mark-to-market at the last
    # available close.
    last_i = end_pos
    holding_days = (idx[last_i] - entry_date).days
    return idx[last_i], float(df["close"].iloc[last_i]), "still_open", holding_days, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trades_path")
    ap.add_argument("--atr-mult", type=float, default=2.0,
                    help="trailing_atr_multiple, default 2.0 -- matches TRIGGERED_DEFAULTS' "
                        "general default (backtest_triggered.py step 1b)")
    ap.add_argument("--atr-period", type=int, default=14, help="ATR period, default 14")
    ap.add_argument("--max-hold-days", type=int, default=250,
                    help="cap on simulated forward days per trade, default 250 (~1yr)")
    args = ap.parse_args()

    trades = pd.read_csv(args.trades_path, parse_dates=["entry_date", "exit_date"])
    trades["win"] = trades["pnl"] > 0

    bench = _tz_naive(pd.read_csv(os.path.join(LONG_CACHE_DIR, "_NIFTY.csv"),
                                  index_col=0, parse_dates=True))
    cache_end_date = bench.index.max().date()
    symbols = sorted(trades["symbol"].unique())
    long_candles = load_long_history_cached(symbols, end_date=cache_end_date)

    rows = []
    for _, t in trades.iterrows():
        sym = t["symbol"]
        if sym not in long_candles or long_candles[sym].empty:
            continue
        df = long_candles[sym]
        result = simulate_trailing_exit(
            df, t["entry_date"], float(t["entry_price"]), float(t["exit_stop_price"]),
            args.atr_mult, args.atr_period, args.max_hold_days)
        if result is None:
            continue
        tr_exit_date, tr_exit_price, tr_reason, tr_hold_days, still_open = result
        qty = t["qty"]
        tr_pnl = (tr_exit_price - float(t["entry_price"])) * qty
        tr_ret_pct = (tr_exit_price / float(t["entry_price"]) - 1) * 100

        rows.append({
            "symbol": sym, "entry_date": t["entry_date"],
            "orig_exit_date": t["exit_date"], "orig_exit_reason": t["reason"],
            "orig_ret_pct": round(float(t["ret_pct"]), 2), "orig_pnl": round(float(t["pnl"]), 2),
            "orig_hold_days": t["holding_days"],
            "trail_exit_date": tr_exit_date, "trail_exit_reason": tr_reason,
            "trail_ret_pct": round(tr_ret_pct, 2), "trail_pnl": round(tr_pnl, 2),
            "trail_hold_days": tr_hold_days, "trail_still_open": still_open,
        })

    feat = pd.DataFrame(rows)
    print(f"Simulated trailing exits for {len(feat)} of {len(trades)} trades "
         f"(atr_mult={args.atr_mult}, atr_period={args.atr_period}).\n")

    n_still_open = feat["trail_still_open"].sum()
    print(f"Still open at end of simulated window (never hit trailing stop): "
         f"{n_still_open}/{len(feat)}\n")

    print("=== Aggregate comparison ===")
    print(f"{'Metric':30s} {'Original (fixed)':>18s} {'Trailing':>18s}")
    orig_total_pnl = feat["orig_pnl"].sum()
    trail_total_pnl = feat["trail_pnl"].sum()
    orig_win_rate = (feat["orig_pnl"] > 0).mean() * 100
    trail_win_rate = (feat["trail_pnl"] > 0).mean() * 100
    orig_avg_ret = feat["orig_ret_pct"].mean()
    trail_avg_ret = feat["trail_ret_pct"].mean()
    orig_avg_hold = feat["orig_hold_days"].mean()
    trail_avg_hold = feat["trail_hold_days"].mean()
    orig_wins_sum = feat.loc[feat["orig_pnl"] > 0, "orig_pnl"].sum()
    orig_losses_sum = -feat.loc[feat["orig_pnl"] < 0, "orig_pnl"].sum()
    orig_pf = orig_wins_sum / orig_losses_sum if orig_losses_sum > 0 else float("inf")
    trail_wins_sum = feat.loc[feat["trail_pnl"] > 0, "trail_pnl"].sum()
    trail_losses_sum = -feat.loc[feat["trail_pnl"] < 0, "trail_pnl"].sum()
    trail_pf = trail_wins_sum / trail_losses_sum if trail_losses_sum > 0 else float("inf")
    print(f"{'Total P&L':30s} {orig_total_pnl:>18,.0f} {trail_total_pnl:>18,.0f}")
    print(f"{'Win rate %':30s} {orig_win_rate:>18.1f} {trail_win_rate:>18.1f}")
    print(f"{'Avg ret %':30s} {orig_avg_ret:>18.2f} {trail_avg_ret:>18.2f}")
    print(f"{'Avg hold days':30s} {orig_avg_hold:>18.1f} {trail_avg_hold:>18.1f}")
    print(f"{'Profit factor':30s} {orig_pf:>18.2f} {trail_pf:>18.2f}")
    print()

    print("=== Exit reason breakdown (trailing simulation) ===")
    print(feat["trail_exit_reason"].value_counts())
    print()

    feat["pnl_diff"] = feat["trail_pnl"] - feat["orig_pnl"]
    print("=== Biggest differences (trailing vs original), top 15 by |diff| ===")
    cols = ["symbol", "entry_date", "orig_exit_reason", "orig_ret_pct", "orig_hold_days",
           "trail_exit_reason", "trail_ret_pct", "trail_hold_days", "pnl_diff"]
    top_diff = feat.reindex(feat["pnl_diff"].abs().sort_values(ascending=False).index).head(15)
    with pd.option_context("display.max_rows", None, "display.width", 220):
        print(top_diff[cols].to_string(index=False))
    print()

    print("=== By original exit reason ===")
    for reason in feat["orig_exit_reason"].unique():
        sub = feat[feat["orig_exit_reason"] == reason]
        print(f"  {reason}: n={len(sub)}, orig_pnl_sum={sub['orig_pnl'].sum():,.0f}, "
             f"trail_pnl_sum={sub['trail_pnl'].sum():,.0f}, "
             f"diff={sub['pnl_diff'].sum():,.0f}")


if __name__ == "__main__":
    main()
