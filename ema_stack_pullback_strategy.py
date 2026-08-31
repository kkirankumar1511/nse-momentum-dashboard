"""
EMA13/21/50/200 stack-alignment + RSI-crossing signal-candle strategy.
A SEPARATE strategy from quant_pattern.py/swing_confluence_strategy.py --
no shared scoring, no shared entry mechanics, only reuses
`indicators.ema`/`rsi` and the existing Rank-1 watchlist pipeline
(backtest.rank_universe_asof).

2026-08-27 rewrite, explicit request (verbatim rule list):
  "when EMA-stack-crossover, my EMA 13>EMA 21 > EMA 50 > EMA 200
   1. lets make signal candle as when prev candle RSI is below 60 and
      signal candle RSI is above 59.50, then mark that candle as signal
      candle
   2. wait for next 3 candle, the candle close is above signal candle
      take the entry at close
   3. stop loss, EMA 13 with smal threshold
   4. target 1:2"
This fully replaces the prior version's ATR*2/ATR*3 trailing-stop exit
and "first RSI>60 same-day entry" trigger.

2026-08-27 addition, explicit request ("add 1 more condition from prior
10 days atleat 1 days RSI should have below RSI 50"): a signal candle
must also have at least one day in the prior 10 sessions where RSI
dipped below 50 -- requires a genuine recent momentum reset/pullback,
not a stock whose RSI has been continuously elevated the whole time.

Design choices made where the source spec left a gap (flagged, not
verified facts):
  - Stack gate: EMA13>EMA21>EMA50>EMA200 must hold on the signal candle
    itself (unchanged carry-over from the prior version) -- the spec
    doesn't restate this explicitly in the new rule list, but the
    opening line ("when EMA-stack-crossover, my EMA13>21>50>200") reads
    as re-affirming the same precondition, not dropping it.
  - "prev candle RSI below 60 and signal candle RSI above 59.50" is an
    intentionally asymmetric threshold (not a clean 60-crossing) -- read
    literally: rsi[i-1] < 60 AND rsi[i] >= 59.50. A candle can qualify
    even with a marginal RSI dip-then-recover inside that half-point
    band, which a strict ">=60 both sides" test would miss.
  - "wait for next 3 candle, the candle close is above signal candle" is
    read as: close > the SIGNAL candle's own close (not its high) --
    the first of the next 3 candles to satisfy this fires entry at that
    candle's own close. No qualifying close within 3 candles => the
    signal expires unfilled.
  - Only one signal watched at a time: a new signal candle can't arm
    while a previous one's 3-candle confirmation window is still open --
    matches the "first signal, then wait" sequential-state-machine
    convention used throughout this codebase's other pattern strategies.
  - "stop loss, EMA13 with small threshold" reuses this codebase's
    existing buffer-below-a-level convention (e.g. quant_pattern's
    qp_stop_buffer_pct): stop = EMA13 (as of the ENTRY day) * (1 -
    es_stop_buffer_pct/100).
  - "target 1:2" = fixed risk/reward, target = entry + 2*(entry - stop),
    same convention as swing_confluence_strategy's sw_target_rr.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import indicators

EMA_STACK_DEFAULTS: dict = {
    "es_ema_fast": 13,
    "es_ema_mid": 21,
    "es_ema_trend": 50,
    "es_ema_slow": 200,
    "es_rsi_period": 14,
    "es_rsi_prev_max": 60.0,
    "es_rsi_signal_min": 59.50,
    "es_confirm_window": 3,
    "es_stop_buffer_pct": 0.2,
    "es_target_rr": 2.0,
    "es_prior_low_rsi_lookback": 10,
    "es_prior_low_rsi_max": 50.0,
    "es_entry_buffer_pct": 0.2,
}


def precompute_ema_stack_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    close = df["close"]
    ema_fast = indicators.ema(close, cfg.get("es_ema_fast", 13))
    ema_mid = indicators.ema(close, cfg.get("es_ema_mid", 21))
    ema_trend = indicators.ema(close, cfg.get("es_ema_trend", 50))
    ema_slow = indicators.ema(close, cfg.get("es_ema_slow", 200))
    rsi = indicators.rsi(close, cfg.get("es_rsi_period", 14))
    stack_ok = (ema_fast > ema_mid) & (ema_mid > ema_trend) & (ema_trend > ema_slow)
    return pd.DataFrame({
        "ema_fast": ema_fast, "ema_mid": ema_mid, "ema_trend": ema_trend,
        "ema_slow": ema_slow, "rsi": rsi, "stack_ok": stack_ok,
    }, index=df.index)


def precompute_ema_stack_signals(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Day-by-day state machine: a signal candle (stack_ok, prior-day
    RSI < es_rsi_prev_max, today's RSI >= es_rsi_signal_min, AND at
    least one of the prior es_prior_low_rsi_lookback (10) days had RSI
    below es_prior_low_rsi_max (50) -- explicit request, to require a
    genuine recent momentum reset/pullback rather than a stock that's
    been continuously strong) arms a 3-candle confirmation window; the
    first of the next es_confirm_window candles whose close clears the
    signal candle's own close by es_entry_buffer_pct (0.2%, explicit
    request -- a real breakout past the signal candle, not a marginal
    tick above it) fires confirmed_entry at THAT candle's close, with
    stop = that day's EMA13 * (1 - es_stop_buffer_pct/100) and a fixed
    1:2 target. Only one signal is watched at a time -- a new one can't
    arm while a confirmation window is still open."""
    ind = precompute_ema_stack_indicators(df, cfg)
    n = len(df)
    close = df["close"].to_numpy()
    stack_ok = ind["stack_ok"].to_numpy()
    rsi = ind["rsi"].to_numpy()
    ema_fast = ind["ema_fast"].to_numpy()
    prev_max = cfg.get("es_rsi_prev_max", 60.0)
    signal_min = cfg.get("es_rsi_signal_min", 59.50)
    window = cfg.get("es_confirm_window", 3)
    stop_buffer = cfg.get("es_stop_buffer_pct", 0.2)
    target_rr = cfg.get("es_target_rr", 2.0)
    low_rsi_lookback = cfg.get("es_prior_low_rsi_lookback", 10)
    low_rsi_max = cfg.get("es_prior_low_rsi_max", 50.0)
    had_low_rsi_recently = (
        (ind["rsi"] < low_rsi_max).shift(1).rolling(low_rsi_lookback).max().fillna(0).astype(bool)
    ).to_numpy()
    entry_buffer_pct = cfg.get("es_entry_buffer_pct", 0.2)

    confirmed_entry = np.zeros(n, dtype=bool)
    out_entry_price = np.full(n, np.nan)
    out_stop = np.full(n, np.nan)
    out_target = np.full(n, np.nan)
    out_signal_date = np.full(n, np.datetime64("NaT"), dtype="datetime64[ns]")

    pending = None  # {"signal_close": float, "trigger_level": float, "days_left": int, "signal_date": Timestamp}
    for i in range(n):
        if pending is not None:
            if close[i] > pending["trigger_level"] and not np.isnan(ema_fast[i]):
                entry_price = close[i]
                stop = ema_fast[i] * (1 - stop_buffer / 100)
                risk = entry_price - stop
                confirmed_entry[i] = True
                out_entry_price[i] = entry_price
                out_stop[i] = stop
                out_target[i] = entry_price + target_rr * risk
                out_signal_date[i] = pending["signal_date"]
                pending = None
            else:
                pending["days_left"] -= 1
                if pending["days_left"] <= 0:
                    pending = None

        if pending is None and stack_ok[i] and not np.isnan(rsi[i]):
            prev_rsi = rsi[i - 1] if i > 0 else np.nan
            if (not np.isnan(prev_rsi) and prev_rsi < prev_max and rsi[i] >= signal_min
                    and had_low_rsi_recently[i]):
                pending = {"signal_close": close[i],
                          "trigger_level": close[i] * (1 + entry_buffer_pct / 100),
                          "days_left": window, "signal_date": df.index[i]}

    return pd.DataFrame({
        "confirmed_entry": confirmed_entry, "entry_price": out_entry_price,
        "stop": out_stop, "target": out_target, "signal_date": out_signal_date,
    }, index=df.index)
