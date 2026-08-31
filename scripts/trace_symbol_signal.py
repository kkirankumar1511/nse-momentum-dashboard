"""
Ad-hoc, read-only trace: for ONE symbol, walk the EMA21-touch state
machine day-by-day and print every raw pattern match (signal-candle-shape
match, ignoring watchlist), whether that day's watchlist check would have
passed, and the resulting state transitions -- so a specific historical
trade (e.g. SIEMENS confirming 2026-07-31) can be explained candle-by-
candle instead of just trusting the aggregate backtest output. Not a
strategy change.

Run with: python scripts/trace_symbol_signal.py SIEMENS 2026-05-01 2026-08-18 [rsi_min]
(rsi_min optional 4th arg -- overrides the WATCHLIST gate's rsi_min, to
match a specific saved backtest run's --rsi-min override.)
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
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached, rank_universe_asof
from scripts.run_ema21touch_backtest_local import (
    FILTER_CFG_OVERRIDE, NEW_EMA21_TOUCH_LATEST_LOW_CFG,
    FUNDAMENTALS_HISTORY_CACHE, SECTOR_DATA_CACHE,
)

CFG = {**ts.TRIGGERED_DEFAULTS, **FILTER_CFG_OVERRIDE, **NEW_EMA21_TOUCH_LATEST_LOW_CFG}


def main():
    sym = sys.argv[1]
    win_start = dt.datetime.strptime(sys.argv[2], "%Y-%m-%d")
    win_end = dt.datetime.strptime(sys.argv[3], "%Y-%m-%d") if len(sys.argv) > 3 else dt.datetime.today()
    if len(sys.argv) > 4:
        CFG["rsi_min"] = float(sys.argv[4])
        print(f"Overriding watchlist rsi_min -> {CFG['rsi_min']} (from CLI arg).")
    if len(sys.argv) > 5 and sys.argv[5] == "close_above_ema21":
        CFG["ha_ema21_touch_signal_close_above_ema13"] = False
        print("Overriding ha_ema21_touch_signal_close_above_ema13 -> False (from CLI arg).")
    if len(sys.argv) > 6 and "rs_formation" in sys.argv[6]:
        CFG["ha_ema21_touch_sector_rs_formation_only"] = True
        print("Overriding ha_ema21_touch_sector_rs_formation_only -> True (from CLI arg).")
    if len(sys.argv) > 6 and "above_ema_formation" in sys.argv[6]:
        CFG["ha_ema21_touch_sector_above_ema_formation_only"] = True
        print("Overriding ha_ema21_touch_sector_above_ema_formation_only -> True (from CLI arg).")
    if len(sys.argv) > 6 and "noprior" in sys.argv[6]:
        CFG["ha_ema21_touch_prior_rsi_lookback_days"] = None
        print("Overriding ha_ema21_touch_prior_rsi_lookback_days -> None (from CLI arg).")

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

    precomputed_daily = {}
    for s, df in candles.items():
        if not df.empty and len(df) >= CFG["ema_slow"]:
            precomputed_daily[s] = indicators.precompute_daily_series(df, CFG)
    precomputed_weekly_monthly = {}
    if CFG.get("weekly_monthly_gate_enabled", False):
        for s, df in long_candles.items():
            if not df.empty:
                precomputed_weekly_monthly[s] = indicators.precompute_weekly_monthly_bars(df["close"])

    df = candles[sym]
    ha = ti.precompute_heikin_ashi(df)
    ema13 = indicators.ema(ha["ha_close"], CFG["ha_ema13_period"])
    ema21 = indicators.ema(ha["ha_close"], CFG["ha_ema21_period"])
    rsi = indicators.rsi(ha["ha_close"], CFG["ha_rsi_period"])
    signal_rsi_min = CFG["ha_ema21_touch_signal_rsi_min"]
    prior_lookback = CFG["ha_ema21_touch_prior_rsi_lookback_days"]
    prior_rsi_min = CFG["ha_ema21_touch_prior_rsi_min"]
    prior_rsi_ok = (rsi.shift(1).rolling(prior_lookback).max() > prior_rsi_min
                   if prior_lookback else None)
    prior_ema13_lookback = CFG["ha_ema21_touch_prior_above_ema13_lookback_days"]
    prior_above_ema13_ok = None
    if prior_ema13_lookback:
        above_ema13 = ((ha["ha_open"] > ema13) & (ha["ha_close"] > ema13)).astype(int)
        prior_above_ema13_ok = (above_ema13.shift(1).rolling(prior_ema13_lookback).sum() > 0)

    dates_in_window = [d for d in ha.index if win_start <= d <= win_end]

    print(f"=== Raw signal-candle-shape matches for {sym}, "
         f"{win_start.date()} to {win_end.date()} (RSI floor {signal_rsi_min}) ===")
    score_cache = {}
    any_found = False
    for d in dates_in_window:
        i = ha.index.get_loc(d)
        e13, e21 = ema13.iloc[i], ema21.iloc[i]
        if pd.isna(e13) or pd.isna(e21):
            continue
        hc, ho, hh, hl = (ha["ha_close"].iloc[i], ha["ha_open"].iloc[i],
                         ha["ha_high"].iloc[i], ha["ha_low"].iloc[i])
        r = rsi.iloc[i]
        # 2026-08-23: matches the CURRENT is_signal_candle shape (any
        # candle color; close above EMA13 or EMA21 depending on
        # ha_ema21_touch_signal_close_above_ema13, NOT hardcoded to
        # EMA13 -- a real bug here caused this print loop to silently
        # miss candles that qualify under close-above-EMA21 mode, found
        # while tracing GMRAIRPORT's 07-21 confirmation).
        close_gate_level = e13 if CFG["ha_ema21_touch_signal_close_above_ema13"] else e21
        raw_pattern = (hl <= e21 and hh > e13 and hc > close_gate_level
                      and not pd.isna(r) and r > signal_rsi_min)
        if not raw_pattern:
            continue
        any_found = True
        wl_ranked = rank_universe_asof(candles, bench, d, CFG, fundamentals_history,
                                       score_cache, sector_candles, sector_membership,
                                       long_candles, precomputed_daily, None,
                                       precomputed_weekly_monthly)
        in_watchlist, rank_pos, score = False, None, None
        if not wl_ranked.empty and sym in wl_ranked.index:
            gate_passers = wl_ranked[wl_ranked["all_gates"]].sort_values("score", ascending=False)
            score = wl_ranked.loc[sym, "score"]
            if sym in gate_passers.index:
                rank_pos = gate_passers.index.get_loc(sym) + 1
                in_watchlist = rank_pos <= CFG["watchlist_size"]
        p_ok = None
        if prior_rsi_ok is not None:
            v = prior_rsi_ok.iloc[i]
            p_ok = bool(v) if not pd.isna(v) else False
        pa_ok = None
        if prior_above_ema13_ok is not None:
            va = prior_above_ema13_ok.iloc[i]
            pa_ok = bool(va) if not pd.isna(va) else False
        print(f"{d.date()}  HA_RSI={r:.2f}  ha_high={hh:.2f}  ha_low={hl:.2f}  "
             f"score={score}  rank={rank_pos}  in_top{CFG['watchlist_size']}={in_watchlist}  "
             f"prior_{prior_lookback}d_rsi_above_{prior_rsi_min}={p_ok}  "
             f"prior_{prior_ema13_lookback}d_above_ema13={pa_ok}")

    if not any_found:
        print("(no raw signal-candle-shape matches in this window)")

    print(f"\n=== Confirmed entries for {sym} (full-history state machine, "
         f"with real watchlist gate) ===")
    membership_records = {}
    for d in dates_in_window:
        wl_ranked = rank_universe_asof(candles, bench, d, CFG, fundamentals_history,
                                       score_cache, sector_candles, sector_membership,
                                       long_candles, precomputed_daily, None,
                                       precomputed_weekly_monthly)
        if wl_ranked.empty:
            continue
        gate_passers = wl_ranked[wl_ranked["all_gates"]].sort_values("score", ascending=False)
        top_syms = set(gate_passers.index[:CFG["watchlist_size"]])
        membership_records[d] = sym in top_syms
    membership = pd.Series(membership_records).sort_index() if membership_records else None

    # Mirrors backtest_triggered.py's corrected logic exactly: only build
    # the formation-time gate from whichever sub-check(s) have their own
    # flag set -- NOT an unconditional combination of both (that was a
    # real bug here, silently applying "both formation-time" regardless
    # of CFG, which didn't match any of the actual tested configs).
    rs_formation_only = CFG.get("ha_ema21_touch_sector_rs_formation_only", False)
    above_ema_formation_only = CFG.get("ha_ema21_touch_sector_above_ema_formation_only", False)
    sector_gate_ok = None
    if sector_candles is not None and sector_membership is not None \
            and (rs_formation_only or above_ema_formation_only):
        secs = sector_membership.get(sym, [])
        if secs:
            sector_df = sector_candles.get(secs[0])
            if sector_df is not None and not sector_df.empty:
                rs_lookback = CFG["institutional_rs_lookback_days"]
                stock_close = df["close"]
                sector_close = sector_df["close"].reindex(stock_close.index, method="ffill")
                gate_parts = []
                if rs_formation_only:
                    stock_ret = stock_close / stock_close.shift(rs_lookback) - 1
                    sector_ret = sector_close / sector_close.shift(rs_lookback) - 1
                    gate_parts.append((stock_ret - sector_ret) > 0)
                if above_ema_formation_only and CFG.get("sector_above_ema_enabled", False):
                    sector_ema = indicators.ema(sector_close, CFG["sector_above_ema_period"])
                    gate_parts.append(sector_close > sector_ema)
                if gate_parts:
                    sector_gate_ok = gate_parts[0]
                    for part in gate_parts[1:]:
                        sector_gate_ok = sector_gate_ok & part

    touch = ti.precompute_ema21_touch_signals(
        df, ha, CFG["ha_ema13_period"], CFG["ha_ema21_period"], CFG["ha_rsi_period"],
        CFG["ha_ema21_touch_signal_rsi_min"], CFG["ha_ema21_touch_confirm_days"],
        CFG["ha_ema21_touch_breakout_threshold_pct"], CFG["ha_ema21_touch_stop_uses_run_low"],
        CFG["ha_ema21_touch_signal_volume_ema_period"], membership,
        CFG["ha_ema21_touch_prior_rsi_lookback_days"], CFG["ha_ema21_touch_prior_rsi_min"],
        CFG["ha_ema21_touch_signal_volume_sma_period"],
        CFG["ha_ema21_touch_prior_above_ema13_lookback_days"],
        CFG["ha_ema21_touch_signal_close_above_ema13"], sector_gate_ok,
        CFG.get("ha_ema21_touch_require_real_green", False),
        CFG.get("ha_ema21_touch_ema50_slope_lookback_days"),
        CFG.get("ha_ema21_touch_ema50_slope_min_pct", 5.0),
        CFG.get("ha_ema21_touch_allow_reversal_wick_shapes", False),
        CFG.get("ha_ema21_touch_require_ema13_above_ema21", False),
        CFG.get("ha_ema21_touch_require_ha_ema_stack", False),
        CFG.get("ha_ema21_touch_confirm_on_close", False),
        CFG.get("ha_ema21_touch_prior_above_ema13_all_close", False),
        CFG.get("ha_ema21_touch_prior_tiered_ema_check", False),
        CFG.get("ha_ema21_touch_prior_no_ema50_violation_days"))
    hits = touch[touch["confirmed_entry"] & (touch.index >= win_start) & (touch.index <= win_end)]
    if hits.empty:
        print("(no confirmed entries in this window)")
    else:
        print(hits[["signal_high", "signal_low", "trigger_price"]])


if __name__ == "__main__":
    main()
