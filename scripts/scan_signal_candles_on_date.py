"""
Ad-hoc, read-only scan: which symbols had a raw EMA21-touch signal-candle-
shape match (red HA, HA_low<=EMA21, HA_high>EMA13, HA_close>EMA21, HA
RSI>signal_rsi_min) on ONE specific date, across the whole universe --
regardless of watchlist status. Not a strategy change.

Run with: python scripts/scan_signal_candles_on_date.py 2026-07-30
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
import indicators
import trigger_indicators as ti
import trigger_strategy as ts
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached
from scripts.run_ema21touch_backtest_local import (
    FILTER_CFG_OVERRIDE, NEW_EMA21_TOUCH_LATEST_LOW_CFG,
)

CFG = {**ts.TRIGGERED_DEFAULTS, **FILTER_CFG_OVERRIDE, **NEW_EMA21_TOUCH_LATEST_LOW_CFG}


def main():
    target = pd.Timestamp(dt.datetime.strptime(sys.argv[1], "%Y-%m-%d"))

    bench = _tz_naive(pd.read_csv(os.path.join(LONG_CACHE_DIR, "_NIFTY.csv"),
                                  index_col=0, parse_dates=True))
    cache_end_date = bench.index.max().date()
    long_candles = load_long_history_cached(config.UNIVERSE, end_date=cache_end_date)

    signal_rsi_min = CFG["ha_ema21_touch_signal_rsi_min"]
    hits = []
    for sym, df in long_candles.items():
        if df.empty or target not in df.index:
            continue
        ha = ti.precompute_heikin_ashi(df)
        if target not in ha.index:
            continue
        i = ha.index.get_loc(target)
        ema13 = indicators.ema(ha["ha_close"], CFG["ha_ema13_period"])
        ema21 = indicators.ema(ha["ha_close"], CFG["ha_ema21_period"])
        rsi = indicators.rsi(ha["ha_close"], CFG["ha_rsi_period"])
        e13, e21, r = ema13.iloc[i], ema21.iloc[i], rsi.iloc[i]
        if pd.isna(e13) or pd.isna(e21) or pd.isna(r):
            continue
        hc, ho, hh, hl = (ha["ha_close"].iloc[i], ha["ha_open"].iloc[i],
                         ha["ha_high"].iloc[i], ha["ha_low"].iloc[i])
        raw_pattern = (hc < ho and hl <= e21 and hh > e13 and hc > e21 and r > signal_rsi_min)
        if raw_pattern:
            vol_sma50 = df["volume"].rolling(50).mean().iloc[i]
            vol_today = float(df["volume"].iloc[i])
            above_50sma = (not pd.isna(vol_sma50)) and vol_today > vol_sma50
            hits.append((sym, round(r, 2), round(hh, 2), round(hl, 2),
                        vol_today, vol_sma50, above_50sma))

    print(f"=== Raw signal-candle-shape matches on {target.date()} "
         f"(RSI floor {signal_rsi_min}, {len(long_candles)} symbols scanned) ===")
    if not hits:
        print("(none)")
    else:
        for sym, r, hh, hl, vol, vsma, above in sorted(hits, key=lambda h: -h[1]):
            vsma_s = f"{vsma:,.0f}" if not pd.isna(vsma) else "n/a"
            print(f"{sym:16s} HA_RSI={r:6.2f}  ha_high={hh:10.2f}  ha_low={hl:10.2f}  "
                 f"volume={vol:>12,.0f}  vol_50sma={vsma_s:>12}  above_50sma={above}")
        n_above = sum(1 for h in hits if h[6])
        print(f"\n{n_above} of {len(hits)} signal candles had volume above their own 50-day SMA volume.")


if __name__ == "__main__":
    main()
