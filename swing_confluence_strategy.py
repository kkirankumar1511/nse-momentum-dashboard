"""
10-point EMA13/21/50 confluence scoring strategy, from the user-supplied
swing_trading_strategy.md spec -- a SEPARATE strategy from quant_pattern.py,
not a replacement for it (explicit request: "implement separately without
touching this"). Kept fully standalone: this module never imports or
mutates anything in quant_pattern.py's own scoring path, only reuses two of
its private candlestick-geometry functions (Pin Bar, Bullish Engulfing) as
plain pattern detectors -- same candles in, same boolean mask out.

Explicit request: use OUR pin bar and bullish engulfing (as currently
tuned in quant_pattern.py -- 70% wick, prior-5-candle EMA13 filter, 1%
prior-decline requirement, etc.) but the ORIGINAL Morning Star geometry
"the one we use previously without change" -- i.e. the pre-2026-08-27
version, before this session's 9-rule rewrite (tall red via close-to-
close <=-2%, small-body pause candle marking the sequence low, tall green
closing above both candle 1's midpoint AND the 13/21 EMA zone). That
version no longer exists in quant_pattern.py (it was fully replaced), so
it's reconstructed here from scratch, self-contained.

Design choices made where the source spec left a gap (flagged, not
verified facts):
  - The spec's own candle patterns (Bullish Engulfing/Morning Star = 4pts,
    Hammer/Inside Bar Breakout = 3pts) are replaced entirely by OUR THREE
    patterns per explicit request. Mapping chosen: Bullish Engulfing or
    (original) Morning Star -> 4pts (matches the spec's own top tier,
    both being multi-candle reversal completions); Pin Bar -> 3pts
    (mapped to the spec's Hammer tier -- both are single-candle,
    long-lower-wick rejection patterns).
  - "Low wick of Day 5 pattern" for a multi-candle pattern (Bullish
    Engulfing, Morning Star) is read as TODAY's (the completion candle's)
    low, not the whole pattern's lowest point -- matches the spec's own
    reference code, which only ever reads `today['low']`.
  - Stop-loss "lowest point of the multi-candle pattern" (spec's own
    wording, used only for the SL, not the EMA-touch check) = the
    minimum low across all candles in the pattern (1 candle for Pin Bar,
    2 for Bullish Engulfing, 3 for Morning Star).
  - NSE equity tick size = 0.05 (uniform across price bands), matching
    the spec's own reference code's +0.05/-0.05.
  - No Rank-1 stock-selection gate -- the spec's own architecture is a
    single unified cross-sectional score, not this codebase's two-rank
    design; every symbol in the watchlist is scored directly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import indicators
import quant_pattern as qp

SWING_DEFAULTS: dict = {
    "sw_ema_fast": 13,
    "sw_ema_mid": 21,
    "sw_ema_trend": 50,
    "sw_lookback": 5,
    "sw_min_score": 7,
    "sw_tick_size": 0.05,
    "sw_target_rr": 2.0,
    "sw_star_tall_pct_threshold": 2.0,
    "sw_star_small_body_atr_mult": 0.35,
    "sw_star_doji_max_body_pct_of_range": 0.10,
}


def _original_morning_star_mask(df: pd.DataFrame, atr14: pd.Series,
                                zone_upper: pd.Series, cfg: dict) -> pd.Series:
    """The PRE-2026-08-27 Morning Star geometry, reconstructed as-is (not
    the 9-rule rewrite currently in quant_pattern.py):
      1. Candle 1 (t-2): tall red, close-to-close return <= -tall_pct.
      2. Candle 2 (t-1): small body (<= small_mult x ATR14) AND marks the
         LOW of the 3-candle sequence.
      3. Candle 3 (t, today): tall green, close-to-close return >=
         +tall_pct, closes above candle 1's body midpoint AND above the
         13/21 EMA zone (zone_upper = max(EMA13, EMA21)).

    2026-08-27 correction, explicit request ("this is not used exacle
    what we setup with 2nd candle as doji candle"): the small-body-vs-
    ATR check alone doesn't actually enforce a DOJI shape -- a candle
    can be "small vs ATR" yet still have a body that's a sizeable chunk
    of its OWN high-low range (not a doji at all). Added a proper doji
    test on top: candle 2's body must also be <=
    sw_star_doji_max_body_pct_of_range (10%) of its own day's range."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    o1, h1, l1, c1 = o.shift(1), h.shift(1), l.shift(1), c.shift(1)
    o2, l2, c2 = o.shift(2), l.shift(2), c.shift(2)
    small_mult = cfg.get("sw_star_small_body_atr_mult", 0.35)
    tall_pct = cfg.get("sw_star_tall_pct_threshold", 2.0) / 100
    doji_max_pct = cfg.get("sw_star_doji_max_body_pct_of_range", 0.10)

    daily_ret = c.pct_change()
    day2_tall_red = (c2 < o2) & (daily_ret.shift(2) <= -tall_pct)
    day1_small_vs_atr = (c1 - o1).abs() <= small_mult * atr14.shift(1)
    day1_range = (h1 - l1).replace(0, np.nan)
    day1_is_doji = (c1 - o1).abs() <= doji_max_pct * day1_range
    day1_marks_low = (l1 <= l2) & (l1 <= l)
    day0_tall_green = (c > o) & (daily_ret >= tall_pct)
    day0_close_above_mid = c > (o2 - 0.50 * (o2 - c2))
    day0_close_above_zone = c > zone_upper
    return (day2_tall_red & day1_small_vs_atr & day1_is_doji.fillna(False) & day1_marks_low
           & day0_tall_green & day0_close_above_mid & day0_close_above_zone).fillna(False)


def precompute_swing_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """EMA13/21/50 + ATR14 (needed for the original Morning Star's
    small-body/tall-candle thresholds) + the pin bar/engulfing patterns'
    own required indicators, all computed once per symbol."""
    close = df["close"]
    ema_fast = indicators.ema(close, cfg.get("sw_ema_fast", 13))
    ema_mid = indicators.ema(close, cfg.get("sw_ema_mid", 21))
    ema_trend = indicators.ema(close, cfg.get("sw_ema_trend", 50))
    atr14 = indicators.atr(df, 14)
    zone_upper = pd.concat([ema_fast, ema_mid], axis=1).max(axis=1)

    # Pin Bar reuses quant_pattern's OWN tuned defaults (70% wick, its
    # own prior-5-candle EMA13 filter) -- computed as its own series,
    # via quant_pattern's own config key, only because _pin_bar_mask
    # needs it as a separate argument internally.
    qp_cfg = qp.QUANT_PATTERN_DEFAULTS
    ema_pin_prior = indicators.ema(close, qp_cfg.get("qp_pin_prior_ema_period", 13))

    return pd.DataFrame({
        "ema_fast": ema_fast, "ema_mid": ema_mid, "ema_trend": ema_trend,
        "atr14": atr14, "zone_upper": zone_upper, "ema_pin_prior": ema_pin_prior,
    }, index=df.index)


def score_swing_confluence(df: pd.DataFrame, cfg: dict,
                           ind: pd.DataFrame | None = None) -> pd.DataFrame:
    """The spec's 10-point confluence score, with our own three patterns
    substituted for the spec's Bullish Engulfing/Morning Star/Hammer/
    Inside Bar. Returns score (0-10), pattern_name, entry_level, stop,
    target, disqualified (score < sw_min_score)."""
    if ind is None:
        ind = precompute_swing_indicators(df, cfg)
    ema_fast, ema_mid, ema_trend = ind["ema_fast"], ind["ema_mid"], ind["ema_trend"]
    low, high, close = df["low"], df["high"], df["close"]

    qp_cfg = qp.QUANT_PATTERN_DEFAULTS
    pin = qp._pin_bar_mask(df, ema_fast, ind["ema_pin_prior"], qp_cfg)
    engulf = qp._bullish_engulfing_mask(df, qp_cfg)
    star = _original_morning_star_mask(df, ind["atr14"], ind["zone_upper"], cfg)

    score = pd.Series(0.0, index=df.index)
    pattern_name = pd.Series("None", index=df.index, dtype=object)
    # 4pts tier first, then 3pts pin bar overwrites only where pin is
    # true AND neither of the 4pt patterns fired on the same candle.
    score = score.mask(engulf, 4.0)
    pattern_name = pattern_name.mask(engulf, "Bullish Engulfing")
    score = score.mask(star, 4.0)
    pattern_name = pattern_name.mask(star, "Morning Star")
    pin_only = pin & ~(engulf | star)
    score = score.mask(pin_only, 3.0)
    pattern_name = pattern_name.mask(pin_only, "Pin Bar")

    has_pattern = pattern_name != "None"

    # EMA interaction (elif, per the spec's own reference code): low
    # touches/pierces 13 or 21 EMA and closes above it -> 3pts; else if
    # low touches/pierces 50 EMA and closes above it -> 2pts.
    touches_13_21 = ((low <= ema_fast) & (close > ema_fast)) | ((low <= ema_mid) & (close > ema_mid))
    touches_50 = (low <= ema_trend) & (close > ema_trend)
    ema_pts = np.select([touches_13_21, touches_50], [3.0, 2.0], default=0.0)
    score += ema_pts

    lookback = cfg.get("sw_lookback", 5)
    stacked = (ema_fast > ema_mid)
    stacked_all_5 = stacked.rolling(lookback).sum() >= lookback
    score += stacked_all_5.fillna(False).astype(int) * 1.0

    above_mid = close > ema_mid
    above_mid_4of5 = above_mid.rolling(lookback).sum() >= 4
    score += above_mid_4of5.fillna(False).astype(int) * 2.0

    score = score.mask(~has_pattern, 0.0)
    disqualified = score < cfg.get("sw_min_score", 7)

    tick = cfg.get("sw_tick_size", 0.05)
    entry_level = high + tick
    # "lowest point of the multi-candle pattern" -- min low across the
    # pattern's own candles: today only (Pin Bar, 1 candle), today+prior
    # (Bullish Engulfing, 2 candles), today+prior two (Morning Star, 3).
    engulf_low = pd.concat([low, low.shift(1)], axis=1).min(axis=1)
    star_low = pd.concat([low, low.shift(1), low.shift(2)], axis=1).min(axis=1)
    lowest_of_pattern = low.mask(engulf, engulf_low).mask(star, star_low)
    stop = lowest_of_pattern - tick
    risk = entry_level - stop
    target = entry_level + cfg.get("sw_target_rr", 2.0) * risk

    return pd.DataFrame({
        "score": score, "pattern_name": pattern_name.where(~disqualified, "None"),
        "entry_level": entry_level, "stop": stop, "target": target,
        "disqualified": disqualified,
    }, index=df.index)


def precompute_swing_signals(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Day-by-day stop-buy state machine, same shape as quant_pattern's
    precompute_quant_pattern_signals: a qualifying day (score >=
    sw_min_score) arms a pending order at entry_level for the VERY NEXT
    session only (the spec's own wording -- "during the subsequent
    session", singular, not a multi-day window); fills at
    max(entry_level, next day's open) if that day's high reaches it."""
    ind = precompute_swing_indicators(df, cfg)
    res = score_swing_confluence(df, cfg, ind)
    n = len(df)
    high = df["high"].to_numpy()
    open_ = df["open"].to_numpy()
    score = res["score"].to_numpy()
    disq = res["disqualified"].to_numpy()
    entry_level = res["entry_level"].to_numpy()
    stop = res["stop"].to_numpy()
    target = res["target"].to_numpy()
    pattern_name = res["pattern_name"].to_numpy()

    confirmed_entry = np.zeros(n, dtype=bool)
    out_entry_price = np.full(n, np.nan)
    out_stop = np.full(n, np.nan)
    out_target = np.full(n, np.nan)
    out_score = np.full(n, np.nan)
    out_pattern = np.full(n, "", dtype=object)
    out_signal_date = np.full(n, np.datetime64("NaT"), dtype="datetime64[ns]")

    pending = None
    for i in range(n):
        if pending is not None:
            if high[i] >= pending["entry_level"]:
                fill = max(pending["entry_level"], open_[i])
                confirmed_entry[i] = True
                out_entry_price[i] = fill
                out_stop[i] = pending["stop"]
                out_target[i] = pending["target"]
                out_score[i] = pending["score"]
                out_pattern[i] = pending["pattern_name"]
                out_signal_date[i] = pending["signal_date"]
            pending = None

        if not disq[i] and score[i] >= cfg.get("sw_min_score", 7):
            pending = {
                "entry_level": entry_level[i], "stop": stop[i], "target": target[i],
                "score": score[i], "pattern_name": pattern_name[i],
                "signal_date": df.index[i],
            }

    return pd.DataFrame({
        "confirmed_entry": confirmed_entry, "entry_price": out_entry_price,
        "stop": out_stop, "target": out_target, "score": out_score,
        "pattern_name": out_pattern, "signal_date": out_signal_date,
    }, index=df.index)
