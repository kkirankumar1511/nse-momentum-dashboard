"""
Ad-hoc, read-only analysis: for every trade in a saved EMA21-touch
backtest run, walk forward from entry_date to exit_date and count how
many candles (strictly AFTER entry) closed BOTH below the REAL (non-HA)
EMA13 AND red (real close < real open) -- a simple "weak/bearish
candle" count during the holding period -- then cross-tabulate against
win/loss.

Not a strategy change -- pure post-hoc analysis.

Run with: python scripts/analyze_post_entry_weak_candles.py <trades.csv>
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import indicators
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached


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
    for _, t in trades.iterrows():
        sym = t["symbol"]
        if sym not in long_candles or long_candles[sym].empty:
            continue
        df = long_candles[sym]
        idx = df.index
        if t["entry_date"] not in idx or t["exit_date"] not in idx:
            continue
        real_close, real_open = df["close"], df["open"]
        ema13_real = indicators.ema(real_close, 13)

        pos_entry = idx.get_loc(t["entry_date"])
        pos_exit = idx.get_loc(t["exit_date"])
        if pos_exit <= pos_entry:
            continue

        weak_count = 0
        for i in range(pos_entry + 1, pos_exit + 1):
            c, o, e13 = real_close.iloc[i], real_open.iloc[i], ema13_real.iloc[i]
            if pd.isna(e13):
                continue
            if c < e13 and c < o:
                weak_count += 1
        hold_days_counted = pos_exit - pos_entry
        weak_ratio = weak_count / hold_days_counted if hold_days_counted > 0 else None

        rows.append({
            "symbol": sym, "entry_date": t["entry_date"], "exit_date": t["exit_date"],
            "win": t["win"], "ret_pct": round(float(t["ret_pct"]), 2),
            "holding_days_counted": hold_days_counted,
            "weak_candle_count": weak_count,
            "weak_candle_ratio": round(weak_ratio, 3) if weak_ratio is not None else None,
        })

    feat = pd.DataFrame(rows)
    print(f"Analyzed {len(feat)} of {len(trades)} trades.\n")

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

    print(f"Overall win rate: {feat['win'].mean()*100:.1f}% ({feat['win'].sum()}/{len(feat)})\n")

    print("=== weak_candle_count: winners vs losers (mean/median) ===")
    w = feat.loc[feat["win"], "weak_candle_count"]
    l = feat.loc[~feat["win"], "weak_candle_count"]
    print(f"  Winners: mean={w.mean():.2f} median={w.median():.1f} (n={len(w)})")
    print(f"  Losers:  mean={l.mean():.2f} median={l.median():.1f} (n={len(l)})\n")

    print("=== weak_candle_ratio (count / holding days): winners vs losers ===")
    wr = feat.loc[feat["win"], "weak_candle_ratio"]
    lr = feat.loc[~feat["win"], "weak_candle_ratio"]
    print(f"  Winners: mean={wr.mean():.3f} median={wr.median():.3f} (n={len(wr)})")
    print(f"  Losers:  mean={lr.mean():.3f} median={lr.median():.3f} (n={len(lr)})\n")

    bucket_report("weak_candle_count", bins=[-1, 0, 1, 2, 3, 5, 100],
                  labels=["0", "1", "2", "3", "4-5", ">5"])
    bucket_report("weak_candle_ratio", bins=[-0.01, 0, 0.1, 0.2, 0.3, 0.5, 1.01],
                  labels=["0%", "0-10%", "10-20%", "20-30%", "30-50%", ">50%"])

    print("=== Win/loss matrix: weak_candle_count bucket x outcome ===")
    grp = pd.cut(feat["weak_candle_count"], bins=[-1, 0, 1, 2, 3, 5, 100],
                labels=["0", "1", "2", "3", "4-5", ">5"])
    matrix = pd.crosstab(grp, feat["win"].map({True: "win", False: "loss"}))
    print(matrix)


if __name__ == "__main__":
    main()
