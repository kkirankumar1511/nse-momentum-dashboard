"""
Ad-hoc, read-only analysis: for every LOSING trade in a saved backtest
run, compute the REAL-close (not HA) EMA20 and EMA50 as of entry date,
and their spread -- checks whether losing trades cluster around a
specific EMA20-vs-EMA50 trend-alignment pattern (bullish crossover,
death cross, tight/wide spread). Not a strategy change.

Run with: python scripts/analyze_losing_trades_ema_spread.py <trades.csv>
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached
from indicators import ema


def main():
    trades_path = sys.argv[1]
    trades = pd.read_csv(trades_path, parse_dates=["entry_date"])
    losers = trades[trades["pnl"] < 0].copy()
    print(f"{len(losers)} losing trades of {len(trades)} total.")

    bench = _tz_naive(pd.read_csv(os.path.join(LONG_CACHE_DIR, "_NIFTY.csv"),
                                  index_col=0, parse_dates=True))
    cache_end_date = bench.index.max().date()
    symbols = sorted(losers["symbol"].unique())
    long_candles = load_long_history_cached(symbols, end_date=cache_end_date)

    rows = []
    for _, t in losers.iterrows():
        sym = t["symbol"]
        if sym not in long_candles or long_candles[sym].empty:
            continue
        df = long_candles[sym]
        close = df["close"]
        ema20 = ema(close, 20)
        ema50 = ema(close, 50)
        d = t["entry_date"]
        if d not in close.index:
            continue
        e20, e50 = float(ema20.loc[d]), float(ema50.loc[d])
        spread_pct = (e20 - e50) / e50 * 100
        rows.append({
            "symbol": sym, "entry_date": d.date(), "ret_pct": round(t["ret_pct"], 2),
            "ema20": round(e20, 2), "ema50": round(e50, 2),
            "spread_pct": round(spread_pct, 2),
            "ema20_above_ema50": e20 > e50,
        })

    feat = pd.DataFrame(rows)
    print(f"Reconstructed EMA20/EMA50 for {len(feat)} of {len(losers)} losing trades.\n")

    with pd.option_context("display.max_rows", None, "display.width", 200):
        print(feat.sort_values("spread_pct").to_string(index=False))

    print(f"\n=== Summary ===")
    print(f"EMA20 above EMA50 (bullish alignment): "
         f"{feat['ema20_above_ema50'].sum()}/{len(feat)} "
         f"({feat['ema20_above_ema50'].mean()*100:.1f}%)")
    print(f"Mean spread: {feat['spread_pct'].mean():.2f}%, "
         f"median: {feat['spread_pct'].median():.2f}%")
    print(f"Spread distribution:")
    print(feat["spread_pct"].describe().round(2))

    bins = [-100, -5, -2, 0, 2, 5, 100]
    labels = ["< -5%", "-5 to -2%", "-2 to 0%", "0 to 2%", "2 to 5%", "> 5%"]
    grp = pd.cut(feat["spread_pct"], bins=bins, labels=labels)
    print(f"\n{grp.value_counts().sort_index()}")


if __name__ == "__main__":
    main()
