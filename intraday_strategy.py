"""
Pure logic for the "DaysLowVolumnBreakout" intraday strategy -- ported
strictly from DaysLowVolumnBreakout_Strategy_Spec.md (E:\\trading-
workspace\\intraday-pullback-trading), NOT from that project's own
strategy.py/pullback.py modules. Those modules implement an older,
since-rejected variant (top-20 selection + a 200-EMA regime filter + an
EMA13/21/50/200 stack gate + a breakeven-stop-move) -- the spec's own
§9 explicitly lists all of these as tested and found worse. The
Spec-matching logic (top-2 selection, ratio-gated day bias, continuous
EMA21 invalidation, ATR14x5% buffer, no breakeven, no sector filter)
only ever existed in that project's disposable scratch_*.py scripts;
this module is a from-scratch, clean implementation of the same rules,
independently verified to reproduce those scripts' own 5-year published
results byte-for-byte (see verify_intraday_strategy.py).

No Kite/broker/dashboard imports here on purpose -- everything in this
module is pure pandas/stdlib, so it can be unit-tested and backtested
with zero live dependencies.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

LONG = "LONG"
SHORT = "SHORT"

# Spec.md §3.1 constants
SIGNAL_WINDOW_START = "09:35"
SIGNAL_WINDOW_END = "15:05"
NEW_SIGNAL_CUTOFF = dt.time(11, 0)
VOL_THRESHOLD_PCT = 0.05
ATR_PCT_BUFFER = 0.05
BREAKOUT_WINDOW = 1
ATR_PERIOD = 14
EMA_SPAN = 21
REWARD_RISK = 2.0
SQUAREOFF_TIME = "15:10"

# Spec.md §2.2 day-bias ratio gate
BIAS_RATIO_LONG_MIN = 2.0
BIAS_RATIO_SHORT_MAX = 0.5

# Spec.md §7 position sizing
MAX_TRADES_PER_DAY = 2
LEVERAGE = 5.0
MAX_RISK_PCT_PER_TRADE = 0.005

# Spec.md §8 cost model (Zerodha intraday equity)
BROKERAGE_PCT = 0.0003
BROKERAGE_CAP = 20.0
STT_SELL_PCT = 0.00025
EXCHANGE_TXN_PCT = 0.0000297
GST_PCT = 0.18


# ---------------------------------------------------------------------------
# §1 -- continuous (non session-reset) indicators
# ---------------------------------------------------------------------------

def ema21(close: pd.Series) -> pd.Series:
    """Continuous 5-min EMA21 over a symbol's whole multi-day history --
    NEVER reset per day (Spec.md §1). Must be computed once over the
    full series and looked up by timestamp, not recomputed from a
    single day's slice (that would silently produce a different,
    wrongly-cold-started value)."""
    return close.ewm(span=EMA_SPAN, adjust=False, min_periods=EMA_SPAN).mean()


def atr14(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Wilder's ATR, continuous over the whole series (Spec.md §1)."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# ---------------------------------------------------------------------------
# §2 -- daily candidate selection
# ---------------------------------------------------------------------------

def first15_return(close_0925: float, prev_day_close: float) -> float:
    """Spec.md §2.2 -- the 09:25-candle close (= price at 09:30 real
    time) vs the previous day's official close, as a percent."""
    return (close_0925 / prev_day_close - 1) * 100


def day_bias(nifty_ratio: float) -> str | None:
    """Spec.md §2.2's day-bias gate. `nifty_ratio` = advancers/decliners
    among NIFTY50 constituents by first15_return sign (ratio = +inf if
    decliners == 0). Returns None -- the day is SKIPPED entirely -- when
    the ratio doesn't clear either threshold. Do not loosen this to a
    plain >=1/<1 split; that weaker gate was tested and found worse
    (Spec.md §9, this exact threshold pair is the validated one)."""
    if nifty_ratio > BIAS_RATIO_LONG_MIN:
        return LONG
    if nifty_ratio < BIAS_RATIO_SHORT_MAX:
        return SHORT
    return None


def select_candidates(fno_ret_first15: pd.Series, bias: str, n: int = 2) -> pd.Series:
    """Spec.md §2.3-2.4 -- rank the F&O universe (NOT NIFTY50 -- that's
    only used for the breadth ratio above) by first15_return, best-first
    for LONG / worst-first for SHORT, and take the top `n`. No sector or
    trend filter (Spec.md §9.1 -- explicitly tested and rejected).

    An RVOL (relative volume) pre-filter was tried and backtested here
    (5yr replay, 2021-2026): it cut net P&L by 62% (Rs.15.7L -> Rs.6.0L)
    by swapping out the day's strongest first15m movers for weaker ones
    that merely had higher relative volume -- diluting exactly the
    signal this strategy depends on. Removed; do not re-add without a
    backtest showing it actually helps."""
    ranked = fno_ret_first15.sort_values(ascending=(bias == SHORT))
    return ranked.head(n)


# ---------------------------------------------------------------------------
# §3-§5 -- signal detection, entry/stop, target/exit management
# ---------------------------------------------------------------------------

def find_entry(day: pd.DataFrame, direction: str, ema21_series: pd.Series,
               atr14_series: pd.Series) -> dict | None:
    """Spec.md §3.2's walk-forward loop, for one candidate on one day.

    `day`: that symbol's 5-min OHLCV candles for the day, indexed by
    timestamp (any candles outside the signal window are ignored).
    `ema21_series`/`atr14_series`: this symbol's CONTINUOUS multi-day
    series (§1) -- looked up by timestamp, never recomputed per day.

    Returns {"signal_time", "entry_time", "entry_price", "stop_price"}
    on a triggered entry, else None (day invalidated, or no signal ever
    triggered by SIGNAL_WINDOW_END). At most one trade per candidate per
    day -- the moment an entry triggers, this returns immediately."""
    win = day.between_time(SIGNAL_WINDOW_START, SIGNAL_WINDOW_END)
    if win.empty:
        return None

    # "lowest volume so far today" resets each day at 09:15 (§1's one
    # session-scoped exception) -- NOT the same as the signal window,
    # which only starts at 09:35.
    day_open_ts = win.index[0].normalize() + pd.Timedelta(hours=9, minutes=15)
    vol_so_far_full = day.loc[day.index >= day_open_ts, "volume"]

    invalidated = False
    active_signal = None  # {"time", "hi", "lo", "atr"}
    breakout_counter = 0

    for ts, row in win.iterrows():
        # 1. EMA21 day-invalidation gate -- checked every candle,
        # regardless of any active signal (Spec.md §3.2.1, §9.5).
        e21 = ema21_series.get(ts)
        if pd.notna(e21):
            if direction == LONG and row["close"] < e21:
                invalidated = True
            elif direction == SHORT and row["close"] > e21:
                invalidated = True
        if invalidated:
            return None

        # 2. An active signal -- check this candle for the breakout trigger.
        if active_signal is not None:
            breakout_counter += 1
            buf = active_signal["atr"] * ATR_PCT_BUFFER
            if direction == LONG:
                trigger_level = active_signal["hi"] + buf
                triggered = row["high"] >= trigger_level
            else:
                trigger_level = active_signal["lo"] - buf
                triggered = row["low"] <= trigger_level
            if triggered:
                stop_price = ((active_signal["lo"] - buf) if direction == LONG
                             else (active_signal["hi"] + buf))
                return {"signal_time": active_signal["time"], "entry_time": ts,
                       "entry_price": trigger_level, "stop_price": stop_price}
            if breakout_counter >= BREAKOUT_WINDOW:
                active_signal = None
                breakout_counter = 0
            continue

        # 3. No active signal -- check whether THIS candle is a fresh
        # signal candle (only before NEW_SIGNAL_CUTOFF).
        if ts.time() > NEW_SIGNAL_CUTOFF:
            continue
        is_red = row["close"] < row["open"]
        is_green = row["close"] > row["open"]
        wants_color = is_red if direction == LONG else is_green
        vol_min_so_far = vol_so_far_full.loc[:ts].min()
        is_lowest_volume = row["volume"] <= vol_min_so_far * (1 + VOL_THRESHOLD_PCT)
        sig_atr = atr14_series.get(ts)
        if wants_color and is_lowest_volume and pd.notna(sig_atr) and sig_atr > 0:
            active_signal = {"time": ts, "hi": row["high"], "lo": row["low"], "atr": sig_atr}
            breakout_counter = 0

    return None


# ---------------------------------------------------------------------------
# Incremental (candle-by-candle) version of find_entry()'s walk-forward
# loop -- for live/paper use, where candles arrive one at a time rather
# than as a whole day at once. find_entry() itself stays untouched and
# is still what backtests/verification use; this is an additive,
# separately-verified equivalent (see verify_step_candle.py: replayed
# candle-by-candle, this produces IDENTICAL trigger decisions to
# find_entry() over the same 5-year ground truth).
# ---------------------------------------------------------------------------

def new_signal_state() -> dict:
    """A fresh per-candidate-per-day state for step_candle()."""
    return {"invalidated": False, "active_signal": None, "breakout_counter": 0}


def step_candle(state: dict, ts, row: pd.Series, direction: str, e21: float | None,
                sig_atr: float | None, vol_min_so_far: float) -> tuple[dict, dict | None]:
    """One incremental step of the §3.2 walk-forward loop. Does NOT
    mutate `state` -- returns a new state dict (caller keeps its own
    running copy, e.g. one per candidate per day).

    `e21`/`sig_atr`: this candle's own EMA21/ATR14 values (the caller
    looks these up from its continuous series, same as find_entry()).
    `vol_min_so_far`: the running minimum volume from 09:15 through and
    INCLUDING this candle -- the caller must update its own running min
    with this candle's volume BEFORE calling step_candle (find_entry()'s
    vol_so_far_full.loc[:ts].min() is inclusive of ts).

    Returns (new_state, event) -- event is None (nothing happened this
    candle) or one of:
      {"type": "invalidated"}
      {"type": "signal_formed", "time", "high", "low", "atr"}
      {"type": "signal_expired"}
      {"type": "triggered", "signal_time", "entry_time", "entry_price", "stop_price"}
    """
    if state["invalidated"]:
        return state, None
    state = dict(state)

    if pd.notna(e21):
        if direction == LONG and row["close"] < e21:
            state["invalidated"] = True
        elif direction == SHORT and row["close"] > e21:
            state["invalidated"] = True
    if state["invalidated"]:
        state["active_signal"] = None
        return state, {"type": "invalidated"}

    active = state["active_signal"]
    if active is not None:
        state["breakout_counter"] += 1
        buf = active["atr"] * ATR_PCT_BUFFER
        if direction == LONG:
            trigger_level = active["hi"] + buf
            triggered = row["high"] >= trigger_level
        else:
            trigger_level = active["lo"] - buf
            triggered = row["low"] <= trigger_level
        if triggered:
            stop_price = (active["lo"] - buf) if direction == LONG else (active["hi"] + buf)
            event = {"type": "triggered", "signal_time": active["time"], "entry_time": ts,
                    "entry_price": trigger_level, "stop_price": stop_price}
            state["active_signal"] = None
            return state, event
        if state["breakout_counter"] >= BREAKOUT_WINDOW:
            state["active_signal"] = None
            state["breakout_counter"] = 0
            return state, {"type": "signal_expired"}
        return state, None

    if ts.time() > NEW_SIGNAL_CUTOFF:
        return state, None
    is_red = row["close"] < row["open"]
    is_green = row["close"] > row["open"]
    wants_color = is_red if direction == LONG else is_green
    is_lowest_volume = row["volume"] <= vol_min_so_far * (1 + VOL_THRESHOLD_PCT)
    if wants_color and is_lowest_volume and pd.notna(sig_atr) and sig_atr > 0:
        state["active_signal"] = {"time": ts, "hi": row["high"], "lo": row["low"], "atr": sig_atr}
        state["breakout_counter"] = 0
        return state, {"type": "signal_formed", "time": ts, "high": row["high"],
                       "low": row["low"], "atr": sig_atr}
    return state, None


def check_tick_trigger(state: dict, direction: str, ltp: float, ts) -> dict | None:
    """Continuous (tick-driven) breakout-trigger check for use BETWEEN
    candle closes -- Spec.md §6.2 calls for trigger detection to be
    tick-driven rather than waiting for a candle to fully close (up to
    5 minutes of delay). Mirrors step_candle()'s own trigger check
    exactly (same trigger_level/stop_price formulas), just evaluated
    against a single live price instead of a closed candle's high/low.

    Does NOT mutate `state` and does not itself clear active_signal --
    the caller (which owns the tracker's state) does that, the same way
    it already does for step_candle()'s own "triggered" event, so both
    paths update state identically. Returns None if there's no active
    signal (or the tick hasn't reached the trigger level yet).

    Because a real intraday candle's high/low is simply the extreme of
    every tick within it, this can only trigger a candidate on the SAME
    candle step_candle() would have -- it just detects the crossing the
    moment a tick reaches it, instead of waiting for that candle to
    close, so it never changes which candle is the trigger candle."""
    if state.get("invalidated"):
        return None
    active = state.get("active_signal")
    if active is None:
        return None
    buf = active["atr"] * ATR_PCT_BUFFER
    if direction == LONG:
        trigger_level = active["hi"] + buf
        triggered = ltp >= trigger_level
    else:
        trigger_level = active["lo"] - buf
        triggered = ltp <= trigger_level
    if not triggered:
        return None
    stop_price = (active["lo"] - buf) if direction == LONG else (active["hi"] + buf)
    return {"type": "triggered", "signal_time": active["time"], "entry_time": ts,
           "entry_price": trigger_level, "stop_price": stop_price}


def target_price(entry: float, stop: float, direction: str,
                 reward_risk: float = REWARD_RISK) -> float:
    """Spec.md §5 -- flat reward:risk target."""
    risk = abs(entry - stop)
    return entry + reward_risk * risk if direction == LONG else entry - reward_risk * risk


def simulate_exit(day: pd.DataFrame, entry_time: pd.Timestamp, entry: float,
                  stop: float, direction: str, reward_risk: float = REWARD_RISK,
                  squareoff_time: str = SQUAREOFF_TIME) -> list[dict]:
    """Spec.md §5's half-target/half-squareoff exit management, walking
    forward from entry_time over the SAME day's candles. No breakeven
    move on the remaining half after target (§9.2 -- tested and found
    marginally worse). Returns a list of 1-2 leg dicts:
    {"exit_time", "exit_price", "reason", "qty_frac"} -- reason is one
    of "target"/"stop"/"squareoff"/"eod_data_end"."""
    target = target_price(entry, stop, direction, reward_risk)
    sq = day.between_time(squareoff_time, squareoff_time).index
    squareoff_ts = sq[0] if len(sq) else None

    legs: list[dict] = []
    target_taken = False
    remaining_frac = 1.0

    for ts, row in day[day.index > entry_time].iterrows():
        hit_stop = row["low"] <= stop if direction == LONG else row["high"] >= stop
        hit_target = row["high"] >= target if direction == LONG else row["low"] <= target

        if hit_stop:
            legs.append({"exit_time": ts, "exit_price": stop, "reason": "stop",
                        "qty_frac": remaining_frac})
            return legs

        if not target_taken and hit_target:
            legs.append({"exit_time": ts, "exit_price": target, "reason": "target",
                        "qty_frac": 0.5})
            remaining_frac = 0.5
            target_taken = True

        if squareoff_ts is not None and ts >= squareoff_ts:
            legs.append({"exit_time": ts, "exit_price": float(row["close"]),
                        "reason": "squareoff", "qty_frac": remaining_frac})
            return legs

    last = day.iloc[-1]
    legs.append({"exit_time": day.index[-1], "exit_price": float(last["close"]),
                "reason": "eod_data_end", "qty_frac": remaining_frac})
    return legs


# ---------------------------------------------------------------------------
# §7 -- position sizing
# ---------------------------------------------------------------------------

def position_size(capital_alloc: float, risk_budget: float, entry: float, stop: float) -> int:
    """Spec.md §7 -- min of risk-based and capital-based sizing,
    whichever binds first, floored to a whole share."""
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0 or entry <= 0:
        return 0
    qty_by_risk = int(risk_budget // risk_per_share)
    qty_by_capital = int(capital_alloc // entry)
    return max(min(qty_by_risk, qty_by_capital), 0)


def leg_quantities(qty: int, legs: list[dict]) -> list[int]:
    """Splits a whole-share `qty` across simulate_exit()'s legs,
    honoring each leg's qty_frac while keeping the total exact (any
    rounding remainder goes to the last leg)."""
    if len(legs) == 1:
        return [qty]
    first = int(qty * legs[0]["qty_frac"])
    return [first, qty - first]


# ---------------------------------------------------------------------------
# §8 -- transaction cost model
# ---------------------------------------------------------------------------

def round_trip_cost(entry_price: float, exit_price: float, qty: int) -> float:
    """Spec.md §8 -- total cost (brokerage + STT + exchange + GST) for
    one round-trip (one buy leg + one sell leg) of `qty` shares."""
    buy_turnover = entry_price * qty
    sell_turnover = exit_price * qty

    brokerage = (min(BROKERAGE_PCT * buy_turnover, BROKERAGE_CAP)
                + min(BROKERAGE_PCT * sell_turnover, BROKERAGE_CAP))
    stt = STT_SELL_PCT * sell_turnover
    exchange_txn = EXCHANGE_TXN_PCT * (buy_turnover + sell_turnover)
    gst = GST_PCT * (brokerage + exchange_txn)

    return brokerage + stt + exchange_txn + gst
