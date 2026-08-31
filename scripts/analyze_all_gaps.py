"""
Ad-hoc: for each of the 8 identified no-new-entry gaps in the ema21-touch
5yr backtest, finds every (symbol, day) where the entry pattern itself
confirmed (precompute_ema21_touch_signals), then checks whether that
symbol was actually on the day's gate-passing watchlist -- if not, which
specific gate failed. Answers: is the "signal confirmed but never
traded" pattern (found for ABB/GVT&D/POWERGRID in March-May 2026)
consistent across all the gaps, or specific to that one? Local-only,
read-only analysis.

Run with: python scripts/analyze_all_gaps.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
import indicators
import trigger_indicators as ti
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached, rank_universe_asof

GAPS = [
    ("2022-02-10", "2022-04-18"),
    ("2022-05-04", "2022-08-03"),
    ("2022-09-30", "2022-11-11"),
    ("2022-11-24", "2023-01-13"),
    ("2023-03-17", "2023-04-24"),
    ("2023-11-02", "2023-12-22"),
    ("2025-02-04", "2025-04-02"),
    ("2026-03-17", "2026-05-13"),
]

FULL_CFG = {**config.STRATEGY, "rsi_min": 60, "rsi_max": 100, "ema_fast": 50, "ema_slow": 200,
           "mom_lookback_days_short": 63, "mom_lookback_days_long": 126, "skip_recent_days": 5,
           "weekly_monthly_gate_enabled": True, "near_high_threshold": 0.85,
           "fundamental_gate_enabled": True, "fundamental_bonus_weight": 0.50,
           "min_fundamental_score": 50.0, "sector_bonus_weight": 1.00,
           "sector_diversification_enabled": False, "sector_composite_score_enabled": True,
           "history_days": 1200}

print("Loading data + precomputing once for the full universe...")
fundamentals_history = pd.read_pickle("cache/fundamentals_history.pkl")["history"]
sd = pd.read_pickle("cache/sector_data.pkl")
sector_membership, sector_candles = sd["sector_membership"], sd["sector_candles"]
long_candles = load_long_history_cached(config.UNIVERSE, end_date=pd.Timestamp("2026-08-18").date())
bench = _tz_naive(pd.read_csv(os.path.join(LONG_CACHE_DIR, "_NIFTY.csv"), index_col=0, parse_dates=True))

precomputed_daily = {}
for sym, df in long_candles.items():
    if not df.empty and len(df) >= FULL_CFG["ema_slow"]:
        precomputed_daily[sym] = indicators.precompute_daily_series(df, FULL_CFG)
precomputed_weekly_monthly = {}
for sym, df in long_candles.items():
    if not df.empty:
        precomputed_weekly_monthly[sym] = indicators.precompute_weekly_monthly_bars(df["close"])

ha_cache, touch_cache = {}, {}
for sym, df in long_candles.items():
    if df.empty:
        continue
    ha_cache[sym] = ti.precompute_heikin_ashi(df)
    touch_cache[sym] = ti.precompute_ema21_touch_signals(
        df, ha_cache[sym], 13, 21, 14, 50.0, 10, 0.001, stop_uses_run_low=False)
print("Done.\n")

fundamentals_score_cache = {}
GATE_COLS = ["trend_ok", "near_high_ok", "rsi_ok", "quality_ok", "weekly_monthly_gate_ok"]

for start, end in GAPS:
    print(f"=== Gap {start} -> {end} ===")
    confirmed_events = []  # (symbol, date)
    for sym, touch in touch_cache.items():
        window = touch.loc[start:end]
        for d in window.index[window["confirmed_entry"]]:
            confirmed_events.append((sym, d))

    if not confirmed_events:
        print("  No signal-pattern confirmations at all in this window (genuine no-signal gap).\n")
        continue

    gate_fail_counts = {c: 0 for c in GATE_COLS}
    would_have_ranked = []
    n_would_have_made_watchlist = 0
    n_events = len(confirmed_events)

    by_date = {}
    for sym, d in confirmed_events:
        by_date.setdefault(d, []).append(sym)

    for d, syms in by_date.items():
        precomputed_rows = {s: precomputed_daily[s].loc[d] for s in long_candles
                            if s in precomputed_daily and d in precomputed_daily[s].index}
        ranked = rank_universe_asof(
            long_candles, bench, d, FULL_CFG, fundamentals_history=fundamentals_history,
            score_cache=fundamentals_score_cache, sector_candles=sector_candles,
            sector_membership=sector_membership, long_candles=long_candles,
            precomputed=precomputed_rows, precomputed_weekly_monthly=precomputed_weekly_monthly)
        for sym in syms:
            if ranked.empty or sym not in ranked.index:
                gate_fail_counts.setdefault("not_in_universe_that_day", 0)
                gate_fail_counts["not_in_universe_that_day"] += 1
                continue
            row = ranked.loc[sym]
            if bool(row["all_gates"]):
                n_would_have_made_watchlist += 1
                rank = int(ranked["score"].rank(ascending=False).loc[sym])
                would_have_ranked.append(rank)
            else:
                for c in GATE_COLS:
                    if not bool(row[c]):
                        gate_fail_counts[c] += 1

    print(f"  Total confirmed signal-pattern events: {n_events}")
    print(f"  Passed ALL gates that day (would've been watchlist-eligible): {n_would_have_made_watchlist}")
    if would_have_ranked:
        in_top20 = sum(1 for r in would_have_ranked if r <= 20)
        print(f"    Of those, would've ranked in top-20 by score: {in_top20}")
    print(f"  Failed at least one gate: {n_events - n_would_have_made_watchlist}")
    print(f"  Gate failure breakdown (an event can fail multiple gates, first-failed counted here):")
    for c, cnt in gate_fail_counts.items():
        if cnt > 0:
            print(f"    {c}: {cnt}")
    print()
