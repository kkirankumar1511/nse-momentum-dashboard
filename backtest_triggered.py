"""
Experimental trigger-based entry backtest engine.

Replaces the production engine's (backtest.py) "rank top-N, buy all via
equal weight at the same close" entry with: filter/rank the universe
exactly as today (SAME gates/config, reused unchanged), then wait for a
concrete technical trigger (breakout-and-hold or EMA pullback-bounce, each
with volume confirmation -- see trigger_strategy.py) before entering each
name. Sizing/stops are ATR-risk based instead of pure equal weight (see
trigger_strategy.trigger_position_size).

Universe ranking (rank_universe_asof) and sector-diversification cap
(_apply_sector_cap) are reused UNCHANGED from backtest.py, by import, so
the WATCHLIST is built off the same filter pipeline as production. Exits
are NOT shared with production, by design: this engine only exits via the
daily ATR stop-loss + chandelier trailing-stop mechanism (steps 1/1b below,
same logic as backtest.run_backtest's own, just mirrored here since it
isn't factored into a standalone function) -- production's rebalance-day
200-EMA/rank exit (screener.sell_check) is deliberately NOT run here, since
that check belongs to production's rotation strategy (drop whatever's no
longer top-ranked) and is inconsistent with a strategy where every position
already has its own defined risk via a real stop. compute_metrics is
reused unchanged for the equity-curve stats.

Local-only, experimental: NOT wired into dashboard.py, NOT deployed to the
VPS. See scripts/run_triggered_backtest_local.py for a runnable comparison
against the production engine.

Known simplification vs. the production engine (acceptable for a local
research tool, called out explicitly rather than silently differing): the
market regime filter (cfg["regime_filter_enabled"]) is NOT wired in here --
cfg["max_positions"] is used directly, not an effective_max_positions. If a
caller's cfg has it enabled, it is silently ignored by this engine.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import pandas as pd

import indicators
import resistance_zones
import trigger_indicators as ti
from backtest import (Position, Trade, _apply_sector_cap, compute_metrics,
                      rank_universe_asof)

import trigger_strategy as ts


def _rebalance_dates(dates: pd.DatetimeIndex, rebalance: str) -> set:
    """Identical logic to backtest.run_backtest's inline rb_dates block."""
    if rebalance == "D":
        return set(dates)
    if rebalance == "W":
        weekday_dates = dates[dates.dayofweek < 5]
        iso = weekday_dates.isocalendar()
        return set(pd.Series(weekday_dates).groupby(
            [iso["year"].values, iso["week"].values]).max())
    # "MS": first trading day of each month
    return set(pd.Series(dates).groupby([dates.year, dates.month]).min())


def run_triggered_backtest(candles: dict, bench: pd.DataFrame,
                           cfg: dict | None = None,
                           initial_capital: float = 1_000_000,
                           cost_bps: float = 0.0,
                           rebalance: str = "D",
                           warmup_days: int = 780,
                           verbose: bool = False,
                           fundamentals_history: dict | None = None,
                           sector_candles: dict | None = None,
                           sector_membership: dict | None = None,
                           long_candles: dict | None = None,
                           start_date: dt.date | None = None,
                           progress_cb=None,
                           precomputed_pivots: dict | None = None,
                           debug_log: list | None = None) -> dict:
    """See module docstring. cfg is layered on top of
    trigger_strategy.TRIGGERED_DEFAULTS (itself a full copy of
    config.STRATEGY plus the new trigger/sizing/stop keys), so passing
    None reproduces TRIGGERED_DEFAULTS exactly.

    warmup_days defaults to 780 (vs. production's 260) so the ~3-year
    multi-year-breakout window (TRIGGERED_DEFAULTS["multiyear_lookback_
    days"] = 756) has enough history before the first tradeable day.

    2026-08-23 diagnostic-only addition: if `debug_log` (a list) is
    given, every day a symbol has confirmed_entry=True in precomputed_
    ema21_touch gets a {"date", "symbol", "reason"} dict appended,
    classifying exactly why it did or didn't become a real trade
    (entered / already_held / not_on_watchlist_gates_failed /
    sector_cap_excluded / outside_watchlist_size_cutoff /
    max_positions_full / detect_trigger_returned_none /
    insufficient_cash_or_sizing) -- built to answer "why are there so
    few trades" precisely instead of guessing. Zero cost and zero
    behavior change when None (the default) -- purely additive logging,
    no control-flow branches depend on it.
    """
    cfg = {**ts.TRIGGERED_DEFAULTS, **(cfg or {})}
    cost = cost_bps / 10_000
    score_cache: dict = {}

    n_syms = len(candles)
    precomputed: dict = {}
    for i, (sym, df) in enumerate(candles.items()):
        if progress_cb and (i % max(1, n_syms // 20) == 0 or i == n_syms - 1):
            progress_cb(f"Precomputing indicators ({i + 1}/{n_syms})...",
                       (i + 1) / n_syms * 0.1)
        if not df.empty and len(df) >= cfg["ema_slow"]:
            precomputed[sym] = indicators.precompute_daily_series(df, cfg)

    if precomputed_pivots is None and long_candles is not None \
            and cfg.get("resistance_zone_weight", 0.0):
        precomputed_pivots = {}
        window = cfg.get("resistance_zone_pivot_window", 10)
        for sym, df in long_candles.items():
            if not df.empty:
                precomputed_pivots[sym] = resistance_zones.precompute_pivots(df, window=window)

    # 2026-08-25 addition: one-time, full-history precompute of confirmed
    # swing lows per symbol -- only for ha_stop_mode="swing_ema50"'s daily
    # trailing-stop ratchet (see step 1b below). Separate from
    # precomputed_pivots above (that one's highs+lows-combined, built for
    # the unrelated resistance-zone score) -- see resistance_zones.
    # precompute_swing_lows' own docstring for why it's a standalone
    # function rather than reusing/filtering precompute_pivots.
    swing_lows_by_symbol: dict = {}
    if long_candles is not None and cfg.get("ha_stop_mode") == "swing_ema50":
        swing_window = cfg.get("swing_low_window", 10)
        for sym, df in long_candles.items():
            if not df.empty:
                swing_lows_by_symbol[sym] = resistance_zones.precompute_swing_lows(df, window=swing_window)

    # One-time precompute of the weekly/monthly confirmation gate's
    # resampled bar history -- identical to backtest.run_backtest's own
    # block. Without this, rank_universe_asof() re-resamples the full
    # long_candles history for every symbol on every single rebalance day,
    # profiled elsewhere as ~45% of a gate-enabled backtest's runtime --
    # with rebalance="D" (the default here) that's O(days x symbols) full
    # 16-year resamples, not O(symbols) once.
    precomputed_weekly_monthly: dict = {}
    # 2026-08-25 addition: the actual fast path used below now -- resample-
    # free, verified byte-identical to the line above's fast path across
    # ~9,200 sampled values (see indicators.precompute_weekly_monthly_
    # trend_ok's docstring). This is what actually matters here, since
    # rebalance="D" means this gate was being hit on every trading day.
    precomputed_weekly_monthly_ok: dict = {}
    if long_candles is not None and cfg.get("weekly_monthly_gate_enabled", False):
        for sym, df in long_candles.items():
            if not df.empty:
                precomputed_weekly_monthly[sym] = indicators.precompute_weekly_monthly_bars(df["close"])
                precomputed_weekly_monthly_ok[sym] = indicators.precompute_weekly_monthly_trend_ok(df, cfg)

    # One-time precompute of each symbol's FULL-history Heikin-Ashi series
    # -- required (not just an optimization) for the Heikin-Ashi trigger,
    # since HA_open's recursive definition means a truncated window
    # computes a genuinely different value, not an approximation (see
    # trigger_indicators.precompute_heikin_ashi's docstring).
    precomputed_ha: dict = {}
    if (cfg.get("heikin_ashi_enabled", False) or cfg.get("ha_ema21_bounce_enabled", False)
            or cfg.get("ha_ema21_touch_enabled", False)):
        for sym, df in candles.items():
            if not df.empty:
                precomputed_ha[sym] = ti.precompute_heikin_ashi(df)

    # Moved up (was after the ema21-touch precompute block below) --
    # the new watchlist-membership pre-pass needs rb_dates before it can
    # run, since it walks the SAME rebalance days the day loop itself
    # will later use.
    dates = bench.index.sort_values()
    dates = dates[warmup_days:]
    if start_date is not None:
        dates = dates[dates >= pd.Timestamp(start_date)]
    rb_dates = _rebalance_dates(dates, rebalance)

    # 2026-08-22: one-time pre-pass computing, for every rebalance day,
    # which symbols were "shortlisted" that day -- gate-passing AND
    # ranked in the top watchlist_size by score. Deliberately NOT the
    # exact same set backtest_triggered's own day loop watchlist ends up
    # with (that one also excludes already-held positions and applies
    # _apply_sector_cap, both position-state-dependent, not a reflection
    # of the stock's own merit) -- this is a pure, position-independent
    # "was this stock genuinely one of the day's strongest" signal,
    # explicit request: gate SIGNAL CANDLE FORMATION to only start
    # tracking a pattern in a stock that was actually shortlisted the day
    # the candle formed. Costs one extra full rank_universe_asof pass
    # over the whole backtest window (only paid when ha_ema21_touch_
    # enabled) -- the day loop's own rebalance step still does its own,
    # separate, position-aware ranking as before.
    # 2026-08-23: also keep the full ranked DataFrame per date (not just
    # the derived membership booleans) -- the day loop's own step 2a below
    # used to call rank_universe_asof AGAIN for the exact same date
    # whenever watchlist_lag_days==0 (the default), silently redoing this
    # entire pass a second time (screener.build_technical_table's full
    # O(symbols) rebuild isn't cheap, and score_cache only memoizes the
    # fundamentals lookup, not the ranking itself -- profiled as the
    # dominant cost of every ha_ema21_touch_enabled run, roughly doubling
    # runtime for no reason). Reusing this dict there cuts that in half
    # with zero behavior change (identical inputs -> identical output).
    ema21_touch_ranked_by_date: dict[pd.Timestamp, pd.DataFrame] = {}
    ema21_touch_watchlist_membership: dict[str, pd.Series] = {}
    if cfg.get("ha_ema21_touch_enabled", False):
        membership_records: dict[str, dict] = {sym: {} for sym in candles}
        for d in sorted(rb_dates):
            wl_ranked = rank_universe_asof(candles, bench, d, cfg,
                                           fundamentals_history, score_cache,
                                           sector_candles, sector_membership,
                                           long_candles, precomputed,
                                           precomputed_pivots, precomputed_weekly_monthly,
                                           precomputed_weekly_monthly_ok)
            ema21_touch_ranked_by_date[d] = wl_ranked
            if wl_ranked.empty:
                continue
            wl_candidates = wl_ranked[wl_ranked["all_gates"]]
            top_syms = set(wl_candidates.sort_values(
                "score", ascending=False).index[:cfg["watchlist_size"]])
            for sym in candles:
                membership_records[sym][d] = sym in top_syms
        for sym, records in membership_records.items():
            if records:
                ema21_touch_watchlist_membership[sym] = pd.Series(records).sort_index()

    # 2026-08-23: one-time, vectorized precompute of each symbol's sector
    # gate (relative_strength_vs_sector>0, AND if sector_above_ema_enabled
    # also sector_above_ema_ok) over its FULL history -- moved here from
    # trigger_strategy.detect_trigger's ha_ema21_touch branch, which used
    # to recheck both live every day through confirmation (traced to be
    # silently rejecting 26 of 61, 43%, of confirmed signals -- by far
    # the single biggest source of lost trades found). Now checked ONLY
    # at signal-candle formation, same treatment as watchlist_membership/
    # prior_rsi_ok. Uses each symbol's first/primary sector membership
    # (not a date-varying "current best sector" -- a practical
    # simplification; most stocks belong to exactly one sector anyway).
    # 2026-08-23: each of the two sub-checks only joins this formation-
    # time gate if its own flag says so (ha_ema21_touch_sector_rs_
    # formation_only / ha_ema21_touch_sector_above_ema_formation_only,
    # both False by default) -- whichever one is NOT moved here stays as
    # detect_trigger's original live daily recheck instead, so the two
    # can be isolated independently rather than only tested together.
    rs_formation_only = cfg.get("ha_ema21_touch_sector_rs_formation_only", False)
    above_ema_formation_only = cfg.get("ha_ema21_touch_sector_above_ema_formation_only", False)
    ema21_touch_sector_gate: dict[str, pd.Series] = {}
    if cfg.get("ha_ema21_touch_enabled", False) and sector_candles is not None \
            and sector_membership is not None and (rs_formation_only or above_ema_formation_only):
        rs_lookback = cfg["institutional_rs_lookback_days"]
        sector_above_ema_enabled = cfg.get("sector_above_ema_enabled", False)
        sector_ema_period = cfg["sector_above_ema_period"]
        for sym, df in candles.items():
            if df.empty:
                continue
            secs = sector_membership.get(sym, [])
            if not secs:
                continue
            sector_df = sector_candles.get(secs[0])
            if sector_df is None or sector_df.empty:
                continue
            stock_close = df["close"]
            sector_close = sector_df["close"].reindex(stock_close.index, method="ffill")
            gate_parts = []
            if rs_formation_only:
                stock_ret = stock_close / stock_close.shift(rs_lookback) - 1
                sector_ret = sector_close / sector_close.shift(rs_lookback) - 1
                gate_parts.append((stock_ret - sector_ret) > 0)
            if above_ema_formation_only and sector_above_ema_enabled:
                sector_ema = indicators.ema(sector_close, sector_ema_period)
                gate_parts.append(sector_close > sector_ema)
            if gate_parts:
                gate_ok = gate_parts[0]
                for part in gate_parts[1:]:
                    gate_ok = gate_ok & part
                ema21_touch_sector_gate[sym] = gate_ok

    # 2026-08-21: one-time precompute of the EMA21-touch-then-wait
    # pattern's own multi-day state machine -- see trigger_indicators.
    # precompute_ema21_touch_signals' docstring for why this can't be
    # recomputed fresh from a point-in-time slice like the other two HA
    # patterns can (it's a genuine forward state machine, not a same-day
    # or short-fixed-lookback check).
    precomputed_ema21_touch: dict = {}
    if cfg.get("ha_ema21_touch_enabled", False):
        for sym, df in candles.items():
            if not df.empty and sym in precomputed_ha:
                precomputed_ema21_touch[sym] = ti.precompute_ema21_touch_signals(
                    df, precomputed_ha[sym], cfg["ha_ema13_period"], cfg["ha_ema21_period"],
                    cfg["ha_rsi_period"], cfg["ha_ema21_touch_signal_rsi_min"],
                    cfg["ha_ema21_touch_confirm_days"],
                    cfg["ha_ema21_touch_breakout_threshold_pct"],
                    cfg["ha_ema21_touch_stop_uses_run_low"],
                    cfg["ha_ema21_touch_signal_volume_ema_period"],
                    ema21_touch_watchlist_membership.get(sym),
                    cfg["ha_ema21_touch_prior_rsi_lookback_days"],
                    cfg["ha_ema21_touch_prior_rsi_min"],
                    cfg["ha_ema21_touch_signal_volume_sma_period"],
                    cfg["ha_ema21_touch_prior_above_ema13_lookback_days"],
                    cfg["ha_ema21_touch_signal_close_above_ema13"],
                    ema21_touch_sector_gate.get(sym),
                    cfg.get("ha_ema21_touch_require_real_green", False),
                    cfg.get("ha_ema21_touch_ema50_slope_lookback_days"),
                    cfg.get("ha_ema21_touch_ema50_slope_min_pct", 5.0),
                    cfg.get("ha_ema21_touch_allow_reversal_wick_shapes", False),
                    cfg.get("ha_ema21_touch_require_ema13_above_ema21", False),
                    cfg.get("ha_ema21_touch_require_ha_ema_stack", False),
                    cfg.get("ha_ema21_touch_confirm_on_close", False),
                    cfg.get("ha_ema21_touch_prior_above_ema13_all_close", False),
                    cfg.get("ha_ema21_touch_prior_tiered_ema_check", False),
                    cfg.get("ha_ema21_touch_prior_no_ema50_violation_days"))

    cash = initial_capital
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    curve = []
    trigger_type_counts: dict[str, int] = {}
    # (symbol, entry_date) -> trigger type, so the final trades table can
    # show WHY each position was opened, not just why it closed (Trade's
    # own `reason` field is exit-only, shared with backtest.py's Trade).
    entry_trigger_log: dict[tuple[str, pd.Timestamp], str] = {}
    # symbol -> the ORIGINAL stop set at entry, never modified by the
    # trailing ratchet -- lets close_position() tell whether an exit hit
    # the initial stop untouched or a ratcheted (trailing) stop, without
    # touching backtest.py's shared Position dataclass (which only tracks
    # the CURRENT stop, overwritten as it trails).
    initial_stops: dict[str, float] = {}
    # (symbol, exit_date) -> {"exit_stop_price", "stop_type"}, merged onto
    # the final trades table the same way entry_trigger_log is -- 2026-08-
    # 15, explicit request to see whether each stop-out was the initial
    # stop or a trailed one, and at what price.
    exit_stop_log: dict[tuple[str, pd.Timestamp], dict] = {}
    # symbols that have crossed ha_breakeven_trigger_r and had their stop
    # jumped to breakeven -- 2026-08-16, tracks which positions are in the
    # "wide trail" phase of the breakeven-then-wide-trail stop mechanism
    # (see TRIGGERED_DEFAULTS' ha_breakeven_trail_enabled docstring).
    breakeven_locked: set[str] = set()
    # symbol -> profit-target price, only set for trigger types that
    # compute one (currently just the fast/21 EMA swing-pullback pattern
    # -- see trigger_strategy.detect_trigger's "target" key). Positions
    # without an entry here never get a target-based exit, only stop/
    # trailing-stop, same as before this feature existed.
    position_targets: dict[str, float] = {}
    # Symbols currently held via the institutional pullback trigger
    # (identified by trig["shape"], only that trigger sets it) -- these
    # get the strategy's own extra exit rules (close below EMA21, EMA21
    # crosses below EMA50, bearish engulfing near highs) on top of the
    # shared stop/trailing-stop/target checks; no other trigger type does.
    institutional_positions: set[str] = set()
    # 2026-08-25 addition: symbols currently held via heikin_ashi_trend
    # with ha_stop_mode="swing_ema50" -- these get the swing-low-trail +
    # EMA50-close-below exit logic in steps 1b/1c-swing below instead of
    # the generic ATR chandelier. Set at both entry sites, matching
    # institutional_positions'/ema21_touch_positions' own pattern.
    swing_ema50_positions: set[str] = set()
    # 2026-08-24 addition: symbols currently held via ha_ema21_touch,
    # only tracked so the half-target/EMA21-tail feature below knows
    # which open positions are eligible for it (no other trigger type
    # is). See ha_ema21_touch_half_target_ema21_tail's own comment.
    ema21_touch_positions: set[str] = set()
    # Symbols whose ha_ema21_touch position has ALREADY had half its qty
    # booked at the fixed 1:2 target (see step 1c below) -- the
    # remaining half rides until the REAL (non-HA) close first closes
    # below the REAL EMA21, checked in the new step below step 1
    # (starting the day AFTER the split, matching scripts/analyze_half_
    # target_ema21_tail.py's post-hoc simulation this was validated
    # against: +4.7% total P&L, win rate unchanged, on the 389-trade
    # 5yr priorrsi20_novolsma set). The original stop still protects
    # this remaining half throughout (step 1 above already checks ALL
    # `positions` generically, no special-casing needed there).
    ema21_tail_positions: set[str] = set()
    # Gate-passers not yet held, refreshed at each rebalance and scanned
    # daily for a trigger (see step 2b) -- unlike the production engine,
    # a slot free here does NOT mean an immediate buy, only eligibility to
    # be checked for a trigger.
    watchlist: dict[str, pd.Series] = {}
    ranked = pd.DataFrame()

    def close_position(sym, price, date, reason):
        nonlocal cash
        pos = positions.pop(sym)
        proceeds = pos.qty * price * (1 - cost)
        cash += proceeds
        trades.append(Trade(sym, pos.entry_date, date, pos.entry_price,
                            price * (1 - cost), pos.qty, reason, sector=pos.sector))
        # pos.stop is the CURRENT stop level at the moment of exit (already
        # ratcheted by step 1b if the trailing stop had moved it) --
        # comparing against the entry's untouched initial_stops[sym] value
        # tells whether this exit's stop level was ever actually trailed,
        # regardless of which check (stop/target) ultimately closed it.
        orig_stop = initial_stops.pop(sym, None)
        breakeven_locked.discard(sym)
        if reason == "stop":
            stop_type = "initial" if orig_stop is not None and abs(pos.stop - orig_stop) < 1e-6 else "trailing"
        else:
            stop_type = "n/a"
        exit_stop_log[(sym, date)] = {"exit_stop_price": round(pos.stop, 4), "stop_type": stop_type}
        position_targets.pop(sym, None)
        institutional_positions.discard(sym)
        ema21_touch_positions.discard(sym)
        ema21_tail_positions.discard(sym)
        swing_ema50_positions.discard(sym)

    def partial_close_at_target(sym, price, date):
        """Books HALF of an ha_ema21_touch position's qty at the target
        price (a real, permanent Trade record -- not paper) and leaves
        the rest open under `ema21_tail_positions`' exit rule. Mirrors
        close_position's cash/trade bookkeeping for the closed half only;
        the position itself (stop, entry_date, etc.) is untouched for
        the remaining half."""
        nonlocal cash
        pos = positions[sym]
        half = pos.qty // 2
        proceeds = half * price * (1 - cost)
        cash += proceeds
        trades.append(Trade(sym, pos.entry_date, date, pos.entry_price,
                            price * (1 - cost), half, "target_half", sector=pos.sector))
        pos.qty -= half
        position_targets.pop(sym, None)
        ema21_touch_positions.discard(sym)
        ema21_tail_positions.add(sym)

    def _price_asof(sym: str, date) -> float | None:
        sliced = candles[sym].loc[:date, "close"]
        return float(sliced.iloc[-1]) if not sliced.empty else None

    n_dates = len(dates)
    for i, date in enumerate(dates):
        if progress_cb and (i % max(1, n_dates // 100) == 0 or i == n_dates - 1):
            progress_cb(f"Simulating {date.date()}...", 0.1 + (i + 1) / n_dates * 0.9)

        # 1) stop checks on today's bar -- identical to
        # backtest.run_backtest's step 1.
        for sym in list(positions):
            df = candles[sym]
            if date not in df.index:
                continue
            bar = df.loc[date]
            pos = positions[sym]
            if bar["low"] <= pos.stop:
                fill = min(pos.stop, bar["high"])
                fill = min(fill, bar["open"]) if bar["open"] < pos.stop else fill
                close_position(sym, fill, date, "stop")

        # 1a-bis) ha_ema21_touch tail-leg exit -- the REMAINING half of a
        # position already split at target (see step 1c below) exits on
        # the first day its REAL (non-HA) close falls below its REAL
        # EMA21 (period ha_ema21_period, 21 by default) -- explicit
        # request for "normal candle close below normal EMA21", NOT the
        # HA close/EMA21 the state machine itself runs on. Placed BEFORE
        # step 1c so a symbol split TODAY by 1c isn't also checked here
        # today -- its first tail check is tomorrow, matching scripts/
        # analyze_half_target_ema21_tail.py's post-hoc simulation this
        # was validated against. The stop (checked above, step 1) still
        # protects this leg -- unchanged, no special-casing needed.
        if cfg.get("ha_ema21_touch_half_target_ema21_tail", False):
            for sym in list(ema21_tail_positions):
                if sym not in positions:
                    continue
                df = candles[sym]
                if date not in df.index:
                    continue
                df_upto = df.loc[:date]
                ema21_now = float(indicators.ema(
                    df_upto["close"], cfg["ha_ema21_period"]).iloc[-1])
                today_close = float(df_upto["close"].iloc[-1])
                if today_close < ema21_now:
                    close_position(sym, today_close, date, "close_below_ema21")

        # 1b) trailing stop ratchet -- generic ATR-chandelier trail for
        # most positions (identical to run_backtest's step 1b, with
        # atr_stop_multiple/trailing_atr_multiple overridden to 2.0/2.0
        # by TRIGGERED_DEFAULTS). Institutional positions use their OWN
        # spec's trailing rule instead -- "trail remaining using EMA21 or
        # previous 3-day low" -- candidate new stop is the HIGHER
        # (tighter) of the two, same monotonic-ratchet-up-only convention.
        if cfg.get("trailing_stop_enabled", False):
            for sym, pos in positions.items():
                df = candles[sym]
                if date not in df.index:
                    continue
                pos.highest_close = max(pos.highest_close, float(df.loc[date, "close"]))
                df_upto = df.loc[:date]
                if sym in institutional_positions:
                    ema21_now = float(indicators.ema(
                        df_upto["close"], cfg["institutional_ema21_period"]).iloc[-1])
                    three_day_low = float(df_upto["low"].tail(3).min())
                    new_stop = max(ema21_now, three_day_low)
                elif sym in swing_ema50_positions:
                    # 2026-08-25 addition: trail UP to the most recently
                    # CONFIRMED swing low (never down), minus the same
                    # buffer used for the initial stop -- lets a genuine
                    # trend run for as long as it keeps making higher
                    # lows, instead of the tight ATR trail's tendency to
                    # exit within days of one good move (see the real
                    # SIEMENS/BDL/TATASTEEL trades this was designed to
                    # fix). The EMA50-close-below exit (step 1c-swing
                    # below) is the parallel safety net for a trend that
                    # reverses before a new swing low ever confirms.
                    buffer_pct = cfg.get("swing_stop_buffer_pct", 0.3)
                    swing_df = swing_lows_by_symbol.get(sym)
                    new_stop = pos.stop
                    if swing_df is not None and not swing_df.empty:
                        confirmed = swing_df[swing_df["confirmed_date"] <= date]
                        if not confirmed.empty:
                            latest_low = float(confirmed.iloc[-1]["price"])
                            new_stop = latest_low * (1 - buffer_pct / 100)
                elif cfg.get("ha_breakeven_trail_enabled", False) and sym in initial_stops:
                    # Breakeven-then-wide-trail: stop stays FIXED at the
                    # initial ATR stop (no ratchet at all) until price
                    # reaches ha_breakeven_trigger_r * initial risk in
                    # unrealized profit -- then jumps straight to
                    # breakeven and switches to the wider ha_breakeven_
                    # trail_atr_multiple chandelier trail from then on.
                    risk = pos.entry_price - initial_stops[sym]
                    if sym not in breakeven_locked and risk > 0:
                        trigger_price = pos.entry_price + cfg["ha_breakeven_trigger_r"] * risk
                        if pos.highest_close >= trigger_price:
                            breakeven_locked.add(sym)
                            new_stop = pos.entry_price
                        else:
                            new_stop = pos.stop
                    elif sym in breakeven_locked:
                        atr_now = float(indicators.atr(df_upto, cfg["atr_period"]).iloc[-1])
                        new_stop = pos.highest_close - cfg["ha_breakeven_trail_atr_multiple"] * atr_now
                    else:
                        new_stop = pos.stop
                else:
                    atr_now = float(indicators.atr(df_upto, cfg["atr_period"]).iloc[-1])
                    new_stop = pos.highest_close - cfg["trailing_atr_multiple"] * atr_now
                if new_stop > pos.stop:
                    pos.stop = new_stop

        # 1c) profit-target check -- only for positions whose entry
        # trigger computed one (currently the fast/21 EMA swing-pullback
        # pattern's "prior swing high or fixed risk-reward" target).
        # Closes at the target price if today's HIGH reached it. Checked
        # after the stop-check above (a position that stopped out today
        # is already gone from `positions`, so it can't also "hit its
        # target" the same day) -- conservative when both could plausibly
        # have happened intraday, same convention as everywhere else in
        # this engine that resolves same-day ambiguity toward the worse
        # outcome.
        for sym in list(position_targets):
            if sym not in positions:
                continue
            df = candles[sym]
            if date not in df.index:
                continue
            bar = df.loc[date]
            target = position_targets[sym]
            if bar["high"] >= target:
                # 2026-08-24 addition: ha_ema21_touch positions, when the
                # half-target/EMA21-tail feature is on, book HALF here and
                # let the rest ride (see partial_close_at_target and the
                # new tail-exit step above step 1b). qty<2 can't be split
                # meaningfully -- falls back to the original full-close
                # behavior, same as every other trigger type always does.
                if (cfg.get("ha_ema21_touch_half_target_ema21_tail", False)
                        and sym in ema21_touch_positions and positions[sym].qty >= 2):
                    partial_close_at_target(sym, target, date)
                else:
                    close_position(sym, target, date, "target")

        # 1d) institutional-strategy-specific exits -- "Exit if any
        # occurs: daily close below EMA21 / bearish engulfing near highs
        # / EMA21 crosses below EMA50" (volume climax not implemented,
        # see trigger_strategy.TRIGGERED_DEFAULTS' comment on why). Only
        # applied to positions opened via that trigger -- checked in this
        # priority order, first match closes at today's close.
        for sym in list(institutional_positions):
            if sym not in positions:
                continue
            df = candles[sym]
            if date not in df.index:
                continue
            df_upto = df.loc[:date]
            close_s, open_s, high_s = df_upto["close"], df_upto["open"], df_upto["high"]
            ema21_now = float(indicators.ema(
                close_s, cfg["institutional_ema21_period"]).iloc[-1])
            today_close = float(close_s.iloc[-1])

            if today_close < ema21_now:
                close_position(sym, today_close, date, "close_below_ema21")
            elif ti.bearish_engulfing_near_highs(open_s, high_s, close_s):
                close_position(sym, today_close, date, "bearish_engulfing")
            elif ti.ema_cross_below(close_s, cfg["institutional_ema21_period"], cfg["ema_fast"]):
                close_position(sym, today_close, date, "ema21_cross_below_ema50")

        # 1c-swing) EMA50-close-below exit for swing_ema50 positions --
        # the parallel safety net to the swing-low trail above (step 1b):
        # fires even if the swing-low stop hasn't caught up yet, e.g. a
        # strong trend that reverses before a new pullback low has had
        # time to confirm (the real BRITANNIA 2024-06 trade this was
        # designed around: price ran +21% with no confirmed swing low
        # forming the whole way, but closing below EMA50 on 2024-10-11
        # caught the reversal a full month before the swing low would
        # have). Reuses precomputed[sym]["ema_fast"] (real-close EMA50,
        # cfg["ema_fast"]=50 for this strategy) -- zero extra computation.
        # Checked after step 1's stop-check and step 1b's ratchet, same
        # "already-closed positions can't double-exit" convention as
        # every other exit step here.
        for sym in list(swing_ema50_positions):
            if sym not in positions:
                continue
            df = candles[sym]
            if date not in df.index or sym not in precomputed or date not in precomputed[sym].index:
                continue
            today_close = float(df.loc[date, "close"])
            ema50_now = float(precomputed[sym].loc[date, "ema_fast"])
            if today_close < ema50_now:
                close_position(sym, today_close, date, "close_below_ema50")

        # 2) rebalance day: recompute the universe/ranking with the SAME
        # gates/config as production, and refresh the standing watchlist
        # to the top watchlist_size gate-passers not already held.
        #
        # Deliberately does NOT run the 200-EMA/rank-based sell_check exit
        # production uses -- that check belongs to production's rotation
        # strategy (drop whatever's no longer top-ranked), which is a
        # different risk model than this one. Every position here already
        # has its own defined risk (initial + trailing ATR stop, sized via
        # trigger_position_size's max-loss cap), so exiting it early
        # because it slipped in the rank is inconsistent with having given
        # it a real stop in the first place -- confirmed via a real traced
        # trade (ASIANPAINT 2026-07-29/30): it only got a watchlist slot
        # on a modest raw rank, then failed a rank-based keep_zone check
        # the very next day regardless of price action, a near-mechanical
        # 1-day round trip unrelated to the trade's own stop. Positions
        # here exit ONLY via step 1/1b's stop-loss or trailing stop.
        if date in rb_dates:
            # 2026-08-22: watchlist_lag_days (0 by default, unchanged
            # behavior) -- when set, ranks/gates using the close from N
            # trading days BEFORE today instead of today's own close, but
            # still applies the resulting watchlist to TODAY's trigger
            # scan below. Exists to validate a live/intraday design: a
            # live system can't know today's own close-based gates until
            # after today's close, so it can only build an actionable
            # watchlist from the last FULLY KNOWN day (yesterday) before
            # market open -- this backtest mode reproduces that exact
            # constraint instead of silently assuming same-day gate data
            # that wouldn't actually be available yet at trade time.
            rank_date = date
            lag = cfg.get("watchlist_lag_days", 0)
            if lag > 0:
                rank_date = dates[max(0, i - lag)]
            # 2026-08-23: reuse the pre-pass's already-computed ranking for
            # this exact date instead of recomputing it -- only valid when
            # rank_date == date (lag==0, the default), since that's the
            # only case the pre-pass covers the same date this call wants.
            if lag == 0 and rank_date in ema21_touch_ranked_by_date:
                ranked = ema21_touch_ranked_by_date[rank_date]
            else:
                ranked = rank_universe_asof(candles, bench, rank_date, cfg,
                                           fundamentals_history, score_cache,
                                           sector_candles, sector_membership,
                                           long_candles, precomputed,
                                           precomputed_pivots, precomputed_weekly_monthly,
                                           precomputed_weekly_monthly_ok)
            if not ranked.empty:
                candidates = ranked[ranked["all_gates"]]
                # 2026-08-25 revision: ema_intact_gate_enabled now filters
                # purely by EMA violation, over the SAME pool the baseline
                # watchlist already uses (all gate-passers -> sector cap ->
                # watchlist_size) -- no longer pre-narrows to the top
                # max_positions by rank first. That earlier pre-narrowing
                # (matching backtest.run_backtest()'s "top max_positions,
                # then filter within that slice" design) excluded EMA-
                # clean names like SIEMENS/ADANIGREEN purely for ranking
                # outside the top max_positions, not for any EMA
                # violation -- explicit correction, per real-trade trace:
                # "using ema_intact_ok whatever baseline trades should be
                # there only it should exclude the stocks which are close
                # below EMA 50 within its 5 days prior". `candidates`
                # itself stays the full ranked pool -- nothing else here
                # (sector cap, watchlist_size truncation) changes.
                ema_gate_on = cfg.get("ema_intact_gate_enabled", False)
                watch_syms_pre_cap = [
                    s for s in candidates.index if s not in positions
                    and (not ema_gate_on or bool(candidates.loc[s].get("ema_intact_ok", False)))]
                watch_syms = _apply_sector_cap(watch_syms_pre_cap, positions, ranked, cfg)
                watchlist = {sym: candidates.loc[sym]
                            for sym in watch_syms[:cfg["watchlist_size"]]}

                if debug_log is not None and cfg.get("ha_ema21_touch_enabled", False):
                    # Only "already_held" is a real, final skip at this
                    # stage now -- anything else not in `watchlist` (gate
                    # failure, sector cap, score cutoff) still gets a real
                    # shot via step 2c's straggler pass below (that's the
                    # whole point of the 2026-08-23 fix), so it's classified
                    # there instead, not prematurely here.
                    confirmed_today = {
                        s for s, t in precomputed_ema21_touch.items()
                        if date in t.index and bool(t.loc[date, "confirmed_entry"])}
                    for s in confirmed_today:
                        if s in positions:
                            debug_log.append({"date": date, "symbol": s, "reason": "already_held"})

        # 2b) daily trigger scan over the standing watchlist -- every day,
        # not just at rebalance. Highest-ranked stock first when multiple
        # names trigger the same day and slots are scarce (same
        # convention as every fill loop in run_backtest).
        if watchlist and len(positions) < cfg["max_positions"]:
            ordered = sorted(watchlist.items(),
                            key=lambda kv: kv[1].get("score", 0), reverse=True)
            debug_confirmed_today = None
            if debug_log is not None and cfg.get("ha_ema21_touch_enabled", False):
                debug_confirmed_today = {
                    s for s, t in precomputed_ema21_touch.items()
                    if date in t.index and bool(t.loc[date, "confirmed_entry"])}
            for idx, (sym, row) in enumerate(ordered):
                if len(positions) >= cfg["max_positions"]:
                    if debug_confirmed_today:
                        # Every symbol from here on in the score-ordered
                        # list never gets its turn -- log each once.
                        for s2, _ in ordered[idx:]:
                            if s2 in debug_confirmed_today:
                                debug_log.append({"date": date, "symbol": s2,
                                                 "reason": "max_positions_full"})
                    break
                if sym in positions or date not in candles[sym].index:
                    continue
                df_upto = candles[sym].loc[:date]
                sector_df_upto = None
                if sector_candles is not None:
                    sym_sector = row.get("top_sector") if hasattr(row, "get") else None
                    sector_df = sector_candles.get(sym_sector) if sym_sector else None
                    if sector_df is not None and not sector_df.empty:
                        sector_df_upto = sector_df.loc[:date]
                ha_upto = None
                if sym in precomputed_ha:
                    ha_upto = precomputed_ha[sym].loc[:date]
                ema21_touch_upto = None
                if sym in precomputed_ema21_touch:
                    ema21_touch_upto = precomputed_ema21_touch[sym].loc[:date]
                trig = ts.detect_trigger(df_upto, cfg, sector_df_upto, ha_upto, ema21_touch_upto)
                if trig is None:
                    if debug_confirmed_today and sym in debug_confirmed_today:
                        debug_log.append({"date": date, "symbol": sym,
                                         "reason": "detect_trigger_returned_none"})
                    continue

                price = trig["price"]
                if "stop" in trig:
                    # Swing-pullback trigger already computed a swing-
                    # low/EMA-anchored stop -- use it instead of the
                    # generic ATR-based one.
                    initial_stop = trig["stop"]
                else:
                    atr_now = float(indicators.atr(df_upto, cfg["atr_period"]).iloc[-1])
                    initial_stop = price - cfg["atr_stop_multiple"] * atr_now

                equity_now = cash + sum(
                    p.qty * (_price_asof(s, date) or 0.0)
                    for s, p in positions.items())
                qty = ts.trigger_position_size(
                    equity_now, price, initial_stop,
                    cfg["max_positions"], cfg["max_loss_pct_per_trade"])
                qty = min(qty, int(cash / (price * (1 + cost))))
                if qty <= 0:
                    if debug_confirmed_today and sym in debug_confirmed_today:
                        debug_log.append({"date": date, "symbol": sym,
                                         "reason": "insufficient_cash_or_sizing"})
                    continue

                cash -= qty * price * (1 + cost)
                entry_price = price * (1 + cost)
                sector = row.get("top_sector") if hasattr(row, "get") else None
                positions[sym] = Position(sym, qty, entry_price, initial_stop, date,
                                          highest_close=entry_price, sector=sector)
                initial_stops[sym] = initial_stop
                if debug_confirmed_today and sym in debug_confirmed_today:
                    debug_log.append({"date": date, "symbol": sym, "reason": "entered"})
                if "target" in trig:
                    position_targets[sym] = trig["target"]
                if "shape" in trig:
                    institutional_positions.add(sym)
                if trig["type"] == "ha_ema21_touch":
                    ema21_touch_positions.add(sym)
                if trig.get("swing_ema50_stop"):
                    swing_ema50_positions.add(sym)
                trigger_type_counts[trig["type"]] = trigger_type_counts.get(trig["type"], 0) + 1
                entry_trigger_log[(sym, date)] = trig["type"]
                watchlist.pop(sym, None)
                if verbose:
                    target_str = f" target {trig['target']:.1f}" if "target" in trig else ""
                    print(f"{date.date()} BUY  {sym:8s} x{qty} @ {price:.1f} "
                         f"stop {initial_stop:.1f}{target_str} trigger={trig['type']}")

        # 2c) 2026-08-23 fix: ema21_touch confirmations are only supposed
        # to be gated by watchlist membership at SIGNAL FORMATION time
        # (see trigger_indicators.precompute_ema21_touch_signals'
        # docstring -- "PENDING's confirmation countdown does NOT
        # recheck it, a stock that drops off the shortlist while a
        # signal is already PENDING can still confirm and trade"). But
        # step 2b above only calls detect_trigger for symbols currently
        # in `watchlist`, which requires all_gates=True on the
        # CONFIRMATION day too -- silently dropping valid confirmations
        # for symbols that fell off today's watchlist since their signal
        # formed (found via scripts/diagnose_ema21touch_funnel.py's
        # cross-check: ~5% of confirmed signals were affected). This
        # straggler pass handles exactly those: confirmed today, not
        # already held, not already in this day's watchlist (so step 2b
        # above never saw them at all).
        if cfg.get("ha_ema21_touch_enabled", False) and len(positions) < cfg["max_positions"]:
            confirmed_today = {
                s for s, t in precomputed_ema21_touch.items()
                if date in t.index and bool(t.loc[date, "confirmed_entry"])}
            stragglers = confirmed_today - set(watchlist.keys()) - set(positions.keys())
            for sym in stragglers:
                if len(positions) >= cfg["max_positions"]:
                    if debug_log is not None:
                        debug_log.append({"date": date, "symbol": sym,
                                         "reason": "max_positions_full"})
                    break
                if date not in candles[sym].index:
                    continue
                df_upto = candles[sym].loc[:date]
                sector_df_upto = None
                sym_sector = None
                if sector_candles is not None and 'ranked' in dir() and not ranked.empty \
                        and sym in ranked.index:
                    sym_sector = ranked.loc[sym, "top_sector"]
                    sector_df = sector_candles.get(sym_sector) if sym_sector else None
                    if sector_df is not None and not sector_df.empty:
                        sector_df_upto = sector_df.loc[:date]
                ha_upto = precomputed_ha[sym].loc[:date] if sym in precomputed_ha else None
                ema21_touch_upto = (precomputed_ema21_touch[sym].loc[:date]
                                   if sym in precomputed_ema21_touch else None)
                trig = ts.detect_trigger(df_upto, cfg, sector_df_upto, ha_upto, ema21_touch_upto)
                if trig is None:
                    if debug_log is not None:
                        debug_log.append({"date": date, "symbol": sym,
                                         "reason": "detect_trigger_returned_none"})
                    continue
                price = trig["price"]
                if "stop" in trig:
                    initial_stop = trig["stop"]
                else:
                    atr_now = float(indicators.atr(df_upto, cfg["atr_period"]).iloc[-1])
                    initial_stop = price - cfg["atr_stop_multiple"] * atr_now
                equity_now = cash + sum(
                    p.qty * (_price_asof(s, date) or 0.0)
                    for s, p in positions.items())
                qty = ts.trigger_position_size(
                    equity_now, price, initial_stop,
                    cfg["max_positions"], cfg["max_loss_pct_per_trade"])
                qty = min(qty, int(cash / (price * (1 + cost))))
                if qty <= 0:
                    if debug_log is not None:
                        debug_log.append({"date": date, "symbol": sym,
                                         "reason": "insufficient_cash_or_sizing"})
                    continue
                cash -= qty * price * (1 + cost)
                entry_price = price * (1 + cost)
                positions[sym] = Position(sym, qty, entry_price, initial_stop, date,
                                          highest_close=entry_price, sector=sym_sector)
                initial_stops[sym] = initial_stop
                if debug_log is not None:
                    debug_log.append({"date": date, "symbol": sym, "reason": "entered"})
                if "target" in trig:
                    position_targets[sym] = trig["target"]
                if "shape" in trig:
                    institutional_positions.add(sym)
                if trig["type"] == "ha_ema21_touch":
                    ema21_touch_positions.add(sym)
                if trig.get("swing_ema50_stop"):
                    swing_ema50_positions.add(sym)
                trigger_type_counts[trig["type"]] = trigger_type_counts.get(trig["type"], 0) + 1
                entry_trigger_log[(sym, date)] = trig["type"]
                if verbose:
                    print(f"{date.date()} BUY  {sym:8s} x{qty} @ {price:.1f} "
                         f"stop {initial_stop:.1f} trigger={trig['type']} (straggler)")

        # 3) mark to market
        mtm = cash + sum(
            p.qty * (_price_asof(s, date) or 0.0)
            for s, p in positions.items())
        curve.append((date, mtm))

    last = dates[-1]
    open_positions = []
    for sym, pos in positions.items():
        if candles[sym].loc[:last].empty:
            continue
        last_price = float(candles[sym].loc[:last, "close"].iloc[-1])
        unrealized_pnl = (last_price - pos.entry_price) * pos.qty
        open_positions.append({
            "symbol": sym, "entry_date": pos.entry_date, "entry_price": pos.entry_price,
            "current_price": last_price, "qty": pos.qty, "stop": pos.stop,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_ret_pct": (last_price / pos.entry_price - 1) * 100,
            "holding_days": (last - pos.entry_date).days,
            "sector": pos.sector,
            "entry_reason": entry_trigger_log.get((sym, pos.entry_date), ""),
        })
    open_positions_df = pd.DataFrame(open_positions)

    equity = pd.Series(dict(curve)).sort_index()
    metrics = compute_metrics(equity, trades, bench.loc[equity.index[0]:])
    metrics["Final Capital"] = round(float(equity.iloc[-1]), 2)
    metrics["Open positions"] = len(open_positions_df)
    trades_df = pd.DataFrame([dataclasses.asdict(t) | {
        "pnl": t.pnl, "ret_pct": t.ret_pct, "holding_days": t.holding_days,
        "entry_reason": entry_trigger_log.get((t.symbol, t.entry_date), ""),
        **exit_stop_log.get((t.symbol, t.exit_date), {"exit_stop_price": None, "stop_type": None}),
    } for t in trades])
    return {
        "equity_curve": equity,
        "trades": trades_df,
        "open_positions": open_positions_df,
        "final_capital": float(equity.iloc[-1]),
        "metrics": metrics,
        "trigger_type_counts": trigger_type_counts,
    }
