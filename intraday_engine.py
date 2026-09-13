"""
Live/paper orchestration for the "DaysLowVolumnBreakout" intraday
strategy -- run as `python intraday_engine.py` during real market hours
(09:15-15:15 IST). Paper mode (default) simulates every fill at the
exact computed price, no Kite orders; live mode (gated off by default,
see config.STRATEGY["intraday_live_enabled"]) places real MIS orders.

Structured so the DECISION logic (given data already fetched) is
independently testable without wall-clock waits or a real Kite session
-- see verify_intraday_engine.py, which replays one real historical
day's actual 5-min candles through run_selection()/process_candle()/
check_intracandle_exit() and confirms the engine reproduces the same
entries/exits the already-verified batch backtest (intraday_strategy.
find_entry()/simulate_exit()) produces for that day. Only run_live()
itself (the real-time wall-clock loop) is unverifiable outside real
market hours.
"""

from __future__ import annotations

import datetime as dt
import time

import pandas as pd

import config
import intraday_db as idb
import intraday_market as mkt
import intraday_strategy as strat
import live_ticker
import kite_client
import nse_holidays

EMA_WARMUP_DAYS = 120  # >> "several weeks" Spec.md §1 asks for, comfortably covers EMA21/ATR14 warmup
DEFAULT_PAPER_CAPITAL = 1_000_000.0
# How often the loop wakes up to check the ticker's in-memory cache and
# wall-clock conditions (candle boundary, 15:10 squareoff) -- NOT a network
# poll cadence any more (see live_ticker.py): trigger/stop/target prices
# themselves come from live WebSocket ticks the instant they arrive, this
# just bounds how quickly the loop notices them.
CHECK_INTERVAL_SECONDS = 1
TICK_STALE_SECONDS = 20  # fall back to a REST get_ltp() if the feed goes quiet this long


# ---------------------------------------------------------------------------
# §2 -- day-bias + candidate selection (testable: pass in already-fetched data)
# ---------------------------------------------------------------------------

def run_selection(date: str, nifty50_symbols: list[str], fno_symbols: list[str],
                  close_0925: dict[str, float], prev_day_close: dict[str, float],
                  mode: str) -> dict:
    """Spec.md §2, given today's 09:25-candle closes + previous closes
    already fetched for the full nifty50 UNION fno universe. Writes
    intraday_days/intraday_daily_selection and returns
    {"nifty_ratio", "day_bias", "candidates": [{"symbol","direction"}]}
    -- candidates is empty when day_bias is None (skip day)."""
    nifty_ratio, _ = mkt.compute_first15_breadth(nifty50_symbols, close_0925, prev_day_close)
    bias = strat.day_bias(nifty_ratio)
    idb.record_day(date, nifty_ratio, bias)

    if bias is None:
        return {"nifty_ratio": nifty_ratio, "day_bias": None, "candidates": []}

    fno_rets = {}
    for sym in fno_symbols:
        c0925, prev_close = close_0925.get(sym), prev_day_close.get(sym)
        if c0925 is None or prev_close is None or prev_close == 0:
            continue
        fno_rets[sym] = strat.first15_return(c0925, prev_close)

    top = strat.select_candidates(pd.Series(fno_rets), bias, n=strat.MAX_TRADES_PER_DAY)
    candidates = [{"symbol": sym, "direction": bias} for sym in top.index]
    idb.record_candidates(date, [
        {"rank": i + 1, "symbol": sym, "ret_first15_pct": round(float(ret), 3)}
        for i, (sym, ret) in enumerate(top.items())])
    return {"nifty_ratio": nifty_ratio, "day_bias": bias, "candidates": candidates}


# ---------------------------------------------------------------------------
# Per-candidate tracking
# ---------------------------------------------------------------------------

class CandidateTracker:
    """Live state for one of the day's (at most 2) candidates -- the
    engine keeps one of these per candidate, updated as candles close
    and (once a position opens) as LTP ticks arrive."""

    def __init__(self, date: str, symbol: str, direction: str,
                ema21_series: pd.Series, atr14_series: pd.Series):
        self.date = date
        self.symbol = symbol
        self.direction = direction
        self.ema21_series = ema21_series
        self.atr14_series = atr14_series
        self.signal_state = strat.new_signal_state()
        self.vol_min_so_far: float | None = None
        self.signal_db_id: int | None = None
        self.position_id: int | None = None
        self.done = False  # invalidated, or position fully closed -- nothing left to do today


def process_candle(tracker: CandidateTracker, ts: pd.Timestamp, row: pd.Series,
                   capital_alloc: float, risk_budget: float, mode: str) -> dict | None:
    """One closed 5-min candle for one candidate -- advances its signal
    state (step_candle()) and, on a trigger, sizes and records the new
    position. Returns the event dict (see step_candle()'s docstring),
    or a {"type": "position_opened", ...} event on a sized entry, or
    None if nothing happened or tracker.done already.

    Caller must update tracker.vol_min_so_far with this candle's own
    volume (running min, inclusive) BEFORE calling this -- see
    step_candle()'s docstring for why (the running min starts at 09:15,
    three candles before the signal window itself opens at 09:35)."""
    if tracker.done or tracker.position_id is not None:
        # A position is already open (triggered by this same candle's
        # close just above, or by a live tick before this candle even
        # closed -- see check_tick_entry()) -- step_candle() would
        # otherwise keep hunting for a brand-new signal candle on every
        # subsequent close and could trigger a SECOND, unmanaged entry
        # for a candidate that already has one open (each candidate
        # gets at most one position per day, Spec.md §4).
        return None
    e21 = tracker.ema21_series.get(ts)
    sig_atr = tracker.atr14_series.get(ts)
    new_state, event = strat.step_candle(
        tracker.signal_state, ts, row, tracker.direction, e21, sig_atr, tracker.vol_min_so_far)
    tracker.signal_state = new_state

    if event is None:
        return None

    if event["type"] == "invalidated":
        if tracker.signal_db_id is not None:
            idb.update_signal_status(tracker.signal_db_id, "invalidated")
        tracker.done = True
        return event

    if event["type"] == "signal_formed":
        tracker.signal_db_id = idb.create_signal(
            tracker.date, tracker.symbol, str(event["time"]), event["high"], event["low"], event["atr"])
        return event

    if event["type"] == "signal_expired":
        if tracker.signal_db_id is not None:
            idb.update_signal_status(tracker.signal_db_id, "expired")
        tracker.signal_db_id = None
        return event

    if event["type"] == "triggered":
        if tracker.signal_db_id is not None:
            idb.update_signal_status(tracker.signal_db_id, "triggered")
        return _open_position_from_trigger(tracker, event, capital_alloc, risk_budget, mode)

    return event


def _open_position_from_trigger(tracker: CandidateTracker, event: dict,
                                capital_alloc: float, risk_budget: float, mode: str) -> dict:
    """Shared by both trigger paths -- process_candle()'s candle-close
    "triggered" event (step_candle()) and check_tick_trigger()'s live-
    tick equivalent -- so a breakout is sized/recorded identically no
    matter which one detected it first."""
    entry_price, stop_price = event["entry_price"], event["stop_price"]
    qty = strat.position_size(capital_alloc, risk_budget, entry_price, stop_price)
    if qty <= 0:
        tracker.done = True
        return {"type": "entry_skipped_zero_qty"}
    target = strat.target_price(entry_price, stop_price, tracker.direction)
    order_id = None
    if mode == "live":
        side = "BUY" if tracker.direction == strat.LONG else "SELL"
        order_id = kite_client.place_order(
            tracker.symbol, qty, side, product="MIS", order_type="SL",
            price=entry_price, trigger_price=entry_price)
    tracker.position_id = idb.record_new_position(
        tracker.date, tracker.symbol, tracker.direction, str(event["entry_time"]),
        entry_price, stop_price, target, qty, mode,
        signal_time=str(event["signal_time"]), order_id=order_id)
    return {"type": "position_opened", "position_id": tracker.position_id,
           "qty": qty, "entry_price": entry_price, "stop_price": stop_price, "target": target}


def check_tick_entry(tracker: CandidateTracker, ltp: float, now: dt.datetime,
                     capital_alloc: float, risk_budget: float, mode: str) -> dict | None:
    """Tick-driven counterpart to process_candle()'s candle-close trigger
    check (Spec.md §6.2) -- called on every live tick for a candidate
    that has an active signal but no position yet, so a breakout is
    caught the instant price crosses the trigger level rather than
    waiting up to 5 minutes for the candle to close. No-ops once
    tracker.done or a position already exists."""
    if tracker.done or tracker.position_id is not None:
        return None
    event = strat.check_tick_trigger(tracker.signal_state, tracker.direction, ltp, now)
    if event is None:
        return None
    # Mirror step_candle()'s own state mutation on trigger (clears
    # active_signal) so the candle-close path, if it still runs for this
    # boundary, doesn't see a stale active signal and re-trigger it.
    tracker.signal_state = dict(tracker.signal_state, active_signal=None, breakout_counter=0)
    if tracker.signal_db_id is not None:
        idb.update_signal_status(tracker.signal_db_id, "triggered")
    return _open_position_from_trigger(tracker, event, capital_alloc, risk_budget, mode)


def check_intracandle_exit(tracker: CandidateTracker, ltp: float, now: dt.datetime,
                           mode: str) -> dict | None:
    """Spec.md §6.2 -- between candle closes, watch LTP against the open
    position's stop/target continuously rather than waiting for the
    next 5-min candle. No-ops if this tracker has no open position."""
    if tracker.position_id is None:
        return None
    pos = idb.get_position(tracker.position_id)
    if pos is None or pos["status"] != "open":
        return None

    direction = pos["direction"]
    hit_stop = ltp <= pos["stop_price"] if direction == strat.LONG else ltp >= pos["stop_price"]
    hit_target = ltp >= pos["target_price"] if direction == strat.LONG else ltp <= pos["target_price"]
    # A position already down to its runner half (qty_remaining < qty) has
    # already taken its target leg -- only the stop (or 15:10) applies now.
    already_took_target = pos["qty_remaining"] < pos["qty"]

    if hit_stop:
        return _close_leg(tracker, pos, "stop", pos["qty_remaining"], pos["stop_price"], now, mode)
    if hit_target and not already_took_target:
        half = pos["qty"] // 2
        return _close_leg(tracker, pos, "target", half, pos["target_price"], now, mode)
    return None


def force_squareoff(tracker: CandidateTracker, ltp: float, now: dt.datetime, mode: str) -> dict | None:
    """Spec.md §5.4 -- 15:10 force-close, time-driven regardless of price."""
    if tracker.position_id is None:
        return None
    pos = idb.get_position(tracker.position_id)
    if pos is None or pos["status"] != "open":
        return None
    return _close_leg(tracker, pos, "squareoff", pos["qty_remaining"], ltp, now, mode)


def _close_leg(tracker: CandidateTracker, pos: dict, leg_type: str, qty: int,
              exit_price: float, now: dt.datetime, mode: str) -> dict:
    side = "SELL" if pos["direction"] == strat.LONG else "BUY"
    order_id = None
    if mode == "live":
        order_id = kite_client.place_order(pos["symbol"], qty, side, product="MIS", order_type="MARKET")
    sign = 1 if pos["direction"] == strat.LONG else -1
    gross = (exit_price - pos["entry_price"]) * qty * sign
    cost = strat.round_trip_cost(pos["entry_price"], exit_price, qty)
    net = gross - cost
    idb.close_position_leg(tracker.position_id, leg_type, qty, exit_price, str(now), gross, cost, net, order_id)
    if leg_type in ("stop", "squareoff"):
        tracker.done = True
    return {"type": "leg_closed", "leg_type": leg_type, "qty": qty, "exit_price": exit_price, "net_pnl": net}


# ---------------------------------------------------------------------------
# Real-time loop -- run_live() itself is not unit-testable (wall-clock,
# real Kite session); everything it calls above is.
# ---------------------------------------------------------------------------

def _prev_close_and_0925(symbols: list[str], today: dt.date) -> tuple[dict[str, float], dict[str, float]]:
    """Fetches, for each symbol: previous trading day's daily close, and
    today's 09:25-candle close (= price at 09:30 real time). A symbol
    missing either is silently excluded (matches the validated scratch
    script's own dropna behavior, see intraday_strategy.day_bias's
    caller in run_selection())."""
    close_0925, prev_close = {}, {}
    today_ts = pd.Timestamp(today)
    for sym in symbols:
        try:
            daily = kite_client.fetch_daily_candles(sym, days=10)
            prior = daily[daily.index.normalize() < today_ts]
            if not prior.empty:
                prev_close[sym] = float(prior["close"].iloc[-1])
            intraday = kite_client.fetch_intraday_candles(sym, days=2, interval="5minute")
            row = intraday[intraday.index == today_ts + pd.Timedelta(hours=9, minutes=25)]
            if not row.empty:
                close_0925[sym] = float(row["close"].iloc[0])
        except Exception as e:
            print(f"[intraday_engine] {sym}: prev_close/0925 fetch failed -- {e}")
        time.sleep(0.1)
    return close_0925, prev_close


def _wait_until(target: dt.time) -> None:
    now = dt.datetime.now()
    target_dt = dt.datetime.combine(now.date(), target)
    if now < target_dt:
        time.sleep((target_dt - now).total_seconds())


def _last_closed_candle_label(now: dt.datetime) -> dt.datetime:
    """The label (start time) of the most recently FULLY CLOSED 5-min
    candle as of `now` -- e.g. at 09:37, the candle labeled 09:30
    (covering 09:30-09:35) is the last one closed; the candle labeled
    09:35 (covering 09:35-09:40) is still forming. Getting this off by
    one candle would make the engine try to process a bar that hasn't
    closed yet (caught during review, before ever running live)."""
    floor = now.replace(second=0, microsecond=0) - dt.timedelta(minutes=now.minute % 5)
    return floor - dt.timedelta(minutes=5)


def _get_live_ltp(ticker: live_ticker.LiveTicker, token: int, symbol: str) -> float | None:
    """Prefers the live WebSocket tick; falls back to a one-off REST
    get_ltp() call if the feed has gone stale (e.g. mid-reconnect) or
    hasn't produced a tick for this token yet, so a quiet patch in the
    feed can't silently freeze trigger/stop/target checks."""
    age = ticker.last_tick_age(token)
    if age is not None and age <= TICK_STALE_SECONDS:
        return ticker.get_ltp(token)
    try:
        return kite_client.get_ltp([symbol])[symbol]
    except Exception as e:
        print(f"[intraday_engine] {symbol}: REST LTP fallback failed -- {e}")
        return ticker.get_ltp(token)


def run_live(mode: str = "paper") -> None:
    """Entry point: `python intraday_engine.py` (paper mode) during real
    market hours. Idles/exits immediately on a non-trading day."""
    today = dt.date.today()
    if not nse_holidays.is_trading_day(today):
        print(f"{dt.datetime.now():%d %b %Y %H:%M:%S} Not an NSE trading day -- exiting.")
        return
    date_str = today.isoformat()
    idb.ensure_capital_seeded(mode, DEFAULT_PAPER_CAPITAL)

    print(f"Waiting for 09:30 ({dt.datetime.now():%H:%M:%S} now)...")
    _wait_until(dt.time(9, 30))

    nifty50 = mkt.fetch_nifty50_constituents()
    fno_syms = list(config.UNIVERSE)
    all_syms = sorted(set(nifty50) | set(fno_syms))
    print(f"Fetching 09:25 close + prev close for {len(all_syms)} symbols...")
    close_0925, prev_close = _prev_close_and_0925(all_syms, today)

    sel = run_selection(date_str, nifty50, fno_syms, close_0925, prev_close, mode)
    print(f"nifty_ratio={sel['nifty_ratio']:.2f}  day_bias={sel['day_bias']}  "
         f"candidates={sel['candidates']}")
    if sel["day_bias"] is None:
        print("No clear day bias -- no trading today.")
        return
    if not sel["candidates"]:
        print("Day bias set but no valid candidates -- no trading today.")
        return

    capital = idb.get_capital(mode)["current_capital"]
    capital_alloc = (capital / strat.MAX_TRADES_PER_DAY) * strat.LEVERAGE
    risk_budget = capital * strat.MAX_RISK_PCT_PER_TRADE

    trackers = []
    for c in sel["candidates"]:
        sym = c["symbol"]
        hist = kite_client.fetch_intraday_candles(sym, days=EMA_WARMUP_DAYS, interval="5minute")
        ema21_series = strat.ema21(hist["close"])
        atr14_series = strat.atr14(hist)
        t = CandidateTracker(date_str, sym, c["direction"], ema21_series, atr14_series)
        # Seed the running vol-min from today's pre-window candles (09:15-
        # 09:30) -- see step_candle()'s docstring; the running min starts
        # at session open, not at the 09:35 signal-window start.
        today_so_far = hist[hist.index.normalize() == pd.Timestamp(today)]
        pre_window = today_so_far.loc[today_so_far.index < pd.Timestamp(today) + pd.Timedelta(hours=9, minutes=35)]
        t.vol_min_so_far = float(pre_window["volume"].min()) if not pre_window.empty else None
        trackers.append(t)

    # Live tick feed (WebSocket, not REST polling) -- both candidates'
    # trigger/stop/target checks below react to real ticks as they
    # arrive instead of a fixed poll cadence. See live_ticker.py.
    inst_map = kite_client.instrument_map()
    token_by_symbol = {t.symbol: inst_map[t.symbol] for t in trackers if t.symbol in inst_map}
    ticker = live_ticker.LiveTicker({tok: sym for sym, tok in token_by_symbol.items()})
    ticker.start()

    try:
        last_candle_ts = None
        print("Entering intraday loop (09:35-15:10)...")
        while True:
            now = dt.datetime.now()
            if now.time() >= dt.time(15, 10):
                break

            boundary = _last_closed_candle_label(now)
            if boundary.time() >= dt.time(9, 35) and (last_candle_ts is None or boundary > last_candle_ts):
                for t in trackers:
                    if t.done:
                        continue
                    fresh = kite_client.fetch_intraday_candles(t.symbol, days=2, interval="5minute")
                    row_df = fresh[fresh.index == boundary]
                    if row_df.empty:
                        continue
                    row = row_df.iloc[0]
                    t.vol_min_so_far = (row["volume"] if t.vol_min_so_far is None
                                       else min(t.vol_min_so_far, row["volume"]))
                    event = process_candle(t, boundary, row, capital_alloc, risk_budget, mode)
                    if event:
                        print(f"{boundary} {t.symbol}: {event}")
                last_candle_ts = boundary

            for t in trackers:
                if t.done:
                    continue
                token = token_by_symbol.get(t.symbol)
                if token is None:
                    continue
                ltp = _get_live_ltp(ticker, token, t.symbol)
                if ltp is None:
                    continue
                now2 = dt.datetime.now()
                if t.position_id is None:
                    event = check_tick_entry(t, ltp, now2, capital_alloc, risk_budget, mode)
                else:
                    event = check_intracandle_exit(t, ltp, now2, mode)
                if event:
                    print(f"{now2:%H:%M:%S} {t.symbol}: {event}")

            time.sleep(CHECK_INTERVAL_SECONDS)

        print("15:10 -- squaring off any remaining open positions...")
        for t in trackers:
            if t.position_id is None:
                continue
            token = token_by_symbol.get(t.symbol)
            ltp = _get_live_ltp(ticker, token, t.symbol) if token is not None else None
            if ltp is None:
                try:
                    ltp = kite_client.get_ltp([t.symbol])[t.symbol]
                except Exception:
                    pos = idb.get_position(t.position_id)
                    ltp = pos["entry_price"] if pos else None
            if ltp is None:
                continue
            event = force_squareoff(t, ltp, dt.datetime.now(), mode)
            if event:
                print(f"squareoff {t.symbol}: {event}")
    finally:
        ticker.stop()

    # Sum every leg closed today (target/stop legs closed earlier in the
    # loop, plus the squareoff legs just above) straight from the DB --
    # a single source of truth, no separate running total to keep in sync.
    all_legs_today = idb.get_legs(date=date_str, mode=mode)
    total_day_pnl = float(all_legs_today["net_pnl"].sum()) if not all_legs_today.empty else 0.0
    new_capital = idb.apply_day_pnl(mode, total_day_pnl)
    print(f"\nDay done. Net P&L: Rs.{total_day_pnl:+,.2f}  New capital: Rs.{new_capital:,.2f}")


if __name__ == "__main__":
    import sys
    _mode = "live" if "--live" in sys.argv else "paper"
    if _mode == "live" and not config.STRATEGY.get("intraday_live_enabled", False):
        print("intraday_live_enabled is off in config.STRATEGY -- refusing to run live mode. "
             "Running paper mode instead.")
        _mode = "paper"
    run_live(mode=_mode)
