"""
New, self-contained indicator helpers for the experimental triggered-entry
backtest (see trigger_strategy.py / backtest_triggered.py). Nothing here is
imported by or affects the production backtest.py/indicators.py/
screener.py -- kept separate so this local-only experiment can't drift the
tested/deployed engine.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import indicators


def heikin_ashi(open_: pd.Series, high: pd.Series, low: pd.Series,
                close: pd.Series) -> pd.DataFrame:
    """Standard Heikin-Ashi transform. HA_close is a plain OHLC4 average
    (smoothing); HA_open recursively averages the PRIOR bar's own HA_open
    and HA_close -- this recursion is what makes HA trend-following/laggy
    by construction, and why it must be computed from the start of
    whatever series is passed in (a truncated window gives a DIFFERENT,
    wrong answer, not just a less-precise one -- see
    precompute_heikin_ashi for why this is precomputed once over full
    history rather than recomputed from a truncated df_upto every day).
    HA_high/HA_low extend to include the real bar's high/low so a HA
    candle's range is never narrower than what actually happened."""
    ha_close = (open_ + high + low + close) / 4.0
    ha_open = pd.Series(index=close.index, dtype=float)
    if len(close) == 0:
        return pd.DataFrame({"ha_open": ha_open, "ha_high": ha_open.copy(),
                            "ha_low": ha_open.copy(), "ha_close": ha_close})
    ha_open.iloc[0] = (float(open_.iloc[0]) + float(close.iloc[0])) / 2.0
    # .copy() -- pandas can hand back a read-only view here (copy-on-write
    # builds, or certain DataFrame construction paths) depending on
    # pandas version; the in-place recursive fill below needs a writable
    # array regardless of where this Series came from.
    ha_open_vals = ha_open.values.copy()
    ha_close_vals = ha_close.values
    for i in range(1, len(close)):
        ha_open_vals[i] = (ha_open_vals[i - 1] + ha_close_vals[i - 1]) / 2.0
    ha_open = pd.Series(ha_open_vals, index=close.index)
    ha_high = pd.concat([high, ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([low, ha_open, ha_close], axis=1).min(axis=1)
    return pd.DataFrame({"ha_open": ha_open, "ha_high": ha_high,
                        "ha_low": ha_low, "ha_close": ha_close})


def precompute_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """One-time, O(symbols) precompute of a symbol's FULL-history Heikin-
    Ashi series -- called once per symbol before the daily loop (same
    pattern as indicators.precompute_daily_series/resistance_zones.
    precompute_pivots/precompute_weekly_monthly_bars), not recomputed
    from a truncated window every day. Safe for the same reason those
    are: HA is purely CAUSAL (each bar depends only on bars up to and
    including it, never future bars), so precomputing the whole series
    once and slicing .loc[:date] per day is byte-identical to recomputing
    fresh from df.loc[:date] every time -- just far cheaper, and (unlike
    the other precomputed series) also the ONLY correct way to do it at
    all, since HA_open's recursion means a truncated window computes a
    genuinely different value, not an approximation."""
    return heikin_ashi(df["open"], df["high"], df["low"], df["close"])


def heikin_ashi_trend_entry(df_upto: pd.DataFrame, ha_upto: pd.DataFrame,
                            ema13_period: int, ema21_period: int,
                            ema50_period: int, ema200_period: int,
                            rsi_period: int, rsi_min: float,
                            signal_lookback_days: int,
                            volume_sma_period: int) -> dict | None:
    """Heikin-Ashi trend-following entry (2026-08-13 spec -- replaces
    every prior trigger entirely for this strategy; the sector-based
    market filter and Minervini trend-template pre-filter are also
    dropped, not just the old EMA21/breakout entries):

      3. HA EMA13 > HA EMA21 > HA EMA50 > HA EMA200, all computed on
         HA_close, evaluated as of TODAY (the entry/confirmation day).
      4. HA RSI(14) > `rsi_min` (60 by default), on HA_close, also as of
         TODAY -- re-checked fresh on the entry candle itself, not just
         whenever the signal candle happened to occur.
      5. SIGNAL: the NEAREST red HA candle (HA_close < HA_open) within the
         trailing `signal_lookback_days` -- always this ONE candle, never
         a fallback to an older red candle further back if it fails the
         RSI check below (2026-08-14: previously kept searching backward
         past a failed nearest red candle, which let an older, weaker
         candle silently substitute as the signal -- caught via a real
         trade, TORNTPHARM 2026-07-31, where the nearest red candle
         07-30 failed but the search fell back to 07-29 anyway).
         RSI check on that ONE candle: its OWN HA RSI(14) must be >
         `rsi_min` at the time it formed (a red pullback candle during a
         genuine RSI>60 uptrend reads very differently from one where RSI
         had already broken down) -- OR, if its own RSI had already
         dropped <= `rsi_min`, the candle immediately BEFORE it still had
         RSI > `rsi_min` (catches the pullback right at the RSI breakdown
         point). If neither holds, there is no signal at all for today --
         the function returns None, it does not look further back.
      6/7. CONFIRMATION: today's REAL (not HA) close must close above
         that signal candle's HA HIGH, entry AT today's real close.
         Additionally, no day strictly between the signal candle and
         today may have already closed above that HA high -- otherwise
         this is a stale signal that already broke out earlier and
         would silently keep re-firing every subsequent day it stays
         above that level.
      8. Entry day's REAL volume must exceed its own trailing
         `volume_sma_period`-day SIMPLE moving average of REAL volume.

    Returns None if nothing fired, else {"entry_price": today's real
    close, "trigger_low": the signal candle's HA LOW (for the stop)}."""
    ha_close, ha_open, ha_high, ha_low = (ha_upto["ha_close"], ha_upto["ha_open"],
                                          ha_upto["ha_high"], ha_upto["ha_low"])
    if len(ha_close) < ema200_period + 1:
        return None

    ema13 = indicators.ema(ha_close, ema13_period)
    ema21 = indicators.ema(ha_close, ema21_period)
    ema50 = indicators.ema(ha_close, ema50_period)
    ema200 = indicators.ema(ha_close, ema200_period)
    if not (float(ema13.iloc[-1]) > float(ema21.iloc[-1])
           > float(ema50.iloc[-1]) > float(ema200.iloc[-1])):
        return None

    ha_rsi = indicators.rsi(ha_close, rsi_period)
    if pd.isna(ha_rsi.iloc[-1]) or not float(ha_rsi.iloc[-1]) > rsi_min:
        return None

    real_close = df_upto["close"]
    real_volume = df_upto["volume"]
    if len(real_close) < 2:
        return None
    today_close = float(real_close.iloc[-1])

    sig_pos = None
    for k in range(1, signal_lookback_days + 1):
        pos = -1 - k
        if abs(pos) > len(ha_close):
            break
        if float(ha_close.iloc[pos]) < float(ha_open.iloc[pos]):
            # Found the NEAREST red HA candle in the lookback window --
            # the RSI check applies ONLY to this one candle. If it fails,
            # the search stops here entirely (return None below) rather
            # than continuing further back to an older red candle that
            # might separately qualify -- 2026-08-14 clarification, after
            # a real trade (TORNTPHARM 2026-07-31) showed the old
            # keep-searching behaviour skipping past a failed nearest red
            # candle (07-30) to accept an older one (07-29) instead.
            #
            # RSI check itself: the candle's OWN HA RSI(14) must be >
            # rsi_min at the time it formed -- OR, if it had already
            # dropped to <= rsi_min, the candle immediately BEFORE it
            # must still have been > rsi_min (catches the pullback right
            # at the RSI>60 breakdown point).
            sig_rsi_ok = not pd.isna(ha_rsi.iloc[pos]) and float(ha_rsi.iloc[pos]) > rsi_min
            if not sig_rsi_ok:
                prior_pos = pos - 1
                if abs(prior_pos) <= len(ha_rsi):
                    prior_rsi = ha_rsi.iloc[prior_pos]
                    sig_rsi_ok = not pd.isna(prior_rsi) and float(prior_rsi) > rsi_min
            if sig_rsi_ok:
                sig_pos = pos
            break
    if sig_pos is None:
        return None

    sig_high = float(ha_high.iloc[sig_pos])
    sig_low = float(ha_low.iloc[sig_pos])
    if today_close <= sig_high:
        return None  # hasn't closed above the signal candle's HA high yet

    between_closes = real_close.iloc[sig_pos + 1:-1]
    if not between_closes.empty and bool((between_closes > sig_high).any()):
        return None  # already broke out on an earlier day -- stale signal

    if len(real_volume) < volume_sma_period:
        return None
    volume_sma = float(real_volume.tail(volume_sma_period).mean())
    if not float(real_volume.iloc[-1]) > volume_sma:
        return None

    # signal_date: the signal candle's own timestamp -- 2026-08-15 addition,
    # additive only (existing "ha_low"/"atr" ha_stop_mode branches simply
    # ignore this key) -- lets a caller locate the swing high/low that
    # preceded this pullback, for the new Fibonacci stop/target mode.
    #
    # 2026-08-25 addition, also additive-only: HA EMA13/EMA21 values at
    # BOTH the signal candle and today (the entry/confirmation candle) --
    # lets ha_stop_mode="swing_ema50" set its tiered initial stop without
    # trigger_strategy.py needing to recompute ema13/ema21 itself.
    return {"entry_price": today_close, "trigger_low": sig_low,
           "signal_date": ha_close.index[sig_pos],
           "sig_ema13": float(ema13.iloc[sig_pos]), "sig_ema21": float(ema21.iloc[sig_pos]),
           "entry_ema13": float(ema13.iloc[-1]), "entry_ema21": float(ema21.iloc[-1])}


def heikin_ashi_ema21_bounce_entry(df_upto: pd.DataFrame, ha_upto: pd.DataFrame,
                                   ema13_period: int = 13, ema21_period: int = 21,
                                   ema50_period: int = 50, ema200_period: int = 200,
                                   rsi_period: int = 14, rsi_min: float = 58.0) -> dict | None:
    """2026-08-15: new, INDEPENDENT Heikin-Ashi entry pattern -- a same-day
    EMA21-pullback-bounce, distinct from heikin_ashi_trend_entry's
    red-signal-candle-then-breakout-next-day logic. Explicit spec:

      1. Stock selection: same pipeline as the other HA strategy (caller
         passes the same watchlist/gate-filtered candidates; this function
         only evaluates the entry pattern itself).
      2. Real (non-HA) close > HA EMA(`ema50_period`) -- price above the
         medium-term HA trend.
      3. HA EMA13 > HA EMA21 > HA EMA50 > HA EMA200 (same stack condition
         as the other HA entry).
      4. TODAY's own candle (not a prior signal candle): HA_open <= HA
         EMA21 AND HA_low <= HA EMA21 (2026-08-16: both open and low, not
         just the low wicking down to it -- the whole candle body sits
         into the pullback zone) AND HA_close >= HA EMA13 (but closed back
         at/above the fastest EMA -- a same-candle rejection/bounce, not a
         multi-day pullback+breakout) AND HA RSI(`rsi_period`) > `rsi_min`
         (58 by spec, deliberately looser than heikin_ashi_trend_entry's
         60 -- a different pattern, its own threshold).

    Entry fills at TODAY's real close (the bounce candle itself, not a
    breakout confirmation the next day -- this pattern's signal and
    confirmation are the same candle).

    Returns {"entry_price": today's real close, "stop": today's HA low}
    (the caller applies the 1:2 target) or None if any condition fails."""
    ha_close, ha_open, ha_high, ha_low = (ha_upto["ha_close"], ha_upto["ha_open"],
                                          ha_upto["ha_high"], ha_upto["ha_low"])
    if len(ha_close) < ema200_period + 1:
        return None

    ema13 = indicators.ema(ha_close, ema13_period)
    ema21 = indicators.ema(ha_close, ema21_period)
    ema50 = indicators.ema(ha_close, ema50_period)
    ema200 = indicators.ema(ha_close, ema200_period)
    e13_now = float(ema13.iloc[-1])
    e21_now = float(ema21.iloc[-1])
    e50_now = float(ema50.iloc[-1])
    e200_now = float(ema200.iloc[-1])
    if not (e13_now > e21_now > e50_now > e200_now):
        return None

    real_close = df_upto["close"]
    if real_close.empty:
        return None
    today_real_close = float(real_close.iloc[-1])
    if not today_real_close > e50_now:
        return None

    ha_rsi = indicators.rsi(ha_close, rsi_period)
    if pd.isna(ha_rsi.iloc[-1]):
        return None
    today_rsi = float(ha_rsi.iloc[-1])
    today_ha_open = float(ha_open.iloc[-1])
    today_ha_low = float(ha_low.iloc[-1])
    today_ha_close = float(ha_close.iloc[-1])

    # 2026-08-16: stricter touch condition, explicit request -- BOTH the
    # candle's HA open AND HA low must be at/below EMA21 (not just the
    # low wicking down to it), so the whole body sits into the pullback
    # zone, not just a lower wick.
    if not (today_ha_open <= e21_now and today_ha_low <= e21_now
           and today_ha_close >= e13_now and today_rsi > rsi_min):
        return None

    return {"entry_price": today_real_close, "stop": today_ha_low}


def precompute_ema21_touch_signals(df: pd.DataFrame, ha: pd.DataFrame,
                                   ema13_period: int = 13, ema21_period: int = 21,
                                   rsi_period: int = 14, signal_rsi_min: float = 50.0,
                                   confirm_lookback_days: int = 10,
                                   breakout_threshold_pct: float = 0.001,
                                   stop_uses_run_low: bool = True,
                                   signal_volume_ema_period: int | None = None,
                                   watchlist_membership: pd.Series | None = None,
                                   prior_rsi_lookback_days: int | None = None,
                                   prior_rsi_min: float = 60.0,
                                   signal_volume_sma_period: int | None = None,
                                   prior_above_ema13_lookback_days: int | None = None,
                                   signal_close_above_ema13: bool = True,
                                   sector_gate_ok: pd.Series | None = None,
                                   require_real_green: bool = False,
                                   ema50_slope_lookback_days: int | None = None,
                                   ema50_slope_min_pct: float = 5.0,
                                   allow_reversal_wick_shapes: bool = False,
                                   require_ema13_above_ema21: bool = False,
                                   require_ha_ema_stack: bool = False,
                                   confirm_on_close: bool = False,
                                   prior_above_ema13_all_close: bool = False,
                                   prior_tiered_ema_check: bool = False,
                                   prior_no_ema50_violation_days: int | None = None) -> pd.DataFrame:
    """2026-08-21: one-time, full-history precompute for a THIRD,
    independent Heikin-Ashi entry pattern -- EMA21-touch-then-wait-for-
    breakout -- kept fully separate from heikin_ashi_trend_entry and
    heikin_ashi_ema21_bounce_entry (neither is modified by this). Unlike
    those two (each a same-day or short-fixed-lookback check,
    recomputable fresh from any point-in-time slice), this pattern is a
    genuine multi-day state machine, so it's walked forward ONCE over the
    whole series here (same reasoning as precompute_heikin_ashi/
    precompute_weekly_monthly_bars -- redoing an O(days) walk from
    scratch on every single backtest day would be O(days^2) per symbol).

    Explicit spec (2026-08-21 request, signal-candle shape revised
    2026-08-23):
      HUNTING (default state) -- looking for a new signal candle:
        HA_low <= HA EMA21 (the "touch"), HA_high > HA EMA13, HA_close
        above HA EMA13 if `signal_close_above_ema13` (default True) else
        HA EMA21 -- kept as an explicit toggle to compare both variants,
        since which one is "right" is still being tested -- and its OWN
        HA RSI(`rsi_period`) > `signal_rsi_min` (50 by spec -- briefly raised to 55 on
        2026-08-21, then reverted back to 50 on 2026-08-22 by explicit
        request. This is the ONLY RSI gate anywhere in this pattern,
        since the separate day-of-entry RSI gate that used to live in
        heikin_ashi_ema21_touch_entry was also removed by explicit
        request),
        and, if `signal_volume_ema_period` is given (None = no gate, the
        default), the candle's own REAL volume above its trailing EMA
        (not SMA) of real volume over that period -- an OPTIONAL extra
        filter on the signal candle itself, distinct from (and NOT a
        revival of) the day-of-entry volume gate that was separately
        removed from heikin_ashi_ema21_touch_entry; that one was removed
        because entry fires on an INTRADAY crossing, where the day's
        final volume isn't actually known yet at the moment of a live
        entry -- this one is fine because the signal candle is fully
        formed and in the past by the time it's evaluated, so its whole
        day's volume is real, already-known information. Also, if
        `watchlist_membership` is given (a bool Series indexed by date,
        None = no gate, the default), the candle's own date must be True
        in it -- i.e. the stock must have actually been on that day's
        top-`watchlist_size` shortlist for a candle to start (or extend)
        a run at all. This is the ONLY place watchlist membership is
        checked anywhere in this pattern now (2026-08-22 explicit
        request) -- PENDING's confirmation countdown below does NOT
        recheck it, matching "once a signal candle is found, only look
        at whether price crosses in the next N days" -- a stock that
        drops off the shortlist while a signal is already PENDING can
        still confirm and trade.
        2026-08-23 addition: `sector_gate_ok` (a bool Series indexed by
        date, None = no gate, the default) -- same treatment as
        `watchlist_membership` above, checked ONLY at signal-candle
        formation, never rechecked during PENDING. Moved here from
        trigger_strategy.detect_trigger's ha_ema21_touch branch, which
        used to recheck relative_strength_vs_sector/sector_above_ema_ok
        EVERY day through confirmation -- traced (scripts/
        trace_skip_reasons.py) to be silently rejecting 26 of 61 (43%)
        confirmed signals, by far the single biggest source of lost
        trades found. The caller computes this series (needs sector
        candle data this function doesn't have access to).
        2026-08-23 addition: `require_real_green` (False = no gate, the
        default) -- if True, the signal candle's REAL (non-HA) close must
        be above its REAL open, i.e. an ordinary green daily candle, on
        top of the HA-based shape check above (which no longer cares
        about color at all). Found via post-hoc analysis: signal candles
        that are HA-red/-neutral but happen to close real-green win 46.5%
        of the time (avg +2.15%) vs 38.6% (avg +0.37%) for real-red ones
        -- HA smoothing can mask a day where real buying pressure already
        returned, and this catches that.
        2026-08-23 addition: `ema50_slope_lookback_days` (None = no gate,
        the default) -- if given, the REAL (non-HA) close's EMA50 must
        have risen by at least `ema50_slope_min_pct` (5.0% by default)
        over that many trading days, measured at the signal candle. Post-
        hoc analysis (scripts/analyze_win_factors.py) found this specific
        combination -- EMA50 (not EMA21 or EMA200) over a ~20-day window
        (not EMA50's own 10-day window, too short to show a meaningful
        move) -- is where a genuine edge shows up: trades with EMA50 up
        >=6% over the prior 20 days won 50.0% (avg +2.84%) vs 41.8% (avg
        +1.15%) for the rest; below roughly 5-6% the slope carries no
        signal at all (pure noise around the ~44% baseline). EMA200's
        slope showed the OPPOSITE relationship (higher slope = worse
        outcomes, likely chasing an already-extended structural trend) --
        deliberately NOT implemented as a gate.
        2026-08-24 addition: `require_ema13_above_ema21` (False = no gate,
        the default) -- if True, HA EMA13 must be above HA EMA21 at the
        signal candle (the fast/slow EMA in the "right" order for an
        uptrend). Isolated from the full "HA EMA13>EMA21>EMA50>EMA200"
        stacked-trend condition that heikin_ashi_trend_entry and
        heikin_ashi_ema21_bounce_entry both require (trigger_indicators.py
        lines 76/200) -- post-hoc analysis found the FULL stack barely
        ever fails here (94%+ of signal candles already satisfy it) and,
        when it does fail, only the EMA13-vs-EMA21 piece specifically
        carries a real signal: trades where EMA13<EMA21 at signal time
        won 33.3% (n=12) vs the 42.1% overall baseline, on a 416-trade
        sample; EMA21<EMA50 or EMA50<EMA200 failing alone showed no
        effect (both ~43%, right at baseline). This is the concrete case
        that surfaced it: LT's 2023-03-16 signal candle passed every
        other gate but had EMA13 (2160.81) just under EMA21 (2161.44)
        the whole prior week, and the trade lost.
        2026-08-24 addition: `require_ha_ema_stack` (False = no gate, the
        default) -- the FULL "HA EMA13 > HA EMA21 > HA EMA50 > HA EMA200"
        condition (all on HA_close), matching heikin_ashi_trend_entry and
        heikin_ashi_ema21_bounce_entry's own spec exactly, which this
        pattern never inherited. Independent of `require_ema13_above_
        ema21` above (the post-hoc analysis found only the EMA13-vs-21
        piece carries real signal; this flag lets the full stack be
        tested too, for direct comparison).
        2026-08-24 addition: `confirm_on_close` (False = no change, the
        default) -- changes the PENDING confirmation check from "REAL
        HIGH crosses above the signal high by breakout_threshold_pct,
        fill at that exact crossing level" to "REAL CLOSE closes above
        that same trigger level, fill at that day's close" -- explicit
        request to test a close-based (not intrabar-crossing-based)
        confirmation. `confirm_lookback_days` still controls the window
        (e.g. 3 for "check the next 3 candles") -- unchanged, only the
        crossing condition and fill price change.
        2026-08-24 addition: `allow_reversal_wick_shapes` (False = no
        change, the default) -- only takes effect when `require_real_
        green` is also True. Widens that gate: a candle also passes if
        it's a hammer or dragonfly-doji (small real body, real lower wick
        >= 2x body, minimal real upper wick -- i.e. a long lower wick
        showing rejection of lower prices), REGARDLESS of its real color.
        Explicit request to test whether a red hammer/doji at the signal
        candle (which fails plain require_real_green) is still a valid
        reversal-at-support signal, since that's exactly the setup this
        pattern is built around (pullback to EMA21). Post-hoc analysis
        found a directional edge (hammer_red: 50.0% win/14 trades vs
        44.6% win/186 trades for plain candles) but on samples too thin
        to trust (14-21 trades) -- implemented to test at proper scale
        rather than decide from that alone.
        2026-08-22 addition: if `prior_rsi_lookback_days` is given (None
        = no gate, the default), at least one of the `prior_rsi_lookback_
        days` HA candles strictly BEFORE the signal candle must have had
        HA-close RSI(`rsi_period`) above `prior_rsi_min` (60 by spec) --
        i.e. the stock must have shown genuine recent momentum (on its
        own HA RSI, the SAME series/period as the signal candle's own
        RSI>`signal_rsi_min` check above) before pulling back into this
        signal, even though the signal day's own HA RSI is typically much
        lower by construction (that's what makes it a pullback). Uses HA
        RSI throughout, not real-close RSI (that's the separate,
        independent watchlist rsi_ok gate).
        2026-08-22 addition: `signal_volume_sma_period` (None = no gate,
        the default) -- a SECOND, independent optional volume filter on
        the signal candle, using a plain trailing SMA of real volume
        (not EMA) -- kept as a separate parameter from
        `signal_volume_ema_period` above so both can be tested/compared
        without one overwriting the other.
        2026-08-23 addition: `prior_above_ema13_lookback_days` (None = no
        gate, the default) -- at least one of the prior N HA candles
        strictly BEFORE the signal candle must have had BOTH its HA_open
        AND HA_close above HA EMA13 (not just the close, and not just the
        high as the signal candle's own hh > e13 check requires) --
        a second, independent recent-strength check alongside
        `prior_rsi_lookback_days`, both evaluated over the candle
        strictly preceding the signal, not the signal candle itself.
        2026-08-24 addition: `prior_above_ema13_all_close` (False = the
        above "any 1 of N" behavior, the default) -- if True, switches
        to a much stricter requirement: ALL N of the prior candles' HA
        CLOSE (not open+close, just close) must be above HA EMA13.
        Explicit request to test a stronger recent-strength bar.
        2026-08-24 addition: `prior_tiered_ema_check` (False = no gate,
        the default) -- a third, STRUCTURED alternative to the two
        prior_above_ema13 variants above: the candle IMMEDIATELY before
        the signal candle (i-1) must have HA close > HA EMA21 (a looser
        bar -- it's allowed to already be pulling back toward the EMA,
        same as the signal candle itself will), while each of the 4
        candles BEFORE that one (i-2 through i-5) must have HA close >
        HA EMA13 (the stronger bar, but placed further back from the
        signal so it doesn't conflict with the immediate pre-signal
        pullback). Independent of prior_above_ema13_lookback_days/
        prior_above_ema13_all_close -- doesn't use either.
        2026-08-24 addition: `prior_no_ema50_violation_days` (None = no
        gate, the default) -- a structural-trend-intact check: NONE of
        the prior N candles (strictly before the signal candle) may have
        closed (HA close) below HA EMA50 OR HA EMA200 -- i.e. the trend
        hasn't broken either level at any point in that trailing window
        (2026-08-24 revision: extended from EMA50-only to require both,
        per explicit request, replacing `require_ha_ema_stack` as the
        long-term-trend confirmation being tested). Independent of every
        other prior-strength/stack gate above.
        2026-08-22 revision: if the VERY NEXT candle also independently
        qualifies as a signal candle, the run EXTENDS instead of
        committing immediately -- the run keeps extending for as long as
        consecutive candles keep qualifying. Only once a candle fails to
        qualify (or HA_close drops below EMA21) does the run END and
        commit to PENDING, using the LATEST qualifying candle's HA high
        as the breakout trigger level. The stop is controlled by
        `stop_uses_run_low`: True (default) -- the LOWEST HA low across
        EVERY candle in the whole run (a multi-day pullback's stop
        should protect against the deepest point of that whole pullback,
        not just its last candle); False -- just the LATEST qualifying
        candle's own HA low, ignoring the rest of the run, kept as an
        explicit alternative to compare against. A single isolated
        signal candle (the common case) is just a run of length one, so
        both modes agree there -- this only matters for multi-candle
        runs. Found (run ends) -> PENDING, remembering the run's final
        high/low.
      PENDING -- waiting up to `confirm_lookback_days` trading days TOTAL
        (2026-08-23: the same-day check below, on the day the run ends,
        now counts as day 1 of this budget -- confirm_lookback_days=1
        means ONLY that immediate day is checked, no PENDING days after
        it) for the REAL (non-HA) intraday HIGH to cross above the signal
        candle's HA high by at least `breakout_threshold_pct` (0.1% by
        spec -- a crossing check against the day's HIGH, not a
        close-above-level requirement; the candle can close anywhere,
        even back below the signal high, and this still counts).
        Confirmed on some day -> that day is a hit (confirmed_entry=True
        there; 2026-08-21 revision: entry now fills at the crossing
        level itself -- signal_high * (1+breakout_threshold_pct), stored
        as "trigger_price" -- NOT that day's real close, since the whole
        point of the threshold is to enter exactly where price crossed
        it, per explicit request), then back to HUNTING. HA_close < HA
        EMA21 on any day while pending, before confirming -> LOCKED_OUT
        (the pending signal is cancelled, not just expired).
        confirm_lookback_days elapses with no confirmation and no
        invalidation -> plain timeout, back to HUNTING (no lockout).
      LOCKED_OUT -- HA_close dropped below EMA21 at some point (either
        while HUNTING or PENDING); stays locked out of the search
        entirely until HA_close reclaims above HA EMA13, then back to
        HUNTING.

    Returns a DataFrame indexed like df/ha with columns "confirmed_entry"
    (bool, True only on a day the breakout confirms), "signal_high",
    "signal_low", "trigger_price" (that confirmed signal's HA high/low
    and the exact crossing-level entry price, NaN every other day)."""
    ha_close, ha_open, ha_high, ha_low = (ha["ha_close"], ha["ha_open"],
                                          ha["ha_high"], ha["ha_low"])
    real_high = df["high"]
    real_low = df["low"]
    real_volume = df["volume"]
    real_open = df["open"]
    real_close = df["close"]
    n = len(ha_close)
    ema13 = indicators.ema(ha_close, ema13_period)
    ema21 = indicators.ema(ha_close, ema21_period)
    # HA-based EMA50/EMA200, only computed when needed -- matches
    # heikin_ashi_trend_entry/heikin_ashi_ema21_bounce_entry's own
    # "computed on HA_close" stack spec (this function's other EMA50 use,
    # ema50_slope_ok above, is deliberately on the REAL close instead).
    ema50_ha_stack = (indicators.ema(ha_close, 50)
                     if (require_ha_ema_stack or prior_no_ema50_violation_days) else None)
    ema200_ha_stack = (indicators.ema(ha_close, 200)
                      if (require_ha_ema_stack or prior_no_ema50_violation_days) else None)
    rsi = indicators.rsi(ha_close, rsi_period)
    volume_ema = (indicators.ema(real_volume, signal_volume_ema_period)
                 if signal_volume_ema_period else None)
    volume_sma = (real_volume.rolling(signal_volume_sma_period).mean()
                 if signal_volume_sma_period else None)
    # "any 1 of the prior N HA candles had HA RSI above prior_rsi_min" --
    # shift(1) excludes today itself (strictly BEFORE the signal candle,
    # per spec), rolling(...).max() over the shifted series is the
    # highest HA RSI seen in that trailing window.
    prior_rsi_ok = (rsi.shift(1).rolling(prior_rsi_lookback_days).max() > prior_rsi_min
                   if prior_rsi_lookback_days else None)
    # "any 1 of the prior N HA candles had BOTH its open AND close above
    # HA EMA13" -- same shift(1)-then-rolling pattern as prior_rsi_ok,
    # but on a boolean (open>e13 AND close>e13) series: rolling(...).sum()
    # over the shifted 0/1 series is > 0 iff at least one qualifying
    # candle exists in that trailing window.
    prior_above_ema13_ok = None
    if prior_above_ema13_lookback_days:
        if prior_above_ema13_all_close:
            # 2026-08-24 addition, stricter alternative: ALL of the prior
            # N candles' HA close (not open+close, just close) must be
            # above HA EMA13 -- explicit request for a much stronger
            # recent-strength requirement than "any 1 of N had both open
            # and close above."
            above_ema13_close = (ha_close > ema13).astype(int)
            prior_above_ema13_ok = (above_ema13_close.shift(1)
                                   .rolling(prior_above_ema13_lookback_days).sum()
                                   == prior_above_ema13_lookback_days)
        else:
            above_ema13 = ((ha_open > ema13) & (ha_close > ema13)).astype(int)
            prior_above_ema13_ok = (above_ema13.shift(1)
                                   .rolling(prior_above_ema13_lookback_days).sum() > 0)
    # 2026-08-24 addition: structured two-tier prior-strength check --
    # immediate prior candle (i-1) HA close > HA EMA21 (looser, already
    # pulling back), AND each of the 4 candles before THAT (i-2..i-5)
    # had HA close > HA EMA13 (stronger). shift(1) isolates the
    # immediate-prior value; shift(2).rolling(4).sum()==4 on the shifted-
    # by-2 series covers exactly positions i-2..i-5 (shift(2) puts
    # original i-2 at position i, then rolling(4) looks back 3 more).
    prior_tiered_ok = None
    if prior_tiered_ema_check:
        immediate_above_ema21 = (ha_close > ema21).shift(1)
        four_before_above_ema13 = (ha_close > ema13).astype(int)
        four_before_ok = (four_before_above_ema13.shift(2).rolling(4).sum() == 4)
        prior_tiered_ok = immediate_above_ema21.fillna(False) & four_before_ok.fillna(False)
    # "NONE of the prior N HA candles closed below HA EMA50 OR HA EMA200"
    # -- structural trend-intact check (2026-08-24 revision: extended
    # from EMA50-only to require BOTH, per explicit request -- "should
    # not close below 50 AND below 200 EMA"), same shift(1)-then-rolling
    # pattern as the other prior-strength gates.
    prior_ema50_no_violation_ok = None
    if prior_no_ema50_violation_days:
        above_ema50 = ((ha_close >= ema50_ha_stack) & (ha_close >= ema200_ha_stack)).astype(int)
        prior_ema50_no_violation_ok = (above_ema50.shift(1)
                                      .rolling(prior_no_ema50_violation_days).sum()
                                      == prior_no_ema50_violation_days)
    # REAL (non-HA) close's EMA50 % change over the prior N trading days,
    # measured AT the signal candle (not shifted -- the signal candle's
    # own EMA50 value is already-known, real information at the time
    # it's evaluated, same reasoning as the real-volume gates above).
    ema50_slope_ok = None
    if ema50_slope_lookback_days:
        ema50_real = indicators.ema(real_close, 50)
        ema50_slope_pct = (ema50_real / ema50_real.shift(ema50_slope_lookback_days) - 1) * 100
        ema50_slope_ok = ema50_slope_pct >= ema50_slope_min_pct
    # Vectorized version of scripts/analyze_win_factors.py's row-wise
    # hammer/dragonfly-doji classification (same thresholds) -- small
    # real body, long real lower wick (>=2x body), minimal real upper
    # wick, independent of close color.
    reversal_wick_shape_ok = None
    if allow_reversal_wick_shapes:
        body = (real_close - real_open).abs()
        oc_max = pd.concat([real_open, real_close], axis=1).max(axis=1)
        oc_min = pd.concat([real_open, real_close], axis=1).min(axis=1)
        upper_wick = real_high - oc_max
        lower_wick = oc_min - real_low
        candle_range = (real_high - real_low).replace(0, np.nan)
        dragonfly = ((body <= 0.1 * candle_range) & (lower_wick / candle_range >= 0.6)
                    & (upper_wick / candle_range <= 0.15))
        hammer = (body > 0) & (lower_wick >= 2 * body) & (upper_wick <= body)
        reversal_wick_shape_ok = (dragonfly | hammer).fillna(False)

    confirmed = [False] * n
    sig_high_out = [float("nan")] * n
    sig_low_out = [float("nan")] * n
    trigger_price_out = [float("nan")] * n

    state = "HUNTING"
    sig_high = sig_low = None
    days_pending = 0
    warmup = max(ema21_period, rsi_period) + 1

    for i in range(warmup, n):
        e13, e21 = ema13.iloc[i], ema21.iloc[i]
        if pd.isna(e13) or pd.isna(e21):
            continue
        hc, ho, hh, hl = ha_close.iloc[i], ha_open.iloc[i], ha_high.iloc[i], ha_low.iloc[i]

        if state == "LOCKED_OUT":
            if hc > e13:
                state = "HUNTING"
            continue

        if state == "PENDING":
            if hc < e21:
                state, sig_high, sig_low, days_pending = "LOCKED_OUT", None, None, 0
                continue
            trigger_level = sig_high * (1 + breakout_threshold_pct)
            if confirm_on_close:
                rc = real_close.iloc[i]
                crossed = not pd.isna(rc) and rc >= trigger_level
                fill_price = float(rc) if crossed else None
            else:
                rh = real_high.iloc[i]
                crossed = not pd.isna(rh) and rh >= trigger_level
                fill_price = trigger_level if crossed else None
            if crossed:
                confirmed[i] = True
                sig_high_out[i] = sig_high
                sig_low_out[i] = sig_low
                trigger_price_out[i] = fill_price
                state, sig_high, sig_low, days_pending = "HUNTING", None, None, 0
                continue
            days_pending += 1
            if days_pending >= confirm_lookback_days:
                state, sig_high, sig_low, days_pending = "HUNTING", None, None, 0
            continue

        # HUNTING (sig_high/sig_low double as "is a run currently being
        # built" -- non-None here means at least one qualifying candle
        # has been seen since the last commit/reset, still extending)
        if hc < e21:
            state, sig_high, sig_low = "LOCKED_OUT", None, None
            continue
        r = rsi.iloc[i]
        # 2026-08-23 explicit request: drop the red-candle requirement
        # (candle color no longer matters) and make the close-above-EMA
        # check toggleable between EMA13 and EMA21 via
        # signal_close_above_ema13 (was: hc < ho and hc > e21, hardcoded).
        # The touch condition (hl <= e21) and the HA_high > EMA13
        # condition are unchanged either way, per "keep the rest thing as
        # is" -- so is everything else in this function (LOCKED_OUT still
        # keys off hc < e21, PENDING/confirmation logic untouched).
        close_gate_level = e13 if signal_close_above_ema13 else e21
        is_signal_candle = (hl <= e21 and hh > e13 and hc > close_gate_level
                           and not pd.isna(r) and r > signal_rsi_min)
        if is_signal_candle and volume_ema is not None:
            v_ema = volume_ema.iloc[i]
            is_signal_candle = not pd.isna(v_ema) and float(real_volume.iloc[i]) > v_ema
        if is_signal_candle and volume_sma is not None:
            v_sma = volume_sma.iloc[i]
            is_signal_candle = not pd.isna(v_sma) and float(real_volume.iloc[i]) > v_sma
        if is_signal_candle and prior_rsi_ok is not None:
            p_ok = prior_rsi_ok.iloc[i]
            is_signal_candle = bool(p_ok) if not pd.isna(p_ok) else False
        if is_signal_candle and prior_above_ema13_ok is not None:
            pa_ok = prior_above_ema13_ok.iloc[i]
            is_signal_candle = bool(pa_ok) if not pd.isna(pa_ok) else False
        if is_signal_candle and prior_tiered_ok is not None:
            pt_ok = prior_tiered_ok.iloc[i]
            is_signal_candle = bool(pt_ok) if not pd.isna(pt_ok) else False
        if is_signal_candle and prior_ema50_no_violation_ok is not None:
            pe50_ok = prior_ema50_no_violation_ok.iloc[i]
            is_signal_candle = bool(pe50_ok) if not pd.isna(pe50_ok) else False
        if is_signal_candle and ema50_slope_ok is not None:
            es_ok = ema50_slope_ok.iloc[i]
            is_signal_candle = bool(es_ok) if not pd.isna(es_ok) else False
        if is_signal_candle and require_ema13_above_ema21:
            is_signal_candle = e13 > e21
        if is_signal_candle and require_ha_ema_stack:
            e50s, e200s = ema50_ha_stack.iloc[i], ema200_ha_stack.iloc[i]
            is_signal_candle = (not pd.isna(e50s) and not pd.isna(e200s)
                               and e13 > e21 > e50s > e200s)
        if is_signal_candle and require_real_green:
            real_green = float(real_close.iloc[i]) > float(real_open.iloc[i])
            if reversal_wick_shape_ok is not None:
                is_signal_candle = real_green or bool(reversal_wick_shape_ok.iloc[i])
            else:
                is_signal_candle = real_green
        if is_signal_candle and sector_gate_ok is not None:
            sg_ok = sector_gate_ok.iloc[i]
            is_signal_candle = bool(sg_ok) if not pd.isna(sg_ok) else False
        if is_signal_candle and watchlist_membership is not None:
            today_date = ha_close.index[i]
            is_signal_candle = bool(watchlist_membership.get(today_date, False))
        if is_signal_candle:
            sig_high = hh
            if sig_low is None:
                sig_low = hl
            elif stop_uses_run_low:
                sig_low = min(sig_low, hl)
            else:
                sig_low = hl  # latest candle's own low only, ignore the rest of the run
        elif sig_high is not None:
            # The run just ended. Check TODAY (the candle that broke the
            # run) against the crossing price immediately, same as any
            # other day would be checked -- 2026-08-22 fix: without this,
            # a candle that fails to extend the run but ALSO happens to
            # cross the trigger level that same day would silently wait
            # until tomorrow instead of confirming right away (caught via
            # a real traced trade, ALKEM 2026-07-29: that day's real high
            # already crossed the trigger level, but it wasn't checked at
            # all until the following day under the old logic). Not
            # confirmed today -> commit to PENDING as before, waiting
            # from tomorrow.
            trigger_level = sig_high * (1 + breakout_threshold_pct)
            if confirm_on_close:
                rc = real_close.iloc[i]
                crossed_today = not pd.isna(rc) and rc >= trigger_level
                fill_price_today = float(rc) if crossed_today else None
            else:
                rh = real_high.iloc[i]
                crossed_today = not pd.isna(rh) and rh >= trigger_level
                fill_price_today = trigger_level if crossed_today else None
            if crossed_today:
                confirmed[i] = True
                sig_high_out[i] = sig_high
                sig_low_out[i] = sig_low
                trigger_price_out[i] = fill_price_today
                sig_high, sig_low = None, None
            else:
                # 2026-08-23 fix: today's same-day check now counts as the
                # FIRST day against confirm_lookback_days -- it used to be
                # a "free" check outside the budget (days_pending reset to
                # 0 here), silently allowing confirm_lookback_days+1 total
                # days to be checked instead of confirm_lookback_days.
                # Caught via explicit question: confirm_lookback_days=1
                # was still checking a SECOND day after the immediate next
                # candle already failed to confirm. If today alone already
                # exhausts the budget (confirm_lookback_days<=1), skip
                # PENDING entirely and go straight back to HUNTING.
                days_pending = 1
                if days_pending >= confirm_lookback_days:
                    state, sig_high, sig_low, days_pending = "HUNTING", None, None, 0
                else:
                    state = "PENDING"

    return pd.DataFrame({"confirmed_entry": confirmed, "signal_high": sig_high_out,
                        "signal_low": sig_low_out, "trigger_price": trigger_price_out},
                       index=ha.index)


def heikin_ashi_ema21_touch_entry(df_upto: pd.DataFrame, ha_upto: pd.DataFrame,
                                  precomputed_touch: pd.DataFrame,
                                  fill_at_close: bool = False) -> dict | None:
    """2026-08-22 (revised): per-day check for the EMA21-touch-then-wait
    pattern -- now JUST a lookup into `precomputed_touch` (see that
    function) for whether TODAY is a confirmed-breakout day. Explicit
    request: "once you found the signal candle... only look at [whether]
    entry price is crossing or not in next 10 days" -- so the day-of-
    entry RSI gate, volume gate, AND trend-stack (HA EMA13>21>50>200)
    gate that used to all live here were removed one by one (RSI
    2026-08-21; volume and trend-stack 2026-08-22). Every remaining gate
    (signal-candle RSI, watchlist-shortlist membership) is checked ONCE,
    at the moment a signal candle starts (see precompute_ema21_touch_
    signals) -- once PENDING, confirmation is purely mechanical: did
    price cross the level within the window, yes or no.

    2026-08-22: `fill_at_close` (False by default) -- True switches the
    fill to TODAY's real close instead of the exact crossing level, for
    a live design that only checks once daily AFTER market close (no
    continuous intraday price monitoring): a system that only learns
    "today's high crossed the trigger" once the day is already over can
    no longer fill at that intraday level -- the best it can honestly
    claim is that day's own close, the same convention heikin_ashi_
    trend_entry already uses. Found (on real data) to meaningfully hurt
    performance vs. the exact-crossing-price fill -- entering at a
    day's close means paying for whatever the stock already ran during
    that day, not the price at the moment it actually broke out.

    Returns {"entry_price": the exact crossing level (signal_high *
    (1+breakout_threshold_pct), precomputed as "trigger_price") OR
    today's real close if fill_at_close, "stop": the confirmed signal
    candle's HA low} or None if today isn't a confirmed day."""
    today = ha_upto.index[-1]
    if today not in precomputed_touch.index:
        return None
    row = precomputed_touch.loc[today]
    if not bool(row["confirmed_entry"]):
        return None

    entry_price = float(df_upto["close"].iloc[-1]) if fill_at_close else float(row["trigger_price"])
    return {"entry_price": entry_price, "stop": float(row["signal_low"])}


def sector_above_ema_ok(sector_df_upto: pd.DataFrame, ema_period: int = 200) -> bool:
    """2026-08-15: sector-level ABSOLUTE trend filter -- the sector index's
    own daily close must be above its own EMA(`ema_period`), as of `date`
    (sector_df_upto is already point-in-time truncated by the caller).

    Distinct from relative_strength_vs_sector (which only checks the STOCK
    is outperforming its sector, regardless of whether the sector itself is
    healthy) -- this instead checks the SECTOR is itself in a real uptrend.
    Added after tracing NIFTY IT/NIFTY REALTY's high loss rates back to
    entries firing while those sectors were already extended 30-60%+ off a
    6-month low (see monthly_trend_persistence_ok's docstring for the
    related stock-level finding) -- this is the sector-level analogue,
    explicit request after that trace."""
    if sector_df_upto is None or sector_df_upto.empty:
        return False
    close = sector_df_upto["close"]
    if len(close) < ema_period:
        return False
    ema_now = indicators.ema(close, ema_period)
    if pd.isna(ema_now.iloc[-1]):
        return False
    return float(close.iloc[-1]) > float(ema_now.iloc[-1])


def sector_not_overextended_ok(sector_df_upto: pd.DataFrame, lookback_months: int = 6,
                               max_pct_change: float = 25.0) -> bool:
    """2026-08-15: sector-level OVEREXTENSION filter -- rejects entries when
    the sector index has already run more than `max_pct_change`% over the
    trailing `lookback_months` months. Neither sector_above_ema_ok nor a
    sector-level EMA13/21/50/200 stack check catches this: a sector up
    30-60%+ in 6 months is DEFINITIONALLY above its own EMAs and fully
    stacked (verified on the real NIFTY IT/REALTY trades that motivated
    this -- both were checked and only 1-3 of 28 flagged trades would have
    been excluded by either of those, since a strong extended rally
    naturally produces a healthy-looking EMA structure). This is the
    metric that actually distinguishes "healthy uptrend" from "already run
    too far, too fast, due for a pause" -- both traits a trend-following
    entry can't otherwise tell apart.

    `max_pct_change=25.0` is calibrated off the REAL distribution of
    6-month sector returns across every tracked sector index (median
    +6.5%, 80th percentile ~22.5%, 95th percentile ~41.7%) -- roughly the
    80th percentile, not a number reverse-engineered to exclude IT/REALTY
    specifically."""
    if sector_df_upto is None or sector_df_upto.empty:
        return False
    close = sector_df_upto["close"]
    lookback_days = lookback_months * 21  # ~21 trading days/month
    if len(close) < lookback_days + 1:
        return False
    prior = float(close.iloc[-(lookback_days + 1)])
    if prior <= 0:
        return False
    pct_change = (float(close.iloc[-1]) / prior - 1) * 100
    return pct_change <= max_pct_change


def find_swing_for_fib(df_upto: pd.DataFrame, signal_date, lookback_days: int = 60):
    """2026-08-15: finds the up-leg (swing LOW -> swing HIGH) that the
    current pullback (whose signal candle sits at `signal_date`) is
    retracing -- the reference swing for Fibonacci retracement/extension
    levels. `df_upto` is real daily OHLC (point-in-time truncated).

    swing_high = the highest HIGH within the trailing `lookback_days`
    ending at signal_date (the peak right before the pullback started).
    swing_low = the lowest LOW that preceded swing_high, searched over the
    SAME lookback window ending at swing_high's own date -- the low the
    up-move started from.

    Returns (swing_low, swing_high) or None if no valid swing (e.g. not
    enough history, or the "high" isn't actually above the "low" --
    degenerate/flat data)."""
    if signal_date not in df_upto.index:
        return None
    window_high = df_upto.loc[:signal_date, "high"].tail(lookback_days)
    if window_high.empty:
        return None
    swing_high = float(window_high.max())
    swing_high_date = window_high.idxmax()

    window_low = df_upto.loc[:swing_high_date, "low"].tail(lookback_days)
    if window_low.empty:
        return None
    swing_low = float(window_low.min())

    if swing_high <= swing_low:
        return None
    return swing_low, swing_high


def fibonacci_stop_target(swing_low: float, swing_high: float, entry_price: float,
                          stop_fib_level: float = 0.786,
                          target_fib_extension: float = 1.618) -> tuple[float, float] | None:
    """2026-08-15: Fibonacci-based stop/target, given the swing
    find_swing_for_fib() found. `stop_fib_level` (default 0.786, the
    "78.6% retracement" institutional reference level) sets the stop BELOW
    that retracement of the swing -- i.e., the pullback is considered
    invalidated if it retraces deeper than this. `target_fib_extension`
    (default 1.618) projects the target as that multiple of the swing's
    range, extended beyond swing_high (a "161.8% extension").

    Returns (stop, target) or None if the computed levels don't make sense
    relative to entry_price (stop above entry, or target below entry --
    can happen with a very shallow/degenerate swing; fail closed rather
    than return an invalid bracket)."""
    diff = swing_high - swing_low
    stop = swing_high - stop_fib_level * diff
    target = swing_high + (target_fib_extension - 1.0) * diff
    if stop >= entry_price or target <= entry_price:
        return None
    return stop, target


def monthly_trend_persistence_ok(df_upto: pd.DataFrame, ema_period: int = 50,
                                 above_pct_lookback_months: int = 36,
                                 above_pct_min: float = 0.90,
                                 new_high_lookback_months: int = 24,
                                 new_high_window_months: int = 12,
                                 new_high_min_count: int = 6) -> bool:
    """2026-08-15: monthly-timeframe trend-QUALITY gate -- distinguishes a
    persistently trending stock (e.g. BSE: 5y of yearly closes climbing
    almost every year, 0 monthly-50EMA whipsaws, frequent fresh 12-month
    highs) from a choppy/range-bound one that merely sits above its own
    monthly 50 EMA without actually making progress (e.g. HINDZINC: flat
    2021-2023, one rally, then rolling over -- still ~93-100% above its
    monthly 50 EMA depending on the window, so EMA-position alone does NOT
    separate the two; verified on real data before adding this).

    Two conditions, both required:
      1. Monthly close > monthly EMA(`ema_period`) for at least
         `above_pct_min` of the trailing `above_pct_lookback_months` months
         -- filters out names that spend meaningful time BELOW their own
         monthly trend.
      2. At least `new_high_min_count` months, within the trailing
         `new_high_lookback_months`, where the monthly close made a new
         high vs. its own trailing `new_high_window_months` -- the actual
         discriminator found on real data (BSE: 11 such months in the last
         24; HINDZINC: 4, right at the universe median) -- this is what
         actually separates "genuinely still trending" from "parked above
         a lagging average without progress."

    `df_upto` is real daily OHLC (point-in-time, already `.loc[:date]`
    truncated by the caller) -- resampled to monthly here. The last
    (possibly partial, current-month-so-far) bar is real point-in-time
    information, not a look-ahead -- same as any other indicator computed
    on a truncated daily series."""
    monthly_close = df_upto["close"].resample("ME").last().dropna()
    min_needed = max(ema_period, new_high_lookback_months + new_high_window_months,
                     above_pct_lookback_months)
    if len(monthly_close) < min_needed:
        return False

    monthly_ema = indicators.ema(monthly_close, ema_period)
    above = monthly_close > monthly_ema
    above_recent = above.tail(above_pct_lookback_months)
    if float(above_recent.mean()) < above_pct_min:
        return False

    roll_max = monthly_close.rolling(new_high_window_months).max()
    is_new_high = monthly_close == roll_max
    recent_new_highs = is_new_high.tail(new_high_lookback_months)
    if int(recent_new_highs.sum()) < new_high_min_count:
        return False

    return True


def is_new_high(close: pd.Series, lookback_days: int) -> bool:
    """True if today's close is above the highest close of the trailing
    `lookback_days`, EXCLUDING today -- a genuine breakout above the prior
    range, not just "today happens to be the max of a window that includes
    itself"."""
    if len(close) < lookback_days + 1:
        return False
    window = close.iloc[:-1].tail(lookback_days)
    if window.empty:
        return False
    return bool(close.iloc[-1] > window.max())


def closed_strong(high: pd.Series, low: pd.Series, close: pd.Series,
                  max_upper_wick_pct: float = 0.30) -> bool:
    """True if today closed in the upper part of its own day's range --
    rejects an inverted-hammer/shooting-star breakout day where price
    spiked to a new high intraday but sellers pushed it back down before
    the close (a real ASIANPAINT 2026-07-29 case: high 2864 vs close
    2758.4 on a 2718.5-2864 range is a 72.6% upper wick -- is_new_high()
    alone can't see this, since it only looks at the closing PRICE level,
    not the candle's own shape). max_upper_wick_pct=0.30 means the close
    must sit in the top 70% of the day's high-low range."""
    if len(high) < 1:
        return False
    h, l, c = float(high.iloc[-1]), float(low.iloc[-1]), float(close.iloc[-1])
    day_range = h - l
    if day_range <= 0:
        return True  # no-range/doji day, nothing to reject on
    upper_wick_pct = (h - c) / day_range
    return upper_wick_pct <= max_upper_wick_pct


def breakout_hold_confirmed(close: pd.Series, high: pd.Series, low: pd.Series,
                            volume: pd.Series, breakout_lookback_days: int,
                            volume_avg_days: int, volume_multiple: float,
                            max_upper_wick_pct: float) -> bool:
    """Two-day breakout-then-hold pattern: YESTERDAY must have been a
    genuine breakout (new high vs its own trailing `breakout_lookback_
    days`, a strong non-rejected close, and volume confirmation -- the
    same three conditions is_new_high/closed_strong/volume_surge already
    check, just evaluated as of yesterday instead of today by slicing the
    series to end there), AND today's close must still be at or above
    yesterday's close -- confirms the breakout actually held instead of
    reversing the very next day (the ASIANPAINT 2026-07-29/30 failure
    mode this was added to filter out from a second angle), at the cost
    of entering one day later than the breakout candle itself."""
    if len(close) < breakout_lookback_days + 2:
        return False
    close_upto_yday = close.iloc[:-1]
    high_upto_yday = high.iloc[:-1]
    low_upto_yday = low.iloc[:-1]
    volume_upto_yday = volume.iloc[:-1]

    if not is_new_high(close_upto_yday, breakout_lookback_days):
        return False
    if not closed_strong(high_upto_yday, low_upto_yday, close_upto_yday, max_upper_wick_pct):
        return False
    if not volume_surge(volume_upto_yday, volume_avg_days, volume_multiple):
        return False

    return float(close.iloc[-1]) >= float(close.iloc[-2])


def trend_template_ok(close: pd.Series, ema_fast_period: int, ema_slow_period: int,
                      ema_slow_trend_days: int = 21) -> bool:
    """Simplified Minervini-style "trend template" health check, applied
    as an extra prerequisite before ANY trigger is allowed to fire (not
    just breakouts) -- stricter than the shared production trend_ok gate
    (which only checks price>ema50/ema200 and a 5-day ema50 slope):
      * price above both the fast and slow EMA
      * fast EMA above slow EMA (no bearish crossover)
      * slow EMA itself has been RISING over the trailing
        `ema_slow_trend_days` (~1 month by default) -- not just "price
        happens to be above a flat or declining long-term average."
    Simplified from Minervini's full 8-criteria template (skips the
    150-day MA and 52-week-low distance legs) -- 52-week-high proximity
    is already covered by the shared near_high_ok gate upstream."""
    if len(close) < ema_slow_period + ema_slow_trend_days + 1:
        return False
    ema_fast = indicators.ema(close, ema_fast_period)
    ema_slow = indicators.ema(close, ema_slow_period)
    price = float(close.iloc[-1])
    fast_now, slow_now = float(ema_fast.iloc[-1]), float(ema_slow.iloc[-1])
    slow_then = float(ema_slow.iloc[-1 - ema_slow_trend_days])
    return (price > fast_now and price > slow_now
           and fast_now > slow_now
           and slow_now > slow_then)


def volume_surge(volume: pd.Series, avg_days: int, multiple: float) -> bool:
    """True if today's volume exceeds `multiple` x the trailing `avg_days`
    average volume, with the average computed over the days BEFORE today
    (shifted) so today's own spike doesn't inflate its own baseline."""
    if len(volume) < avg_days + 1:
        return False
    baseline = volume.shift(1).rolling(avg_days).mean().iloc[-1]
    if pd.isna(baseline) or baseline <= 0:
        return False
    return bool(volume.iloc[-1] > multiple * baseline)


def ema_sloping_up(close: pd.Series, ema_period: int, slope_days: int) -> bool:
    """True if the EMA(ema_period) ITSELF is higher today than it was
    `slope_days` trading days ago -- a direct slope check on the pullback
    EMA, distinct from trend_template_ok's slope check (which only looks
    at the slow/~200 EMA)."""
    if len(close) < ema_period + slope_days + 1:
        return False
    ema_series = indicators.ema(close, ema_period)
    return float(ema_series.iloc[-1]) > float(ema_series.iloc[-1 - slope_days])


def is_hammer(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series,
             min_lower_wick_body_ratio: float = 2.0,
             max_upper_wick_body_ratio: float = 0.5) -> bool:
    """Classic bullish hammer on today's candle: a small body sitting
    near the TOP of the day's range, a lower wick at least
    `min_lower_wick_body_ratio`x the body (the long tail showing sellers
    were rejected intraday), and an upper wick no more than
    `max_upper_wick_body_ratio`x the body (not much given back after the
    reversal)."""
    o, h, l, c = float(open_.iloc[-1]), float(high.iloc[-1]), float(low.iloc[-1]), float(close.iloc[-1])
    day_range = h - l
    if day_range <= 0:
        return False
    body = abs(c - o)
    body_floor = max(body, day_range * 0.05)  # a near-zero doji body would make the ratio meaningless
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return (lower_wick >= min_lower_wick_body_ratio * body_floor
           and upper_wick <= max_upper_wick_body_ratio * body_floor)


def is_bullish_engulfing(open_: pd.Series, close: pd.Series) -> bool:
    """Today's real body (open-to-close) fully engulfs yesterday's real
    body, with yesterday a down (red) candle and today an up (green)
    candle -- the classic two-candle bullish reversal."""
    if len(close) < 2:
        return False
    y_open, y_close = float(open_.iloc[-2]), float(close.iloc[-2])
    t_open, t_close = float(open_.iloc[-1]), float(close.iloc[-1])
    yesterday_red = y_close < y_open
    today_green = t_close > t_open
    engulfs = t_open <= y_close and t_close >= y_open
    return yesterday_red and today_green and engulfs


def ema_swing_pullback_reversal(open_: pd.Series, high: pd.Series, low: pd.Series,
                                close: pd.Series, volume: pd.Series, ema_period: int,
                                drift_lookback_days: int, touch_tolerance_pct: float,
                                hammer_min_wick_ratio: float,
                                hammer_max_upper_wick_ratio: float,
                                require_declining_volume: bool) -> dict | None:
    """Full swing-trade "pullback to a rising EMA" pattern -- trend
    context, multi-day drift, a shallow touch, and a real reversal
    candle -- returning the price LEVELS needed for stop/target
    placement instead of just True/False:

      1. TREND: the EMA itself must be sloping up (ema_sloping_up) --
         "the 21 EMA line is sloping upward."
      2. PULLBACK: price drifted DOWN from a recent swing high over the
         trailing `drift_lookback_days` (yesterday's close below that
         window's highest high) -- optionally also requiring volume to
         have been declining through the drift (this window's later half
         averaging lower volume than its earlier half), matching
         "ideally accompanied by declining trading volume."
      3. TOUCH: today's low comes down to/slightly below today's EMA,
         within `touch_tolerance_pct` -- a shallow touch, not a violent
         breakdown through the average.
      4. TRIGGER CANDLE: today's close must have reclaimed back above
         the EMA, AND show real reversal shape -- a hammer, a bullish
         engulfing bar, or a close comfortably (not barely) above the
         EMA. Requiring the reclaim unconditionally (not just as one of
         the three options) guarantees price ends up above both the EMA
         and the eventual stop level.

    Returns None if the pattern didn't fire, else {"swing_low": lowest
    low in the drift window (for stop placement), "swing_high": highest
    high in the drift window (for the profit-target's "prior swing high"
    option), "ema_touch_level": today's EMA value (for the "beneath the
    touch point" stop alternative)}."""
    if len(close) < ema_period + drift_lookback_days + 2:
        return None
    if not ema_sloping_up(close, ema_period, max(3, drift_lookback_days // 2)):
        return None

    ema_series = indicators.ema(close, ema_period)
    window_high = high.iloc[-(drift_lookback_days + 1):-1]
    window_low = low.iloc[-(drift_lookback_days + 1):-1]
    window_volume = volume.iloc[-(drift_lookback_days + 1):-1]
    if window_high.empty:
        return None
    swing_high = float(window_high.max())
    swing_low = float(window_low.min())

    prior_close = float(close.iloc[-2])
    if not prior_close < swing_high:
        return None

    if require_declining_volume and len(window_volume) >= 4:
        half = len(window_volume) // 2
        early_avg = float(window_volume.iloc[:half].mean())
        late_avg = float(window_volume.iloc[half:].mean())
        if not (late_avg < early_avg):
            return None

    today_ema = float(ema_series.iloc[-1])
    today_low = float(low.iloc[-1])
    touched = today_ema * (1 - touch_tolerance_pct * 3) <= today_low <= today_ema * (1 + touch_tolerance_pct)
    if not touched:
        return None

    today_close = float(close.iloc[-1])
    if today_close <= today_ema:
        return None  # must have reclaimed the EMA, not just wicked below it

    hammer = is_hammer(open_, high, low, close, hammer_min_wick_ratio, hammer_max_upper_wick_ratio)
    engulfing = is_bullish_engulfing(open_, close)
    strong_reclaim = today_close > today_ema * 1.005
    if not (hammer or engulfing or strong_reclaim):
        return None

    return {"swing_low": swing_low, "swing_high": swing_high, "ema_touch_level": today_ema}


def higher_highs_and_lows(high: pd.Series, low: pd.Series, pivot_window: int = 3,
                          n_pivots: int = 2, lookback_days: int = 60) -> bool:
    """Lightweight swing-structure check: a bar is a pivot high if its
    high is the max within +/- pivot_window bars (pivot low similarly for
    lows), confirmed pivot_window bars after the fact (point-in-time
    safe -- a pivot isn't knowable until then). True if the last
    `n_pivots` confirmed pivot highs are ascending AND the last n_pivots
    confirmed pivot lows are ascending. An approximation, not a precise
    instrument -- noisy on short daily-bar windows by nature of what
    "swing structure" means."""
    window = high.tail(lookback_days)
    low_window = low.tail(lookback_days)
    if len(window) < pivot_window * 2 + 1:
        return False

    pivot_highs, pivot_lows = [], []
    # Only bars with pivot_window confirmed bars AFTER them are checkable
    # (need pivot_window MORE bars past the pivot to confirm it) -- stop
    # pivot_window before the end for that reason.
    for i in range(pivot_window, len(window) - pivot_window):
        h_slice = window.iloc[i - pivot_window:i + pivot_window + 1]
        l_slice = low_window.iloc[i - pivot_window:i + pivot_window + 1]
        if window.iloc[i] == h_slice.max():
            pivot_highs.append(float(window.iloc[i]))
        if low_window.iloc[i] == l_slice.min():
            pivot_lows.append(float(low_window.iloc[i]))

    if len(pivot_highs) < n_pivots or len(pivot_lows) < n_pivots:
        return False
    recent_highs = pivot_highs[-n_pivots:]
    recent_lows = pivot_lows[-n_pivots:]
    return (all(recent_highs[i] < recent_highs[i + 1] for i in range(len(recent_highs) - 1))
           and all(recent_lows[i] < recent_lows[i + 1] for i in range(len(recent_lows) - 1)))


def sector_market_filter_ok(sector_close: pd.Series, sector_high: pd.Series,
                            sector_low: pd.Series, ema_fast_period: int = 50,
                            ema_slow_period: int = 200) -> bool:
    """Broad-market health check applied at the SECTOR level (per the
    user's explicit substitution: "instead of nifty 50 we can consider
    the stock broader sector") instead of NIFTY 50:
      * sector index price above its own EMA50
      * sector EMA50 > EMA200
      * sector showing higher highs & higher lows (structure)
    "Skip long trades in bearish markets" = this whole check just
    returning False."""
    if len(sector_close) < ema_slow_period + 1:
        return False
    ema_fast = indicators.ema(sector_close, ema_fast_period)
    ema_slow = indicators.ema(sector_close, ema_slow_period)
    price = float(sector_close.iloc[-1])
    fast_now, slow_now = float(ema_fast.iloc[-1]), float(ema_slow.iloc[-1])
    return (price > fast_now and fast_now > slow_now
           and higher_highs_and_lows(sector_high, sector_low))


def is_inside_bar(high: pd.Series, low: pd.Series) -> bool:
    """Today's high/low fully contained within yesterday's high/low range
    -- a contraction/consolidation day."""
    if len(high) < 2:
        return False
    return (float(high.iloc[-1]) < float(high.iloc[-2])
           and float(low.iloc[-1]) > float(low.iloc[-2]))


def small_bodied_candle(open_: pd.Series, close: pd.Series, atr_now: float,
                        max_body_atr_ratio: float = 0.6) -> bool:
    """True if today's real body (open-to-close) is small relative to
    ATR -- "small-bodied candles" during the pullback, i.e. low-
    conviction selling, not a sharp breakdown."""
    if atr_now <= 0:
        return False
    body = abs(float(close.iloc[-1]) - float(open_.iloc[-1]))
    return body <= max_body_atr_ratio * atr_now


def gap_up_pct(open_: pd.Series, close: pd.Series) -> float:
    """Today's open vs. yesterday's close, as a fraction (0.05 = 5%
    gap up) -- for the "avoid gap-up >5% on entry" filter."""
    if len(close) < 2:
        return 0.0
    prev_close = float(close.iloc[-2])
    if prev_close <= 0:
        return 0.0
    return (float(open_.iloc[-1]) - prev_close) / prev_close


def bearish_engulfing(open_: pd.Series, close: pd.Series) -> bool:
    """Mirror of is_bullish_engulfing: today's real body fully engulfs
    yesterday's, with yesterday green and today red."""
    if len(close) < 2:
        return False
    y_open, y_close = float(open_.iloc[-2]), float(close.iloc[-2])
    t_open, t_close = float(open_.iloc[-1]), float(close.iloc[-1])
    yesterday_green = y_close > y_open
    today_red = t_close < t_open
    engulfs = t_open >= y_close and t_close <= y_open
    return yesterday_green and today_red and engulfs


def bearish_engulfing_near_highs(open_: pd.Series, high: pd.Series, close: pd.Series,
                                 near_high_lookback: int = 20,
                                 near_high_pct: float = 0.03) -> bool:
    """A bearish_engulfing() day occurring within `near_high_pct` of the
    trailing `near_high_lookback`-day high -- a reversal AT resistance,
    not just any red day."""
    if len(close) < near_high_lookback + 1:
        return False
    if not bearish_engulfing(open_, close):
        return False
    recent_high = float(high.tail(near_high_lookback).max())
    return float(close.iloc[-1]) >= recent_high * (1 - near_high_pct)


def ema_cross_below(close: pd.Series, ema_fast_period: int, ema_slow_period: int) -> bool:
    """True if the fast EMA was above the slow EMA yesterday and is at or
    below it today -- a fresh bearish crossover, for the "EMA21 crosses
    below EMA50" exit rule."""
    if len(close) < ema_slow_period + 2:
        return False
    ema_fast = indicators.ema(close, ema_fast_period)
    ema_slow = indicators.ema(close, ema_slow_period)
    was_above = float(ema_fast.iloc[-2]) > float(ema_slow.iloc[-2])
    now_at_or_below = float(ema_fast.iloc[-1]) <= float(ema_slow.iloc[-1])
    return was_above and now_at_or_below


def pullback_trigger_shape(open_: pd.Series, high: pd.Series, low: pd.Series,
                           close: pd.Series, hammer_min_wick_ratio: float,
                           hammer_max_upper_wick_ratio: float) -> str | None:
    """Checked on a single candle (the caller passes a series already
    sliced to end on the day being evaluated as the SIGNAL day): which of
    the three allowed institutional-strategy trigger shapes it matches,
    in priority order -- bullish engulfing, inside bar, hammer. None if
    it matches none of them."""
    if is_bullish_engulfing(open_, close):
        return "engulfing"
    if is_inside_bar(high, low):
        return "inside_bar"
    if is_hammer(open_, high, low, close, hammer_min_wick_ratio, hammer_max_upper_wick_ratio):
        return "hammer"
    return None


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    """Wilder's Average Directional Index -- trend STRENGTH (not
    direction; a strong downtrend also scores high). Not in indicators.py
    anywhere in this codebase; only needed here for the institutional
    strategy's quality score ("ADX > 25" = trending, not choppy)."""
    if len(close) < period * 2 + 1:
        return 0.0
    high_diff = high.diff()
    low_diff = -low.diff()
    plus_dm = pd.Series(np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0.0),
                        index=high.index)
    minus_dm = pd.Series(np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0.0),
                         index=high.index)

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()],
                   axis=1).max(axis=1)

    smoothed_tr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / smoothed_tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / smoothed_tr

    di_sum = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan)
    adx_series = dx.ewm(alpha=1 / period, adjust=False).mean()
    val = adx_series.iloc[-1]
    return float(val) if not pd.isna(val) else 0.0


def relative_strength_vs_sector(stock_close: pd.Series, sector_close: pd.Series,
                                lookback_days: int = 21) -> float:
    """Stock return minus sector-index return over the trailing
    `lookback_days`, in percentage points -- the same relative-strength
    formula indicators.relative_strength already uses for the production
    screener's rs_3m/rs_6m, just applied against the stock's own sector
    (per the user's substitution) instead of NIFTY 50, and reimplemented
    here (not imported) to keep this module self-contained and not
    require a full bench DataFrame shape."""
    if len(stock_close) < lookback_days + 1 or len(sector_close) < lookback_days + 1:
        return 0.0
    stock_ret = float(stock_close.iloc[-1] / stock_close.iloc[-1 - lookback_days] - 1)
    sector_ret = float(sector_close.iloc[-1] / sector_close.iloc[-1 - lookback_days] - 1)
    return (stock_ret - sector_ret) * 100


def institutional_quality_score(high: pd.Series, low: pd.Series, close: pd.Series,
                                volume: pd.Series, sector_close: pd.Series | None,
                                shape: str, adx_period: int, rs_lookback_days: int) -> float:
    """0-100 quality score for a setup that has ALREADY fired
    institutional_pullback_entry (called on it, not as a pre-filter),
    per the user's rubric:

      EMA alignment: 20        (already a hard prerequisite for the entry
      Rising EMA21: 10          to have fired at all -- always full marks
                                 here, since a setup that lacked either
                                 never reaches this function)
      ADX > 25: 15              (trend strength -- NOT already checked
                                 anywhere else in this strategy)
      Declining pullback volume: 15  (re-derived over a fixed trailing
                                 window here, independent of exactly which
                                 pullback depth institutional_pullback_
                                 entry matched)
      Bullish reversal candle: 15    (hammer/engulfing = full marks, a
                                 genuine reversal shape; inside_bar = half
                                 marks, a continuation/consolidation shape,
                                 weaker as a "reversal")
      Breakout volume > 1.5x avg (on the signal/trigger candle): 15
      Relative strength vs sector: 10  (outperforming by >2pp = full,
                                 outperforming at all = half, else 0)

    Points not otherwise earned are simply not added -- this always
    returns a plain 0-100 sum, no rounding/normalization games."""
    score = 30.0  # EMA alignment + rising EMA21, already hard-gated upstream

    if adx(high, low, close, adx_period) > 25:
        score += 15

    window_volume = volume.iloc[-7:-2]
    if len(window_volume) >= 4:
        half = len(window_volume) // 2
        if float(window_volume.iloc[half:].mean()) < float(window_volume.iloc[:half].mean()):
            score += 15

    if shape in ("hammer", "engulfing"):
        score += 15
    elif shape == "inside_bar":
        score += 7.5

    if volume_surge(volume.iloc[:-1], 20, 1.5):
        score += 15

    if sector_close is not None and not sector_close.empty:
        rs = relative_strength_vs_sector(close, sector_close, rs_lookback_days)
        if rs > 2.0:
            score += 10
        elif rs > 0:
            score += 5

    return score


def hammer_near_ema21_confirmed(open_: pd.Series, high: pd.Series, low: pd.Series,
                                close: pd.Series, ema_period: int,
                                hammer_min_wick_ratio: float,
                                hammer_max_upper_wick_ratio: float) -> dict | None:
    """Simplified single-candle "hammer at EMA21" pattern (2026-08-12
    revision, replacing institutional_pullback_entry's multi-day drift/
    pullback-structure search per explicit request to trim it down):

      SIGNAL (yesterday): a hammer candle (is_hammer) whose LOW is below
      its own EMA21 and whose CLOSE is above it -- touch-and-reclaim in
      ONE candle, not a multi-day drift.

      CONFIRMATION (today): today's CLOSE (not just an intraday high
      touch) must close above yesterday's high.

    Returns None if the pattern didn't fire, else {"trigger_low":
    yesterday's low (for the stop), "trigger_high": yesterday's high}."""
    if len(close) < ema_period + 2:
        return None
    ema_series = indicators.ema(close, ema_period)
    sig_low = float(low.iloc[-2])
    sig_high = float(high.iloc[-2])
    sig_close = float(close.iloc[-2])
    sig_ema = float(ema_series.iloc[-2])

    if not (sig_low < sig_ema and sig_close > sig_ema):
        return None
    if not is_hammer(open_.iloc[:-1], high.iloc[:-1], low.iloc[:-1], close.iloc[:-1],
                     hammer_min_wick_ratio, hammer_max_upper_wick_ratio):
        return None

    today_close = float(close.iloc[-1])
    if today_close <= sig_high:
        return None  # hasn't closed above the hammer's high yet

    return {"trigger_low": sig_low, "trigger_high": sig_high}


def institutional_hammer_entry(open_: pd.Series, high: pd.Series, low: pd.Series,
                               close: pd.Series, volume: pd.Series,
                               ema21_period: int, ema50_period: int, ema200_period: int,
                               hammer_min_wick_ratio: float,
                               hammer_max_upper_wick_ratio: float,
                               max_gap_up_pct: float = 0.05,
                               ema21_slope_days: int = 3) -> dict | None:
    """Simplified institutional entry (2026-08-12 revision): same STOCK
    SELECTION prerequisite as institutional_pullback_entry (EMA21>EMA50>
    EMA200, price above EMA21, EMA21 rising, stock HH/HL), same gap-up
    avoid-filter, but the pullback-structure-search + 3-trigger-shape
    block is REPLACED by hammer_near_ema21_confirmed -- hammer only, no
    multi-day drift/small-body/declining-volume search.

    Returns None if the pattern didn't fire, else {"entry_price": TODAY's
    close (the confirmation signal itself -- unlike the other trigger
    types, which fill at the trigger candle's high), "trigger_low": the
    hammer candle's low (for the stop)}."""
    if len(close) < ema200_period + 2:
        return None

    ema21 = indicators.ema(close, ema21_period)
    ema50 = indicators.ema(close, ema50_period)
    ema200 = indicators.ema(close, ema200_period)

    price_yday = float(close.iloc[-2])
    e21_yday, e50_yday, e200_yday = float(ema21.iloc[-2]), float(ema50.iloc[-2]), float(ema200.iloc[-2])
    stack_ok = e21_yday > e50_yday > e200_yday and price_yday > e21_yday
    if not stack_ok:
        return None
    if not ema_sloping_up(close.iloc[:-1], ema21_period, ema21_slope_days):
        return None
    if not higher_highs_and_lows(high.iloc[:-1], low.iloc[:-1]):
        return None

    hammer_result = hammer_near_ema21_confirmed(
        open_, high, low, close, ema21_period,
        hammer_min_wick_ratio, hammer_max_upper_wick_ratio)
    if hammer_result is None:
        return None

    if gap_up_pct(open_, close) > max_gap_up_pct:
        return None

    return {"entry_price": float(close.iloc[-1]), "trigger_low": hammer_result["trigger_low"]}


def institutional_pullback_entry(open_: pd.Series, high: pd.Series, low: pd.Series,
                                 close: pd.Series, volume: pd.Series,
                                 ema21_period: int, ema50_period: int, ema200_period: int,
                                 min_pullback_days: int, max_pullback_days: int,
                                 atr_now: float, max_body_atr_ratio: float,
                                 require_declining_volume: bool,
                                 hammer_min_wick_ratio: float,
                                 hammer_max_upper_wick_ratio: float,
                                 max_gap_up_pct: float = 0.05,
                                 retracement_max_distance_pct: float = 0.02) -> dict | None:
    """"EMA 21 Institutional Retracement Swing" stock-selection + pullback
    + entry-trigger rules (market filter and position sizing/exits are
    handled by the caller, not here):

      STOCK SELECTION: EMA21 > EMA50 > EMA200 (full stack alignment),
      price above EMA21, EMA21 rising, stock showing higher highs/higher
      lows (higher_highs_and_lows).

      PULLBACK (searched over the trailing min_pullback_days..
      max_pullback_days, i.e. "2-5 candle pullback"): price ACTUALLY
      RETRACED close to EMA21 (some day's low, across the drift window
      plus the signal candle, came within retracement_max_distance_pct
      of that day's EMA21 -- a real proximity check, not just "stayed
      above EMA50") while staying above EMA50 throughout (no close below
      EMA50 during the pullback window), with small-bodied candles
      (small_bodied_candle) and (optionally) declining volume through
      the drift.

      ENTRY TRIGGER: the day right before the pullback window's end
      (i.e. the most recent day, "yesterday" relative to today) must
      match one of the three trigger shapes (pullback_trigger_shape) --
      today then needs to have broken above THAT signal candle's high,
      "buy above trigger candle high". Entry fills at the signal
      candle's high, or today's open if it gapped above that level.

    Returns None if the pattern didn't fire, else {"entry_price": fill
    price, "trigger_low": the signal candle's low (for the stop),
    "shape": which trigger pattern fired}."""
    if len(close) < ema200_period + max_pullback_days + 2:
        return None

    ema21 = indicators.ema(close, ema21_period)
    ema50 = indicators.ema(close, ema50_period)
    ema200 = indicators.ema(close, ema200_period)

    price_yday = float(close.iloc[-2])
    e21_yday, e50_yday, e200_yday = float(ema21.iloc[-2]), float(ema50.iloc[-2]), float(ema200.iloc[-2])
    stack_ok = e21_yday > e50_yday > e200_yday and price_yday > e21_yday
    if not stack_ok:
        return None
    if not ema_sloping_up(close.iloc[:-1], ema21_period, max(3, min_pullback_days)):
        return None
    if not higher_highs_and_lows(high.iloc[:-1], low.iloc[:-1]):
        return None

    # signal candle = yesterday; entry trigger checked on yesterday's own
    # shape, confirmed by TODAY breaking its high.
    shape = pullback_trigger_shape(open_.iloc[:-1], high.iloc[:-1], low.iloc[:-1],
                                   close.iloc[:-1], hammer_min_wick_ratio,
                                   hammer_max_upper_wick_ratio)
    if shape is None:
        return None
    signal_high = float(high.iloc[-2])
    signal_low = float(low.iloc[-2])
    today_high = float(high.iloc[-1])
    if today_high < signal_high:
        return None  # hasn't broken above the trigger candle's high yet

    # pullback structure check: over the days BEFORE the signal candle
    # (2-5 candles), price stayed above EMA50, showed small bodies, and
    # ACTUALLY RETRACED CLOSE TO EMA21 (checked across that drift window
    # PLUS the signal candle itself, where the touch typically happens --
    # a bug caught on a real BHEL 2026-05-29 trade that entered ~6% above
    # its own EMA21 because this retracement proximity check was missing
    # entirely; the docstring claimed it existed, the code never checked
    # it) -- searched for the shortest window (min_pullback_days) that
    # satisfies it, growing up to max_pullback_days ("avoid pullback >
    # 8-10 candles" is enforced by capping max_pullback_days itself).
    pullback_ok = False
    for depth in range(min_pullback_days, max_pullback_days + 1):
        start = -1 - 1 - depth  # before the signal candle
        end = -1 - 1
        if abs(start) > len(close):
            break
        win_close = close.iloc[start:end]
        win_open = open_.iloc[start:end]
        win_ema50 = ema50.iloc[start:end]
        if win_close.empty:
            continue
        stayed_above_ema50 = bool((win_close > win_ema50).all())
        small_bodies = all(
            small_bodied_candle(win_open.iloc[i:i + 1], win_close.iloc[i:i + 1], atr_now, max_body_atr_ratio)
            for i in range(len(win_close)))
        volume_ok = True
        if require_declining_volume:
            win_volume = volume.iloc[start:end]
            if len(win_volume) >= 2:
                half = len(win_volume) // 2
                volume_ok = float(win_volume.iloc[half:].mean()) < float(win_volume.iloc[:half].mean())

        retrace_low = low.iloc[start:-1]  # drift window + the signal candle
        retrace_ema21 = ema21.iloc[start:-1]
        retraced_to_ema21 = bool((retrace_low <= retrace_ema21 * (1 + retracement_max_distance_pct)).any())

        if stayed_above_ema50 and small_bodies and volume_ok and retraced_to_ema21:
            pullback_ok = True
            break
    if not pullback_ok:
        return None

    today_open = float(open_.iloc[-1])
    if gap_up_pct(open_, close) > max_gap_up_pct:
        return None  # "avoid gap-up >5% on entry"

    entry_price = max(signal_high, today_open)
    return {"entry_price": entry_price, "trigger_low": signal_low, "shape": shape}


def ema_pullback_bounce(close: pd.Series, high: pd.Series, low: pd.Series,
                        ema_period: int, lookback_days: int) -> bool:
    """Two-candle trend-pullback continuation pattern (volume is checked
    separately by the caller, via volume_surge on today):

      1. SIGNAL candle (searched within the trailing `lookback_days`,
         excluding today): the day before IT closed above its own
         EMA(ema_period) (trend intact coming in), then the signal
         candle's LOW dips below the EMA but its CLOSE reclaims back
         above it -- a bullish EMA-reclaim candle, not just "some day
         got close to the average."
      2. CONFIRMATION: today's close breaks above that signal candle's
         HIGH -- the actual entry trigger, "pull back, then take out the
         signal candle's high on strong volume."

    Searches back through the window for the most recent qualifying
    signal candle whose high today's close actually clears, so a stale
    signal from days ago that was already broken (and failed to follow
    through) doesn't keep firing on unrelated later days."""
    if len(close) < ema_period + lookback_days + 2:
        return False
    ema_series = indicators.ema(close, ema_period)
    today_close = float(close.iloc[-1])

    for k in range(1, lookback_days + 1):
        sig_pos = -1 - k
        prior_pos = sig_pos - 1
        if abs(prior_pos) > len(close):
            break
        sig_low = float(low.iloc[sig_pos])
        sig_high = float(high.iloc[sig_pos])
        sig_close = float(close.iloc[sig_pos])
        sig_ema = float(ema_series.iloc[sig_pos])
        prior_close = float(close.iloc[prior_pos])
        prior_ema = float(ema_series.iloc[prior_pos])

        prior_uptrend = prior_close > prior_ema
        signal_reclaim = sig_low < sig_ema and sig_close > sig_ema
        if prior_uptrend and signal_reclaim and today_close > sig_high:
            return True
    return False
