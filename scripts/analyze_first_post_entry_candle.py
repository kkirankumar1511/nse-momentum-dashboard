"""
Ad-hoc, read-only analysis: for every trade in a saved EMA21-touch
backtest run, look at the ENTRY DAY'S OWN candle (T=0, the same day the
trigger-price crossing fires the entry) -- is it weak (real close <
real EMA13) or strong, red or green by the time IT closes, and how big
a move is it relative to the entry (crossing) price -- then cross-
tabulate against the eventual win/loss outcome. Distinct from analyze_
post_entry_weak_candles.py (which measures weakness averaged/summed
across the WHOLE holding period) -- this looks at just the entry
candle's own close, per explicit request ("after taking entry the
candle is form, is it weak or strong -- e.g. a big red candle -- is
there any point holding till stop" -- clarified to mean the ENTRY
candle itself, not the day after).

Not a strategy change -- pure post-hoc analysis.

Run with: python scripts/analyze_first_post_entry_candle.py <trades.csv>
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
        if t["entry_date"] not in idx:
            continue
        real_close, real_open = df["close"], df["open"]
        ema13_real = indicators.ema(real_close, 13)

        pos_entry = idx.get_loc(t["entry_date"])

        entry_price = float(t["entry_price"])
        c1, o1, e13_1 = real_close.iloc[pos_entry], real_open.iloc[pos_entry], ema13_real.iloc[pos_entry]
        if pd.isna(e13_1):
            continue

        is_red = c1 < o1
        is_below_ema13 = c1 < e13_1
        if is_red and is_below_ema13:
            category = "weak_red_below_ema13"
        elif is_red:
            category = "red_above_ema13"
        elif is_below_ema13:
            category = "green_below_ema13"
        else:
            category = "strong_green_above_ema13"

        entry_candle_ret_pct = (c1 / entry_price - 1) * 100

        rows.append({
            "symbol": sym, "entry_date": t["entry_date"], "win": t["win"],
            "ret_pct": round(float(t["ret_pct"]), 2), "pnl": float(t["pnl"]), "qty": t["qty"],
            "entry_price": entry_price, "entry_candle_close": c1,
            "entry_candle_category": category,
            "entry_candle_ret_pct": round(entry_candle_ret_pct, 2),
        })

    feat = pd.DataFrame(rows)
    print(f"Analyzed {len(feat)} of {len(trades)} trades.\n")
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

    bucket_report("entry_candle_category")
    bucket_report("entry_candle_ret_pct", bins=[-100, -3, -1, 0, 1, 3, 100],
                  labels=["<-3%", "-3to-1%", "-1to0%", "0to1%", "1to3%", ">3%"])

    print("=== Win/loss matrix: entry_candle_category x outcome ===")
    matrix = pd.crosstab(feat["entry_candle_category"], feat["win"].map({True: "win", False: "loss"}))
    print(matrix)
    print()

    print("=== Specific check: 'weak_red_below_ema13' AND entry_candle_ret_pct < -1% (a genuinely BIG red entry-day candle) ===")
    big_red = feat[(feat["entry_candle_category"] == "weak_red_below_ema13")
                   & (feat["entry_candle_ret_pct"] < -1)]
    if len(big_red):
        print(f"  n={len(big_red)}, win_rate={big_red['win'].mean()*100:.1f}%, "
             f"avg_ret={big_red['ret_pct'].mean():.2f}%")
    else:
        print("  (none)")
    print()

    print("=== What-if: exit SAME DAY at entry-candle close, for trades whose "
         "entry candle was weak_red_below_ema13 ===")
    for label, subset in [
        ("weak_red_below_ema13 (all)", feat[feat["entry_candle_category"] == "weak_red_below_ema13"]),
        ("weak_red_below_ema13 AND entry_candle_ret_pct<-1%",
         feat[(feat["entry_candle_category"] == "weak_red_below_ema13")
             & (feat["entry_candle_ret_pct"] < -1)]),
    ]:
        if len(subset) == 0:
            continue
        actual_pnl_sum = subset["pnl"].sum()
        sameday_pnl = (subset["entry_candle_close"] - subset["entry_price"]) * subset["qty"]
        sameday_pnl_sum = sameday_pnl.sum()
        print(f"  {label}: n={len(subset)}")
        print(f"    Actual (held to normal exit): pnl_sum={actual_pnl_sum:,.0f}, "
             f"win_rate={subset['win'].mean()*100:.1f}%")
        print(f"    Same-day exit at entry-candle close: pnl_sum={sameday_pnl_sum:,.0f}, "
             f"win_rate={(sameday_pnl>0).mean()*100:.1f}%")
        print(f"    Difference: {sameday_pnl_sum - actual_pnl_sum:,.0f}")
    print()

    print("=== Overall total P&L: actual vs same-day-exit-on-weak-red-entry-candle ===")
    is_weak = feat["entry_candle_category"] == "weak_red_below_ema13"
    hybrid_pnl = feat["pnl"].where(
        ~is_weak, (feat["entry_candle_close"] - feat["entry_price"]) * feat["qty"])
    print(f"  Actual total pnl_sum: {feat['pnl'].sum():,.0f}")
    print(f"  Hybrid (same-day exit only for weak_red_below_ema13 entries): "
         f"{hybrid_pnl.sum():,.0f}")


if __name__ == "__main__":
    main()
