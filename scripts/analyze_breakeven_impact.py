"""
Ad-hoc, read-only analysis: for every WINNING (target-hit) trade in a
saved backtest run, simulate a "move stop to breakeven once price
reaches 1R" rule and check whether price ever pulled back to entry
price AFTER first reaching 1R but BEFORE reaching the 2R target. If so,
that winner would have been stopped out at breakeven (0R) instead of
banking the full 2R -- this quantifies the cost of adding a breakeven
stop, to weigh against analyze_losing_trades_mfe.py's finding that ~33%
of losers had already reached 1R before failing. Not a strategy change.

Run with: python scripts/analyze_breakeven_impact.py <trades.csv>
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
        window = df.loc[t["entry_date"]:t["exit_date"]]
        if window.empty:
            continue
        risk = t["entry_price"] - t["exit_stop_price"]
        if risk <= 0:
            continue
        one_r_price = t["entry_price"] + risk
        target_price = t["exit_price"]  # target exits fill at the target level

        # First day the HIGH reaches 1R.
        reached_1r = window[window["high"] >= one_r_price]
        if reached_1r.empty:
            rows.append({"symbol": sym, "entry_date": t["entry_date"],
                        "reached_1r": False, "breakeven_would_stop": None})
            continue
        first_1r_date = reached_1r.index[0]

        # From the day AFTER 1R was reached (breakeven stop is set that day,
        # but the trade already survived that day's own low to still be
        # open/heading toward target -- checking from the SAME day risks
        # double-counting the day that triggered breakeven itself; using
        # the next day onward is the conservative, standard convention) up
        # to (not including) the exit day, did price ever dip back to/below
        # entry (breakeven)?
        after_1r = window.loc[first_1r_date:].iloc[1:]
        dipped_to_breakeven = bool((after_1r["low"] <= t["entry_price"]).any())

        rows.append({"symbol": sym, "entry_date": t["entry_date"],
                    "reached_1r": True, "breakeven_would_stop": dipped_to_breakeven})

    feat = pd.DataFrame(rows)
    n = len(feat)
    n_reached_1r = feat["reached_1r"].sum()
    n_breakeven_stopped = (feat["breakeven_would_stop"] == True).sum()
    n_safe = ((feat["reached_1r"]) & (feat["breakeven_would_stop"] == False)).sum()

    print(f"\nOf {n} winning trades:")
    print(f"  Never even reached 1R before hitting target directly: "
         f"{n - n_reached_1r} ({(n - n_reached_1r)/n*100:.1f}%) -- breakeven rule "
         f"never engages for these, no impact")
    print(f"  Reached 1R, then pulled back to breakeven before target "
         f"(WOULD BE STOPPED at breakeven instead of winning): "
         f"{n_breakeven_stopped} ({n_breakeven_stopped/n*100:.1f}%)")
    print(f"  Reached 1R and went straight to target without pulling back "
         f"(breakeven rule harmless, still a full winner): "
         f"{n_safe} ({n_safe/n*100:.1f}%)")


if __name__ == "__main__":
    main()
