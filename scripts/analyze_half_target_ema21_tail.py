"""
Ad-hoc, read-only analysis: for every trade in a saved EMA21-touch
backtest run, simulate a PARTIAL-SCALING exit instead of the current
"100% out at the fixed 1:2 target" rule:

  - The initial stop and the 1:2 target level are unchanged (target =
    entry + target_rr * (entry - initial_stop), target_rr=2.0 by
    default, matching ha_ema21_touch_target_rr).
  - If the ORIGINAL fixed stop is hit before the target is ever reached,
    behavior is IDENTICAL to today (100% out at the stop) -- this only
    changes what happens once the target IS reached.
  - Once the target is first touched (real HIGH >= target, same
    same-day-ambiguity convention as backtest_triggered.py's step 1c):
    HALF the position exits there (booking profit), and the REMAINING
    half keeps riding, exiting when the REAL (non-HA) close first closes
    BELOW the REAL EMA21 (both computed on real price, per explicit
    request -- NOT the HA close/EMA21 the state machine itself runs on)
    -- OR when the original fixed stop is hit, whichever comes first
    (the stop still acts as a hard floor throughout, it isn't removed
    for the tail leg).

Not a strategy change -- pure post-hoc simulation for comparison.

Run with: python scripts/analyze_half_target_ema21_tail.py <trades.csv> [--target-rr 2.0] [--max-hold-days 250]
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


def simulate(df: pd.DataFrame, real_close: pd.Series, ema21_real: pd.Series,
             entry_date: pd.Timestamp, entry_price: float, initial_stop: float,
             target_price: float, max_hold_days: int):
    """Returns a dict describing the full simulated outcome for ONE trade
    (both legs), or None if entry_date isn't in the data."""
    idx = df.index
    if entry_date not in idx:
        return None
    pos = idx.get_loc(entry_date)
    end_pos = min(pos + max_hold_days, len(idx) - 1)

    # Leg 1: everything up to (and including) the day the position first
    # fully resolves -- either the stop (100% out, no split ever
    # happens) or the target (50% out, tail leg begins).
    for i in range(pos + 1, end_pos + 1):
        bar = df.iloc[i]
        if bar["low"] <= initial_stop:
            fill = min(initial_stop, bar["high"])
            if bar["open"] < initial_stop:
                fill = bar["open"]
            return {
                "split_happened": False, "leg1_exit_date": idx[i], "leg1_exit_price": fill,
                "leg1_exit_reason": "stop", "leg1_hold_days": (idx[i] - entry_date).days,
                "leg2_exit_date": None, "leg2_exit_price": None,
                "leg2_exit_reason": None, "leg2_hold_days": None, "leg2_still_open": False,
            }
        if bar["high"] >= target_price:
            leg1_exit_date, leg1_hold_days = idx[i], (idx[i] - entry_date).days
            target_i = i
            break
    else:
        # Neither stop nor target reached within the window -- mark the
        # WHOLE position still open (mirrors trailing-stop script's
        # still_open handling).
        return {
            "split_happened": False, "leg1_exit_date": idx[end_pos],
            "leg1_exit_price": float(df["close"].iloc[end_pos]),
            "leg1_exit_reason": "still_open", "leg1_hold_days": (idx[end_pos] - entry_date).days,
            "leg2_exit_date": None, "leg2_exit_price": None,
            "leg2_exit_reason": None, "leg2_hold_days": None, "leg2_still_open": True,
        }

    # Leg 2 (the tail, remaining half): from the day AFTER the target day
    # onward, exit on the first HA-close-below-EMA21 day OR the original
    # stop, whichever comes first.
    for j in range(target_i + 1, end_pos + 1):
        bar = df.iloc[j]
        if bar["low"] <= initial_stop:
            fill = min(initial_stop, bar["high"])
            if bar["open"] < initial_stop:
                fill = bar["open"]
            return {
                "split_happened": True, "leg1_exit_date": leg1_exit_date,
                "leg1_exit_price": target_price, "leg1_exit_reason": "target_half",
                "leg1_hold_days": leg1_hold_days,
                "leg2_exit_date": idx[j], "leg2_exit_price": fill,
                "leg2_exit_reason": "stop", "leg2_hold_days": (idx[j] - entry_date).days,
                "leg2_still_open": False,
            }
        rc_j, e21_j = real_close.iloc[j], ema21_real.iloc[j]
        if not pd.isna(e21_j) and rc_j < e21_j:
            return {
                "split_happened": True, "leg1_exit_date": leg1_exit_date,
                "leg1_exit_price": target_price, "leg1_exit_reason": "target_half",
                "leg1_hold_days": leg1_hold_days,
                "leg2_exit_date": idx[j], "leg2_exit_price": float(bar["close"]),
                "leg2_exit_reason": "close_below_ema21", "leg2_hold_days": (idx[j] - entry_date).days,
                "leg2_still_open": False,
            }

    # Tail never resolved within the window -- mark leg2 still open.
    return {
        "split_happened": True, "leg1_exit_date": leg1_exit_date,
        "leg1_exit_price": target_price, "leg1_exit_reason": "target_half",
        "leg1_hold_days": leg1_hold_days,
        "leg2_exit_date": idx[end_pos], "leg2_exit_price": float(df["close"].iloc[end_pos]),
        "leg2_exit_reason": "still_open", "leg2_hold_days": (idx[end_pos] - entry_date).days,
        "leg2_still_open": True,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trades_path")
    ap.add_argument("--target-rr", type=float, default=2.0,
                    help="risk:reward for the target level, default 2.0 -- matches "
                        "ha_ema21_touch_target_rr")
    ap.add_argument("--max-hold-days", type=int, default=250)
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
        real_close = df["close"]
        ema21_real = indicators.ema(real_close, 21)

        entry_price = float(t["entry_price"])
        initial_stop = float(t["exit_stop_price"])
        risk = entry_price - initial_stop
        if risk <= 0:
            continue
        target_price = entry_price + args.target_rr * risk

        sim = simulate(df, real_close, ema21_real, t["entry_date"], entry_price, initial_stop,
                       target_price, args.max_hold_days)
        if sim is None:
            continue

        qty = t["qty"]
        if not sim["split_happened"]:
            new_pnl = (sim["leg1_exit_price"] - entry_price) * qty
        else:
            half = qty // 2
            rest = qty - half
            leg1_pnl = (sim["leg1_exit_price"] - entry_price) * half
            leg2_pnl = (sim["leg2_exit_price"] - entry_price) * rest
            new_pnl = leg1_pnl + leg2_pnl
        new_ret_pct = new_pnl / (entry_price * qty) * 100
        total_hold_days = (sim["leg2_hold_days"] if sim["split_happened"]
                           else sim["leg1_hold_days"])

        rows.append({
            "symbol": sym, "entry_date": t["entry_date"],
            "orig_exit_reason": t["reason"], "orig_ret_pct": round(float(t["ret_pct"]), 2),
            "orig_pnl": round(float(t["pnl"]), 2), "orig_hold_days": t["holding_days"],
            "split_happened": sim["split_happened"],
            "leg1_exit_reason": sim["leg1_exit_reason"], "leg1_hold_days": sim["leg1_hold_days"],
            "leg2_exit_reason": sim["leg2_exit_reason"], "leg2_hold_days": sim["leg2_hold_days"],
            "new_pnl": round(new_pnl, 2), "new_ret_pct": round(new_ret_pct, 2),
            "new_hold_days": total_hold_days,
            "still_open": (sim["leg2_still_open"] if sim["split_happened"]
                          else sim["leg1_exit_reason"] == "still_open"),
        })

    feat = pd.DataFrame(rows)
    print(f"Simulated half-target/EMA21-tail exits for {len(feat)} of {len(trades)} trades "
         f"(target_rr={args.target_rr}).\n")

    n_split = feat["split_happened"].sum()
    n_still_open = feat["still_open"].sum()
    print(f"Trades where target was reached and the split kicked in: {n_split}/{len(feat)}")
    print(f"Still open at end of simulated window: {n_still_open}/{len(feat)}\n")

    print("=== Aggregate comparison ===")
    orig_total_pnl = feat["orig_pnl"].sum()
    new_total_pnl = feat["new_pnl"].sum()
    orig_win_rate = (feat["orig_pnl"] > 0).mean() * 100
    new_win_rate = (feat["new_pnl"] > 0).mean() * 100
    orig_avg_ret = feat["orig_ret_pct"].mean()
    new_avg_ret = feat["new_ret_pct"].mean()
    orig_wins_sum = feat.loc[feat["orig_pnl"] > 0, "orig_pnl"].sum()
    orig_losses_sum = -feat.loc[feat["orig_pnl"] < 0, "orig_pnl"].sum()
    orig_pf = orig_wins_sum / orig_losses_sum if orig_losses_sum > 0 else float("inf")
    new_wins_sum = feat.loc[feat["new_pnl"] > 0, "new_pnl"].sum()
    new_losses_sum = -feat.loc[feat["new_pnl"] < 0, "new_pnl"].sum()
    new_pf = new_wins_sum / new_losses_sum if new_losses_sum > 0 else float("inf")
    print(f"{'Metric':30s} {'Original (100% @ target)':>26s} {'Half+EMA21-tail':>18s}")
    print(f"{'Total P&L':30s} {orig_total_pnl:>26,.0f} {new_total_pnl:>18,.0f}")
    print(f"{'Win rate %':30s} {orig_win_rate:>26.1f} {new_win_rate:>18.1f}")
    print(f"{'Avg ret %':30s} {orig_avg_ret:>26.2f} {new_avg_ret:>18.2f}")
    print(f"{'Avg hold days':30s} {feat['orig_hold_days'].mean():>26.1f} {feat['new_hold_days'].mean():>18.1f}")
    print(f"{'Profit factor':30s} {orig_pf:>26.2f} {new_pf:>18.2f}")
    print()

    print("=== Leg2 (tail) exit reason breakdown, split trades only ===")
    print(feat.loc[feat["split_happened"], "leg2_exit_reason"].value_counts())
    print()

    feat["pnl_diff"] = feat["new_pnl"] - feat["orig_pnl"]
    print("=== By original exit reason ===")
    for reason in feat["orig_exit_reason"].unique():
        sub = feat[feat["orig_exit_reason"] == reason]
        print(f"  {reason}: n={len(sub)}, orig_pnl_sum={sub['orig_pnl'].sum():,.0f}, "
             f"new_pnl_sum={sub['new_pnl'].sum():,.0f}, diff={sub['pnl_diff'].sum():,.0f}")
    print()

    print("=== Biggest differences, top 15 by |diff| ===")
    cols = ["symbol", "entry_date", "orig_exit_reason", "orig_ret_pct", "orig_hold_days",
           "split_happened", "leg2_exit_reason", "new_ret_pct", "new_hold_days", "pnl_diff"]
    top_diff = feat.reindex(feat["pnl_diff"].abs().sort_values(ascending=False).index).head(15)
    with pd.option_context("display.max_rows", None, "display.width", 220):
        print(top_diff[cols].to_string(index=False))


if __name__ == "__main__":
    main()
