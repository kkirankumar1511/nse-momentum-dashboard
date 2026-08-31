"""
Ad-hoc, read-only analysis: for every trade in a saved EMA21-touch
backtest run, replay the SAME entry with a DIFFERENT initial stop --
instead of the signal candle's own HA low (the current rule), place the
stop slightly BELOW the signal candle's HA EMA21 (the same EMA21 line
the "touch" entry condition itself keys off), by `--buffer-pct` (0.5%
default). Since the strategy's own target is a DERIVED value (entry +
target_rr * (entry - stop)), the target is recomputed from the NEW stop
too, matching what the real engine would do if this stop rule were
actually used -- this isn't "add a second independent change," it's
"what if just the stop rule changed, letting everything downstream of
it follow naturally."

Not a strategy change -- pure post-hoc simulation for comparison.

Run with: python scripts/analyze_ema21_based_stop.py <trades.csv> [--buffer-pct 0.5] [--target-rr 2.0] [--max-hold-days 250]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import indicators
import trigger_indicators as ti
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached


def find_signal_date(ha: pd.DataFrame, entry_date: pd.Timestamp, signal_high: float,
                     lookback: int = 15) -> pd.Timestamp | None:
    idx = ha.index
    if entry_date not in idx:
        return None
    pos = idx.get_loc(entry_date)
    window = idx[max(0, pos - lookback):pos + 1]
    matches = [d for d in window if abs(float(ha.loc[d, "ha_high"]) - signal_high) < 0.01]
    return matches[-1] if matches else None


def simulate(df: pd.DataFrame, entry_date: pd.Timestamp, entry_price: float,
             new_stop: float, target_price: float, max_hold_days: int):
    idx = df.index
    if entry_date not in idx:
        return None
    pos = idx.get_loc(entry_date)
    end_pos = min(pos + max_hold_days, len(idx) - 1)
    for i in range(pos + 1, end_pos + 1):
        bar = df.iloc[i]
        if bar["low"] <= new_stop:
            fill = min(new_stop, bar["high"])
            if bar["open"] < new_stop:
                fill = bar["open"]
            return idx[i], fill, "stop", (idx[i] - entry_date).days, False
        if bar["high"] >= target_price:
            return idx[i], target_price, "target", (idx[i] - entry_date).days, False
    return idx[end_pos], float(df["close"].iloc[end_pos]), "still_open", \
        (idx[end_pos] - entry_date).days, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trades_path")
    ap.add_argument("--buffer-pct", type=float, default=0.5,
                    help="%% below HA EMA21 for the new stop, default 0.5")
    ap.add_argument("--target-rr", type=float, default=2.0)
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
    n_no_signal = 0
    for _, t in trades.iterrows():
        sym = t["symbol"]
        if sym not in long_candles or long_candles[sym].empty:
            continue
        df = long_candles[sym]
        ha = ti.precompute_heikin_ashi(df)
        ema21_ha = indicators.ema(ha["ha_close"], 21)

        touch = ti.precompute_ema21_touch_signals(df, ha, signal_close_above_ema13=False,
                                                   require_real_green=True)
        if t["entry_date"] not in touch.index:
            n_no_signal += 1
            continue
        sig_high = touch.loc[t["entry_date"], "signal_high"]
        if pd.isna(sig_high):
            n_no_signal += 1
            continue
        sig_date = find_signal_date(ha, t["entry_date"], sig_high)
        if sig_date is None:
            n_no_signal += 1
            continue

        e21_sig = ema21_ha.loc[sig_date]
        if pd.isna(e21_sig):
            continue
        new_stop = float(e21_sig) * (1 - args.buffer_pct / 100)

        entry_price = float(t["entry_price"])
        risk = entry_price - new_stop
        if risk <= 0:
            # New stop would be ABOVE entry (EMA21 too close/above entry price) --
            # can't use it as a stop at all, skip.
            continue
        new_target = entry_price + args.target_rr * risk

        sim = simulate(df, t["entry_date"], entry_price, new_stop, new_target, args.max_hold_days)
        if sim is None:
            continue
        exit_date, exit_price, reason, hold_days, still_open = sim
        qty = t["qty"]
        new_pnl = (exit_price - entry_price) * qty
        new_ret_pct = (exit_price / entry_price - 1) * 100

        rows.append({
            "symbol": sym, "entry_date": t["entry_date"],
            "orig_reason": t["reason"], "orig_ret_pct": round(float(t["ret_pct"]), 2),
            "orig_pnl": round(float(t["pnl"]), 2), "orig_stop": float(t["exit_stop_price"]),
            "orig_hold_days": t["holding_days"],
            "new_stop": round(new_stop, 2), "new_target": round(new_target, 2),
            "new_reason": reason, "new_ret_pct": round(new_ret_pct, 2),
            "new_pnl": round(new_pnl, 2), "new_hold_days": hold_days,
            "still_open": still_open,
        })

    feat = pd.DataFrame(rows)
    print(f"Simulated {len(feat)} of {len(trades)} trades ({n_no_signal} signal candles "
         f"not reconstructed, buffer_pct={args.buffer_pct}%).\n")

    print(f"Stop moved further from entry (wider risk) vs original: "
         f"{(feat['new_stop'] < feat['orig_stop']).sum()}/{len(feat)}")
    print(f"Stop moved closer to entry (tighter risk) vs original: "
         f"{(feat['new_stop'] > feat['orig_stop']).sum()}/{len(feat)}\n")

    print("=== Aggregate comparison ===")
    orig_total_pnl, new_total_pnl = feat["orig_pnl"].sum(), feat["new_pnl"].sum()
    orig_wr, new_wr = (feat["orig_pnl"] > 0).mean() * 100, (feat["new_pnl"] > 0).mean() * 100
    orig_wins = feat.loc[feat["orig_pnl"] > 0, "orig_pnl"].sum()
    orig_losses = -feat.loc[feat["orig_pnl"] < 0, "orig_pnl"].sum()
    orig_pf = orig_wins / orig_losses if orig_losses > 0 else float("inf")
    new_wins = feat.loc[feat["new_pnl"] > 0, "new_pnl"].sum()
    new_losses = -feat.loc[feat["new_pnl"] < 0, "new_pnl"].sum()
    new_pf = new_wins / new_losses if new_losses > 0 else float("inf")
    print(f"{'Metric':25s} {'Original (candle low)':>24s} {'EMA21-based stop':>18s}")
    print(f"{'Total P&L':25s} {orig_total_pnl:>24,.0f} {new_total_pnl:>18,.0f}")
    print(f"{'Win rate %':25s} {orig_wr:>24.1f} {new_wr:>18.1f}")
    print(f"{'Avg ret %':25s} {feat['orig_ret_pct'].mean():>24.2f} {feat['new_ret_pct'].mean():>18.2f}")
    print(f"{'Avg hold days':25s} {feat['orig_hold_days'].mean():>24.1f} {feat['new_hold_days'].mean():>18.1f}")
    print(f"{'Profit factor':25s} {orig_pf:>24.2f} {new_pf:>18.2f}")
    print()

    print("=== New exit reason breakdown ===")
    print(feat["new_reason"].value_counts())


if __name__ == "__main__":
    main()
