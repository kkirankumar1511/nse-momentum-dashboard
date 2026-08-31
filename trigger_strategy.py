"""
Trigger detection and ATR-risk position sizing for the experimental
triggered-entry backtest (backtest_triggered.py). Local-only research —
not imported by the production engine (backtest.py) and not wired into
dashboard.py.

Replaces the production engine's "rank top-N, buy all via equal weight at
the same close" entry with: filter/rank the universe exactly as today
(same gates, same config, unchanged), then wait for one of four concrete
technical triggers before entering each name, checked in this priority
order per stock per day (first match wins):

  a) multi-year breakout, with volume confirmation
  b) short-term (1-2 month) breakout, with volume confirmation
  c) pullback-and-bounce off the 50 EMA, with volume confirmation
  d) pullback-and-bounce off the 21 EMA, with volume confirmation

Each of the 4 can be toggled on/off independently (cfg["multiyear_
breakout_enabled"], "shortterm_breakout_enabled", "pullback_slow_ema_
enabled", "pullback_fast_ema_enabled") for isolating one pattern at a time
during testing -- all default True (full behavior).

See the plan this was built from for the reasoning behind each default
below (in particular the position-sizing interpretation — 0.50% max loss
of TOTAL portfolio equity, matching the existing risk_per_trade_pct
convention in screener.position_size(); a per-slot-capital reading was
tried first and mechanically confirmed to starve every trade down to
~8-15% of its equal-weight size, since a typical 2xATR stop is 5-8% away
from entry -- the risk cap dominated almost every fill).
"""
from __future__ import annotations

import pandas as pd

import config
import indicators
import trigger_indicators as ti

# Starts from a full copy of the production STRATEGY config (so every
# existing filter/gate default -- near_high_threshold, rsi bounds,
# fundamental gate, etc. -- carries over unchanged, per "all stock filter
# strategy should be same as per current config only"), then layers on the
# new trigger/sizing/stop parameters, overriding a few production defaults
# (max_positions, atr_stop_multiple, trailing_atr_multiple) with the
# values this strategy explicitly asked for.
TRIGGERED_DEFAULTS: dict = {
    **config.STRATEGY,

    "watchlist_size": 10,
    # 2026-08-22: 0 (default) reproduces existing behavior exactly --
    # the watchlist rebalance step ranks/gates using TODAY's own close.
    # Set >0 to rank/gate using the close from that many trading days
    # BEFORE today instead, while still applying the resulting watchlist
    # to today's trigger scan -- validates a live/intraday design where
    # the watchlist has to be built from the last FULLY KNOWN day
    # (yesterday's close), since today's own close-based gates aren't
    # actually knowable until after today's market close. See
    # backtest_triggered.py's rebalance-step comment.
    "watchlist_lag_days": 0,

    "multiyear_lookback_days": 756,      # ~3 trading years
    "shortterm_lookback_days": 42,       # ~2 months
    "breakout_volume_avg_days": 20,
    "breakout_volume_multiple": 1.5,
    # Rejects an inverted-hammer/shooting-star breakout day -- close must
    # sit in the top (1 - this) fraction of the day's high-low range. See
    # trigger_indicators.closed_strong's docstring for the real ASIANPAINT
    # 2026-07-29 case (close only in the bottom 27% of its range) this was
    # added specifically to filter out.
    "breakout_max_upper_wick_pct": 0.30,

    "pullback_ema_fast": 21,
    "pullback_ema_slow": 50,
    "pullback_lookback_days": 5,
    "pullback_volume_avg_days": 20,
    "pullback_volume_multiple": 1.3,

    # "EMA 21 Institutional Retracement Swing" rules (2026-08-12 spec,
    # simplified 2026-08-12 same day per explicit follow-up request), now
    # what the FAST/21 EMA pullback trigger runs -- see
    # trigger_indicators.institutional_hammer_entry's docstring for the
    # current rule set: hammer-only entry (no engulfing/inside-bar, no
    # multi-day pullback-structure search), stop = the hammer candle's
    # own low, target = 1:3 risk-reward, no quality-score gate. Supersedes
    # BOTH institutional_pullback_entry (the fuller 3-trigger-shape
    # version) and the even earlier ema_swing_pullback_reversal -- both
    # still defined, no longer wired to any trigger.
    #
    # NOT faithfully implemented, approximated instead (flagged, not
    # silently dropped):
    #   * "book 30%/30%/trail rest" partial scaling -- the engine has no
    #     partial-exit position model, so this uses a single fixed target
    #     for the whole position instead (profit_target_rr below).
    #   * "volume climax" exit -- underspecified, not implemented.
    #   * earnings-within-2-3-days avoid-filter -- no earnings calendar
    #     data available locally, not implemented.
    "institutional_ema21_period": 21,
    "institutional_max_gap_up_pct": 0.05,
    "hammer_min_lower_wick_ratio": 2.0,
    "hammer_max_upper_wick_ratio": 0.5,
    "institutional_rs_lookback_days": 21,   # still used by the hard RS-vs-sector gate
    "profit_target_rr": 3.0,                # 1:3, per explicit request (was 1:2)

    # Below: only used by institutional_pullback_entry (the superseded
    # fuller version) and institutional_quality_score (removed from the
    # active path) -- kept defined so those functions still run if called
    # directly, not read by the current hammer-only trigger.
    "institutional_min_pullback_days": 2,
    "institutional_max_pullback_days": 5,
    "institutional_max_body_atr_ratio": 0.6,
    "institutional_require_declining_volume": True,
    "institutional_retracement_max_distance_pct": 0.02,
    "institutional_adx_period": 14,
    "institutional_min_quality_score": 80.0,
    "institutional_stop_atr_multiple": 1.0,
    # "Risk a maximum of 1% of total capital per trade" -- reuses the
    # SAME max_loss_pct_per_trade key trigger_position_size() already
    # applies to every trigger type (currently 0.50, see that key's own
    # comment); overridden to 1.0 specifically for isolated institutional-
    # trigger testing via the CLI script's TRIGGER_ISOLATION_OVERRIDE,
    # since only one trigger type is ever active during that testing.

    # Kept for the older (no longer wired) ema_swing_pullback_reversal
    # pattern -- not used by the institutional entry above.
    "pullback_drift_lookback_days": 8,
    "pullback_touch_tolerance_pct": 0.01,
    "require_declining_pullback_volume": False,
    "pullback_stop_buffer_pct": 0.005,
    "profit_target_mode": "swing_high",     # "swing_high" | "risk_reward"

    # Per-trigger on/off switches, for isolating one pattern at a time
    # during testing -- all True reproduces the full 4-trigger behavior.
    "multiyear_breakout_enabled": True,
    "shortterm_breakout_enabled": True,
    "pullback_slow_ema_enabled": True,
    "pullback_fast_ema_enabled": True,

    # Minervini-style trend-template pre-filter (indicators.trend_template_
    # ok) -- how many trading days back to check the slow EMA was rising
    # over, ~1 month by default. Applied as a hard prerequisite before ANY
    # of the 4 triggers below.
    "trend_template_ema_slow_trend_days": 21,

    "max_positions": 5,
    "atr_stop_multiple": 2.0,
    "trailing_stop_enabled": True,
    "trailing_atr_multiple": 2.0,
    "max_loss_pct_per_trade": 0.50,

    # Heikin-Ashi trend entry (2026-08-13 spec) -- an INDEPENDENT trigger
    # path, checked before and separately from everything above (does NOT
    # go through trend_template_ok or the sector market filter -- neither
    # is part of this spec). Off by default; the CLI script's isolation
    # override turns this on AND disables all 4 triggers above for a
    # clean, single-strategy test. See trigger_indicators.
    # heikin_ashi_trend_entry's docstring for the full rule set: HA
    # EMA13>21>50>200, HA RSI(14)>60, most-recent red HA candle as
    # signal, today's REAL close breaking its HA high as confirmation,
    # entry day's REAL volume > its own 50-day SMA. Stop = signal
    # candle's HA low, target = fixed risk-reward (profit_target_rr,
    # reused from above -- 1:2 per this spec's request).
    "heikin_ashi_enabled": False,
    "ha_ema13_period": 13,
    "ha_ema21_period": 21,
    "ha_ema50_period": 50,
    "ha_ema200_period": 200,
    "ha_rsi_period": 14,
    "ha_rsi_min": 60.0,
    "ha_signal_lookback_days": 10,
    "ha_volume_sma_period": 50,
    # 2026-08-13: stop can be the signal candle's HA low (spec's original
    # point 9) or ATR-based off the real entry price -- "ha_low" keeps
    # the original spec exactly; "atr" tests whether a volatility-scaled
    # stop (reusing the same atr_period as every other trigger type)
    # performs better than the fixed HA-low reference.
    "ha_stop_mode": "ha_low",           # "ha_low" | "atr" | "fibonacci"
    "ha_stop_atr_multiple": 2.0,
    # 2026-08-15: "fibonacci" ha_stop_mode -- finds the swing (low->high)
    # the current pullback is retracing, sets stop below the
    # fib_stop_level retracement (0.786 = 78.6%, the institutional
    # reference level per practitioner sources) and target at the
    # fib_target_extension (1.618 = 161.8%) extension of that swing beyond
    # the high. See trigger_indicators.find_swing_for_fib/
    # fibonacci_stop_target docstrings. Explicit request to test this
    # against the existing ATR-based stop/target.
    "fib_swing_lookback_days": 60,
    "fib_stop_level": 0.786,
    "fib_target_extension": 1.618,
    # 2026-08-14: True keeps a fixed/symmetric profit target alongside the
    # stop (whichever hits first exits); False = pure trail-only, no
    # profit cap at all -- ride the position until the daily-trailing
    # stop eventually catches it, per explicit request after a real
    # NATIONALUM trade exited at a fixed ATR*2 target instead of trailing.
    "ha_target_enabled": True,

    # 2026-08-15: monthly trend-persistence gate -- distinguishes a
    # persistently trending stock from one that merely sits above its own
    # monthly 50 EMA without making real progress (see trigger_indicators.
    # monthly_trend_persistence_ok's docstring -- verified on real BSE vs
    # HINDZINC data: EMA-position alone doesn't separate them, new-12m-high
    # frequency does). Off by default, non-destructive; applied inside
    # detect_trigger's Heikin-Ashi branch only.
    "monthly_trend_persistence_enabled": False,
    "monthly_trend_ema_period": 50,
    "monthly_trend_above_pct_lookback": 36,
    "monthly_trend_above_pct_min": 0.90,
    "monthly_trend_new_high_lookback": 24,
    "monthly_trend_new_high_window": 12,
    "monthly_trend_new_high_min_count": 6,

    # 2026-08-15: sector-level ABSOLUTE trend filter -- the sector index's
    # own close must be above its own 200 EMA, not just the stock
    # outperforming a possibly-unhealthy sector (relative_strength_vs_
    # sector only checks relative performance, not sector health itself).
    # See trigger_indicators.sector_above_ema_ok's docstring -- explicit
    # request after tracing NIFTY IT/REALTY's high loss rates to entries
    # firing while those sectors were already 30-60%+ extended.
    "sector_above_ema_enabled": False,
    "sector_above_ema_period": 200,

    # 2026-08-15: sector-level OVEREXTENSION filter -- rejects entries when
    # the sector has already run more than `sector_overext_max_pct_change`%
    # over the trailing `sector_overext_lookback_months` months. See
    # trigger_indicators.sector_not_overextended_ok's docstring -- this is
    # the metric that actually explains NIFTY IT/REALTY's high loss rates
    # (both were already 25-67% into a 6-month rally at nearly every
    # entry), unlike the EMA-based gates above which a strong rally
    # naturally satisfies regardless of how extended it already is.
    "sector_overextension_enabled": False,
    "sector_overext_lookback_months": 6,
    "sector_overext_max_pct_change": 25.0,

    # 2026-08-15: new, INDEPENDENT Heikin-Ashi entry pattern -- same-day
    # EMA21-pullback-bounce (see trigger_indicators.
    # heikin_ashi_ema21_bounce_entry's docstring), explicit spec: same
    # stock selection, HA EMA13>21>50>200 stack, today's HA low touches
    # EMA21 while HA close holds >= EMA13, HA RSI > 58, stop = today's HA
    # low, target = fixed 1:2 R:R. Off by default; mutually exclusive in
    # practice with heikin_ashi_enabled (both are independent branches in
    # detect_trigger, checked in order -- enable only one at a time for a
    # clean test, matching this strategy's own established convention).
    "ha_ema21_bounce_enabled": False,
    "ha_ema21_bounce_rsi_min": 58.0,
    "ha_ema21_bounce_target_rr": 2.0,

    # 2026-08-16: breakeven-then-wide-trail stop, explicit request -- keep
    # the initial ATR*1 stop FIXED (no ratchet at all) until the position
    # reaches `ha_breakeven_trigger_r` (1.0 = 1:1) times its own initial
    # risk in unrealized profit; at that point jump the stop straight to
    # breakeven (entry price) and THEN start the normal daily ATR
    # chandelier trail, but with the WIDER `ha_breakeven_trail_atr_
    # multiple` (2.0) instead of the tighter initial multiple -- gives
    # winners more room once they've proven themselves, addresses the
    # earlier finding that many losing trades had already reached 0.5-1R+
    # profit before reversing under the old immediate-ATR*1-trail. Applied
    # in backtest_triggered.py's step 1b, non-institutional positions
    # only. Off by default.
    "ha_breakeven_trail_enabled": False,
    "ha_breakeven_trigger_r": 1.0,
    "ha_breakeven_trail_atr_multiple": 2.0,

    # 2026-08-21: new, INDEPENDENT Heikin-Ashi entry pattern -- EMA21-
    # touch-then-wait-for-breakout (see trigger_indicators.
    # precompute_ema21_touch_signals/heikin_ashi_ema21_touch_entry's
    # docstrings), explicit spec: same stock selection + sector gates as
    # heikin_ashi_enabled, but its OWN entry pattern -- a multi-day state
    # machine (HUNTING for a signal candle whose HA low touches EMA21
    # while its HA high pokes above EMA13 and its HA close holds above
    # EMA21, then PENDING up to ha_ema21_touch_confirm_days for the real
    # intraday HIGH to cross the signal's HA high by a small threshold,
    # cancelled early if HA close drops below EMA21 first). Stop =
    # signal candle's HA low, target = fixed 1:2 R:R (per spec, "make
    # stop as 1:2 target and see the result first" -- not trail-only
    # like heikin_ashi_enabled). Off by default; mutually exclusive in
    # practice with the other two HA patterns (all three are independent
    # branches in detect_trigger, checked in order -- enable only one at
    # a time for a clean test).
    #
    # 2026-08-21 revision, explicit request: (1) the day-of-entry RSI
    # gate that used to exist here was REMOVED entirely -- the signal
    # candle's own RSI gate (below) is now the only RSI check in this
    # pattern; (2) entry price is the exact crossing level (signal_high
    # * (1+breakout_threshold_pct)), not that day's real close; (3) the
    # signal candle's own RSI floor raised 50 -> 55, then reverted back
    # to 50 (2026-08-22, explicit request).
    "ha_ema21_touch_enabled": False,
    "ha_ema21_touch_signal_rsi_min": 50.0,   # the ONLY RSI gate in this pattern
    "ha_ema21_touch_confirm_days": 10,       # N -- how long a signal stays PENDING
    "ha_ema21_touch_target_rr": 2.0,
    # 2026-08-21 revision: confirmation is a crossing check against the
    # day's REAL HIGH (high >= signal_high * (1+this)), not a close-
    # above-level requirement -- see trigger_indicators.
    # precompute_ema21_touch_signals' docstring.
    "ha_ema21_touch_breakout_threshold_pct": 0.001,
    # 2026-08-22: when multiple consecutive candles each independently
    # qualify as a signal candle (a multi-day run), True (default) uses
    # the LOWEST HA low across the whole run as the stop; False uses
    # just the LATEST qualifying candle's own HA low, kept as an
    # explicit alternative to compare against.
    "ha_ema21_touch_stop_uses_run_low": True,
    # 2026-08-22: optional extra filter on the SIGNAL candle itself --
    # None (default) means no gate. When set, the signal candle's own
    # real volume must be above its trailing EMA of real volume over
    # this many days. Distinct from (not a revival of) the day-of-entry
    # volume gate removed from heikin_ashi_ema21_touch_entry -- see
    # trigger_indicators.precompute_ema21_touch_signals' docstring for
    # why that one had to go but this one is fine.
    "ha_ema21_touch_signal_volume_ema_period": None,
    # 2026-08-22: False (default) fills at the exact crossing level
    # (signal_high * (1+breakout_threshold_pct)) -- assumes continuous
    # intraday price monitoring. True fills at TODAY's real close
    # instead, for a live design that only checks once daily after
    # market close -- see trigger_indicators.heikin_ashi_ema21_touch_
    # entry's docstring for why this replaces the lagged-watchlist
    # alternative (found to lose most of this pattern's trades).
    "ha_ema21_touch_fill_at_close": False,
    # 2026-08-22 addition: optional extra filter on the SIGNAL candle --
    # None (default) means no gate. When set, at least one of the prior
    # N HA candles (strictly before the signal candle) must have had HA
    # RSI above ha_ema21_touch_prior_rsi_min -- lets a stock qualify on
    # recently-shown momentum even though a pullback (the signal candle
    # itself, by construction) drags its OWN HA RSI down. See
    # trigger_indicators.precompute_ema21_touch_signals' docstring.
    "ha_ema21_touch_prior_rsi_lookback_days": None,
    "ha_ema21_touch_prior_rsi_min": 60.0,
    # 2026-08-22 addition: plain trailing SMA (not EMA) of real volume --
    # a second, independent optional signal-candle volume filter, kept
    # separate from ha_ema21_touch_signal_volume_ema_period so both can
    # be compared. None (default) = no gate.
    "ha_ema21_touch_signal_volume_sma_period": None,
    # 2026-08-23 addition: optional extra filter on the SIGNAL candle --
    # None (default) means no gate. When set, at least one of the prior
    # N HA candles (strictly before the signal candle) must have had BOTH
    # its open AND close above HA EMA13. A second, independent recent-
    # strength check alongside ha_ema21_touch_prior_rsi_lookback_days --
    # see trigger_indicators.precompute_ema21_touch_signals' docstring.
    "ha_ema21_touch_prior_above_ema13_lookback_days": None,
    # 2026-08-23 addition: the signal candle's own close-above-EMA gate --
    # True (default) checks HA EMA13, False checks HA EMA21 (the original
    # spec before the 2026-08-23 loosening). Kept toggleable since both
    # are still being compared.
    "ha_ema21_touch_signal_close_above_ema13": True,
    # 2026-08-23 addition: independently toggle each sector gate between
    # "live, rechecked every day through confirmation" (False, the
    # original behavior) and "checked once at signal-candle formation
    # only" (True) -- see detect_trigger's ha_ema21_touch branch for the
    # A/B test result that made both-True clearly worse than both-False,
    # motivating isolating them to find out which one actually matters.
    "ha_ema21_touch_sector_rs_formation_only": False,
    "ha_ema21_touch_sector_above_ema_formation_only": False,
    # 2026-08-23 addition: the signal candle's REAL (non-HA) close must be
    # above its REAL open (an ordinary green daily candle) on top of the
    # HA-based shape check, which no longer cares about color at all.
    # True (default, since 2026-08-23) -- validated at both smoke (0.6y:
    # 24->21 trades, win rate 54.2%->57.1%, alpha +37.94%->+38.76%) and
    # full 5yr scale (CAGR 11.23%->13.97%, alpha +3.44%->+6.19%, on top
    # of the prior_rsi_lookback_days=20 config) -- a rare gate this
    # session that held up at 5yr instead of regressing. See
    # trigger_indicators.precompute_ema21_touch_signals' docstring for
    # the original post-hoc finding motivating it (real-green signal
    # candles win 46.5% vs 38.6% for real-red ones).
    "ha_ema21_touch_require_real_green": True,
    # 2026-08-23 addition, off by default -- if set, the signal candle's
    # REAL-close EMA50 must have risen by ha_ema21_touch_ema50_slope_min_
    # pct over the prior N trading days. See trigger_indicators.
    # precompute_ema21_touch_signals' docstring for the post-hoc finding
    # motivating this (EMA50 up >=6% over 20 days: 50.0% win/+2.84% avg
    # vs 41.8% win/+1.15% avg for the rest -- below ~5-6% the slope
    # carries no signal). Not yet validated at 5yr scale.
    "ha_ema21_touch_ema50_slope_lookback_days": None,
    "ha_ema21_touch_ema50_slope_min_pct": 5.0,
    # 2026-08-24 addition, off by default -- only takes effect alongside
    # require_real_green=True. Widens that gate to also accept a red
    # hammer/dragonfly-doji (long lower wick, rejection of lower prices)
    # as a valid signal candle, not just a plain green close. See
    # trigger_indicators.precompute_ema21_touch_signals' docstring --
    # not yet validated at scale (post-hoc sample was only 14-21 trades).
    # 2026-08-24 addition, off by default -- confirmation crosses on REAL
    # CLOSE closing above the trigger level instead of REAL HIGH crossing
    # it intrabar, filled at that day's close instead of the exact
    # crossing level. confirm_lookback_days (or confirm_days via the CLI
    # override) still controls how many days are checked -- e.g. 3 for
    # "check the next 3 candles for a close above."
    "ha_ema21_touch_confirm_on_close": False,
    "ha_ema21_touch_allow_reversal_wick_shapes": False,
    # 2026-08-24: True (baked in as default) -- HA EMA13 must be above HA
    # EMA21 at the signal candle. See trigger_indicators.
    # precompute_ema21_touch_signals' docstring -- isolated from the full
    # 4-EMA stack condition after post-hoc analysis found only this piece
    # (not EMA21-vs-50 or EMA50-vs-200) carries a real signal (33.3% win
    # vs 42.1% baseline on the failing cases). Validated at 5yr scale on
    # top of require_real_green+prior_rsi_lookback=20: CAGR 13.97%->
    # 14.78%, alpha +6.19%->+7.01%, Sharpe 1.40->1.47, max DD -13.01%->
    # -12.17%, PF 1.77->1.83, trade count nearly unchanged (196->195).
    "ha_ema21_touch_require_ema13_above_ema21": True,
    # 2026-08-24 addition, off by default -- the FULL stacked condition
    # (HA EMA13>EMA21>EMA50>EMA200), matching heikin_ashi_trend_entry/
    # heikin_ashi_ema21_bounce_entry's own spec. Independent of
    # ha_ema21_touch_require_ema13_above_ema21 above so both can be
    # compared -- post-hoc analysis found only the EMA13-vs-21 piece
    # carries real signal, so this is expected to underperform the
    # isolated version, but tested per explicit request.
    "ha_ema21_touch_require_ha_ema_stack": False,
    # 2026-08-24 addition, off by default -- replaces the "100% out at
    # the fixed 1:2 target" exit with: book HALF the position at target,
    # let the REST ride until the REAL close first closes below the REAL
    # EMA21 (the original stop still protects the remaining half
    # throughout). See backtest_triggered.py's partial_close_at_target
    # and the new tail-exit step for the implementation. Validated via
    # post-hoc trade-level simulation (scripts/analyze_half_target_
    # ema21_tail.py) before this real engine implementation: +4.7% total
    # P&L, win rate unchanged, on the 389-trade 5yr priorrsi20_novolsma
    # set -- not yet validated via a real smoke/5yr backtest run of this
    # actual engine code path.
    "ha_ema21_touch_half_target_ema21_tail": False,
    # 2026-08-24 addition, off by default -- stricter alternative to the
    # "any 1 of N" prior_above_ema13 gate: ALL N of the prior candles'
    # HA close must be above HA EMA13. See trigger_indicators.
    # precompute_ema21_touch_signals' docstring.
    "ha_ema21_touch_prior_above_ema13_all_close": False,
    # 2026-08-24 addition, off by default -- structured two-tier prior-
    # strength alternative: immediate prior candle close > EMA21, prior
    # 4 candles before that all close > EMA13. See trigger_indicators.
    # precompute_ema21_touch_signals' docstring.
    "ha_ema21_touch_prior_tiered_ema_check": False,
    # 2026-08-24 addition, off by default -- none of the prior N candles
    # (strictly before the signal) may have closed (HA) below HA EMA50.
    # See trigger_indicators.precompute_ema21_touch_signals' docstring.
    "ha_ema21_touch_prior_no_ema50_violation_days": None,
}


def detect_trigger(df_upto: pd.DataFrame, cfg: dict,
                   sector_df_upto: pd.DataFrame | None = None,
                   ha_upto: pd.DataFrame | None = None,
                   ema21_touch_upto: pd.DataFrame | None = None) -> dict | None:
    """df_upto: one symbol's OHLCV sliced up to and including today (point-
    in-time safe, no lookahead). sector_df_upto: the stock's OWN sector
    index's OHLC, sliced the same way -- used by the institutional
    pullback trigger's market filter AND the RS gate, AND by the
    Heikin-Ashi trigger's RS gate (see trigger_strategy.TRIGGERED_
    DEFAULTS' comment on "Relative strength vs. Nifty"/sector).
    ha_upto: the symbol's PRECOMPUTED Heikin-Ashi series (see
    trigger_indicators.precompute_heikin_ashi), sliced the same way --
    only used by the Heikin-Ashi trigger. Returns {"type": <trigger
    name>, "price": today's close (or the trigger's own computed
    entry_price)} for the first matching pattern in priority order, or
    None if nothing fired today.

    heikin_ashi_enabled is checked FIRST and is a fully INDEPENDENT path
    -- it does NOT go through trend_template_ok, the sector market
    filter, or any of the 4 older triggers below (none of that is part
    of that spec). The CLI script's isolation override turns this on and
    disables all 4 older triggers for a clean single-strategy test.

    Every OTHER trigger below requires trend_template_ok() to pass first
    (Minervini-style trend health pre-filter, stricter than the shared
    production trend_ok gate) -- a stock in a technically weak/flattening
    trend never gets one of those, regardless of what its price/volume
    did today.

    Breakout triggers (a/b) use breakout_hold_confirmed(): the breakout
    itself must have happened YESTERDAY and today's close must still hold
    at/above it -- entering the day AFTER the breakout instead of the
    breakout day itself, trading a day of entry price for confirmation
    that the move wasn't immediately reversed (the ASIANPAINT 2026-07-29/
    30 failure this and closed_strong() both target, from different
    angles).

    Pullback and Heikin-Ashi triggers' results additionally carry "stop"
    and "target" keys -- backtest_triggered.py uses these instead of the
    generic ATR-based stop when present, and opens a profit-target exit
    that no other trigger type has."""
    open_, close, high, low, volume = (df_upto["open"], df_upto["close"], df_upto["high"],
                                       df_upto["low"], df_upto["volume"])
    price = float(close.iloc[-1])

    if cfg.get("heikin_ashi_enabled", False):
        if sector_df_upto is None or sector_df_upto.empty:
            return None
        if ti.relative_strength_vs_sector(
                close, sector_df_upto["close"], cfg["institutional_rs_lookback_days"]) <= 0:
            return None
        if cfg.get("sector_above_ema_enabled", False) and not ti.sector_above_ema_ok(
                sector_df_upto, cfg["sector_above_ema_period"]):
            return None
        if cfg.get("sector_overextension_enabled", False) and not ti.sector_not_overextended_ok(
                sector_df_upto, cfg["sector_overext_lookback_months"], cfg["sector_overext_max_pct_change"]):
            return None
        if cfg.get("monthly_trend_persistence_enabled", False) and not ti.monthly_trend_persistence_ok(
                df_upto, cfg["monthly_trend_ema_period"], cfg["monthly_trend_above_pct_lookback"],
                cfg["monthly_trend_above_pct_min"], cfg["monthly_trend_new_high_lookback"],
                cfg["monthly_trend_new_high_window"], cfg["monthly_trend_new_high_min_count"]):
            return None
        if ha_upto is None or ha_upto.empty:
            return None

        entry = ti.heikin_ashi_trend_entry(
            df_upto, ha_upto, cfg["ha_ema13_period"], cfg["ha_ema21_period"],
            cfg["ha_ema50_period"], cfg["ha_ema200_period"], cfg["ha_rsi_period"],
            cfg["ha_rsi_min"], cfg["ha_signal_lookback_days"], cfg["ha_volume_sma_period"])
        if entry is None:
            return None

        entry_price = entry["entry_price"]
        stop_mode = cfg.get("ha_stop_mode", "ha_low")
        if stop_mode == "atr":
            atr_now = float(indicators.atr(df_upto, cfg["atr_period"]).iloc[-1])
            stop = entry_price - cfg["ha_stop_atr_multiple"] * atr_now
            # Symmetric ATR bracket per explicit request ("target as same
            # atr multiply instead of 1:2") -- target distance mirrors
            # the stop distance exactly (same multiple, same ATR value),
            # not the fixed profit_target_rr ratio.
            target = entry_price + cfg["ha_stop_atr_multiple"] * atr_now
        elif stop_mode == "fibonacci":
            swing = ti.find_swing_for_fib(df_upto, entry["signal_date"], cfg["fib_swing_lookback_days"])
            if swing is None:
                return None
            fib_result = ti.fibonacci_stop_target(
                swing[0], swing[1], entry_price, cfg["fib_stop_level"], cfg["fib_target_extension"])
            if fib_result is None:
                return None
            stop, target = fib_result
        elif stop_mode in ("swing_ema50", "ema_tiered_fixed"):
            # 2026-08-25 revision (2nd pass, explicit correction): take
            # the combined initial low FIRST -- min(signal candle's HA
            # low, entry candle's HA low) -- THEN apply the tiered EMA
            # check ONCE to that single value, against the ENTRY candle's
            # HA EMA13/EMA21 (today's trend context, not the signal
            # candle's older one). Tiers, using <= ("touching" counts):
            # if the combined low <= EMA21, use the low itself; elif it's
            # <= EMA13 (i.e. sitting between EMA21 and EMA13), also use
            # the low; else (above BOTH EMAs) use EMA13 itself, giving a
            # shallow, barely-pulled-back low more room than its own
            # (irrelevantly tight) value would. Written as explicit tiers
            # rather than a flat min() because the priority order matters
            # when EMA13 < EMA21 (e.g. the real LT 2023-03-16 case found
            # earlier this session). Then trails UP to each newly-
            # CONFIRMED swing low (never down), with an independent
            # EMA50-close-below exit as a parallel safety net -- see
            # backtest_triggered.py's step 1b/1c for the actual daily
            # ratchet + EMA50 check (this only sets the INITIAL stop). No
            # fixed target -- pure trail, same "let the big move run"
            # intent as ha_target_enabled=False.
            sig_low = entry["trigger_low"]
            entry_ha_low = float(ha_upto["ha_low"].iloc[-1])
            combined_low = min(sig_low, entry_ha_low)
            e13, e21 = entry["entry_ema13"], entry["entry_ema21"]
            if combined_low <= e21:
                stop_candidate = combined_low
            elif combined_low <= e13:
                stop_candidate = combined_low
            else:
                stop_candidate = e13
            stop = stop_candidate * (1 - cfg.get("swing_stop_buffer_pct", 0.3) / 100)
            if stop_mode == "ema_tiered_fixed":
                # 2026-08-25 addition: same tiered EMA13/EMA21 stop
                # formula as swing_ema50, but as a FIXED initial stop
                # (no daily swing-low ratchet, no EMA50-close-below exit
                # -- explicit request: "not ATR based, its with our EMA
                # stop logic" + "only initial stop + target is 1:2 hard
                # exit"), paired with a hard profit_target_rr target
                # exactly like the ha_low/default branch below. Omitting
                # the swing_ema50_stop flag (set only for stop_mode==
                # "swing_ema50" itself, below) keeps this out of
                # backtest_triggered.py's trailing/EMA50-exit machinery
                # entirely -- a plain stop+target bracket trade.
                risk = max(entry_price - stop, 0.01)
                target = entry_price + cfg["profit_target_rr"] * risk
        else:
            stop = entry["trigger_low"]
            risk = max(entry_price - stop, 0.01)
            target = entry_price + cfg["profit_target_rr"] * risk
        result = {"type": "heikin_ashi_trend", "price": entry_price, "stop": stop}
        # "It should trail every day ATR*N ... until that hits it should
        # not exit" -- pure trail-only mode, no fixed profit cap at all.
        # Omitting "target" entirely (not just setting it very high) means
        # backtest_triggered.py's position_targets never gets an entry
        # for this symbol, so its 1c profit-target check simply never
        # fires -- the position can ONLY close via the daily stop/
        # trailing-stop check (or 1d's institutional exits, which HA
        # positions are never added to either). ha_target_enabled=True
        # (default) keeps the fixed/symmetric target from before.
        # swing_ema50 is unconditionally target-less (no `target` local
        # even exists for that branch) -- pure trail, matching its own
        # "let the big move run" design intent.
        if stop_mode != "swing_ema50" and cfg.get("ha_target_enabled", True):
            result["target"] = target
        if stop_mode == "swing_ema50":
            result["swing_ema50_stop"] = True
        return result

    if cfg.get("ha_ema21_bounce_enabled", False):
        # Same stock selection as the other HA strategy -- same RS-vs-
        # sector hard gate -- but an entirely independent entry PATTERN
        # (see trigger_indicators.heikin_ashi_ema21_bounce_entry's
        # docstring): a same-day EMA21-touch-and-bounce, not a
        # multi-day red-candle-then-breakout.
        if sector_df_upto is None or sector_df_upto.empty:
            return None
        if ti.relative_strength_vs_sector(
                close, sector_df_upto["close"], cfg["institutional_rs_lookback_days"]) <= 0:
            return None
        if ha_upto is None or ha_upto.empty:
            return None

        entry = ti.heikin_ashi_ema21_bounce_entry(
            df_upto, ha_upto, cfg["ha_ema13_period"], cfg["ha_ema21_period"],
            cfg["ha_ema50_period"], cfg["ha_ema200_period"], cfg["ha_rsi_period"],
            cfg["ha_ema21_bounce_rsi_min"])
        if entry is None:
            return None

        entry_price = entry["entry_price"]
        stop = entry["stop"]
        risk = max(entry_price - stop, 0.01)
        target = entry_price + cfg["ha_ema21_bounce_target_rr"] * risk
        return {"type": "ha_ema21_bounce", "price": entry_price, "stop": stop, "target": target}

    if cfg.get("ha_ema21_touch_enabled", False):
        # Same stock selection as heikin_ashi_enabled -- an entirely
        # independent entry PATTERN (see trigger_indicators.
        # heikin_ashi_ema21_touch_entry's docstring): a multi-day
        # touch-EMA21-then-wait-for-breakout state machine, not a
        # same-day bounce or a short fixed-lookback red-candle search.
        # 2026-08-23: the two sector gates below used to ALWAYS run live
        # here, every day through confirmation -- traced to be silently
        # rejecting 26 of 61 (43%) confirmed signals, but an A/B test
        # moving BOTH to formation-time-only (checked once, in
        # precompute_ema21_touch_signals via backtest_triggered.py's
        # ema21_touch_sector_gate) made overall quality clearly WORSE
        # (win rate 52.2%->40.7%, profit factor 2.07->1.32 on the same
        # smoke window) -- unlike the RSI/watchlist gates, these sector
        # checks are carrying real signal, not just noise. Each is now
        # independently toggleable (ha_ema21_touch_sector_rs_formation_
        # only / ha_ema21_touch_sector_above_ema_formation_only, both
        # False by default = original live-recheck-every-day behavior)
        # so the two can be isolated to see which one matters.
        rs_formation_only = cfg.get("ha_ema21_touch_sector_rs_formation_only", False)
        above_ema_formation_only = cfg.get("ha_ema21_touch_sector_above_ema_formation_only", False)
        need_sector_df = not rs_formation_only or (
            cfg.get("sector_above_ema_enabled", False) and not above_ema_formation_only)
        if need_sector_df and (sector_df_upto is None or sector_df_upto.empty):
            return None
        if not rs_formation_only:
            if ti.relative_strength_vs_sector(
                    close, sector_df_upto["close"], cfg["institutional_rs_lookback_days"]) <= 0:
                return None
        if cfg.get("sector_above_ema_enabled", False) and not above_ema_formation_only:
            if not ti.sector_above_ema_ok(sector_df_upto, cfg["sector_above_ema_period"]):
                return None
        if ha_upto is None or ha_upto.empty or ema21_touch_upto is None or ema21_touch_upto.empty:
            return None

        entry = ti.heikin_ashi_ema21_touch_entry(
            df_upto, ha_upto, ema21_touch_upto,
            cfg.get("ha_ema21_touch_fill_at_close", False))
        if entry is None:
            return None

        entry_price = entry["entry_price"]
        stop = entry["stop"]
        risk = max(entry_price - stop, 0.01)
        target = entry_price + cfg["ha_ema21_touch_target_rr"] * risk
        return {"type": "ha_ema21_touch", "price": entry_price, "stop": stop, "target": target}

    if not ti.trend_template_ok(close, cfg["ema_fast"], cfg["ema_slow"],
                                cfg["trend_template_ema_slow_trend_days"]):
        return None

    if cfg.get("multiyear_breakout_enabled", True) and ti.breakout_hold_confirmed(
            close, high, low, volume, cfg["multiyear_lookback_days"],
            cfg["breakout_volume_avg_days"], cfg["breakout_volume_multiple"],
            cfg["breakout_max_upper_wick_pct"]):
        return {"type": "multiyear_breakout", "price": price}

    if cfg.get("shortterm_breakout_enabled", True) and ti.breakout_hold_confirmed(
            close, high, low, volume, cfg["shortterm_lookback_days"],
            cfg["breakout_volume_avg_days"], cfg["breakout_volume_multiple"],
            cfg["breakout_max_upper_wick_pct"]):
        return {"type": "shortterm_breakout", "price": price}

    if (cfg.get("pullback_slow_ema_enabled", True)
            and ti.ema_pullback_bounce(close, high, low, cfg["pullback_ema_slow"],
                                       cfg["pullback_lookback_days"])
            and ti.volume_surge(volume, cfg["pullback_volume_avg_days"],
                                cfg["pullback_volume_multiple"])):
        return {"type": f"pullback_{cfg['pullback_ema_slow']}ema", "price": price}

    if cfg.get("pullback_fast_ema_enabled", True):
        # Market filter (sector substituted for NIFTY 50 per the user's
        # explicit instruction) -- "skip long trades in bearish markets"
        # means no sector data at all also means no trade, fail-closed.
        if sector_df_upto is None or sector_df_upto.empty:
            return None
        if not ti.sector_market_filter_ok(sector_df_upto["close"], sector_df_upto["high"],
                                          sector_df_upto["low"], cfg["ema_fast"], cfg["ema_slow"]):
            return None

        # "Relative strength vs. Nifty" is listed under STOCK SELECTION in
        # the spec (same tier as EMA alignment/HH-HL), not the optional
        # quality score -- a hard prerequisite: the stock must be
        # outperforming its own sector, not just "not underperforming
        # much". The quality score below ALSO scores RS (for degree of
        # outperformance, full/half/zero credit) -- that's intentional,
        # not a duplicate: this gates eligibility, the score gates
        # conviction.
        if ti.relative_strength_vs_sector(
                close, sector_df_upto["close"], cfg["institutional_rs_lookback_days"]) <= 0:
            return None

        # 2026-08-12 simplification: hammer-only entry (no engulfing/
        # inside-bar, no multi-day pullback-structure search, no quality
        # score gate) -- see institutional_hammer_entry's docstring.
        entry = ti.institutional_hammer_entry(
            open_, high, low, close, volume,
            cfg["institutional_ema21_period"], cfg["ema_fast"], cfg["ema_slow"],
            cfg["hammer_min_lower_wick_ratio"], cfg["hammer_max_upper_wick_ratio"],
            cfg["institutional_max_gap_up_pct"])
        if entry is not None:
            entry_price = entry["entry_price"]
            stop = entry["trigger_low"]  # hammer candle's own low, no ATR alternative
            risk = max(entry_price - stop, 0.01)
            target = entry_price + cfg["profit_target_rr"] * risk  # 1:3 by default now
            return {"type": f"pullback_{cfg['pullback_ema_fast']}ema", "price": entry_price,
                   "stop": stop, "target": target, "shape": "hammer"}

    return None


def trigger_position_size(equity: float, price: float, initial_stop: float,
                          max_positions: int, max_loss_pct: float) -> int:
    """Equal-weight per-slot capital (equity / max_positions) is the
    starting share count -- "capital should divide based on max
    position" -- capped so a full stop-out at `initial_stop` can't lose
    more than `max_loss_pct` of TOTAL portfolio equity (not the slot's own
    capital; see module docstring for why -- a per-slot reading made the
    risk cap dominate almost every trade)."""
    if price <= 0 or max_positions <= 0:
        return 0
    per_slot_capital = equity / max_positions
    equal_weight_qty = int(per_slot_capital / price)

    stop_distance = max(price - initial_stop, 0.01)
    max_loss_amount = equity * max_loss_pct / 100
    risk_capped_qty = int(max_loss_amount / stop_distance)

    return max(min(equal_weight_qty, risk_capped_qty), 0)
