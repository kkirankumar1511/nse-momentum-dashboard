"""
Ad-hoc, read-only analysis: for every trade in a saved EMA21-touch
backtest run, reconstruct the signal candle and check the REAL-price
Supertrend indicator (period 10, multiplier 3 -- standard defaults) as
of that candle -- is the signal candle's real close above a "green"
(uptrend) Supertrend line, or below a "red" (downtrend) one -- then
cross-tabulate against win/loss.

No Supertrend implementation existed anywhere in this codebase
(indicators.py/trigger_indicators.py) -- implemented here standalone,
standard formula (ATR-band flip-flop), real OHLC (not HA).

Not a strategy change -- pure post-hoc analysis.

Run with: python scripts/analyze_supertrend_factor.py <trades.csv> [--period 10] [--multiplier 3.0]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import indicators
import trigger_indicators as ti
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """Standard Supertrend: returns (line: pd.Series, trend: pd.Series of
    +1/-1, +1 = uptrend/green, -1 = downtrend/red)."""
    high, low, close = df["high"], df["low"], df["close"]
    atr = indicators.atr(df, period)
    hl2 = (high + low) / 2
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr

    n = len(df)
    final_upper = [0.0] * n
    final_lower = [0.0] * n
    trend = [1] * n
    line = [0.0] * n

    for i in range(n):
        if i == 0 or pd.isna(atr.iloc[i]):
            final_upper[i] = float(basic_upper.iloc[i]) if not pd.isna(basic_upper.iloc[i]) else 0.0
            final_lower[i] = float(basic_lower.iloc[i]) if not pd.isna(basic_lower.iloc[i]) else 0.0
            trend[i] = 1
            line[i] = final_lower[i]
            continue
        bu, bl = float(basic_upper.iloc[i]), float(basic_lower.iloc[i])
        prev_close = float(close.iloc[i - 1])
        final_upper[i] = bu if (bu < final_upper[i - 1] or prev_close > final_upper[i - 1]) \
            else final_upper[i - 1]
        final_lower[i] = bl if (bl > final_lower[i - 1] or prev_close < final_lower[i - 1]) \
            else final_lower[i - 1]

        c = float(close.iloc[i])
        if trend[i - 1] == 1:
            trend[i] = -1 if c < final_lower[i] else 1
        else:
            trend[i] = 1 if c > final_upper[i] else -1
        line[i] = final_lower[i] if trend[i] == 1 else final_upper[i]

    return pd.Series(line, index=df.index), pd.Series(trend, index=df.index)


def find_signal_date(ha, entry_date, signal_high, lookback=15):
    idx = ha.index
    if entry_date not in idx:
        return None
    pos = idx.get_loc(entry_date)
    window = idx[max(0, pos - lookback):pos + 1]
    matches = [d for d in window if abs(float(ha.loc[d, "ha_high"]) - signal_high) < 0.01]
    return matches[-1] if matches else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trades_path")
    ap.add_argument("--period", type=int, default=10)
    ap.add_argument("--multiplier", type=float, default=3.0)
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

        st_line, st_trend = supertrend(df, args.period, args.multiplier)
        i = df.index.get_loc(sig_date)
        trend_at_signal = st_trend.iloc[i]
        color = "green" if trend_at_signal == 1 else "red"

        rows.append({
            "symbol": sym, "sig_date": sig_date, "win": t["win"],
            "ret_pct": round(float(t["ret_pct"]), 2),
            "supertrend_color": color,
            "supertrend_line": round(float(st_line.iloc[i]), 2),
            "real_close": round(float(df["close"].iloc[i]), 2),
        })

    feat = pd.DataFrame(rows)
    print(f"Reconstructed {len(feat)} of {len(trades)} trades ({n_no_signal} signal "
         f"candles not found, period={args.period}, multiplier={args.multiplier}).\n")
    print(f"Overall win rate: {feat['win'].mean()*100:.1f}% ({feat['win'].sum()}/{len(feat)})\n")

    tbl = feat.groupby("supertrend_color").agg(
        n=("win", "size"), win_rate=("win", "mean"), avg_ret=("ret_pct", "mean"))
    tbl["win_rate"] = (tbl["win_rate"] * 100).round(1)
    tbl["avg_ret"] = tbl["avg_ret"].round(2)
    print("--- supertrend_color ---")
    print(tbl)
    print()

    print("=== Win/loss matrix: supertrend_color x outcome ===")
    matrix = pd.crosstab(feat["supertrend_color"], feat["win"].map({True: "win", False: "loss"}))
    print(matrix)


if __name__ == "__main__":
    main()
