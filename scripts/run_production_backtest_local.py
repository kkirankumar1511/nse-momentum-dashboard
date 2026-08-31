"""
Runs the STANDARD production backtest engine (backtest.run_backtest --
the actual live/deployed momentum rank-and-rebalance strategy, NOT the
experimental ema21-touch pattern) using the exact live Admin-tab config
(confirmed identical to config.STRATEGY as of 2026-08-23 -- the VPS's
strategy_config DB table has zero overrides from the code defaults).
Mirrors dashboard.py's own Backtest page call to bt.run_backtest exactly.
Local-only, read-only.

Run with: python scripts/run_production_backtest_local.py --years 5
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import backtest as bt
import config

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--rebalance", choices=["daily", "weekly", "monthly"], default="monthly")
    ap.add_argument("--rsi-exit-gate", action="store_true",
                    help="overrides rsi_exit_gate_enabled -> True")
    ap.add_argument("--rsi-exit-max", type=float, default=None,
                    help="overrides rsi_exit_max")
    ap.add_argument("--fundamental-bonus-weight", type=float, default=None,
                    help="overrides fundamental_bonus_weight (default 0.5)")
    ap.add_argument("--ema-intact-gate", action="store_true",
                    help="overrides ema_intact_gate_enabled -> True -- a candidate must "
                        "not have closed below its own EMA50 or EMA200 (real close) at "
                        "any point in the trailing --ema-intact-lookback days to be "
                        "eligible for a NEW buy (existing ranking/sell logic untouched)")
    ap.add_argument("--ema-intact-lookback", type=int, default=20,
                    help="overrides ema_intact_lookback_days (default 20)")
    ap.add_argument("--ema-intact-which", choices=["both", "ema50", "ema200"], default="both",
                    help="which EMA(s) the ema-intact gate checks, default both")
    ap.add_argument("--rsi-min", type=float, default=None,
                    help="overrides rsi_min (default 45)")
    ap.add_argument("--rsi-max", type=float, default=None,
                    help="overrides rsi_max (default 80)")
    ap.add_argument("--ema-pullback-weight", type=float, default=None,
                    help="overrides ema_pullback_weight (default 0.0) -- EMA13/21 "
                        "pullback-proximity score tilt")
    ap.add_argument("--atr-stop-multiple", type=float, default=None,
                    help="overrides atr_stop_multiple (default 2.5)")
    ap.add_argument("--trailing-atr-multiple", type=float, default=None,
                    help="overrides trailing_atr_multiple (default 4.0)")
    ap.add_argument("--mom-method", choices=["fixed_lookback", "regression"], default=None,
                    help="overrides mom_method (default fixed_lookback) -- 'regression' "
                        "uses the R^2-weighted regression-slope rs_3m/rs_6m instead of "
                        "the plain two-point return")
    ap.add_argument("--core-ema-fast", type=int, default=None,
                    help="overrides cfg['ema_fast'] -- the core trend-gate fast EMA (default 50)")
    ap.add_argument("--core-ema-slow", type=int, default=None,
                    help="overrides cfg['ema_slow'] -- the core trend-gate slow EMA (default 200)")
    ap.add_argument("--mom-lookback-short", type=int, default=None,
                    help="overrides mom_lookback_days_short (default 63)")
    ap.add_argument("--mom-lookback-long", type=int, default=None,
                    help="overrides mom_lookback_days_long (default 126)")
    ap.add_argument("--sector-bonus-weight", type=float, default=None,
                    help="overrides sector_bonus_weight (default 0.0)")
    ap.add_argument("--weekly-monthly-gate", action="store_true",
                    help="overrides weekly_monthly_gate_enabled -> True")
    ap.add_argument("--regime-filter", action="store_true",
                    help="overrides regime_filter_enabled -> True -- halves max_positions "
                        "on any day NIFTY closes below its own regime_ema_period (200) EMA, "
                        "never force-sells existing positions")
    ap.add_argument("--entry-confirm-days", type=int, default=None,
                    help="overrides entry_confirm_days (default 0/off)")
    ap.add_argument("--entry-confirm-pool-size", type=int, default=None,
                    help="overrides entry_confirm_pool_size (default max_positions*2)")
    ap.add_argument("--mad-stop", action="store_true",
                    help="overrides mad_stop_enabled -> True (replaces both the ATR "
                        "initial and trailing stop with the MAD volatility trail's "
                        "own ratcheting lower band)")
    ap.add_argument("--mad-med-len", type=int, default=None, help="overrides mad_stop_med_len (default 21)")
    ap.add_argument("--mad-mad-len", type=int, default=None, help="overrides mad_stop_mad_len (default 21)")
    ap.add_argument("--mad-dev-factor", type=float, default=None, help="overrides mad_stop_dev_factor (default 2.0)")
    ap.add_argument("--mad-atr-floor-mult", type=float, default=None, help="overrides mad_stop_atr_floor_mult (default 2.0)")
    ap.add_argument("--out-suffix", type=str, default="")
    ap.add_argument("--start-date", type=str, default=None,
                    help="YYYY-MM-DD -- fixed start date instead of trailing --years")
    args = ap.parse_args()

    cfg = dict(config.STRATEGY)
    if args.rsi_exit_gate:
        cfg["rsi_exit_gate_enabled"] = True
        cfg["rsi_exit_max"] = 100.0
        print("Overriding rsi_exit_gate_enabled -> True, rsi_exit_max -> 100.0 (from --rsi-exit-gate).")
    if args.rsi_exit_max is not None:
        cfg["rsi_exit_max"] = args.rsi_exit_max
        print(f"Overriding rsi_exit_max -> {args.rsi_exit_max} (from --rsi-exit-max).")
    if args.fundamental_bonus_weight is not None:
        cfg["fundamental_bonus_weight"] = args.fundamental_bonus_weight
        print(f"Overriding fundamental_bonus_weight -> {args.fundamental_bonus_weight} "
             f"(from --fundamental-bonus-weight).")
    if args.ema_intact_gate:
        cfg["ema_intact_gate_enabled"] = True
        cfg["ema_intact_lookback_days"] = args.ema_intact_lookback
        cfg["ema_intact_check_ema50"] = args.ema_intact_which in ("both", "ema50")
        cfg["ema_intact_check_ema200"] = args.ema_intact_which in ("both", "ema200")
        print(f"Overriding ema_intact_gate_enabled -> True, ema_intact_lookback_days -> "
             f"{args.ema_intact_lookback}, checking={args.ema_intact_which} "
             f"(from --ema-intact-gate/--ema-intact-which).")
    if args.rsi_min is not None:
        cfg["rsi_min"] = args.rsi_min
        print(f"Overriding rsi_min -> {args.rsi_min} (from --rsi-min).")
    if args.rsi_max is not None:
        cfg["rsi_max"] = args.rsi_max
        print(f"Overriding rsi_max -> {args.rsi_max} (from --rsi-max).")
    if args.ema_pullback_weight is not None:
        cfg["ema_pullback_weight"] = args.ema_pullback_weight
        print(f"Overriding ema_pullback_weight -> {args.ema_pullback_weight} "
             f"(from --ema-pullback-weight).")
    if args.atr_stop_multiple is not None:
        cfg["atr_stop_multiple"] = args.atr_stop_multiple
        print(f"Overriding atr_stop_multiple -> {args.atr_stop_multiple} (from --atr-stop-multiple).")
    if args.trailing_atr_multiple is not None:
        cfg["trailing_atr_multiple"] = args.trailing_atr_multiple
        print(f"Overriding trailing_atr_multiple -> {args.trailing_atr_multiple} "
             f"(from --trailing-atr-multiple).")
    if args.mom_method is not None:
        cfg["mom_method"] = args.mom_method
        print(f"Overriding mom_method -> {args.mom_method} (from --mom-method).")
    if args.core_ema_fast is not None:
        cfg["ema_fast"] = args.core_ema_fast
        print(f"Overriding ema_fast -> {args.core_ema_fast} (from --core-ema-fast).")
    if args.core_ema_slow is not None:
        cfg["ema_slow"] = args.core_ema_slow
        print(f"Overriding ema_slow -> {args.core_ema_slow} (from --core-ema-slow).")
    if args.mom_lookback_short is not None:
        cfg["mom_lookback_days_short"] = args.mom_lookback_short
        print(f"Overriding mom_lookback_days_short -> {args.mom_lookback_short} (from --mom-lookback-short).")
    if args.mom_lookback_long is not None:
        cfg["mom_lookback_days_long"] = args.mom_lookback_long
        print(f"Overriding mom_lookback_days_long -> {args.mom_lookback_long} (from --mom-lookback-long).")
    if args.sector_bonus_weight is not None:
        cfg["sector_bonus_weight"] = args.sector_bonus_weight
        print(f"Overriding sector_bonus_weight -> {args.sector_bonus_weight} (from --sector-bonus-weight).")
    if args.weekly_monthly_gate:
        cfg["weekly_monthly_gate_enabled"] = True
        print("Overriding weekly_monthly_gate_enabled -> True (from --weekly-monthly-gate).")
    if args.regime_filter:
        cfg["regime_filter_enabled"] = True
        print("Overriding regime_filter_enabled -> True (from --regime-filter).")
    if args.entry_confirm_days is not None:
        cfg["entry_confirm_days"] = args.entry_confirm_days
        print(f"Overriding entry_confirm_days -> {args.entry_confirm_days} (from --entry-confirm-days).")
    if args.entry_confirm_pool_size is not None:
        cfg["entry_confirm_pool_size"] = args.entry_confirm_pool_size
        print(f"Overriding entry_confirm_pool_size -> {args.entry_confirm_pool_size} "
             f"(from --entry-confirm-pool-size).")
    if args.mad_stop:
        cfg["mad_stop_enabled"] = True
        print("Overriding mad_stop_enabled -> True (from --mad-stop).")
    if args.mad_med_len is not None:
        cfg["mad_stop_med_len"] = args.mad_med_len
    if args.mad_mad_len is not None:
        cfg["mad_stop_mad_len"] = args.mad_mad_len
    if args.mad_dev_factor is not None:
        cfg["mad_stop_dev_factor"] = args.mad_dev_factor
    if args.mad_atr_floor_mult is not None:
        cfg["mad_stop_atr_floor_mult"] = args.mad_atr_floor_mult

    fundamentals_history = None
    if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
        fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]
        print(f"Loaded fundamentals_history ({len(fundamentals_history)} symbols).")

    sector_membership = sector_candles = None
    if os.path.exists(SECTOR_DATA_CACHE):
        _sd = pd.read_pickle(SECTOR_DATA_CACHE)
        sector_membership, sector_candles = _sd["sector_membership"], _sd["sector_candles"]
        print(f"Loaded sector data cached {_sd['run_time']}.")

    end_date = dt.date.today() - dt.timedelta(days=1)
    long_candles = None
    if cfg.get("weekly_monthly_gate_enabled", False) or cfg.get("resistance_zone_weight", 0.0) > 0:
        long_candles = bt.load_long_history_cached(config.UNIVERSE, end_date=end_date)
        print(f"Loaded long_candles for {len(long_candles)} symbols.")

    candles = bt.load_long_history_cached(config.UNIVERSE, end_date=end_date)
    bench = bt._tz_naive(pd.read_csv(os.path.join(bt.LONG_CACHE_DIR, "_NIFTY.csv"),
                                     index_col=0, parse_dates=True))

    if args.start_date is not None:
        sim_start_date = dt.datetime.strptime(args.start_date, "%Y-%m-%d").date()
        print(f"Overriding start date -> {sim_start_date} (from --start-date).")
    else:
        sim_start_date = dt.date.today() - dt.timedelta(days=int(args.years * 365))
    rebalance_code = {"daily": "D", "weekly": "W", "monthly": "MS"}[args.rebalance]

    print(f"\nRunning production backtest.run_backtest() with live config.STRATEGY, "
         f"rebalance={args.rebalance}, {args.years}y from {sim_start_date}...\n")
    res = bt.run_backtest(
        candles, bench, cfg, initial_capital=args.capital,
        rebalance=rebalance_code, fundamentals_history=fundamentals_history,
        sector_candles=sector_candles, sector_membership=sector_membership,
        long_candles=long_candles, start_date=sim_start_date)

    print("=== PRODUCTION STRATEGY (live Admin-tab config) ===")
    for k, v in res["metrics"].items():
        print(f"{k:24s} {v}")

    out_dir = "result"
    os.makedirs(out_dir, exist_ok=True)
    eq_path = os.path.join(out_dir, f"backtest_equity_production_live{args.out_suffix}.csv")
    tr_path = os.path.join(out_dir, f"backtest_trades_production_live{args.out_suffix}.csv")
    res["equity_curve"].rename("equity").to_csv(eq_path)
    res["trades"].to_csv(tr_path, index=False)
    print(f"\nSaved: {eq_path}, {tr_path}")


if __name__ == "__main__":
    main()
