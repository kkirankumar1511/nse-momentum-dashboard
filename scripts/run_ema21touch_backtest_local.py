"""
Local-only comparison runner: the currently-live paper-trade HA-trend
strategy (heikin_ashi_enabled) vs. a NEW, independent entry pattern --
EMA21-touch-then-wait-for-breakout (ha_ema21_touch_enabled) -- over the
SAME cached universe/period/stock-selection. Does not touch
heikin_ashi_trend_entry, heikin_ashi_ema21_bounce_entry,
scripts/run_triggered_backtest_local.py, or paper_engine.py -- the new
pattern lives entirely in new, additive functions/config keys/branches
(trigger_indicators.precompute_ema21_touch_signals/
heikin_ashi_ema21_touch_entry, trigger_strategy.TRIGGERED_DEFAULTS'
ha_ema21_touch_* keys, detect_trigger's ha_ema21_touch_enabled branch),
all defaulting off/None so the existing strategies are byte-identical to
before this file existed.

Uses load_long_history_cached()'s cache/long/*.csv (append-only, never
truncated) as its main candle source -- see run_triggered_backtest_
local.py's own docstring for why, same reasoning applies here.

Run with:  python scripts/run_ema21touch_backtest_local.py --years 5
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
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached
from backtest_triggered import run_triggered_backtest

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")

# Same stock-selection pipeline as the live paper-trade strategy --
# copied verbatim from scripts/run_triggered_backtest_local.py's
# FILTER_CFG_OVERRIDE (point 1 of the new spec: "same stock selection").
FILTER_CFG_OVERRIDE: dict = {
    "rsi_min": 60,
    "rsi_max": 100,
    "ema_fast": 50,
    "ema_slow": 200,
    "mom_lookback_days_short": 63,
    "mom_lookback_days_long": 126,
    "skip_recent_days": 5,
    "rsi_exit_gate_enabled": False,
    "weekly_monthly_gate_enabled": True,
    "advanced_equal_weight_sizing": False,
    "equal_weight_tolerance_pct": 0.20,
    "near_high_threshold": 0.85,
    "fundamental_gate_enabled": True,
    "fundamental_bonus_weight": 0.50,
    "min_fundamental_score": 50.0,
    "sector_bonus_weight": 1.00,
    "sector_diversification_enabled": False,
    "sector_composite_score_enabled": True,
    "history_days": 1200,
}

# The currently-live paper-trade config -- copied verbatim from
# scripts/run_triggered_backtest_local.py's HEIKIN_ASHI_OVERRIDE, exactly
# as paper_engine.py's own PAPER_CFG has it frozen. This is the
# BASELINE this new pattern is compared against.
BASELINE_HA_CFG: dict = {
    "heikin_ashi_enabled": True,
    "multiyear_breakout_enabled": False,
    "shortterm_breakout_enabled": False,
    "pullback_slow_ema_enabled": False,
    "pullback_fast_ema_enabled": False,
    "watchlist_size": 20,
    "profit_target_rr": 2.0,
    "max_loss_pct_per_trade": 1.0,
    "ha_stop_mode": "atr",
    "ha_stop_atr_multiple": 1.0,
    "trailing_stop_enabled": True,
    "trailing_atr_multiple": 1.0,
    "ha_target_enabled": False,
    "ha_signal_lookback_days": 2,
    "monthly_trend_persistence_enabled": False,
    "sector_above_ema_enabled": True,
    "sector_overextension_enabled": False,
}

# NEW strategy (2026-08-21 spec): EMA21-touch-then-wait-for-breakout.
# Same stock selection + sector gates as BASELINE_HA_CFG above, but an
# entirely different entry pattern -- see trigger_indicators.
# heikin_ashi_ema21_touch_entry/precompute_ema21_touch_signals'
# docstrings for the full rule set. Explicitly NOT trailing (fixed stop
# = signal candle's HA low, fixed 1:2 target) -- "for now make stop as
# 1:2 target and see the result first," per the spec.
NEW_EMA21_TOUCH_CFG: dict = {
    "heikin_ashi_enabled": False,
    "ha_ema21_bounce_enabled": False,
    "multiyear_breakout_enabled": False,
    "shortterm_breakout_enabled": False,
    "pullback_slow_ema_enabled": False,
    "pullback_fast_ema_enabled": False,
    # 2026-08-22: 999 -- effectively no top-N cap, every gate-passer is
    # eligible (was 20). The watchlist's other gates (trend/near-high/
    # rsi_ok/weekly-monthly/price/quality) still apply -- this only
    # removes the score-based top-20 ranking cutoff.
    "watchlist_size": 999,
    "max_loss_pct_per_trade": 1.0,
    "trailing_stop_enabled": False,
    "sector_above_ema_enabled": True,
    "sector_overextension_enabled": False,
    "monthly_trend_persistence_enabled": False,
    "ha_ema21_touch_enabled": True,
    "ha_ema21_touch_signal_rsi_min": 50.0,
    # 2026-08-23: 1 (was 10) -- explicit request to only check the
    # IMMEDIATE next candle after the signal for the breakout crossing,
    # not wait up to 10 days. See precompute_ema21_touch_signals' PENDING
    # state: days_pending starts at 0 after a same-day-checked commit, so
    # confirm_lookback_days=1 means the very next day is the only one
    # checked before the signal times out back to HUNTING.
    "ha_ema21_touch_confirm_days": 1,
    "ha_ema21_touch_target_rr": 2.0,
    "ha_ema21_touch_breakout_threshold_pct": 0.001,
    # 2026-08-22: True -- multi-candle signal runs use the LOWEST HA low
    # across the whole run as the stop. See NEW_EMA21_TOUCH_LATEST_LOW_CFG
    # below for the explicit alternative (latest candle's own low only),
    # kept as a separate cfg so both can be compared in the same run.
    "ha_ema21_touch_stop_uses_run_low": True,
    # 2026-08-22 addition: the watchlist's real-close rsi_ok gate (60-100)
    # rejects nearly every signal candle on its own day (a pullback, by
    # construction, drags real RSI down -- empirically ~99% of signal
    # candles were watchlist-blocked before this). This substitutes a
    # look-back: at least one of the prior 10 HA candles must have shown
    # HA RSI above 60, proving recent genuine momentum instead of
    # requiring it on the exact pullback day.
    "ha_ema21_touch_prior_rsi_lookback_days": 10,
    "ha_ema21_touch_prior_rsi_min": 60.0,
    # 2026-08-22 addition: signal candle's own real volume must be above
    # its trailing 50-day SMA -- a plain SMA (not EMA), per explicit
    # request, kept separate from ha_ema21_touch_signal_volume_ema_period.
    "ha_ema21_touch_signal_volume_sma_period": 50,
    # 2026-08-23 addition: at least one of the prior 5 HA candles must
    # have had BOTH its open AND close above HA EMA13 -- a second,
    # independent recent-strength check alongside the prior-RSI gate above.
    "ha_ema21_touch_prior_above_ema13_lookback_days": 5,
    # 2026-08-23 addition, baked in as default after validating at both
    # smoke and 5yr scale (see TRIGGERED_DEFAULTS' own comment on this
    # key for the numbers) -- signal candle's real close must be above
    # its real open.
    "ha_ema21_touch_require_real_green": True,
}

# 2026-08-22 explicit request: same as NEW_EMA21_TOUCH_CFG in every way
# except the stop -- uses just the LATEST qualifying signal candle's own
# HA low (ignoring the rest of a multi-candle run), not the run's lowest.
NEW_EMA21_TOUCH_LATEST_LOW_CFG: dict = {
    **NEW_EMA21_TOUCH_CFG,
    "ha_ema21_touch_stop_uses_run_low": False,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--cost-bps", type=float, default=0.0)
    ap.add_argument("--out-suffix", type=str, default="")
    ap.add_argument("--start-date", type=str, default=None,
                    help="YYYY-MM-DD -- use with --end-date for a fixed calendar window")
    ap.add_argument("--end-date", type=str, default=None,
                    help="YYYY-MM-DD, defaults to today if --start-date is given without it")
    ap.add_argument("--only-latest-low", action="store_true",
                    help="skip baseline and the run-low variant -- run only the "
                        "latest-candle-low-stop variant, for faster iteration")
    ap.add_argument("--signal-volume-ema", type=int, default=None,
                    help="overrides ha_ema21_touch_signal_volume_ema_period on the "
                        "latest-low variant -- signal candle's own real volume must be "
                        "above its trailing EMA of real volume over this many days")
    ap.add_argument("--watchlist-size", type=int, default=None,
                    help="overrides watchlist_size on every config run this invocation -- "
                        "how many top-scoring gate-passers are eligible each day (999 "
                        "effectively means all gate-passers, no top-N cap)")
    ap.add_argument("--rsi-min", type=float, default=None,
                    help="overrides the WATCHLIST/gate rsi_min (FILTER_CFG_OVERRIDE's, "
                        "default 60) on every config run this invocation -- NOT the "
                        "same as ha_ema21_touch_signal_rsi_min (the signal candle's own "
                        "RSI floor, separate setting). E.g. --rsi-min 50 tests loosening "
                        "the watchlist gate to match the signal candle's own floor.")
    ap.add_argument("--watchlist-lag-days", type=int, default=None,
                    help="overrides watchlist_lag_days -- validates the live/intraday "
                        "design where the watchlist is built from N trading days ago's "
                        "close (the last fully-known day) instead of today's own close, "
                        "since a live system can't know today's close-based gates until "
                        "after today's market close. 1 = yesterday's close.")
    ap.add_argument("--fill-at-close", action="store_true",
                    help="overrides ha_ema21_touch_fill_at_close -- fills at today's real "
                        "close instead of the exact crossing level, for a once-daily "
                        "after-close live design (no intraday monitoring, no lagged "
                        "watchlist needed since today's own gates are already known by "
                        "the time of the once-daily check).")
    ap.add_argument("--prior-rsi-lookback", type=int, default=None,
                    help="overrides ha_ema21_touch_prior_rsi_lookback_days on the "
                        "latest-low variant -- 0 or a negative value disables the gate")
    ap.add_argument("--prior-rsi-min", type=float, default=None,
                    help="overrides ha_ema21_touch_prior_rsi_min (default 60.0)")
    ap.add_argument("--signal-volume-sma", type=int, default=None,
                    help="overrides ha_ema21_touch_signal_volume_sma_period -- 0 or "
                        "negative disables the gate")
    ap.add_argument("--signal-rsi-min", type=float, default=None,
                    help="overrides ha_ema21_touch_signal_rsi_min (the signal candle's "
                        "OWN HA RSI floor, default 50.0) -- NOT the watchlist gate's "
                        "rsi_min (separate setting, use --rsi-min for that).")
    ap.add_argument("--prior-above-ema13-lookback", type=int, default=None,
                    help="overrides ha_ema21_touch_prior_above_ema13_lookback_days -- "
                        "0 or a negative value disables the gate")
    ap.add_argument("--confirm-days", type=int, default=None,
                    help="overrides ha_ema21_touch_confirm_days (default now 1 -- only "
                        "the immediate next candle after the signal is checked for the "
                        "breakout crossing before it times out back to HUNTING)")
    ap.add_argument("--close-above-ema21", action="store_true",
                    help="overrides ha_ema21_touch_signal_close_above_ema13 -> False -- "
                        "signal candle's close must be above HA EMA21 instead of HA "
                        "EMA13 (the original spec before the 2026-08-23 loosening)")
    ap.add_argument("--max-positions", type=int, default=None,
                    help="overrides max_positions (TRIGGERED_DEFAULTS default is 5) -- "
                        "the hard cap on concurrent open positions")
    ap.add_argument("--sector-rs-formation-only", action="store_true",
                    help="overrides ha_ema21_touch_sector_rs_formation_only -> True -- "
                        "checks relative_strength_vs_sector once at signal formation "
                        "instead of live every day through confirmation")
    ap.add_argument("--sector-above-ema-formation-only", action="store_true",
                    help="overrides ha_ema21_touch_sector_above_ema_formation_only -> "
                        "True -- checks sector_above_ema_ok once at signal formation "
                        "instead of live every day through confirmation")
    ap.add_argument("--require-real-green", action="store_true",
                    help="overrides ha_ema21_touch_require_real_green -> True -- "
                        "signal candle's REAL (non-HA) close must be above its REAL "
                        "open, on top of the HA-based shape check")
    ap.add_argument("--sector-bonus-weight", type=float, default=None,
                    help="overrides the WATCHLIST gate's sector_bonus_weight (default "
                        "1.00 -- adds sector-relative-strength to the ranking score in "
                        "screener.py). 0 disables it.")
    ap.add_argument("--ema50-slope-lookback", type=int, default=None,
                    help="overrides ha_ema21_touch_ema50_slope_lookback_days (None/off "
                        "by default) -- signal candle's real-close EMA50 must have risen "
                        "by --ema50-slope-min-pct over this many trading days. 0 or "
                        "negative disables the gate.")
    ap.add_argument("--ema50-slope-min-pct", type=float, default=None,
                    help="overrides ha_ema21_touch_ema50_slope_min_pct (default 5.0)")
    ap.add_argument("--allow-reversal-wick-shapes", action="store_true",
                    help="overrides ha_ema21_touch_allow_reversal_wick_shapes -> True -- "
                        "widens require_real_green to also accept a red hammer/dragonfly-"
                        "doji (long lower wick) signal candle, not just plain green")
    ap.add_argument("--require-ema13-above-ema21", action="store_true",
                    help="overrides ha_ema21_touch_require_ema13_above_ema21 -> True -- "
                        "HA EMA13 must be above HA EMA21 at the signal candle")
    ap.add_argument("--require-ha-ema-stack", action="store_true",
                    help="overrides ha_ema21_touch_require_ha_ema_stack -> True -- the "
                        "FULL HA EMA13>EMA21>EMA50>EMA200 condition at the signal candle")
    ap.add_argument("--half-target-ema21-tail", action="store_true",
                    help="overrides ha_ema21_touch_half_target_ema21_tail -> True -- "
                        "books half the position at the fixed 1:2 target, lets the rest "
                        "ride until real close < real EMA21 (stop still applies throughout)")
    ap.add_argument("--confirm-on-close", action="store_true",
                    help="overrides ha_ema21_touch_confirm_on_close -> True -- confirmation "
                        "fires when REAL CLOSE closes above the trigger level (filled at "
                        "that close) instead of REAL HIGH crossing it intrabar (filled at "
                        "the exact crossing level). Combine with --confirm-days N to check "
                        "N candles for a close above.")
    ap.add_argument("--prior-above-ema13-all-close", action="store_true",
                    help="overrides ha_ema21_touch_prior_above_ema13_all_close -> True -- "
                        "stricter alternative to the 'any 1 of N' prior_above_ema13 gate: "
                        "ALL N of the prior candles' HA close must be above HA EMA13")
    ap.add_argument("--no-require-real-green", action="store_true",
                    help="overrides ha_ema21_touch_require_real_green -> False -- disables "
                        "the default (since 2026-08-24) real-green-candle requirement")
    ap.add_argument("--prior-tiered-ema-check", action="store_true",
                    help="overrides ha_ema21_touch_prior_tiered_ema_check -> True -- "
                        "immediate prior candle close > EMA21, prior 4 candles before that "
                        "all close > EMA13")
    ap.add_argument("--prior-no-ema50-violation-days", type=int, default=None,
                    help="overrides ha_ema21_touch_prior_no_ema50_violation_days -- none of "
                        "the prior N candles may have closed (HA) below HA EMA50. 0 or "
                        "negative disables the gate.")
    args = ap.parse_args()

    fundamentals_history = None
    if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
        fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]
        print(f"Loaded fundamentals_history ({len(fundamentals_history)} symbols).")
    else:
        print("No cached fundamentals_history.pkl -- fundamental gate will fail OPEN.")

    sector_membership = sector_candles = None
    if os.path.exists(SECTOR_DATA_CACHE):
        _sd = pd.read_pickle(SECTOR_DATA_CACHE)
        sector_membership, sector_candles = _sd["sector_membership"], _sd["sector_candles"]
        print(f"Loaded sector data cached {_sd['run_time']} (static local snapshot).")
    else:
        print("No cached sector_data.pkl -- sector bonus/RS gates will be no-ops.")

    # 2026-08-23: pin end_date to the cache's own last date, not today-1 --
    # otherwise load_long_history_cached retries a live Kite fetch (and
    # fails, since this environment has no valid Kite session) for every
    # one of 202 symbols before falling back to stale cache -- ~0.35s
    # sleep plus a failed network round-trip PER symbol, EVERY single run.
    # Same bug/fix as scripts/count_gate_passers_daily.py and the other
    # ad-hoc scripts written earlier this session -- this script itself
    # was never updated to match, so every backtest run all session has
    # been paying this tax on top of its real compute time.
    _bench_for_cache_probe = _tz_naive(pd.read_csv(
        os.path.join(LONG_CACHE_DIR, "_NIFTY.csv"), index_col=0, parse_dates=True))
    _cache_end_date = _bench_for_cache_probe.index.max().date()
    long_candles = load_long_history_cached(config.UNIVERSE, end_date=_cache_end_date)
    print(f"Loaded long_candles for {len(long_candles)} symbols (deep history).")

    TRIG_WARMUP_DAYS = 780

    # Deep, append-only cache as the main candle/bench source -- see
    # run_triggered_backtest_local.py's own comment on why NOT
    # load_candles_cached(..., offline=True)'s cache/*.csv (destructively
    # overwritten by every live/online fetch elsewhere in the app).
    candles = long_candles
    bench_path = os.path.join(LONG_CACHE_DIR, "_NIFTY.csv")
    bench = _tz_naive(pd.read_csv(bench_path, index_col=0, parse_dates=True))

    if args.start_date:
        requested_start = dt.datetime.strptime(args.start_date, "%Y-%m-%d").date()
        window_end = (dt.datetime.strptime(args.end_date, "%Y-%m-%d").date()
                      if args.end_date else dt.date.today())
        bench = bench.loc[:pd.Timestamp(window_end)]
        candles = {sym: df.loc[:pd.Timestamp(window_end)] for sym, df in candles.items()}
    else:
        requested_start = dt.date.today() - dt.timedelta(days=int(args.years * 365))
        window_end = dt.date.today()

    all_dates = bench.index.sort_values()
    if len(all_dates) <= TRIG_WARMUP_DAYS:
        raise SystemExit(
            f"Only {len(all_dates)} cached trading days available locally, "
            f"need at least {TRIG_WARMUP_DAYS} for the triggered engine's warmup alone.")

    data_floor = all_dates[TRIG_WARMUP_DAYS].date()
    sim_start_date = max(data_floor, requested_start)
    achievable_years = (window_end - sim_start_date).days / 365.25

    print(f"Loaded {len(candles)} symbols; local cache spans "
         f"{all_dates[0].date()} to {all_dates[-1].date()}.")
    if sim_start_date > requested_start:
        print(f"NOTE: requested window back to {requested_start}, but the "
             f"{TRIG_WARMUP_DAYS}-trading-day warmup needs history back to "
             f"{data_floor} -- only {achievable_years:.1f}y is achievable.")
    else:
        print(f"Simulating {achievable_years:.1f}y from {sim_start_date} to {window_end} "
             f"(both strategies, identical window).")

    new_latest_low_cfg = {**FILTER_CFG_OVERRIDE, **NEW_EMA21_TOUCH_LATEST_LOW_CFG}
    if args.signal_volume_ema is not None:
        new_latest_low_cfg["ha_ema21_touch_signal_volume_ema_period"] = args.signal_volume_ema
        print(f"Overriding ha_ema21_touch_signal_volume_ema_period -> "
             f"{args.signal_volume_ema} (from --signal-volume-ema).")
    if args.watchlist_size is not None:
        new_latest_low_cfg["watchlist_size"] = args.watchlist_size
        print(f"Overriding watchlist_size -> {args.watchlist_size} (from --watchlist-size).")
    if args.rsi_min is not None:
        new_latest_low_cfg["rsi_min"] = args.rsi_min
        print(f"Overriding WATCHLIST rsi_min -> {args.rsi_min} (from --rsi-min).")
    if args.signal_rsi_min is not None:
        new_latest_low_cfg["ha_ema21_touch_signal_rsi_min"] = args.signal_rsi_min
        print(f"Overriding SIGNAL CANDLE ha_ema21_touch_signal_rsi_min -> "
             f"{args.signal_rsi_min} (from --signal-rsi-min).")
    if args.prior_above_ema13_lookback is not None:
        new_latest_low_cfg["ha_ema21_touch_prior_above_ema13_lookback_days"] = (
            args.prior_above_ema13_lookback if args.prior_above_ema13_lookback > 0 else None)
        print(f"Overriding ha_ema21_touch_prior_above_ema13_lookback_days -> "
             f"{new_latest_low_cfg['ha_ema21_touch_prior_above_ema13_lookback_days']} "
             f"(from --prior-above-ema13-lookback).")
    if args.confirm_days is not None:
        new_latest_low_cfg["ha_ema21_touch_confirm_days"] = args.confirm_days
        print(f"Overriding ha_ema21_touch_confirm_days -> {args.confirm_days} "
             f"(from --confirm-days).")
    if args.close_above_ema21:
        new_latest_low_cfg["ha_ema21_touch_signal_close_above_ema13"] = False
        print("Overriding ha_ema21_touch_signal_close_above_ema13 -> False "
             "(from --close-above-ema21).")
    if args.max_positions is not None:
        new_latest_low_cfg["max_positions"] = args.max_positions
        print(f"Overriding max_positions -> {args.max_positions} (from --max-positions).")
    if args.sector_rs_formation_only:
        new_latest_low_cfg["ha_ema21_touch_sector_rs_formation_only"] = True
        print("Overriding ha_ema21_touch_sector_rs_formation_only -> True "
             "(from --sector-rs-formation-only).")
    if args.sector_above_ema_formation_only:
        new_latest_low_cfg["ha_ema21_touch_sector_above_ema_formation_only"] = True
        print("Overriding ha_ema21_touch_sector_above_ema_formation_only -> True "
             "(from --sector-above-ema-formation-only).")
    if args.require_real_green:
        new_latest_low_cfg["ha_ema21_touch_require_real_green"] = True
        print("Overriding ha_ema21_touch_require_real_green -> True "
             "(from --require-real-green).")
    if args.watchlist_lag_days is not None:
        new_latest_low_cfg["watchlist_lag_days"] = args.watchlist_lag_days
        print(f"Overriding watchlist_lag_days -> {args.watchlist_lag_days} (from --watchlist-lag-days).")
    if args.fill_at_close:
        new_latest_low_cfg["ha_ema21_touch_fill_at_close"] = True
        print("Overriding ha_ema21_touch_fill_at_close -> True (from --fill-at-close).")
    if args.prior_rsi_lookback is not None:
        new_latest_low_cfg["ha_ema21_touch_prior_rsi_lookback_days"] = (
            args.prior_rsi_lookback if args.prior_rsi_lookback > 0 else None)
        print(f"Overriding ha_ema21_touch_prior_rsi_lookback_days -> "
             f"{new_latest_low_cfg['ha_ema21_touch_prior_rsi_lookback_days']} "
             f"(from --prior-rsi-lookback).")
    if args.prior_rsi_min is not None:
        new_latest_low_cfg["ha_ema21_touch_prior_rsi_min"] = args.prior_rsi_min
        print(f"Overriding ha_ema21_touch_prior_rsi_min -> {args.prior_rsi_min} "
             f"(from --prior-rsi-min).")
    if args.signal_volume_sma is not None:
        new_latest_low_cfg["ha_ema21_touch_signal_volume_sma_period"] = (
            args.signal_volume_sma if args.signal_volume_sma > 0 else None)
        print(f"Overriding ha_ema21_touch_signal_volume_sma_period -> "
             f"{new_latest_low_cfg['ha_ema21_touch_signal_volume_sma_period']} "
             f"(from --signal-volume-sma).")
    if args.sector_bonus_weight is not None:
        new_latest_low_cfg["sector_bonus_weight"] = args.sector_bonus_weight
        print(f"Overriding WATCHLIST sector_bonus_weight -> {args.sector_bonus_weight} "
             f"(from --sector-bonus-weight).")
    if args.ema50_slope_lookback is not None:
        new_latest_low_cfg["ha_ema21_touch_ema50_slope_lookback_days"] = (
            args.ema50_slope_lookback if args.ema50_slope_lookback > 0 else None)
        print(f"Overriding ha_ema21_touch_ema50_slope_lookback_days -> "
             f"{new_latest_low_cfg['ha_ema21_touch_ema50_slope_lookback_days']} "
             f"(from --ema50-slope-lookback).")
    if args.ema50_slope_min_pct is not None:
        new_latest_low_cfg["ha_ema21_touch_ema50_slope_min_pct"] = args.ema50_slope_min_pct
        print(f"Overriding ha_ema21_touch_ema50_slope_min_pct -> "
             f"{args.ema50_slope_min_pct} (from --ema50-slope-min-pct).")
    if args.allow_reversal_wick_shapes:
        new_latest_low_cfg["ha_ema21_touch_allow_reversal_wick_shapes"] = True
        print("Overriding ha_ema21_touch_allow_reversal_wick_shapes -> True "
             "(from --allow-reversal-wick-shapes).")
    if args.require_ema13_above_ema21:
        new_latest_low_cfg["ha_ema21_touch_require_ema13_above_ema21"] = True
        print("Overriding ha_ema21_touch_require_ema13_above_ema21 -> True "
             "(from --require-ema13-above-ema21).")
    if args.require_ha_ema_stack:
        new_latest_low_cfg["ha_ema21_touch_require_ha_ema_stack"] = True
        print("Overriding ha_ema21_touch_require_ha_ema_stack -> True "
             "(from --require-ha-ema-stack).")
    if args.half_target_ema21_tail:
        new_latest_low_cfg["ha_ema21_touch_half_target_ema21_tail"] = True
        print("Overriding ha_ema21_touch_half_target_ema21_tail -> True "
             "(from --half-target-ema21-tail).")
    if args.confirm_on_close:
        new_latest_low_cfg["ha_ema21_touch_confirm_on_close"] = True
        print("Overriding ha_ema21_touch_confirm_on_close -> True "
             "(from --confirm-on-close).")
    if args.prior_above_ema13_all_close:
        new_latest_low_cfg["ha_ema21_touch_prior_above_ema13_all_close"] = True
        print("Overriding ha_ema21_touch_prior_above_ema13_all_close -> True "
             "(from --prior-above-ema13-all-close).")
    if args.no_require_real_green:
        new_latest_low_cfg["ha_ema21_touch_require_real_green"] = False
        print("Overriding ha_ema21_touch_require_real_green -> False "
             "(from --no-require-real-green).")
    if args.prior_tiered_ema_check:
        new_latest_low_cfg["ha_ema21_touch_prior_tiered_ema_check"] = True
        print("Overriding ha_ema21_touch_prior_tiered_ema_check -> True "
             "(from --prior-tiered-ema-check).")
    if args.prior_no_ema50_violation_days is not None:
        new_latest_low_cfg["ha_ema21_touch_prior_no_ema50_violation_days"] = (
            args.prior_no_ema50_violation_days if args.prior_no_ema50_violation_days > 0 else None)
        print(f"Overriding ha_ema21_touch_prior_no_ema50_violation_days -> "
             f"{new_latest_low_cfg['ha_ema21_touch_prior_no_ema50_violation_days']} "
             f"(from --prior-no-ema50-violation-days).")

    baseline = new = None
    if not args.only_latest_low:
        baseline_cfg = {**FILTER_CFG_OVERRIDE, **BASELINE_HA_CFG}
        if args.rsi_min is not None:
            baseline_cfg["rsi_min"] = args.rsi_min
        if args.watchlist_lag_days is not None:
            baseline_cfg["watchlist_lag_days"] = args.watchlist_lag_days
        new_cfg = {**FILTER_CFG_OVERRIDE, **NEW_EMA21_TOUCH_CFG}
        if args.watchlist_size is not None:
            baseline_cfg["watchlist_size"] = args.watchlist_size
            new_cfg["watchlist_size"] = args.watchlist_size
        if args.rsi_min is not None:
            new_cfg["rsi_min"] = args.rsi_min
        if args.watchlist_lag_days is not None:
            new_cfg["watchlist_lag_days"] = args.watchlist_lag_days

        print("\n=== BASELINE: live paper-trade HA-trend strategy (heikin_ashi_enabled) ===")
        baseline = run_triggered_backtest(
            candles, bench, baseline_cfg, initial_capital=args.capital, cost_bps=args.cost_bps,
            warmup_days=TRIG_WARMUP_DAYS, fundamentals_history=fundamentals_history,
            sector_candles=sector_candles, sector_membership=sector_membership,
            long_candles=long_candles, start_date=sim_start_date)
        for k, v in baseline["metrics"].items():
            print(f"{k:24s} {v}")
        print(f"Trigger type breakdown: {baseline['trigger_type_counts']}")

        print("\n=== NEW: EMA21-touch-then-wait-for-breakout, run-low stop (ha_ema21_touch_enabled) ===")
        new = run_triggered_backtest(
            candles, bench, new_cfg, initial_capital=args.capital, cost_bps=args.cost_bps,
            warmup_days=TRIG_WARMUP_DAYS, fundamentals_history=fundamentals_history,
            sector_candles=sector_candles, sector_membership=sector_membership,
            long_candles=long_candles, start_date=sim_start_date)
        for k, v in new["metrics"].items():
            print(f"{k:24s} {v}")
        print(f"Trigger type breakdown: {new['trigger_type_counts']}")

    print("\n=== NEW (latest-candle-low stop variant) ===")
    new_latest_low = run_triggered_backtest(
        candles, bench, new_latest_low_cfg, initial_capital=args.capital, cost_bps=args.cost_bps,
        warmup_days=TRIG_WARMUP_DAYS, fundamentals_history=fundamentals_history,
        sector_candles=sector_candles, sector_membership=sector_membership,
        long_candles=long_candles, start_date=sim_start_date)
    for k, v in new_latest_low["metrics"].items():
        print(f"{k:24s} {v}")
    print(f"Trigger type breakdown: {new_latest_low['trigger_type_counts']}")

    nl_eq = new_latest_low["equity_curve"]
    window_msg = (f"new (latest-low): {nl_eq.index.min().date()} to "
                 f"{nl_eq.index.max().date()} ({len(nl_eq)} days).")
    if baseline is not None:
        b_eq, n_eq = baseline["equity_curve"], new["equity_curve"]
        window_msg = (f"baseline: {b_eq.index.min().date()} to {b_eq.index.max().date()} "
                     f"({len(b_eq)} days); new (run-low): {n_eq.index.min().date()} to "
                     f"{n_eq.index.max().date()} ({len(n_eq)} days); " + window_msg)
    print(f"\nActual simulated window -- {window_msg}")

    suf = args.out_suffix
    out_dir = "result"
    os.makedirs(out_dir, exist_ok=True)
    paths = [os.path.join(out_dir, f"backtest_equity_ema21touch_new_latestlow{suf}.csv"),
            os.path.join(out_dir, f"backtest_trades_ema21touch_new_latestlow{suf}.csv")]
    new_latest_low["equity_curve"].rename("equity").to_csv(paths[0])
    new_latest_low["trades"].to_csv(paths[1], index=False)
    if baseline is not None:
        b_paths = [os.path.join(out_dir, f"backtest_equity_ema21touch_baseline{suf}.csv"),
                  os.path.join(out_dir, f"backtest_trades_ema21touch_baseline{suf}.csv"),
                  os.path.join(out_dir, f"backtest_equity_ema21touch_new{suf}.csv"),
                  os.path.join(out_dir, f"backtest_trades_ema21touch_new{suf}.csv")]
        baseline["equity_curve"].rename("equity").to_csv(b_paths[0])
        baseline["trades"].to_csv(b_paths[1], index=False)
        new["equity_curve"].rename("equity").to_csv(b_paths[2])
        new["trades"].to_csv(b_paths[3], index=False)
        paths += b_paths
    print(f"\nSaved: {', '.join(paths)}")


if __name__ == "__main__":
    main()
