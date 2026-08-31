"""
13/21 EMA + VSA candlestick-pattern confluence scoring strategy, from the
user-supplied Quantitative_Pattern_Scoring_Rules.md spec (2026-08-26).

Two-rank design, per explicit request ("there is 2 rank - 1 is to select
the stocks based on our current ranking and 2. to candle pattern rank to
make entry decision"):
  Rank 1 (stock selection): the EXISTING production backtest.rank_universe_asof
  watchlist (RSI band, trend, near-high, weekly/monthly, fundamental gates
  + momentum/fundamental/sector score) -- entirely unchanged, called from
  the runner script exactly as the production backtest already does.
  Rank 2 (entry timing), THIS module: the spec's 100-point pattern/VSA/RRR
  confluence score, computed only for symbols that already cleared rank 1
  on a given day. A pattern scoring >=70 (Grade A/A+, which already implies
  RRR>=1.5 -- see score_quant_pattern's hard-disqualify) arms a stop-buy
  order valid for the next `qp_entry_expiry_sessions` (1 -- the immediate
  next session only, per explicit request) trading session(s) at
  high + qp_entry_atr_buffer*ATR14; if price doesn't breach that level
  within the window, the setup expires unfilled -- see
  precompute_quant_pattern_signals.

Design choices made where the source spec left a gap (flag these to the
user -- they are assumptions, not verified facts):
  - RVOL baseline = `qp_rvol_lookback`-day (20) SMA of volume; the spec
    never states the lookback for "Relative Volume."
  - "Preceding pullback candles" RVOL average (the no-supply +5 bonus) =
    the `qp_pullback_window` (3) candles immediately before the pattern
    candle; the spec never gives a count.
  - Post-entry management: a fixed bracket -- stop = the pattern day's
    sl, target = prior_swing_high, both frozen at entry, first one hit
    closes the trade. The spec's Python only computes RRR at scoring
    time and never describes trailing/partial-exit behavior, so a plain
    stop+target bracket is the closest-to-spec interpretation, not a
    verified rule.
  - prior_swing_high = the nearest CONFIRMED (point-in-time-safe) swing
    high from resistance_zones.precompute_swing_highs(), as of the
    pattern day -- "prior" read literally as the last one that already
    happened and was already confirmed knowable, not a projected level.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import indicators
import resistance_zones

QUANT_PATTERN_DEFAULTS: dict = {
    "qp_ema_fast": 13,
    "qp_ema_mid": 21,
    "qp_ema_trend": 50,
    "qp_ema_slow": 200,
    "qp_atr_period": 14,
    "qp_rvol_lookback": 20,
    "qp_pullback_window": 3,
    "qp_entry_atr_buffer": 0.10,
    "qp_stop_buffer_pct": 0.2,
    "qp_entry_expiry_sessions": 1,
    "qp_min_score": 70,
    "qp_swing_window": 10,
    "qp_star_c1_min_body_pct": 1.0,
    "qp_fallback_rr": 2.0,
    "qp_efficiency_ratio_window": 14,
    "qp_efficiency_ratio_enabled": False,
    "qp_max_efficiency_ratio": 0.30,
    "qp_min_rsi": 55,
    "qp_engulf_min_prior_decline_pct": 1.0,
    "qp_confirm_on_close": False,
    "qp_pin_min_wick_ratio": 0.70,
    "qp_pin_prior_ema_period": 13,
    "qp_pin_prior_lookback": 5,
}


def precompute_quant_pattern_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Per-day EMA13/21/200, ATR14, RVOL, EMA-zone bounds -- the raw
    ingredients score_quant_pattern needs. Vectorized, meant to be
    computed once per symbol over its full history (same
    precompute-once-per-symbol convention as
    indicators.precompute_daily_series)."""
    close, volume = df["close"], df["volume"]
    ema_fast = indicators.ema(close, cfg.get("qp_ema_fast", 13))
    ema_mid = indicators.ema(close, cfg.get("qp_ema_mid", 21))
    ema_trend = indicators.ema(close, cfg.get("qp_ema_trend", 50))
    ema_slow = indicators.ema(close, cfg.get("qp_ema_slow", 200))
    ema_pin_prior = indicators.ema(close, cfg.get("qp_pin_prior_ema_period", 13))
    atr14 = indicators.atr(df, cfg.get("qp_atr_period", 14))
    rsi = indicators.rsi(close, cfg.get("qp_rsi_period", 14))
    vol_avg = volume.rolling(cfg.get("qp_rvol_lookback", 20)).mean()
    rvol = volume / vol_avg.replace(0, np.nan)
    zone_upper = pd.concat([ema_fast, ema_mid], axis=1).max(axis=1)
    zone_lower = pd.concat([ema_fast, ema_mid], axis=1).min(axis=1)
    er_window = cfg.get("qp_efficiency_ratio_window", 14)
    net_change = (close - close.shift(er_window)).abs()
    path_length = close.diff().abs().rolling(er_window).sum()
    efficiency_ratio = net_change / path_length.replace(0, np.nan)
    return pd.DataFrame({
        "ema_fast": ema_fast, "ema_mid": ema_mid, "ema_trend": ema_trend, "ema_slow": ema_slow,
        "ema_pin_prior": ema_pin_prior,
        "ema_fast_shift2": ema_fast.shift(2),
        "atr14": atr14, "rvol": rvol, "rsi": rsi,
        "zone_upper": zone_upper, "zone_lower": zone_lower,
        "efficiency_ratio": efficiency_ratio,
    }, index=df.index)


def _pin_bar_mask(df: pd.DataFrame, ema_fast: pd.Series, ema_pin_prior: pd.Series,
                  cfg: dict) -> pd.Series:
    """Rank 1 (25 pts): lower wick >= qp_pin_min_wick_ratio of range, body
    <= 25% of range, close above the fast EMA (part of the geometry rule
    itself, per the spec's section 1.2 -- not just the separate 10pt
    "clean close" score).
    2026-08-27 change, explicit request: raised the wick threshold from
    the spec's 60% to 70% -- a 94-trade winners-vs-losers check found
    winning Pin Bar trades averaged a 73.9% wick ratio vs losers' 69.8%,
    suggesting a deeper rejection (well past the bare 60% minimum) is a
    real quality signal, not just a pass/fail geometry check.
    2026-08-27 addition, explicit request ("5 candle must be close above
    12 EMA" -> "prior 5 candles" -> corrected to "not EMA12 its EMA13"):
    the qp_pin_prior_lookback (5) candles immediately BEFORE the pin bar
    candle must each have closed above their own day's EMA-qp_pin_prior_
    ema_period (13, same period as ema_fast, computed as its own series
    since qp_pin_prior_ema_period is a separate config key -- kept
    independent in case it's ever tuned away from ema_fast's own 13
    again) -- a sustained-uptrend confirmation ahead of the rejection
    candle itself."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    rng = (h - l).replace(0, np.nan)
    lower_wick = np.minimum(o, c) - l
    body = (c - o).abs()
    min_wick_ratio = cfg.get("qp_pin_min_wick_ratio", 0.70)
    geometry = (lower_wick >= min_wick_ratio * rng) & (body <= 0.25 * rng)
    above_pin_prior_ema = c > ema_pin_prior
    lookback = cfg.get("qp_pin_prior_lookback", 5)
    prior_all_above = pd.Series(True, index=df.index)
    for i in range(1, lookback + 1):
        prior_all_above &= above_pin_prior_ema.shift(i, fill_value=False)
    return geometry.fillna(False) & (c > ema_fast) & prior_all_above


def _bullish_engulfing_mask(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Rank 2 (15 pts): prior red body engulfed by today's green body.
    2026-08-27 additions, explicit requests:
      - today's high must exceed the prior candle's high -- confirms the
        reversal candle actually made a fresh extreme, not just an
        engulf sitting entirely below the prior candle's own high.
      - today's full RANGE must engulf the prior candle's full range,
        not just their bodies: today's high > prior high AND today's
        low < prior low -- EXCEPT when the prior (red) candle has a
        long lower wick (wick > its own body), in which case the low
        requirement is waived. A long wick is a spike, not part of the
        candle's real range -- requiring today's low to dip below that
        spike is an artificially strict bar the body-engulf logic
        never intended.
      - the prior (red) candle must show a real close-to-close decline
        of at least qp_engulf_min_prior_decline_pct (1.0%), same
        style/convention as Morning Star's candle-1 "tall" check. A
        46-trade winners-vs-losers enrichment found this the single
        most consistent differentiator: winning trades' prior candle
        averaged -1.21% while losing trades' averaged only -0.37% --
        a token red candle, not a real one."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    prev_o, prev_h, prev_l, prev_c = o.shift(1), h.shift(1), l.shift(1), c.shift(1)
    prior_bearish = prev_c < prev_o
    current_bullish = c > o
    engulf = (o <= prev_c) & (c >= prev_o)
    higher_high = h > prev_h
    lower_low = l < prev_l
    prev_body = (prev_o - prev_c).abs()
    prev_lower_wick = prev_c - prev_l  # prior is red, so close is the body's lower edge
    prev_long_wick = prev_lower_wick > prev_body
    low_ok = lower_low | prev_long_wick
    prior_decline_pct = cfg.get("qp_engulf_min_prior_decline_pct", 1.0) / 100
    prior_real_decline = c.pct_change().shift(1) <= -prior_decline_pct
    return (prior_bearish & current_bullish & engulf & higher_high & low_ok
           & prior_real_decline).fillna(False)


def _morning_star_mask(df: pd.DataFrame, ema_fast: pd.Series, ema_mid: pd.Series,
                       ema_trend: pd.Series, rsi: pd.Series, cfg: dict) -> pd.Series:
    """Rank 3 (25 pts). 2026-08-27 full rewrite, explicit 9-point spec:
      1. Candle 1 (t-2): red, body > qp_star_c1_min_body_pct (1.0%) of
         its own open -- a real decline, not a token one.
      2. Candle 1 closes below EMA13 OR EMA21.
      3. Candle 2 (t-1): ANY shape (doji, hammer, red, or green -- no
         color/label constraint) as long as its close sits AT OR BELOW
         the midpoint of candle 1's body -- a real pause, not a
         recovery already underway.
      4. Candle 3 (t, today): a big green candle closing AT OR ABOVE
         the midpoint of candle 1's body.
      5. Candle 3 closes above EMA21.
      8. Candle 3 closes above EMA50.
      9. Candle 3's EMA13 > EMA21 > EMA50 (stack). 2026-08-27: the full
         13>21>50>200 stack, briefly hard-coded here after SRF 2026-07-
         15 showed a Grade-A signal where 13>21>50 held but 50<200, was
         then promoted into score_quant_pattern's micro_ok gate so it
         applies uniformly to all three patterns, not Morning Star
         alone -- see the `score.mask(~micro_ok...)` gate below.
    (Numbering matches the request's own list; 6/7 -- the entry trigger
    from max(candle1_high, candle3_high) and the candle-3-low stop --
    are handled in score_quant_pattern/precompute_quant_pattern_signals,
    not here in the pure geometry check.)

    2026-08-27 addition, explicit request: candle 2's high must not
    exceed candle 1's high -- candle 2 has to stay contained within (or
    below) candle 1's range, not spike to a fresh intraday high above
    the decline candle before reversing -- otherwise the "pause" story
    only holds on the close, not on the actual price action.

    2026-08-27 addition, explicit request: candle 2 must OPEN at or
    below candle 1's close -- rules out candle 2 gapping up over
    candle 1's close before the pause even begins, which would mean
    the "reclaim" in candle 3 has a head start it didn't actually earn
    off the lows.

    2026-08-27 addition, explicit request: candle 3's RSI must be above
    55 -- a momentum confirmation on the reclaim candle itself (an
    earlier attempt at an RSI>55 gate applied to ALL three patterns was
    tried and reverted for regressing the combined strategy; this is a
    fresh, Morning-Star-specific test, not a reinstatement of that
    gate).

    2026-08-27 addition, explicit request: candle 3 must close above
    EMA13 too, not just EMA21/EMA50 -- a 94-trade check found 92 of 94
    already satisfied this incidentally (given the EMA13>21>50>200
    stack requirement elsewhere), so this mostly just codifies what was
    already true in practice."""
    o, h, c = df["open"], df["high"], df["close"]
    o1, h1, c1 = o.shift(1), h.shift(1), c.shift(1)
    o2, h2, c2 = o.shift(2), h.shift(2), c.shift(2)
    ema13_c2, ema21_c2 = ema_fast.shift(2), ema_mid.shift(2)

    day2_body_pct = (o2 - c2) / o2 * 100
    day2_red_real = (c2 < o2) & (day2_body_pct > cfg.get("qp_star_c1_min_body_pct", 1.0))
    day2_below_ema = (c2 < ema13_c2) | (c2 < ema21_c2)

    midpoint1 = o2 - 0.50 * (o2 - c2)
    day1_below_mid = c1 <= midpoint1
    day1_open_le_day2_close = o1 <= c2
    day1_high_le_day2_high = h1 <= h2

    day0_green = c > o
    day0_above_mid = c >= midpoint1
    day0_above_ema13 = c > ema_fast
    day0_above_ema21 = c > ema_mid
    day0_above_ema50 = c > ema_trend
    day0_rsi_above_55 = rsi > 55
    return (day2_red_real & day2_below_ema & day1_below_mid & day1_open_le_day2_close
           & day1_high_le_day2_high & day0_green & day0_above_mid & day0_above_ema13
           & day0_above_ema21 & day0_above_ema50 & day0_rsi_above_55).fillna(False)


def _prior_swing_high_series(df: pd.DataFrame, window: int) -> pd.Series:
    """Point-in-time-safe "prior swing high" per day: the price of the
    most recently CONFIRMED swing high as of that day (forward-filled
    from each swing's confirmed_date, never its pivot_date -- a swing
    isn't knowable until `window` bars after it happened)."""
    swings = resistance_zones.precompute_swing_highs(df, window=window)
    if swings.empty:
        return pd.Series(np.nan, index=df.index)
    s = (swings.sort_values("confirmed_date")
               .drop_duplicates("confirmed_date", keep="last")
               .set_index("confirmed_date")["price"])
    combined_index = df.index.union(s.index)
    return s.reindex(combined_index).ffill().reindex(df.index)


def score_quant_pattern(df: pd.DataFrame, cfg: dict,
                        ind: pd.DataFrame | None = None) -> pd.DataFrame:
    """The 100-point confluence score, vectorized over `df`'s full
    history. Returns a DataFrame aligned to df.index: score (0-100,
    already zeroed on any day RRR<1.5 disqualifies it), grade
    (A+/A/B/F), pattern_name, entry_level, stop, target (prior swing
    high), rrr, rvol."""
    if ind is None:
        ind = precompute_quant_pattern_indicators(df, cfg)
    close, high, low = df["close"], df["high"], df["low"]
    ema_fast, ema_slow = ind["ema_fast"], ind["ema_slow"]
    atr14, rvol = ind["atr14"], ind["rvol"]
    zone_upper, zone_lower = ind["zone_upper"], ind["zone_lower"]

    score = pd.Series(0.0, index=df.index)

    # 1. Macro & micro trend (20)
    macro_ok = close > ema_slow
    # 2026-08-27 change, explicit request: full 4-EMA stack alignment
    # (13>21>50>200) instead of just EMA13>EMA21 + EMA13 rising -- a
    # stronger, more demanding trend confirmation.
    micro_ok = (ind["ema_fast"] > ind["ema_mid"]) & (ind["ema_mid"] > ind["ema_trend"]) & (ind["ema_trend"] > ema_slow)
    score += macro_ok.fillna(False).astype(int) * 10
    score += micro_ok.fillna(False).astype(int) * 10

    # 2. EMA value zone (25)
    in_zone = (low <= zone_upper) & (high >= zone_lower)
    clean_close = close > ema_fast
    score += in_zone.fillna(False).astype(int) * 15
    score += clean_close.fillna(False).astype(int) * 10

    # 3. Pattern geometry (25 max). 2026-08-27: reverted after a 5-year
    # backtest showed Morning Star 25/Bullish Engulfing 20/Pin Bar 15
    # was a clear regression (CAGR 13.36%->7.22%, PF 1.95->1.61) versus
    # this configuration -- Pin Bar and Morning Star tied at 25 (Pin Bar
    # winning ties), Bullish Engulfing at 15. Pin Bar is this strategy's
    # largest-sample, most consistently reliable pattern; demoting it
    # below the other two hurt real trade quality. Priority order when
    # more than one pattern's geometry matches the same day (e.g.
    # LAURUSLABS 2026-07-09, which satisfies both Morning Star and
    # Bullish Engulfing): Pin Bar > Morning Star > Bullish Engulfing.
    # Applied in reverse-priority order below since each .mask() call
    # overwrites only the cells where ITS OWN condition is true, so the
    # LAST call for a given cell wins -- pin is applied last so it wins
    # outright even though it's points-tied with Morning Star.
    pin = _pin_bar_mask(df, ema_fast, ind["ema_pin_prior"], cfg)
    engulf = _bullish_engulfing_mask(df, cfg)
    star = _morning_star_mask(df, ema_fast, ind["ema_mid"], ind["ema_trend"], ind["rsi"], cfg)
    pattern_name = pd.Series("None", index=df.index, dtype=object)
    pattern_pts = pd.Series(0, index=df.index)
    pattern_pts = pattern_pts.mask(engulf, 15)
    pattern_name = pattern_name.mask(engulf, "Bullish Engulfing")
    pattern_pts = pattern_pts.mask(star, 25)
    pattern_name = pattern_name.mask(star, "Morning Star")
    pattern_pts = pattern_pts.mask(pin, 25)
    pattern_name = pattern_name.mask(pin, "Pin Bar (Stopping Vol)")
    score += pattern_pts

    # 4. VSA / volume (20 + up to 5 no-supply bonus)
    vol_pts = np.select([rvol >= 1.5, rvol >= 1.0], [20, 10], default=0)
    score += vol_pts
    pullback_rvol_avg = rvol.shift(1).rolling(cfg.get("qp_pullback_window", 3)).mean()
    score += (pullback_rvol_avg < 0.8).fillna(False).astype(int) * 5

    # 5. Risk-to-reward, hard disqualify below 1.5 (zeroes the WHOLE score)
    # 2026-08-27 change, explicit request: for Morning Star specifically,
    # the entry trigger is max(candle 1's high, candle 3's high) --
    # candle 1's high can sit above candle 3's own high (candle 3 only
    # has to reclaim the BODY midpoint, not make a new high), so using
    # just candle 3's high could set the trigger below a real overhead
    # level from 2 days earlier. Pin Bar/Bullish Engulfing keep using
    # their own single/latest candle's high, unaffected.
    star_base_high = pd.concat([high, high.shift(2)], axis=1).max(axis=1)
    entry_base_high = high.where(~star, star_base_high)
    entry_level = entry_base_high + cfg.get("qp_entry_atr_buffer", 0.10) * atr14
    # 2026-08-26 change, explicit request ("stop low * some threshold as
    # buffer same as entry"): percentage buffer below the pattern
    # candle's low, replacing the original ATR-based stop -- same
    # buffer-below-a-price-level style as the swing_ema50 stop mode
    # elsewhere in this codebase (swing_stop_buffer_pct).
    # 2026-08-27 addition, explicit request ("stop loss as EMA13 with
    # threshold"): qp_stop_mode="ema13_pct" swaps the stop base from the
    # candle's own low to EMA13 (same buffer_pct below it), for an
    # isolated Bullish-Engulfing-only A/B test.
    stop_base = ind["ema_fast"] if cfg.get("qp_stop_mode", "low_pct") == "ema13_pct" else low
    stop = stop_base * (1 - cfg.get("qp_stop_buffer_pct", 0.2) / 100)
    risk = entry_level - stop
    # 2026-08-26 fix: a stock in a strong sustained uptrend making new
    # highs has NO confirmed swing high left above its current price --
    # the nearest one on record predates the rally and sits BELOW today's
    # price. Using it as-is made "reward" negative and hard-disqualified
    # an otherwise-valid pattern (confirmed on LAURUSLABS, 2026-07-09/24:
    # a real Bullish Engulfing scored 0 because its only known swing high,
    # 1457, was already 5-10% below the entry price). Since this
    # strategy's whole Rank-1 pool is selected FOR being near/at highs,
    # this bug would have hit almost every candidate. Fallback: when
    # there's no confirmed swing high on record above entry_level, use a
    # plain qp_fallback_rr-multiple target instead (same "target = entry +
    # multiple*risk" convention as trigger_strategy.py's other stop-modes)
    # -- the swing-high target is still used whenever a real one exists
    # above price, this only fills the gap for fresh-high breakouts.
    prior_swing_high = _prior_swing_high_series(df, cfg.get("qp_swing_window", 10))
    no_valid_swing_target = prior_swing_high.isna() | (prior_swing_high <= entry_level)
    fallback_target = entry_level + cfg.get("qp_fallback_rr", 2.0) * risk
    target = prior_swing_high.where(~no_valid_swing_target, fallback_target)
    reward = target - entry_level
    rrr = pd.Series(np.where(risk > 0, reward / risk, 0.0), index=df.index)
    disqualified = rrr < 1.5
    # 2026-08-27 change, explicit request: RRR no longer scores points --
    # a 5-year enrichment of 307 real trades showed higher RRR correlates
    # with WORSE outcomes (avg return +1.24% at RRR 1.75-2.0 vs -2.0% to
    # -3.1% at RRR 2.0-2.5), because a bigger, more "generous"-looking
    # target is simply harder to reach before the stop -- rewarding it
    # with extra points was rewarding the wrong thing. RRR>=1.5 stays as
    # a hard sanity floor (disqualify below it), but the 10 points move
    # to stop tightness instead: a SMALLER risk distance (as % of entry
    # price) doesn't depend on guessing whether a distant target gets
    # hit, and is a real, achievable risk control on its own. Thresholds
    # (<=3% / <=5%) chosen from this same 307-trade sample's risk-pct
    # quartiles (25th=2.76%, median=3.70%, 75th=5.59%).
    risk_pct = risk / entry_level * 100
    stop_tightness_pts = np.select([risk_pct <= 3.0, risk_pct <= 5.0], [10, 5], default=0)
    score += stop_tightness_pts
    score = score.mask(disqualified, 0.0)
    # 2026-08-27 fix, explicit request ("if no pattern match then entire
    # score would be 0"): trend+zone+volume+RRR alone could sum to 80
    # points -- enough to clear the 70-point entry bar with ZERO
    # contribution from Pattern Geometry, confirmed on a real trade
    # (TORNTPHARM, 2026-07-31: score 70, pattern_name None, went on to
    # lose -6.96%). A candlestick pattern is this strategy's whole
    # premise -- no pattern, no entry, same hard-zero treatment as the
    # RRR disqualify above.
    score = score.mask(pattern_pts == 0, 0.0)
    # 2026-08-27 fix, explicit request ("EMA zone touch is 0 then make
    # trade total 0"): a real trade (GVT&D, 2025-09-19) scored 75/Grade A
    # and got stopped out in 1 day despite the candle's range NEVER
    # touching the 13/21 EMA zone at all -- it was already riding well
    # above both EMAs, not pulling back into value. Same hard-zero
    # treatment as the RRR and no-pattern gates above.
    score = score.mask(~in_zone.fillna(False), 0.0)
    # 2026-08-27 addition, explicit request ("that should be hard gate
    # for all pattern"): the full EMA13>EMA21>EMA50>EMA200 stack, first
    # added as a hard requirement inside _morning_star_mask alone (after
    # SRF 2026-07-15 showed a Grade-A Morning Star where 13>21>50 held
    # but 50<200), promoted to apply across all three patterns -- same
    # hard-zero treatment as the RRR/no-pattern/no-zone-touch gates
    # above, not just the additive micro_ok score points.
    score = score.mask(~micro_ok.fillna(False), 0.0)
    # 2026-08-27 addition, explicit request ("identify the stock is not
    # sidewise" -> Kaufman's Efficiency Ratio -> "lets implement and
    # check the result"): a 139-trade check on Bullish Engulfing found
    # this backwards from the spec's own assumption -- LOW efficiency
    # (choppy prior price action) scored 51.1% win rate / +1.86% avg
    # return, HIGH efficiency (a clean prior trend) scored only 34.0% /
    # +0.03% (near breakeven). Hard-disqualify above qp_max_efficiency_
    # ratio (0.30, the empirical boundary of the worst-performing
    # tercile) instead of favoring a "clear trend" the way the original
    # textbook idea assumed -- the data said the opposite.
    # 2026-08-27: made toggleable (default off) per explicit request
    # ("lets diasable that and check") -- LAURUSLABS 2026-07-09 (a real
    # Morning Star, already validated candle-by-candle earlier) got
    # excluded by this gate (ER 0.35 > 0.30) despite being a genuinely
    # rising stock, prompting a re-check of whether the gate earns its
    # keep against real examples, not just the aggregate stats above.
    if cfg.get("qp_efficiency_ratio_enabled", False):
        efficiency_ratio = ind["efficiency_ratio"]
        too_trendy = efficiency_ratio > cfg.get("qp_max_efficiency_ratio", 0.30)
        score = score.mask(too_trendy.fillna(True), 0.0)
    # 2026-08-27: an RSI>55 hard gate was tried and removed -- a 5-year
    # backtest showed it cut trade count by 25% (212->158) while barely
    # moving average return, and CAGR/final equity both dropped
    # (12.37%->9.75%). Matches the earlier isolated Bullish-Engulfing
    # check, where a fixed RSI threshold showed no win/loss separation
    # at all -- RSI isn't a useful filter for this strategy.
    score = score.clip(upper=100)

    grade = pd.Series(
        np.select([score >= 85, score >= 70, score >= 50], ["A+", "A", "B"], default="F"),
        index=df.index)

    return pd.DataFrame({
        "score": score, "grade": grade, "pattern_name": pattern_name,
        "entry_level": entry_level, "stop": stop, "target": target,
        "rrr": rrr, "rvol": rvol,
    }, index=df.index)


def precompute_quant_pattern_signals(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Day-by-day pending stop-buy-order state machine on top of
    score_quant_pattern's per-day scores. A Grade A/A+ setup arms an
    order at entry_level, valid for the NEXT `qp_entry_expiry_sessions`
    (1 -- immediate next session only) trading session(s) -- the
    earliest (and, by default, only) a fresh setup can fill is the
    following session, matching "place a Stop-Buy Order... prior to
    market open on the day following pattern completion." Triggers the
    moment a later session's HIGH reaches entry_level; fill price is
    gap-aware (that day's OPEN if it gapped up through the level, else
    entry_level itself -- a stop order can't fill better than the
    market let it). Only one order live at a time per symbol: a new
    setup while one's already pending does NOT re-arm or replace it,
    matching a real trader not cancel-and-replace a live stop order
    every time a fresh signal fires.

    Returns a DataFrame aligned to df.index: confirmed_entry (bool),
    entry_price, stop, target, rrr, score, grade, pattern_name,
    signal_date (the day the pattern actually formed, not the fill day)."""
    scored = score_quant_pattern(df, cfg)
    expiry = cfg.get("qp_entry_expiry_sessions", 2)
    min_score = cfg.get("qp_min_score", 70)
    pattern_filter = cfg.get("qp_pattern_filter")

    n = len(df)
    idx = df.index
    high = df["high"].to_numpy()
    close_arr = df["close"].to_numpy()
    open_ = df["open"].to_numpy()
    confirm_on_close = cfg.get("qp_confirm_on_close", False)
    score_arr = scored["score"].to_numpy()
    entry_arr = scored["entry_level"].to_numpy()
    stop_arr = scored["stop"].to_numpy()
    target_arr = scored["target"].to_numpy()
    rrr_arr = scored["rrr"].to_numpy()
    grade_arr = scored["grade"].to_numpy()
    pattern_arr = scored["pattern_name"].to_numpy()

    confirmed_entry = np.zeros(n, dtype=bool)
    entry_price = np.full(n, np.nan)
    stop_out = np.full(n, np.nan)
    target_out = np.full(n, np.nan)
    rrr_out = np.full(n, np.nan)
    score_out = np.full(n, np.nan)
    grade_out = np.array([""] * n, dtype=object)
    pattern_out = np.array([""] * n, dtype=object)
    signal_date_out = np.array([pd.NaT] * n, dtype=object)

    pending: dict | None = None

    for i in range(n):
        if pending is not None:
            # 2026-08-27 addition, explicit request ("what if we take the
            # entry if price close above the high of signal candle"):
            # qp_confirm_on_close requires the day's CLOSE (not just an
            # intraday high touch) to reach entry_level, filling at that
            # close price -- a real confirmation instead of a stop-buy
            # that could trigger on a brief wick and reverse.
            triggered = (close_arr[i] >= pending["entry_level"] if confirm_on_close
                        else high[i] >= pending["entry_level"])
            if triggered:
                fill = close_arr[i] if confirm_on_close else max(pending["entry_level"], open_[i])
                confirmed_entry[i] = True
                entry_price[i] = fill
                stop_out[i] = pending["stop"]
                target_out[i] = pending["target"]
                rrr_out[i] = pending["rrr"]
                score_out[i] = pending["score"]
                grade_out[i] = pending["grade"]
                pattern_out[i] = pending["pattern"]
                signal_date_out[i] = pending["signal_date"]
                pending = None
            else:
                pending["sessions_left"] -= 1
                if pending["sessions_left"] <= 0:
                    pending = None  # expired unfilled -- "momentum node has expired"

        if (pending is None and score_arr[i] >= min_score
                and grade_arr[i] in ("A", "A+") and not np.isnan(entry_arr[i])
                and (pattern_filter is None or pattern_arr[i] == pattern_filter)):
            pending = {
                "entry_level": entry_arr[i], "stop": stop_arr[i], "target": target_arr[i],
                "rrr": rrr_arr[i], "score": score_arr[i], "grade": grade_arr[i],
                "pattern": pattern_arr[i], "signal_date": idx[i],
                "sessions_left": expiry,
            }

    return pd.DataFrame({
        "confirmed_entry": confirmed_entry, "entry_price": entry_price,
        "stop": stop_out, "target": target_out, "rrr": rrr_out,
        "score": score_out, "grade": grade_out, "pattern_name": pattern_out,
        "signal_date": signal_date_out,
    }, index=idx)
