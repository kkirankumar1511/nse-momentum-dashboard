"""
Local-only comparison runner: production backtest.run_backtest() vs. the
experimental backtest_triggered.run_triggered_backtest(), over the same
cached universe/period. Not wired into dashboard.py, not deployed to the
VPS -- pure local research script, per "keep it in local for testing."

Uses load_candles_cached(..., offline=True) so it works without a live
Kite session (that only lives wherever the daily OAuth login flow runs,
i.e. the VPS) -- reads whatever's already cached under cache/*.csv.

Run with:  python scripts/run_triggered_backtest_local.py --years 5
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
from backtest import load_candles_cached, load_long_history_cached, run_backtest
from backtest_triggered import run_triggered_backtest

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
# Ad-hoc local snapshot (sector_membership + sector_candles), not written by
# any current code path in this repo -- dashboard.py always fetches sector
# data live via sector_universe.sector_membership_and_candles(), which needs
# a real Kite session this machine doesn't have. This file predates the
# current codebase state; loaded directly here as the only local source of
# sector data, with its own run_time printed so the staleness is visible
# rather than silently assumed fresh.
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")

# Filter/gate config as configured on the Backtest page (screenshot supplied
# 2026-08-11): RSI band widened, weekly/monthly EMA confirmation gate on,
# fundamental gate on, sector bonus weight + composite sector score on,
# plain (non-equal-weight) sizing for the production comparison run.
# Applied to BOTH engines below -- "all stock filter strategy should be
# same as per current config" (the original request's point 1) applies
# just as much to this explicit filter set as it did to config.STRATEGY's
# own defaults.
#
# sector_diversification_enabled removed (2026-08-11, real trade evidence):
# traced a specific ASIANPAINT trade that only made the watchlist because
# the cap excluded 7 higher-scoring Healthcare names once that sector hit
# its 3-stock quota -- ASIANPAINT ranked #15/18 by raw score, got a slot
# purely via the Consumer-sector quota, then failed the very next
# rebalance's keep_zone check (which is NOT sector-cap-adjusted, see
# backtest_triggered.py/backtest.py's identical keep_zone construction) --
# a near-mechanical 1-day round trip unrelated to the stock's own
# strength. sector_bonus_weight/sector_composite_score_enabled stay ON --
# those only TILT scoring, they don't force weak names onto the watchlist
# the way the hard per-sector cap did.
FILTER_CFG_OVERRIDE: dict = {
    "rsi_min": 60,
    "rsi_max": 100,  # RSI is bounded [0,100] -- 100 means no effective upper cap
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

# Step-by-step trigger isolation (2026-08-12): only the 21 EMA pullback
# trigger enabled, everything else off, to test it in isolation before
# layering triggers back on one at a time. Edit this dict directly as you
# step through each pattern -- only applied to the triggered engine (these
# keys don't exist in production's config at all).
TRIGGER_ISOLATION_OVERRIDE: dict = {
    "pullback_ema_fast": 21,
    "multiyear_breakout_enabled": False,
    "shortterm_breakout_enabled": False,
    "pullback_slow_ema_enabled": False,
    "pullback_fast_ema_enabled": True,
    # 2026-08-12: switched from "swing_high" after that mode showed a real
    # win/loss size asymmetry over the first 3mo test (26 targets averaging
    # +2.73%, 12 of them 1-day hits for <1.4% -- the swing high in an 8-day
    # drift window is often barely above entry; 22 stops averaging -3.77%,
    # a full risk-distance away) -- profit factor landed at 0.97 despite a
    # 54% win rate. risk_reward forces every winner to be >= the multiple
    # below x the stop distance by construction, testing whether that fixes
    # the asymmetry.
    "profit_target_mode": "risk_reward",
    "profit_target_rr": 2.0,
}

# 2026-08-12: "EMA 21 Institutional Retracement Swing", simplified same
# day per explicit follow-up (the fuller engulfing/inside-bar/hammer +
# multi-day pullback-structure + quality-score version turned out to
# reject nearly everything -- 1 trade in 3 months across the whole
# universe): sector-based market filter, EMA21>EMA50>EMA200 stack, hammer-
# ONLY entry (candle whose low dips below EMA21 and closes back above it,
# confirmed the next day by closing above that candle's high), stop = the
# hammer candle's own low, target = 1:3 risk-reward, NO quality-score
# gate, top 20 (not the full universe) from the gate-passing list, risking
# 1% of total capital per trade.
INSTITUTIONAL_OVERRIDE: dict = {
    "max_loss_pct_per_trade": 1.0,
    "watchlist_size": 20,
}

# 2026-08-13: "Heikin-Ashi trend entry" -- a full strategy replacement per
# explicit request ("remove all the strategy which we have implemented..
# for entry, stop loss and exit criteria"). Only RS-vs-sector survives
# from everything above; the sector market filter, Minervini trend
# template, and all 4 older triggers (breakouts, both pullback types) are
# explicitly disabled -- see trigger_strategy.detect_trigger's docstring,
# heikin_ashi_enabled is checked first and is a fully independent path.
# Top 50 gate-passers (not full universe, not top 10/20), 1:2 risk-reward
# target, no trailing stop (pure stop+target per the spec's own points
# 8-9, nothing about trailing was requested).
HEIKIN_ASHI_OVERRIDE: dict = {
    "heikin_ashi_enabled": True,
    "multiyear_breakout_enabled": False,
    "shortterm_breakout_enabled": False,
    "pullback_slow_ema_enabled": False,
    "pullback_fast_ema_enabled": False,
    # 2026-08-16: reverted to top-20 -- of everything tested this session
    # (all-gate-passers, various sector/monthly-trend gates, Fibonacci
    # stop/target, breakeven-then-wide-trail), the best-performing config
    # remains ATR*1.0 + top-20 watchlist + sector_above_ema_enabled alone:
    # CAGR 32.69%, alpha +22.35%, Sharpe 1.94 (2021-2026, rsi60-100).
    "watchlist_size": 20,
    "profit_target_rr": 2.0,
    "max_loss_pct_per_trade": 1.0,
    # 2026-08-13: ATR-based stop instead of the spec's original "signal
    # candle's HA low" -- baseline (ha_low, no trailing) was CAGR -11.11%,
    # Sharpe -0.95, 25% win rate, PF 0.58 over the 3mo window; fixed
    # (non-trailing) ATR*1.0/ATR*2.0 brackets over a real ~3yr sample
    # gave +6.27%/+0.90% alpha respectively (585/304 trades).
    "ha_stop_mode": "atr",
    "ha_stop_atr_multiple": 1.0,
    # 2026-08-14: now DAILY-TRAILING per explicit request -- "trail every
    # day atr*N for atr*N initial stop", i.e. the SAME multiple used for
    # both the initial stop and the ongoing chandelier trail (highest
    # close since entry - multiple*ATR, ratcheted up only, recomputed
    # daily -- this reuses the exact same generic trailing-stop block
    # every other trigger type already has, backtest_triggered.py's step
    # 1b; HA positions aren't added to institutional_positions so they
    # were never eligible for the OLDER EMA21/3-day-low trail, only this
    # generic ATR-chandelier one). trailing_atr_multiple is kept in sync
    # with ha_stop_atr_multiple below in main(), including when
    # --ha-atr-multiple overrides it -- so ATR*2 always means "initial
    # AND trailing stop both at 2x ATR", never a mismatched pair.
    "trailing_stop_enabled": True,
    "trailing_atr_multiple": 1.0,
    # 2026-08-14: pure trail-only per explicit request -- caught a real
    # NATIONALUM trade (2025-10-15 to 2025-10-24) that exited at a fixed
    # ATR*2 target instead of continuing to trail. No profit cap now --
    # positions ride until the daily trailing stop eventually catches them.
    "ha_target_enabled": False,
    # 2026-08-14: narrowed from the 10-day TRIGGERED_DEFAULTS default per
    # explicit request -- part of the best-performing config.
    "ha_signal_lookback_days": 2,
    # 2026-08-15: monthly trend-persistence gate, per explicit request
    # after reviewing a real trade -- "don't want a volatile, choppy stock
    # like HINDZINC, want a persistently-trending stock like BSE." Verified
    # on real data: BSE passes (0 monthly-50EMA whipsaws in 5y, 11 fresh
    # 12m highs in the last 24 months), HINDZINC fails (4, right at the
    # universe median) -- see trigger_indicators.monthly_trend_persistence_
    # ok's docstring.
    # 2026-08-15: disabled -- combined with the two sector gates below,
    # all three together collapsed a 29.28% CAGR / +18.93% alpha run down
    # to 3.28% CAGR / -7.06% alpha (real result, rsi60-100 2021-2026).
    # This gate alone already cut alpha to near-zero before the sector
    # gates were even added -- isolating the sector gates without this one
    # to see if they hold up on their own.
    "monthly_trend_persistence_enabled": False,
    # 2026-08-16: kept ON -- confirmed the single best-performing addition
    # tested this session, isolated: CAGR 29.28%->32.69%, alpha +18.93%->
    # +22.35%, Sharpe 1.89->1.94 (2021-2026, top20, rsi60-100). Every other
    # gate/mechanism tried (monthly-trend persistence, sector
    # overextension, Fibonacci stop/target, breakeven-then-wide-trail)
    # made things worse and stays off.
    "sector_above_ema_enabled": True,
    # 2026-08-15: sector overextension filter -- the one that actually
    # explains NIFTY IT/REALTY's high loss rates (excludes 21/28 of the
    # traced losing-heavy trades, net -8.34% of return removed from that
    # subset vs -3.97% remaining -- see trigger_indicators.
    # sector_not_overextended_ok's docstring).
    # 2026-08-15: disabled -- isolated test confirmed this gate alone
    # (29.28%->17.66% CAGR) was the main driver of the earlier combined-
    # gates degradation, not sector_above_ema (which alone IMPROVED
    # results, 29.28%->32.69%). Reverting to off for the Fibonacci-mode
    # test below, which should run against the clean no-sector-gates
    # baseline.
    "sector_overextension_enabled": False,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--cost-bps", type=float, default=0.0)
    ap.add_argument("--engine", choices=["both", "triggered", "production"], default="both",
                    help="skip the other engine for faster iteration while tuning "
                        "trigger_strategy.py -- production is unchanged run to run, "
                        "so re-running it every time you tweak a trigger is wasted time")
    ap.add_argument("--ha-atr-multiple", type=float, default=None,
                    help="overrides HEIKIN_ASHI_OVERRIDE's ha_stop_atr_multiple -- lets "
                        "multiple values run as separate parallel processes without "
                        "editing the file between runs")
    ap.add_argument("--ha-target", choices=["on", "off"], default=None,
                    help="overrides ha_target_enabled -- 'on' keeps a fixed/symmetric "
                        "profit target alongside the stop, 'off' is pure trail-only "
                        "(no profit cap, ride until the trailing stop catches it)")
    ap.add_argument("--ha-signal-lookback", type=int, default=None,
                    help="overrides ha_signal_lookback_days -- how many trading days "
                        "back the signal search looks for the nearest red HA candle "
                        "before giving up with no signal")
    ap.add_argument("--rsi-min", type=float, default=None,
                    help="overrides FILTER_CFG_OVERRIDE's rsi_min gate threshold")
    ap.add_argument("--rsi-max", type=float, default=None,
                    help="overrides FILTER_CFG_OVERRIDE's rsi_max gate threshold "
                        "(100 effectively removes the upper cap, RSI is bounded [0,100])")
    ap.add_argument("--ha-stop-mode", choices=["ha_low", "atr", "fibonacci"], default=None,
                    help="overrides ha_stop_mode -- 'ha_low' (signal candle's HA low), "
                        "'atr' (entry - multiple*ATR), or 'fibonacci' (swing retracement "
                        "stop / extension target, see trigger_indicators.find_swing_for_fib)")
    ap.add_argument("--strategy", choices=["ha_trend", "ha_ema21_bounce"], default=None,
                    help="switches which HA entry pattern is active -- 'ha_trend' (default, "
                        "red-signal-candle-then-breakout) or 'ha_ema21_bounce' (new, same-day "
                        "EMA21-touch-and-bounce, see trigger_indicators."
                        "heikin_ashi_ema21_bounce_entry). Mutually exclusive -- turns the "
                        "other one off.")
    ap.add_argument("--watchlist-size", type=int, default=None,
                    help="overrides watchlist_size -- how many top-scoring gate-passers are "
                        "eligible for a trigger each day (999 effectively means all gate-passers)")
    ap.add_argument("--trailing-stop", choices=["on", "off"], default=None,
                    help="overrides trailing_stop_enabled -- 'off' keeps the stop FIXED at "
                        "whatever the entry trigger set it to (e.g. ha_ema21_bounce's entry-"
                        "candle HA low) for the life of the position, no daily ratchet")
    ap.add_argument("--ema21-bounce-rsi-min", type=float, default=None,
                    help="overrides ha_ema21_bounce_rsi_min (default 58.0)")
    ap.add_argument("--breakeven-trail", choices=["on", "off"], default=None,
                    help="overrides ha_breakeven_trail_enabled -- 'on' keeps the initial ATR "
                        "stop fixed until price reaches ha_breakeven_trigger_r (1:1 default) "
                        "then jumps to breakeven and trails at ha_breakeven_trail_atr_multiple "
                        "(2.0 default) from there")
    ap.add_argument("--sector-above-ema", choices=["on", "off"], default=None,
                    help="overrides sector_above_ema_enabled")
    ap.add_argument("--max-positions", type=int, default=None,
                    help="overrides max_positions -- how many concurrent open positions "
                        "the portfolio can hold (default 5)")
    ap.add_argument("--sector-diversification", choices=["on", "off"], default=None,
                    help="overrides sector_diversification_enabled -- restricts the watchlist "
                        "to stocks whose top sector's GROUP is among the top_n_sectors "
                        "strongest that day (by composite score, since "
                        "sector_composite_score_enabled is on), instead of a stock-count cap")
    ap.add_argument("--top-n-sectors", type=int, default=None,
                    help="overrides top_n_sectors -- how many strongest sector groups are "
                        "eligible when --sector-diversification on (default 3)")
    ap.add_argument("--out-suffix", type=str, default="",
                    help="appended to the saved CSV filenames (e.g. '_atr1') so parallel "
                        "runs with different params don't overwrite each other's output")
    ap.add_argument("--start-date", type=str, default=None,
                    help="YYYY-MM-DD -- use with --end-date for a fixed calendar window "
                        "(e.g. a single year) instead of trailing --years back from today")
    ap.add_argument("--end-date", type=str, default=None,
                    help="YYYY-MM-DD, defaults to today if --start-date is given without it")
    args = ap.parse_args()
    run_prod = args.engine in ("both", "production")
    run_trig = args.engine in ("both", "triggered")

    fundamentals_history = None
    if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
        fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]
        print(f"Loaded fundamentals_history ({len(fundamentals_history)} symbols).")
    else:
        print("No cached fundamentals_history.pkl -- fundamental gate will "
             "fail OPEN (every score treated as unknown) per apply_gates' "
             "documented behavior, not filter anything.")

    sector_membership = sector_candles = None
    if os.path.exists(SECTOR_DATA_CACHE):
        _sd = pd.read_pickle(SECTOR_DATA_CACHE)
        sector_membership, sector_candles = _sd["sector_membership"], _sd["sector_candles"]
        print(f"Loaded sector data cached {_sd['run_time']} (static local "
             f"snapshot, not live -- offline run).")
    else:
        print("No cached sector_data.pkl -- sector bonus/diversification/"
             "composite score will all be no-ops regardless of cfg flags.")

    # end_date pinned to the newly-synced cache's own known-fresh date
    # (scp'd from the VPS, which has a real Kite session) rather than
    # dt.date.today() -- avoids 202 doomed live-fetch attempts (this
    # machine has no valid Kite access_token) on every run; the function
    # already falls back to stale cache on a failed fetch, this just skips
    # paying for the failure every time.
    long_candles = load_long_history_cached(
        config.UNIVERSE, end_date=dt.date.today() - dt.timedelta(days=1))
    print(f"Loaded long_candles for {len(long_candles)} symbols "
         f"(deep history, for the weekly/monthly gate).")

    # Must match run_triggered_backtest's own warmup_days default -- passed
    # explicitly below (not relying on the function default) so this
    # script's floor computation can never silently drift out of sync with
    # the engine's actual behavior.
    TRIG_WARMUP_DAYS = 780

    # Always request the deep-history depth (matches load_long_history_
    # cached's own convention) -- offline mode has no network cost to
    # over-requesting, load_candles_cached just returns whatever calendar-
    # day window is actually on disk, capped by this. Using a fixed large
    # number here (rather than deriving it from --years) means the actual
    # available window is whatever the local cache holds, and the fairness
    # logic below decides the real simulated start date from that -- not
    # from a guessed calendar-to-trading-day conversion, which is exactly
    # what silently broke the first version of this script (an "extra 800
    # calendar days" buffer undershot the 780 TRADING-day warmup need,
    # quietly truncating the triggered engine's simulated window to ~1
    # year while production ran the full requested window in the same
    # invocation -- caught by comparing the two saved equity curves'
    # actual index ranges, not assumed correct).
    days = 6100
    candles, bench = load_candles_cached(config.UNIVERSE, days, offline=True)

    # Fixed calendar window (e.g. "only 2025") truncates candles/bench to
    # end_date BEFORE the engines ever see them -- neither run_backtest
    # nor run_triggered_backtest takes an end_date of their own (only a
    # start_date), so without this truncation the simulation would run
    # straight through to today regardless of --end-date, and any
    # still-open position at the requested cutoff would carry mark-to-
    # market values from AFTER the window you asked to isolate.
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
            f"need at least {TRIG_WARMUP_DAYS} for the triggered engine's "
            f"warmup alone -- fetch more history first.")

    # The triggered engine needs TRIG_WARMUP_DAYS trading days of history
    # before its first tradeable day -- a hard floor on how early a FAIR
    # (identical-window) comparison can start, independent of --years.
    data_floor = all_dates[TRIG_WARMUP_DAYS].date()
    sim_start_date = max(data_floor, requested_start)
    achievable_years = (window_end - sim_start_date).days / 365.25

    print(f"Loaded {len(candles)} symbols (offline/cached mode); local cache "
         f"spans {all_dates[0].date()} to {all_dates[-1].date()}.")
    if sim_start_date > requested_start:
        print(f"NOTE: requested window back to {requested_start}, but the "
             f"triggered engine's {TRIG_WARMUP_DAYS}-trading-day warmup needs "
             f"history back to {data_floor} -- only {achievable_years:.1f}y is "
             f"achievable with the cached history on disk. Both engines below "
             f"run on the SAME window ({sim_start_date} to {window_end}) so the "
             f"comparison stays fair, just shorter than requested.")
    else:
        print(f"Simulating {achievable_years:.1f}y from {sim_start_date} to "
             f"{window_end} (both engines, identical window).")

    prod_cfg = {**config.STRATEGY, **FILTER_CFG_OVERRIDE}
    trig_cfg = {**FILTER_CFG_OVERRIDE, **HEIKIN_ASHI_OVERRIDE}  # layers onto TRIGGERED_DEFAULTS inside the engine
    if args.ha_atr_multiple is not None:
        # Kept in sync deliberately -- "trail every day atr*N for atr*N
        # initial stop" means the SAME multiple drives both, never two
        # different numbers by accident.
        trig_cfg["ha_stop_atr_multiple"] = args.ha_atr_multiple
        trig_cfg["trailing_atr_multiple"] = args.ha_atr_multiple
        print(f"Overriding ha_stop_atr_multiple AND trailing_atr_multiple "
             f"-> {args.ha_atr_multiple} (from --ha-atr-multiple).")
    if args.ha_target is not None:
        trig_cfg["ha_target_enabled"] = (args.ha_target == "on")
        print(f"Overriding ha_target_enabled -> {trig_cfg['ha_target_enabled']} "
             f"(from --ha-target).")
    if args.ha_signal_lookback is not None:
        trig_cfg["ha_signal_lookback_days"] = args.ha_signal_lookback
        print(f"Overriding ha_signal_lookback_days -> {args.ha_signal_lookback} "
             f"(from --ha-signal-lookback).")
    if args.rsi_min is not None:
        trig_cfg["rsi_min"] = args.rsi_min
        print(f"Overriding rsi_min -> {args.rsi_min} (from --rsi-min).")
    if args.rsi_max is not None:
        trig_cfg["rsi_max"] = args.rsi_max
        print(f"Overriding rsi_max -> {args.rsi_max} (from --rsi-max).")
    if args.ha_stop_mode is not None:
        trig_cfg["ha_stop_mode"] = args.ha_stop_mode
        print(f"Overriding ha_stop_mode -> {args.ha_stop_mode} (from --ha-stop-mode).")
    if args.strategy is not None:
        trig_cfg["heikin_ashi_enabled"] = (args.strategy == "ha_trend")
        trig_cfg["ha_ema21_bounce_enabled"] = (args.strategy == "ha_ema21_bounce")
        print(f"Overriding strategy -> {args.strategy} (from --strategy).")
    if args.watchlist_size is not None:
        trig_cfg["watchlist_size"] = args.watchlist_size
        print(f"Overriding watchlist_size -> {args.watchlist_size} (from --watchlist-size).")
    if args.trailing_stop is not None:
        trig_cfg["trailing_stop_enabled"] = (args.trailing_stop == "on")
        print(f"Overriding trailing_stop_enabled -> {trig_cfg['trailing_stop_enabled']} "
             f"(from --trailing-stop).")
    if args.ema21_bounce_rsi_min is not None:
        trig_cfg["ha_ema21_bounce_rsi_min"] = args.ema21_bounce_rsi_min
        print(f"Overriding ha_ema21_bounce_rsi_min -> {args.ema21_bounce_rsi_min} "
             f"(from --ema21-bounce-rsi-min).")
    if args.breakeven_trail is not None:
        trig_cfg["ha_breakeven_trail_enabled"] = (args.breakeven_trail == "on")
        print(f"Overriding ha_breakeven_trail_enabled -> {trig_cfg['ha_breakeven_trail_enabled']} "
             f"(from --breakeven-trail).")
    if args.sector_above_ema is not None:
        trig_cfg["sector_above_ema_enabled"] = (args.sector_above_ema == "on")
        print(f"Overriding sector_above_ema_enabled -> {trig_cfg['sector_above_ema_enabled']} "
             f"(from --sector-above-ema).")
    if args.max_positions is not None:
        trig_cfg["max_positions"] = args.max_positions
        print(f"Overriding max_positions -> {args.max_positions} (from --max-positions).")
    if args.sector_diversification is not None:
        trig_cfg["sector_diversification_enabled"] = (args.sector_diversification == "on")
        print(f"Overriding sector_diversification_enabled -> "
             f"{trig_cfg['sector_diversification_enabled']} (from --sector-diversification).")
    if args.top_n_sectors is not None:
        trig_cfg["top_n_sectors"] = args.top_n_sectors
        print(f"Overriding top_n_sectors -> {args.top_n_sectors} (from --top-n-sectors).")

    prod = trig = None
    if run_prod:
        print("\n=== Production run_backtest() ===")
        prod = run_backtest(candles, bench, prod_cfg,
                            initial_capital=args.capital, cost_bps=args.cost_bps,
                            fundamentals_history=fundamentals_history,
                            sector_candles=sector_candles, sector_membership=sector_membership,
                            long_candles=long_candles,
                            start_date=sim_start_date)
        for k, v in prod["metrics"].items():
            print(f"{k:24s} {v}")

    if run_trig:
        print("\n=== Experimental run_triggered_backtest() ===")
        trig = run_triggered_backtest(candles, bench, trig_cfg,
                                      initial_capital=args.capital, cost_bps=args.cost_bps,
                                      warmup_days=TRIG_WARMUP_DAYS,
                                      fundamentals_history=fundamentals_history,
                                      sector_candles=sector_candles, sector_membership=sector_membership,
                                      long_candles=long_candles,
                                      start_date=sim_start_date)
        for k, v in trig["metrics"].items():
            print(f"{k:24s} {v}")
        print(f"\nTrigger type breakdown: {trig['trigger_type_counts']}")

    # Verify the fairness claim rather than assume it -- print each
    # engine's ACTUAL realized equity-curve date range so a mismatch (like
    # the one this script originally had) is immediately visible, not
    # silently wrong again in some other way. Only meaningful with both.
    if prod is not None and trig is not None:
        p_eq, t_eq = prod["equity_curve"], trig["equity_curve"]
        print(f"\nActual simulated window -- production: {p_eq.index.min().date()} "
             f"to {p_eq.index.max().date()} ({len(p_eq)} days); triggered: "
             f"{t_eq.index.min().date()} to {t_eq.index.max().date()} ({len(t_eq)} days).")

    suf = args.out_suffix
    saved = []
    if prod is not None:
        prod["equity_curve"].rename("equity").to_csv(f"backtest_equity_production{suf}.csv")
        prod["trades"].to_csv(f"backtest_trades_production{suf}.csv", index=False)
        saved += [f"backtest_equity_production{suf}.csv", f"backtest_trades_production{suf}.csv"]
    if trig is not None:
        trig["equity_curve"].rename("equity").to_csv(f"backtest_equity_triggered{suf}.csv")
        trig["trades"].to_csv(f"backtest_trades_triggered{suf}.csv", index=False)
        saved += [f"backtest_equity_triggered{suf}.csv", f"backtest_trades_triggered{suf}.csv"]
    print(f"\nSaved: {', '.join(saved)}")


if __name__ == "__main__":
    main()
