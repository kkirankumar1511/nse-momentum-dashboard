"""
Diagnostic-only (not a strategy change): explains exactly where signal
candles get filtered out under the CURRENT full gate chain (as of
2026-08-23: pattern shape + own RSI -> volume-50-SMA -> prior-10d-RSI>60
-> prior-5d-above-EMA13 -> watchlist membership -> confirm within
confirm_lookback_days, with the 2026-08-23 same-day-counts-as-day-1 fix).
Instruments a copy of trigger_indicators.precompute_ema21_touch_signals'
state machine with a counter at every gate, run once per symbol over the
target window, so the funnel can be attributed stage by stage instead of
guessed at. Does not modify any strategy file.

Run with: python scripts/diagnose_ema21touch_funnel.py --years 0.6
"""
from __future__ import annotations

import argparse
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
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached, rank_universe_asof
from scripts.run_ema21touch_backtest_local import (
    FILTER_CFG_OVERRIDE, NEW_EMA21_TOUCH_LATEST_LOW_CFG,
    FUNDAMENTALS_HISTORY_CACHE, SECTOR_DATA_CACHE,
)

STAGES = ["raw_pattern", "after_volume", "after_prior_rsi", "after_prior_ema13",
         "after_watchlist"]


def instrumented_walk(df, ha, cfg, watchlist_membership, count_from=None,
                      sym=None, confirmations=None):
    """Mirrors precompute_ema21_touch_signals exactly, with a counter at
    each successive gate (each stage counts candles that passed every
    gate up to and including that stage -- a strict funnel, not
    independent pass/fail per gate) plus run/confirm/timeout counters.
    If `confirmations` (a list) is given, every confirmed_entry day is
    appended as (sym, date) -- lets a caller cross-check same-day
    watchlist membership on the ACTUAL confirmation date, separate from
    the (usually earlier) signal-formation date already gated above."""
    ha_close, ha_open, ha_high, ha_low = ha["ha_close"], ha["ha_open"], ha["ha_high"], ha["ha_low"]
    real_high = df["high"]
    real_volume = df["volume"]
    n = len(ha_close)
    ema13 = indicators.ema(ha_close, cfg["ha_ema13_period"])
    ema21 = indicators.ema(ha_close, cfg["ha_ema21_period"])
    rsi = indicators.rsi(ha_close, cfg["ha_rsi_period"])
    signal_rsi_min = cfg["ha_ema21_touch_signal_rsi_min"]
    confirm_lookback_days = cfg["ha_ema21_touch_confirm_days"]
    breakout_threshold_pct = cfg["ha_ema21_touch_breakout_threshold_pct"]
    stop_uses_run_low = cfg["ha_ema21_touch_stop_uses_run_low"]
    close_above_ema13 = cfg["ha_ema21_touch_signal_close_above_ema13"]
    vol_sma_period = cfg["ha_ema21_touch_signal_volume_sma_period"]
    vol_ema_period = cfg["ha_ema21_touch_signal_volume_ema_period"]
    prior_rsi_lookback = cfg["ha_ema21_touch_prior_rsi_lookback_days"]
    prior_rsi_min = cfg["ha_ema21_touch_prior_rsi_min"]
    prior_ema13_lookback = cfg["ha_ema21_touch_prior_above_ema13_lookback_days"]

    volume_sma = real_volume.rolling(vol_sma_period).mean() if vol_sma_period else None
    volume_ema = indicators.ema(real_volume, vol_ema_period) if vol_ema_period else None
    prior_rsi_ok = (rsi.shift(1).rolling(prior_rsi_lookback).max() > prior_rsi_min
                   if prior_rsi_lookback else None)
    prior_above_ema13_ok = None
    if prior_ema13_lookback:
        above13 = ((ha_open > ema13) & (ha_close > ema13)).astype(int)
        prior_above_ema13_ok = above13.shift(1).rolling(prior_ema13_lookback).sum() > 0

    warmup = max(cfg["ha_ema21_period"], cfg["ha_rsi_period"]) + 1
    state = "HUNTING"
    sig_high = sig_low = None
    days_pending = 0

    c = {s: 0 for s in STAGES}
    c.update(runs_committed=0, confirmed=0, timeout=0,
             locked_out_from_pending=0, locked_out_from_hunting=0)

    for i in range(warmup, n):
        e13, e21 = ema13.iloc[i], ema21.iloc[i]
        if pd.isna(e13) or pd.isna(e21):
            continue
        hc, ho, hh, hl = ha_close.iloc[i], ha_open.iloc[i], ha_high.iloc[i], ha_low.iloc[i]
        today_date = ha_close.index[i]
        in_window = count_from is None or today_date >= count_from

        if state == "LOCKED_OUT":
            if hc > e13:
                state = "HUNTING"
            continue

        if state == "PENDING":
            if hc < e21:
                state, sig_high, sig_low, days_pending = "LOCKED_OUT", None, None, 0
                if in_window:
                    c["locked_out_from_pending"] += 1
                continue
            rh = real_high.iloc[i]
            trigger_price = sig_high * (1 + breakout_threshold_pct)
            if not pd.isna(rh) and rh >= trigger_price:
                if in_window:
                    c["confirmed"] += 1
                    if confirmations is not None:
                        confirmations.append((sym, today_date))
                state, sig_high, sig_low, days_pending = "HUNTING", None, None, 0
                continue
            days_pending += 1
            if days_pending >= confirm_lookback_days:
                state, sig_high, sig_low, days_pending = "HUNTING", None, None, 0
                if in_window:
                    c["timeout"] += 1
            continue

        if hc < e21:
            state, sig_high, sig_low = "LOCKED_OUT", None, None
            if in_window:
                c["locked_out_from_hunting"] += 1
            continue
        r = rsi.iloc[i]
        close_gate_level = e13 if close_above_ema13 else e21
        raw = (hl <= e21 and hh > e13 and hc > close_gate_level
              and not pd.isna(r) and r > signal_rsi_min)
        ok = raw
        if ok and in_window:
            c["raw_pattern"] += 1

        if ok and volume_ema is not None:
            v = volume_ema.iloc[i]
            ok = not pd.isna(v) and float(real_volume.iloc[i]) > v
        if ok and volume_sma is not None:
            v = volume_sma.iloc[i]
            ok = not pd.isna(v) and float(real_volume.iloc[i]) > v
        if ok and in_window:
            c["after_volume"] += 1

        if ok and prior_rsi_ok is not None:
            p = prior_rsi_ok.iloc[i]
            ok = bool(p) if not pd.isna(p) else False
        if ok and in_window:
            c["after_prior_rsi"] += 1

        if ok and prior_above_ema13_ok is not None:
            p = prior_above_ema13_ok.iloc[i]
            ok = bool(p) if not pd.isna(p) else False
        if ok and in_window:
            c["after_prior_ema13"] += 1

        if ok and watchlist_membership is not None:
            ok = bool(watchlist_membership.get(today_date, False))
        if ok and in_window:
            c["after_watchlist"] += 1

        is_signal_candle = ok
        if is_signal_candle:
            sig_high = hh
            if sig_low is None:
                sig_low = hl
            elif stop_uses_run_low:
                sig_low = min(sig_low, hl)
            else:
                sig_low = hl
        elif sig_high is not None:
            trigger_price = sig_high * (1 + breakout_threshold_pct)
            rh = real_high.iloc[i]
            if not pd.isna(rh) and rh >= trigger_price:
                if in_window:
                    c["confirmed"] += 1
                    if confirmations is not None:
                        confirmations.append((sym, today_date))
                sig_high, sig_low = None, None
            else:
                days_pending = 1
                if days_pending >= confirm_lookback_days:
                    state, sig_high, sig_low, days_pending = "HUNTING", None, None, 0
                else:
                    state = "PENDING"
                if in_window:
                    c["runs_committed"] += 1
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=0.6)
    ap.add_argument("--rsi-min", type=float, default=50.0)
    ap.add_argument("--close-above-ema21", action="store_true")
    args = ap.parse_args()

    fundamentals_history = None
    if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
        fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]
    sector_membership = sector_candles = None
    if os.path.exists(SECTOR_DATA_CACHE):
        _sd = pd.read_pickle(SECTOR_DATA_CACHE)
        sector_membership, sector_candles = _sd["sector_membership"], _sd["sector_candles"]

    bench = _tz_naive(pd.read_csv(os.path.join(LONG_CACHE_DIR, "_NIFTY.csv"),
                                  index_col=0, parse_dates=True))
    cache_end_date = bench.index.max().date()
    long_candles = load_long_history_cached(config.UNIVERSE, end_date=cache_end_date)
    candles = long_candles

    TRIG_WARMUP_DAYS = 780
    all_dates = bench.index.sort_values()
    data_floor = all_dates[TRIG_WARMUP_DAYS].date()
    requested_start = dt.date.today() - dt.timedelta(days=int(args.years * 365))
    sim_start_date = max(data_floor, requested_start)
    window_end = dt.date.today()
    print(f"Window: {sim_start_date} to {window_end}")

    cfg = {**ts.TRIGGERED_DEFAULTS, **FILTER_CFG_OVERRIDE, **NEW_EMA21_TOUCH_LATEST_LOW_CFG,
          "rsi_min": args.rsi_min}
    if args.close_above_ema21:
        cfg["ha_ema21_touch_signal_close_above_ema13"] = False
    print(f"Config: rsi_min={cfg['rsi_min']} watchlist_size={cfg['watchlist_size']} "
         f"close_above_ema13={cfg['ha_ema21_touch_signal_close_above_ema13']} "
         f"volume_sma={cfg['ha_ema21_touch_signal_volume_sma_period']} "
         f"prior_rsi_lookback={cfg['ha_ema21_touch_prior_rsi_lookback_days']} "
         f"prior_ema13_lookback={cfg['ha_ema21_touch_prior_above_ema13_lookback_days']} "
         f"confirm_days={cfg['ha_ema21_touch_confirm_days']}")

    precomputed_ha = {}
    for sym, df in candles.items():
        if not df.empty:
            precomputed_ha[sym] = ti.precompute_heikin_ashi(df)

    precomputed_daily = {}
    for sym, df in candles.items():
        if not df.empty and len(df) >= cfg["ema_slow"]:
            precomputed_daily[sym] = indicators.precompute_daily_series(df, cfg)

    precomputed_weekly_monthly = {}
    if cfg.get("weekly_monthly_gate_enabled", False):
        for sym, df in long_candles.items():
            if not df.empty:
                precomputed_weekly_monthly[sym] = indicators.precompute_weekly_monthly_bars(df["close"])

    dates = bench.index.sort_values()
    dates = dates[TRIG_WARMUP_DAYS:]
    dates = dates[dates >= pd.Timestamp(sim_start_date)]
    rb_dates = sorted(dates)

    print(f"Building watchlist membership over {len(rb_dates)} days "
         f"({len(candles)} symbols)...")
    membership_records = {sym: {} for sym in candles}
    score_cache = {}
    for j, d in enumerate(rb_dates):
        if j % 30 == 0:
            print(f"  {j}/{len(rb_dates)} ({d.date()})...")
        wl_ranked = rank_universe_asof(candles, bench, d, cfg, fundamentals_history,
                                      score_cache, sector_candles, sector_membership,
                                      long_candles, precomputed_daily, precomputed_pivots=None,
                                      precomputed_weekly_monthly=precomputed_weekly_monthly)
        if wl_ranked.empty:
            continue
        wl_candidates = wl_ranked[wl_ranked["all_gates"]]
        top_syms = set(wl_candidates.sort_values("score", ascending=False).index[:cfg["watchlist_size"]])
        for sym in candles:
            membership_records[sym][d] = sym in top_syms
    membership = {sym: pd.Series(recs).sort_index() for sym, recs in membership_records.items() if recs}

    totals = {s: 0 for s in STAGES}
    totals.update(runs_committed=0, confirmed=0, timeout=0,
                  locked_out_from_pending=0, locked_out_from_hunting=0)
    confirmations: list = []

    for sym, df in candles.items():
        if df.empty or sym not in precomputed_ha:
            continue
        ha = precomputed_ha[sym]
        mask = (ha.index >= pd.Timestamp(sim_start_date)) & (ha.index <= pd.Timestamp(window_end))
        if not mask.any():
            continue
        df_w = df[df.index <= pd.Timestamp(window_end)]
        ha_w = ha[ha.index <= pd.Timestamp(window_end)]
        c = instrumented_walk(df_w, ha_w, cfg, membership.get(sym), pd.Timestamp(sim_start_date),
                             sym=sym, confirmations=confirmations)
        for k in totals:
            totals[k] += c[k]

    # Cross-check: was the stock ALSO in the day-loop's own (position-
    # independent version of) watchlist on the ACTUAL confirmation date,
    # not just its (usually earlier) signal-formation date? The real day
    # loop's step 2b only calls detect_trigger for symbols currently in
    # its watchlist dict, which requires all_gates=True on that SAME day
    # -- if a confirming symbol isn't watchlisted that day, the real
    # backtest never even checks it, silently dropping a valid PENDING
    # confirmation. This quantifies exactly that gap.
    on_watchlist_at_confirm = 0
    off_watchlist_at_confirm = []
    for sym, d in confirmations:
        m = membership.get(sym)
        if m is not None and bool(m.get(d, False)):
            on_watchlist_at_confirm += 1
        else:
            off_watchlist_at_confirm.append((sym, d))

    print("\n=== Funnel (strict, each stage = passed everything up to and including it) ===")
    prev = None
    for s in STAGES:
        v = totals[s]
        drop = "" if prev is None else f"  (dropped {prev - v} = {(prev - v) / prev * 100:.1f}%)" if prev else ""
        print(f"{s:22s} {v:6d}{drop}")
        prev = v
    print(f"\n{'runs_committed':22s} {totals['runs_committed']:6d}  "
         f"(these are the ones that went to PENDING, i.e. did NOT confirm same-day)")
    print(f"{'confirmed (total)':22s} {totals['confirmed']:6d}  "
         f"(same-day + PENDING confirmations combined)")
    print(f"{'timeout':22s} {totals['timeout']:6d}")
    print(f"{'locked_out_from_pending':22s} {totals['locked_out_from_pending']:6d}")

    print(f"\n=== Confirmation-day watchlist cross-check ===")
    print(f"Of {len(confirmations)} confirmed signals: {on_watchlist_at_confirm} were ALSO on "
         f"the watchlist that same day (real day loop would have checked them);")
    print(f"{len(off_watchlist_at_confirm)} were NOT on the watchlist that day (the real day "
         f"loop's step 2b only scans `watchlist`, so detect_trigger is never called for "
         f"these -- the confirmation is silently missed even though precomputed_ema21_touch "
         f"has confirmed_entry=True sitting right there).")
    if off_watchlist_at_confirm:
        print("\nSample of missed confirmations (symbol, date):")
        for sym, d in off_watchlist_at_confirm[:20]:
            print(f"  {sym:16s} {d.date()}")


if __name__ == "__main__":
    main()
