"""
Ad-hoc, read-only analysis: for every trade in a saved EMA21-touch
backtest run, reconstruct the signal candle and check two "is this
entry happening in a stalled/range-bound market" proxies:

  1. Overhead resistance clearance -- reuses resistance_zones.py
     UNCHANGED (precompute_pivots + resistance_clearance_asof, already
     built into this codebase for exactly this purpose: multi-year
     confirmed swing-high/low zones, tolerance/search bands, strength-
     weighted distance). A SMALL clearance value means the entry is
     sitting close to a real prior reversal level above it -- "near
     resistance." None means no nearby overhead zone was found (best
     case -- clean room).
  2. 20-day price-range compression -- (rolling 20-day high - rolling
     20-day low) / close, at the signal candle. A NARROW range signals
     the stock has been going sideways/consolidating in a tight channel
     recently, rather than trending.

Cross-tabulates both against win/loss.

Not a strategy change -- pure post-hoc analysis.

Run with: python scripts/analyze_resistance_sideways_factor.py <trades.csv>
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import resistance_zones as rz
import trigger_indicators as ti
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached


def find_signal_date(ha, entry_date, signal_high, lookback=15):
    idx = ha.index
    if entry_date not in idx:
        return None
    pos = idx.get_loc(entry_date)
    window = idx[max(0, pos - lookback):pos + 1]
    matches = [d for d in window if abs(float(ha.loc[d, "ha_high"]) - signal_high) < 0.01]
    return matches[-1] if matches else None


def main():
    trades_path = sys.argv[1]
    trades = pd.read_csv(trades_path, parse_dates=["entry_date", "exit_date"])
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

        i = df.index.get_loc(sig_date)
        sig_close = float(df["close"].iloc[i])

        pivots = rz.precompute_pivots(df, window=10)
        clearance = rz.resistance_clearance_asof(pivots, sig_close, sig_date)

        range20_high = df["high"].iloc[max(0, i - 19):i + 1].max()
        range20_low = df["low"].iloc[max(0, i - 19):i + 1].min()
        range_pct = (float(range20_high) - float(range20_low)) / sig_close * 100

        near_resistance = "near(<5%)" if (clearance is not None and clearance < 0.05) else (
            "moderate(5-15%)" if (clearance is not None and clearance < 0.15) else "clear/none")

        rows.append({
            "symbol": sym, "sig_date": sig_date, "win": t["win"],
            "ret_pct": round(float(t["ret_pct"]), 2),
            "resistance_clearance_pct": round(clearance * 100, 2) if clearance is not None else None,
            "near_resistance": near_resistance,
            "range20_pct": round(range_pct, 2),
        })

    feat = pd.DataFrame(rows)
    print(f"Reconstructed {len(feat)} of {len(trades)} trades ({n_no_signal} signal "
         f"candles not found).\n")
    print(f"Overall win rate: {feat['win'].mean()*100:.1f}% ({feat['win'].sum()}/{len(feat)})\n")

    def bucket_report(col, bins=None, labels=None):
        s = feat[col]
        grp = pd.cut(s, bins=bins, labels=labels) if bins is not None else s
        tbl = feat.groupby(grp, observed=True).agg(
            n=("win", "size"), win_rate=("win", "mean"), avg_ret=("ret_pct", "mean"))
        tbl["win_rate"] = (tbl["win_rate"] * 100).round(1)
        tbl["avg_ret"] = tbl["avg_ret"].round(2)
        print(f"--- {col} ---")
        print(tbl)
        print()

    bucket_report("near_resistance")
    print("=== Win/loss matrix: near_resistance x outcome ===")
    print(pd.crosstab(feat["near_resistance"], feat["win"].map({True: "win", False: "loss"})))
    print()

    bucket_report("range20_pct", bins=[0, 10, 15, 20, 30, 50, 1000],
                  labels=["<10%(tight)", "10-15%", "15-20%", "20-30%", "30-50%", ">50%(wide)"])
    print("=== Win/loss matrix: range20_pct bucket x outcome ===")
    grp = pd.cut(feat["range20_pct"], bins=[0, 10, 15, 20, 30, 50, 1000],
                labels=["<10%(tight)", "10-15%", "15-20%", "20-30%", "30-50%", ">50%(wide)"])
    print(pd.crosstab(grp, feat["win"].map({True: "win", False: "loss"})))
    print()

    print("=== Combined: near resistance AND tight 20d range (both 'sideways' signs at once) ===")
    combo = feat[(feat["near_resistance"] == "near(<5%)") & (feat["range20_pct"] < 15)]
    if len(combo):
        print(f"  n={len(combo)}, win_rate={combo['win'].mean()*100:.1f}%, "
             f"avg_ret={combo['ret_pct'].mean():.2f}%")
    else:
        print("  (no trades match both conditions)")


if __name__ == "__main__":
    main()
