"""
Technical indicators used by the screener. Pure pandas/numpy — no TA-lib
dependency so it runs anywhere.
"""

from __future__ import annotations

import datetime as dt

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


def weekly_rsi(close: pd.Series, period: int = 14) -> float:
    """RSI(period) on weekly closes (W-FRI anchored, trading-week end) --
    the last (possibly still-forming) week's value, the same 'currently
    developing candle updates daily' convention charting platforms use
    for weekly/monthly indicators. NaN until there's at least period+1
    weekly closes of history."""
    weekly = close.resample("W-FRI").last().dropna()
    if len(weekly) < period + 1:
        return np.nan
    return float(rsi(weekly, period).iloc[-1])


def monthly_rsi(close: pd.Series, period: int = 14) -> float:
    """Same as weekly_rsi() but resampled to calendar month-end closes."""
    monthly = close.resample("ME").last().dropna()
    if len(monthly) < period + 1:
        return np.nan
    return float(rsi(monthly, period).iloc[-1])


def _higher_tf_trend_ok(resampled_close: pd.Series, period_primary: int = 200,
                        period_fallback: int = 50) -> float:
    """True if the latest (possibly still-forming) bar closed above its own
    EMA(period_primary); falls back to EMA(period_fallback) when there
    isn't enough resampled history for the primary period yet (e.g. early
    in a backtest -- a stock with 3 years of daily data has ~150-160
    weekly bars, short of 200). NaN (not even the fallback period's worth
    of history) rather than a default True/False -- same fail-closed
    convention as weekly_rsi/monthly_rsi above."""
    if len(resampled_close) >= period_primary:
        period = period_primary
    elif len(resampled_close) >= period_fallback:
        period = period_fallback
    else:
        return np.nan
    return bool(resampled_close.iloc[-1] > ema(resampled_close, period).iloc[-1])


def weekly_above_ema(close: pd.Series, period_primary: int = 200,
                     period_fallback: int = 50) -> float:
    weekly = close.resample("W-FRI").last().dropna()
    return _higher_tf_trend_ok(weekly, period_primary, period_fallback)


def monthly_above_ema(close: pd.Series, period_primary: int = 200,
                      period_fallback: int = 50) -> float:
    monthly = close.resample("ME").last().dropna()
    return _higher_tf_trend_ok(monthly, period_primary, period_fallback)


def weekly_snapshot(close: pd.Series, rsi_period: int = 14,
                    ema_primary: int = 200, ema_fallback: int = 50) -> tuple[float, float]:
    """RSI + above-EMA trend in one pass off a single weekly resample --
    compute_snapshot's hot path used to call weekly_rsi() and
    weekly_above_ema() separately, each resampling `close` to weekly on
    its own (profiled: resample() dominates each function's cost, so
    that was ~2x the resampling work needed). Returns (rsi, trend_ok),
    each independently NaN per its own insufficient-history rule."""
    weekly = close.resample("W-FRI").last().dropna()
    rsi_val = float(rsi(weekly, rsi_period).iloc[-1]) if len(weekly) >= rsi_period + 1 else np.nan
    return rsi_val, _higher_tf_trend_ok(weekly, ema_primary, ema_fallback)


def monthly_snapshot(close: pd.Series, rsi_period: int = 14,
                     ema_primary: int = 200, ema_fallback: int = 50) -> tuple[float, float]:
    """Same as weekly_snapshot() but off a single monthly resample."""
    monthly = close.resample("ME").last().dropna()
    rsi_val = float(rsi(monthly, rsi_period).iloc[-1]) if len(monthly) >= rsi_period + 1 else np.nan
    return rsi_val, _higher_tf_trend_ok(monthly, ema_primary, ema_fallback)


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


def compute_snapshot(df: pd.DataFrame, bench: pd.DataFrame, cfg: dict,
                     long_close: pd.Series | None = None) -> dict:
    """All technical metrics for one symbol from its daily candles.

    long_close: optional, deep (many-year) daily close history for the
    same symbol -- from backtest.load_long_history_cached() via
    screener.build_technical_table(). Used only for the weekly/monthly
    confirmation gate's RSI/EMA, which need far more history than a
    single backtest run's own `df` window usually has (a 200-bar monthly
    EMA needs ~16.7 years; even the 50-bar fallback needs ~4.2). None
    (default, and always the case for live callers today) falls back to
    `close` (this run's own window), same as before this param existed --
    those two indicators will then likely read NaN (fail-closed) unless
    `df` itself happens to be that deep."""
    if df.empty or len(df) < cfg["ema_slow"]:
        return {}

    close, volume = df["close"], df["volume"]
    ema_f = ema(close, cfg["ema_fast"])
    ema_s = ema(close, cfg["ema_slow"])
    macd_line, signal_line, hist = macd(close)
    atr_now = float(atr(df, cfg["atr_period"]).iloc[-1])
    price = float(close.iloc[-1])

    # Profiled: these 4 resample-based indicators alone were ~45% of a
    # real backtest's total runtime, computed unconditionally on every
    # symbol on every rebalance day even when weekly_monthly_gate_enabled
    # is off (the default) -- pure waste for any run not using that gate.
    # weekly_snapshot/monthly_snapshot also halve the resample cost when
    # it IS on, by sharing one resample per timeframe instead of the
    # naive 2 (rsi + above_ema each resampling independently).
    if cfg.get("weekly_monthly_gate_enabled", False):
        wk_close = long_close if long_close is not None else close
        weekly_rsi_val, weekly_trend_ok = weekly_snapshot(wk_close, cfg["rsi_period"])
        monthly_rsi_val, monthly_trend_ok = monthly_snapshot(wk_close, cfg["rsi_period"])
    else:
        weekly_rsi_val = monthly_rsi_val = np.nan
        weekly_trend_ok = monthly_trend_ok = np.nan

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
        "weekly_rsi": weekly_rsi_val,
        "monthly_rsi": monthly_rsi_val,
        "weekly_above_ema": weekly_trend_ok,
        "monthly_above_ema": monthly_trend_ok,
        "above_ema50": price > float(ema_f.iloc[-1]),
        "above_ema200": price > float(ema_s.iloc[-1]),
        "ema50_rising": float(ema_f.iloc[-1]) > float(ema_f.iloc[-6]),
        "macd_bullish": float(hist.iloc[-1]) > 0,
        "vol_expansion": volume_expansion(volume),
        "avg_volume_3m": float(volume.tail(cfg["mom_lookback_days_short"]).mean()),
        "atr": atr_now,
        "atr_pct": atr_now / price * 100,
        "suggested_stop": price - cfg["atr_stop_multiple"] * atr_now,
    }


def xirr(cash_flows: list[tuple[dt.date, float]]) -> float | None:
    """Annualized rate of return for irregularly-timed cash flows -- the
    correct generalization of CAGR (reduces to plain CAGR when there's a
    single initial deposit, stays accurate once monthly top-ups start,
    unlike a naive CAGR calc which assumes one lump sum). Convention:
    negative amounts are money going in (deposits), positive amounts are
    money coming out (today's portfolio value, treated as a hypothetical
    liquidation).

    Solves for the rate r making NPV == 0 via Newton-Raphson:
        sum(amount_i / (1 + r) ** (days_i / 365)) == 0
    Pure Python/math -- no scipy or numpy_financial dependency, consistent
    with this module's existing pattern of hand-rolled indicators.

    Returns None if there are fewer than 2 cash flows (nothing to
    annualize) or if Newton-Raphson fails to converge (e.g. all flows have
    the same sign, so no rate can zero the NPV)."""
    if len(cash_flows) < 2:
        return None

    t0 = cash_flows[0][0]
    years = [(d - t0).days / 365.0 for d, _ in cash_flows]
    amounts = [a for _, a in cash_flows]

    def npv(rate: float) -> float:
        return sum(a / (1 + rate) ** y for a, y in zip(amounts, years))

    def dnpv(rate: float) -> float:
        return sum(-y * a / (1 + rate) ** (y + 1)
                  for a, y in zip(amounts, years) if y > 0)

    rate = 0.1
    for _ in range(100):
        f = npv(rate)
        df_ = dnpv(rate)
        if df_ == 0:
            return None
        new_rate = rate - f / df_
        if new_rate <= -1:
            new_rate = (rate - 1) / 2  # halve the step toward the -1 boundary
        if abs(new_rate - rate) < 1e-9:
            return new_rate
        rate = new_rate
    return None
