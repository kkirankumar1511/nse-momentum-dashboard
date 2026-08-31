"""
Ad-hoc, read-only analysis: for every LOSING (stop-out) trade in a saved
backtest run, find the maximum favorable excursion (highest real intraday
price reached between entry and the stop-out) and express it in R
multiples (R = entry_price - stop_price, since this strategy uses a
fixed, non-trailing stop). Answers "how many losers still got to 1R /
1.5R / 2R before falling back to the stop." Not a strategy change.

Run with: python scripts/analyze_losing_trades_mfe.py <trades.csv>
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached


def main():
    trades_path = sys.argv[1]
    trades = pd.read_csv(trades_path, parse_dates=["entry_date", "exit_date"])
    losers = trades[trades["reason"] == "stop"].copy()
    print(f"{len(losers)} losing (stop-out) trades of {len(trades)} total.")

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
        window = df.loc[t["entry_date"]:t["exit_date"], "high"]
        if window.empty:
            continue
        max_high = float(window.max())
        risk = t["entry_price"] - t["exit_stop_price"]
        if risk <= 0:
            continue
        r_reached = (max_high - t["entry_price"]) / risk
        rows.append({"symbol": sym, "entry_date": t["entry_date"],
                    "r_reached": r_reached, "holding_days": t["holding_days"]})

    feat = pd.DataFrame(rows)
    n = len(feat)
    print(f"Reconstructed MFE for {n} of {len(losers)} losing trades.\n")

    for thresh in [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
        cnt = (feat["r_reached"] >= thresh).sum()
        print(f"Reached >= {thresh}R before stopping out: {cnt}/{n} ({cnt/n*100:.1f}%)")

    print(f"\nDistribution of R reached (max favorable excursion):")
    print(feat["r_reached"].describe().round(2))

    bins = [-100, 0, 0.5, 1.0, 1.5, 2.0, 3.0, 100]
    labels = ["<0 (never green)", "0-0.5R", "0.5-1R", "1-1.5R", "1.5-2R", "2-3R", ">3R"]
    grp = pd.cut(feat["r_reached"], bins=bins, labels=labels)
    print(f"\n{grp.value_counts().sort_index()}")


if __name__ == "__main__":
    main()
