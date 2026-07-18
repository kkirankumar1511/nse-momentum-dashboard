"""
Technical indicators used by the screener. Pure pandas/numpy — no TA-lib
dependency so it runs anywhere.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line


def momentum_return(close: pd.Series, lookback: int, skip: int = 0) -> float:
    """Return over `lookback` trading days, skipping the most recent `skip`
    days (classic 12-2 / 6-1 momentum construction to avoid the short-term
    reversal effect documented by Jegadeesh 1990)."""
    if len(close) < lookback + skip + 1:
        return np.nan
    end = close.iloc[-1 - skip]
    start = close.iloc[-1 - skip - lookback]
    return (end / start - 1) * 100


def pct_of_52w_high(close: pd.Series) -> float:
    window = close.tail(252)
    return float(close.iloc[-1] / window.max()) if len(window) else np.nan


def volume_expansion(volume: pd.Series, short: int = 20, long: int = 60) -> float:
    if len(volume) < long:
        return np.nan
    return float(volume.tail(short).mean() / volume.tail(long).mean())


def relative_strength(close: pd.Series, bench_close: pd.Series,
                      lookback: int) -> float:
    """Stock return minus benchmark return over `lookback` days (in pct pts)."""
    aligned = pd.concat([close, bench_close], axis=1, join="inner").dropna()
    if len(aligned) < lookback + 1:
        return np.nan
    s, b = aligned.iloc[:, 0], aligned.iloc[:, 1]
    stock_ret = (s.iloc[-1] / s.iloc[-1 - lookback] - 1) * 100
    bench_ret = (b.iloc[-1] / b.iloc[-1 - lookback] - 1) * 100
    return stock_ret - bench_ret


def compute_snapshot(df: pd.DataFrame, bench: pd.DataFrame, cfg: dict) -> dict:
    """All technical metrics for one symbol from its daily candles."""
    if df.empty or len(df) < cfg["ema_slow"]:
        return {}

    close, volume = df["close"], df["volume"]
    ema_f = ema(close, cfg["ema_fast"])
    ema_s = ema(close, cfg["ema_slow"])
    macd_line, signal_line, hist = macd(close)
    atr_now = float(atr(df, cfg["atr_period"]).iloc[-1])
    price = float(close.iloc[-1])

    return {
        "price": price,
        "mom_3m": momentum_return(close, cfg["mom_lookback_days_short"],
                                  cfg["skip_recent_days"]),
        "mom_6m": momentum_return(close, cfg["mom_lookback_days_long"],
                                  cfg["skip_recent_days"]),
        "rs_3m": relative_strength(close, bench["close"],
                                   cfg["mom_lookback_days_short"]),
        "rs_6m": relative_strength(close, bench["close"],
                                   cfg["mom_lookback_days_long"]),
        "pct_52w_high": pct_of_52w_high(close),
        "rsi": float(rsi(close, cfg["rsi_period"]).iloc[-1]),
        "above_ema50": price > float(ema_f.iloc[-1]),
        "above_ema200": price > float(ema_s.iloc[-1]),
        "ema50_rising": float(ema_f.iloc[-1]) > float(ema_f.iloc[-6]),
        "macd_bullish": float(hist.iloc[-1]) > 0,
        "vol_expansion": volume_expansion(volume),
        "atr": atr_now,
        "atr_pct": atr_now / price * 100,
        "suggested_stop": price - cfg["atr_stop_multiple"] * atr_now,
    }
