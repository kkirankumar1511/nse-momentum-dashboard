"""
Ad-hoc: for every trade in a saved ema21-touch trades CSV, reconstructs
the actual signal candle (via precompute_ema21_touch_signals + a
backward HA high/low match, same method used throughout this session's
trade-by-trade explanations), then cross-tabulates win/loss against
whether that signal candle's own real volume was above its trailing
50-day / 100-day SMA. Local-only, read-only analysis -- does not alter
any strategy code or config.

Run with: python scripts/analyze_signal_volume_vs_winrate.py <trades_csv>
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import trigger_indicators as ti

TRADES_CSV = sys.argv[1] if len(sys.argv) > 1 else \
    "result/backtest_trades_ema21touch_new_latestlow_5yr_latestlow_rsi50.csv"

# Must match the params actually used to produce this run (latest-low
# variant, RSI=50, 0.1% threshold, 10-day confirm window).
EMA13, EMA21, RSI_PERIOD, SIGNAL_RSI_MIN = 13, 21, 14, 50.0
CONFIRM_DAYS, THRESHOLD_PCT = 10, 0.001

trades = pd.read_csv(TRADES_CSV, parse_dates=["entry_date", "exit_date"])
print(f"Loaded {len(trades)} trades from {TRADES_CSV}\n")

ha_cache: dict = {}
touch_cache: dict = {}
df_cache: dict = {}

rows = []
unresolved = 0

for _, t in trades.iterrows():
    sym, entry_date = t["symbol"], t["entry_date"]
    if sym not in df_cache:
        df_cache[sym] = pd.read_csv(f"cache/long/{sym}.csv", index_col=0, parse_dates=True)
        ha_cache[sym] = ti.precompute_heikin_ashi(df_cache[sym])
        touch_cache[sym] = ti.precompute_ema21_touch_signals(
            df_cache[sym], ha_cache[sym], EMA13, EMA21, RSI_PERIOD, SIGNAL_RSI_MIN,
            CONFIRM_DAYS, THRESHOLD_PCT, stop_uses_run_low=False)
    df, ha, touch = df_cache[sym], ha_cache[sym], touch_cache[sym]

    if entry_date not in touch.index or not bool(touch.loc[entry_date, "confirmed_entry"]):
        unresolved += 1
        continue
    sig_high, sig_low = touch.loc[entry_date, ["signal_high", "signal_low"]]

    recent = ha.loc[:entry_date].tail(25)
    match = recent[(recent["ha_high"] == sig_high) & (recent["ha_low"] == sig_low)]
    if match.empty:
        unresolved += 1
        continue
    signal_date = match.index[0]

    vol_series = df["volume"]
    if signal_date not in vol_series.index:
        unresolved += 1
        continue
    signal_vol = float(vol_series.loc[signal_date])
    hist = vol_series.loc[:signal_date]
    if len(hist) < 100:
        unresolved += 1
        continue
    sma50 = float(hist.tail(50).mean())
    sma100 = float(hist.tail(100).mean())

    rows.append({
        "symbol": sym, "entry_date": entry_date, "signal_date": signal_date,
        "pnl": t["pnl"], "is_win": t["pnl"] > 0,
        "signal_vol": signal_vol, "sma50": sma50, "sma100": sma100,
        "above_50sma": signal_vol > sma50, "above_100sma": signal_vol > sma100,
    })

result = pd.DataFrame(rows)
print(f"Resolved signal candle for {len(result)}/{len(trades)} trades "
     f"({unresolved} unresolved -- insufficient history or no match found).\n")


def summarize(label, mask):
    sub = result[mask]
    n = len(sub)
    if n == 0:
        print(f"{label}: no trades")
        return
    wins = int(sub["is_win"].sum())
    win_rate = wins / n * 100
    total_pnl = sub["pnl"].sum()
    avg_pnl = sub["pnl"].mean()
    print(f"{label}: n={n} wins={wins} losses={n-wins} win_rate={win_rate:.1f}% "
         f"total_pnl={total_pnl:,.0f} avg_pnl={avg_pnl:,.0f}")


print("=== By: signal candle volume vs its own 50-day SMA ===")
summarize("Volume ABOVE 50-SMA", result["above_50sma"])
summarize("Volume AT/BELOW 50-SMA", ~result["above_50sma"])

print("\n=== By: signal candle volume vs its own 100-day SMA ===")
summarize("Volume ABOVE 100-SMA", result["above_100sma"])
summarize("Volume AT/BELOW 100-SMA", ~result["above_100sma"])

print("\n=== Cross-tab: 50-SMA x 100-SMA ===")
for a in [True, False]:
    for b in [True, False]:
        mask = (result["above_50sma"] == a) & (result["above_100sma"] == b)
        summarize(f"50-SMA={'above' if a else 'below'}, 100-SMA={'above' if b else 'below'}", mask)

print("\n=== Overall (all resolved trades) ===")
summarize("All", pd.Series(True, index=result.index))
