"""
Pure logic for the "DaysLowVolumnBreakout" intraday strategy -- v3
(Top-5 Causal), ported strictly from
strategies/DaysLowVolumnBreakout_v3_Top5Causal_Spec.md, cross-checked
directly against its reference implementation (E:\\trading-workspace\\
intraday-pullback-trading\\scratch_baseline_top5_best.py). v3 changes
vs v2 (kept for history in DaysLowVolumnBreakout_v2_SectorGate_Spec.md):

1. Candidate pool widened from top-2/day to **top-5/day**, walked with
   a genuinely time-ordered ("causal") mechanism -- see
   step_candidates_causal() below. This replaces the old rank-ordered
   per-candidate independent scan, which had a real look-ahead bug (a
   late-firing higher-rank signal could claim a slot ahead of an
   early-firing lower-rank one -- impossible for a live system to know
   in advance). MAX_TRADES_PER_DAY stays 2 -- only the pool searched
   widened, not the number of trades taken.
2. Signal-candle volume tolerance loosened from 5% to 10%
   (VOL_THRESHOLD_PCT).
3. Breakout window widened from 1 to 2 candles (BREAKOUT_WINDOW), with
   a NEW requirement: every candle in that window must be the
   confirming/continuation color (green for LONG, red for SHORT -- the
   opposite of the signal candle's own pullback color). A single
   wrong-colored candle drops the signal immediately, it does not wait
   out the remaining window. The volume condition is checked ONLY at
   signal-candle detection, never re-checked on the breakout candles
   (tested and found dramatically worse if re-checked -- Spec v3 §6/§12).

First-candle gate, EMA21 gate, sector-confirmation gate (still strict
2.0/0.5, independent of the day-bias threshold), target/exit management,
and position sizing are UNCHANGED from v2.

No Kite/broker/dashboard imports here on purpose -- everything in this
module is pure pandas/stdlib, so it can be unit-tested and backtested
with zero live dependencies.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

LONG = "LONG"
SHORT = "SHORT"

# Spec v5 (Gated-2nd-Candle) §2 constants -- current baseline, supersedes
# v2/v3/v4 for paper trading. See strategies/
# DaysLowVolumnBreakout_v5_GatedSecondCandle_Spec.md.
SIGNAL_WINDOW_START = "09:25"  # v5: CHANGED from v3's "09:30" -- day-bias,
# sector-gate, and this candle's own shape are all resolved from the SAME
# 09:25-candle close, so allowing it as a signal candle isn't a look-ahead
# change (spec §3.1) -- verified: CAGR 21.51%->23.93%, PF 1.55->1.63,
# DD -8.34%->-7.57% on the same top-2 pool/rule otherwise.
SIGNAL_WINDOW_END = "15:05"
NEW_SIGNAL_CUTOFF = dt.time(11, 0)  # v5: REVERTED from v3's 10:30 -- that
# value does NOT transfer across pool sizes (spec §3.1's explicit sweep:
# 10:30 on this top-2/gated-2nd-candle rule scores CAGR 15.20%/PF 1.42,
# clearly worse than 11:00; 10:30 only won for the earlier top-5 pool).
VOL_THRESHOLD_PCT = 0.05  # v5: REVERTED from v3's 0.10 back to v2's 5%
# tolerance -- part of the v5 baseline rule set, not carried over from v3.
ATR_PCT_BUFFER = 0.05
BREAKOUT_WINDOW = 2  # v5's "gated-2nd-candle" entry (spec §2): window
# candle #1 checks only the trigger price-cross; if it doesn't trigger,
# THIS ALREADY-CLOSED candle is gated on confirming color + lower volume
# than the signal candle + staying within the signal candle's own
# high/low range before candle #2 gets a chance at the same trigger
# check. See step_candle()/find_entry()'s own trigger-checked-first
# ordering below -- this already matches v5's rule exactly (verified
# 2026-09-18): color/volume/range are only ever evaluated on a candle
# that has ALREADY closed without triggering, never on the candle
# currently being checked for its own trigger, which is what makes this
# genuinely real-time-executable rather than needing to see a candle's
# own close before acting on its own trigger touch (the v3/v4 flaw this
# spec found and rejected -- see spec §1).
ATR_PERIOD = 14
EMA_SPAN = 21
REWARD_RISK = 2.0
# SQUAREOFF_TIME is a CANDLE LABEL (open-time), not a real-clock time --
# the "15:10"-labeled candle covers 15:10:00-15:14:59 and closes at
# 15:15:00 real time (Spec v3 §9's explicit audit finding: a live system
# that naively squares off "at 15:10 real time" exits 5 minutes early on
# the wrong price). intraday_engine.py's live loop must trigger its
# force-squareoff at 15:15:00 real time to match this.
SQUAREOFF_TIME = "15:10"
TOP_N_CANDIDATES = 2  # v5: REVERTED from v3's 5 back to v2's top-2 -- v5
# spec §5's explicit re-test found the top-5 pool actually WORSE than
# top-2 on this same gated-2nd-candle rule once re-verified on a
# corrected candidate list (CAGR 17.93% vs top-2's 24.14%, drawdown
# -19.59% vs -11.46%) -- the top-5 pool's earlier-reported 27.96% CAGR
# was a stale-candidate-list artifact, not a real finding. With
# TOP_N_CANDIDATES == MAX_TRADES_PER_DAY (both 2), every searched
# candidate has a real shot at a slot again, matching v2's original
# plain top-2 design.

# Spec v2 §2.2 day-bias ratio gate -- CHANGED from v1's strict 2.0/0.5
# to this more moderate threshold (§9.4's sweep: neither the strict v1
# value nor a fully-relaxed 1.0/1.0 performed as well as this one).
BIAS_RATIO_LONG_MIN = 1.5
BIAS_RATIO_SHORT_MAX = 0.66

# Spec v2 §6.3 -- entry-time sector-confirmation gate threshold.
# Deliberately kept STRICT (2.0/0.5) independent of the day-bias
# threshold above -- §9.5's sweep found relaxing this to match a looser
# day-bias threshold consistently hurt performance in every combination
# tested. Checked only once a breakout has already triggered (§6),
# against a ratio computed once at 09:30 (see intraday_engine.py).
SECTOR_GATE_RATIO_LONG_MIN = 2.0
SECTOR_GATE_RATIO_SHORT_MAX = 0.5

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
    """Spec v2 §2.2's day-bias gate. `nifty_ratio` = advancers/decliners
    among NIFTY50 constituents by first15_return sign (ratio = +inf if
    decliners == 0). Returns None -- the day is SKIPPED entirely -- when
    the ratio doesn't clear either threshold. Do not casually retune
    BIAS_RATIO_LONG_MIN/SHORT_MAX -- the relationship between this
    threshold and outcome was NOT monotonic in Spec v2 §9.4's sweep."""
    if nifty_ratio > BIAS_RATIO_LONG_MIN:
        return LONG
    if nifty_ratio < BIAS_RATIO_SHORT_MAX:
        return SHORT
    return None


def sector_gate_pass(sector_ratio: float, direction: str) -> bool:
    """Spec v2 §6.3 -- the entry-time sector-confirmation gate itself
    (just the threshold check; intraday_engine.py owns resolving a
    candidate's primary sector and computing sector_ratio, since that
    needs sector-membership I/O this pure module deliberately has none
    of). Checked only once a candidate's breakout has already triggered
    (§6) -- a fail here drops that candidate's trade entirely, it does
    not block the OTHER candidate.

    Deliberately independent of day_bias()'s own (now-relaxed) threshold
    -- this stays at the strict 2.0/0.5 regardless (§9.5)."""
    if direction == LONG:
        return sector_ratio > SECTOR_GATE_RATIO_LONG_MIN
    return sector_ratio < SECTOR_GATE_RATIO_SHORT_MAX


def select_candidates(fno_ret_first15: pd.Series, bias: str, n: int = TOP_N_CANDIDATES) -> pd.Series:
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
               atr14_series: pd.Series, first_candle_low: float,
               first_candle_high: float) -> dict | None:
    """Spec v2 §3.2's walk-forward loop, for one candidate on one day.

    `day`: that symbol's 5-min OHLCV candles for the day, indexed by
    timestamp (any candles outside the signal window are ignored).
    `ema21_series`/`atr14_series`: this symbol's CONTINUOUS multi-day
    series (§1) -- looked up by timestamp, never recomputed per day.
    `first_candle_low`/`first_candle_high`: the day's very first (09:15)
    candle's own low/high, captured once before this loop runs (§3.2 v2
    step 1b) -- a fixed value for the whole day, not looked up per candle.

    Returns {"signal_time", "entry_time", "entry_price", "stop_price"}
    on a triggered entry, else None (day invalidated, or no signal ever
    triggered by SIGNAL_WINDOW_END). At most one trade per candidate per
    day -- the moment an entry triggers, this returns immediately."""
    win = day.between_time(SIGNAL_WINDOW_START, SIGNAL_WINDOW_END)
    if win.empty:
        return None

    # "lowest volume so far today" resets each day at 09:15 (§1's one
    # session-scoped exception) -- NOT the same as the signal window,
    # which starts at 09:30.
    day_open_ts = win.index[0].normalize() + pd.Timedelta(hours=9, minutes=15)
    vol_so_far_full = day.loc[day.index >= day_open_ts, "volume"]

    invalidated = False
    active_signal = None  # {"time", "hi", "lo", "atr"}
    breakout_counter = 0

    for ts, row in win.iterrows():
        # 1. EMA21 day-invalidation gate -- checked every candle,
        # regardless of any active signal (Spec §3.2 step 1, §9.5).
        e21 = ema21_series.get(ts)
        if pd.notna(e21):
            if direction == LONG and row["close"] < e21:
                invalidated = True
            elif direction == SHORT and row["close"] > e21:
                invalidated = True
        # 1b. NEW v2 -- first-candle-close-through invalidation gate,
        # same whole-day-kill-switch semantics as the EMA21 gate above,
        # just a different reference level (the day's own 09:15 candle).
        if direction == LONG and row["close"] < first_candle_low:
            invalidated = True
        elif direction == SHORT and row["close"] > first_candle_high:
            invalidated = True
        if invalidated:
            return None

        # 2. An active signal -- check this candle for the breakout trigger.
        if active_signal is not None:
            breakout_counter += 1
            # Trigger checked FIRST -- see step_candle()'s own comment for
            # why a range/volume gate can never be satisfied by the
            # actual triggering candle (breaking out necessarily exceeds
            # the signal candle's own high/low).
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
            # v3.1 NEW -- this window candle did NOT trigger. Before
            # continuing to the next window candle it must have been a
            # clean continuation of the signal candle's own pullback: the
            # confirming/continuation color (green for LONG, red for
            # SHORT), LOWER volume than the signal candle, and its own
            # high/low still WITHIN the signal candle's range -- fail any
            # of these and the signal drops immediately rather than
            # waiting out the remaining window candle(s) (Spec v3 §6
            # point 1, extended). See step_candle()'s own comment for the
            # live-vs-backtest rationale, kept identical here.
            confirm_is_green = row["close"] > row["open"]
            confirm_is_red = row["close"] < row["open"]
            wants_confirm_color = confirm_is_green if direction == LONG else confirm_is_red
            volume_ok = row["volume"] < active_signal["volume"]
            range_ok = row["high"] <= active_signal["hi"] and row["low"] >= active_signal["lo"]
            if not (wants_confirm_color and volume_ok and range_ok):
                active_signal = None
                breakout_counter = 0
                continue
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
            active_signal = {"time": ts, "hi": row["high"], "lo": row["low"], "atr": sig_atr,
                             "volume": row["volume"]}
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
                sig_atr: float | None, vol_min_so_far: float,
                first_candle_low: float, first_candle_high: float) -> tuple[dict, dict | None]:
    """One incremental step of the §3.2 walk-forward loop. Does NOT
    mutate `state` -- returns a new state dict (caller keeps its own
    running copy, e.g. one per candidate per day).

    `e21`/`sig_atr`: this candle's own EMA21/ATR14 values (the caller
    looks these up from its continuous series, same as find_entry()).
    `vol_min_so_far`: the running minimum volume from 09:15 through and
    INCLUDING this candle -- the caller must update its own running min
    with this candle's volume BEFORE calling step_candle (find_entry()'s
    vol_so_far_full.loc[:ts].min() is inclusive of ts).
    `first_candle_low`/`first_candle_high`: the day's 09:15 candle's own
    low/high (v2 §3.2 step 1b) -- fixed for the whole day, the caller
    captures it once and passes the same value on every call.

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
    # v2 NEW -- first-candle-close-through gate (§3.2 step 1b), same
    # whole-day-kill-switch semantics as the EMA21 gate just above.
    if not state["invalidated"]:
        if direction == LONG and row["close"] < first_candle_low:
            state["invalidated"] = True
        elif direction == SHORT and row["close"] > first_candle_high:
            state["invalidated"] = True
    if state["invalidated"]:
        state["active_signal"] = None
        return state, {"type": "invalidated"}

    active = state["active_signal"]
    if active is not None:
        state["breakout_counter"] += 1
        # Trigger is checked FIRST, before any quality gate -- a genuine
        # breakout candle necessarily exceeds the signal candle's own
        # high/low (that's what "trigger" means), so a range/volume gate
        # can never be satisfied BY the triggering candle itself; it only
        # makes sense as a check on a candle that did NOT trigger, to
        # decide whether the window continues to the next candle.
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
        # v3.1 -- this window candle did NOT trigger. Before letting the
        # window continue to the next candle, it must have been a clean
        # continuation of the signal candle's own pullback: the
        # confirming color (green for LONG, red for SHORT), LOWER volume
        # than the signal candle (not a fresh volume spike), and its own
        # high/low still WITHIN the signal candle's range (hasn't already
        # poked outside it without actually triggering). Fail any of
        # these and the signal drops immediately rather than waiting out
        # the remaining window candle(s). This is a CANDLE-CLOSE-only
        # check (a live tick has no "color"/final volume mid-candle) --
        # check_tick_trigger()'s own tick-driven trigger check
        # deliberately evaluates none of this, matching how the trigger
        # price-crossing itself is checked continuously while these can
        # only be confirmed at close. In practice this only actually
        # runs for a window candle that didn't already trigger via a live
        # tick during its own formation -- process_candle()'s own
        # tracker.done guard skips calling this entirely once a tick has
        # already triggered.
        confirm_is_green = row["close"] > row["open"]
        confirm_is_red = row["close"] < row["open"]
        wants_confirm_color = confirm_is_green if direction == LONG else confirm_is_red
        volume_ok = row["volume"] < active["volume"]
        range_ok = row["high"] <= active["hi"] and row["low"] >= active["lo"]
        if not (wants_confirm_color and volume_ok and range_ok):
            state["active_signal"] = None
            state["breakout_counter"] = 0
            return state, {"type": "signal_expired"}
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
        state["active_signal"] = {"time": ts, "hi": row["high"], "lo": row["low"], "atr": sig_atr,
                                  "volume": row["volume"]}
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

def round_trip_cost(entry_price: float, exit_price: float, qty: int,
                    direction: str = LONG) -> float:
    """Spec.md §8 -- total cost (brokerage + STT + exchange + GST) for
    one round-trip (one buy leg + one sell leg) of `qty` shares.

    `direction` (v3 fix, Spec v3 §9): STT applies to whichever leg is
    the actual SELL order, not always the exit leg. For LONG (buy then
    sell), that's the exit. For SHORT (short-sell then cover-buy), the
    SELL leg is the ENTRY, not the exit -- the previous default (always
    charging STT on exit_turnover) silently mischarged every SHORT
    trade, which this strategy trades more often than LONG (day-bias/
    sector-gate mechanics naturally skew SHORT). `direction` defaults to
    LONG only for source-compatibility with any pre-v3 caller that
    doesn't pass it; live/backtest code should always pass it explicitly."""
    buy_turnover = entry_price * qty
    sell_turnover = exit_price * qty
    stt_turnover = sell_turnover if direction == LONG else buy_turnover

    brokerage = (min(BROKERAGE_PCT * buy_turnover, BROKERAGE_CAP)
                + min(BROKERAGE_PCT * sell_turnover, BROKERAGE_CAP))
    stt = STT_SELL_PCT * stt_turnover
    exchange_txn = EXCHANGE_TXN_PCT * (buy_turnover + sell_turnover)
    gst = GST_PCT * (brokerage + exchange_txn)

    return brokerage + stt + exchange_txn + gst
