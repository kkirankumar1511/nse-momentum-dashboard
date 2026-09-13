# DaysLowVolumnBreakout — Full Strategy Specification

This document is the complete, implementation-ready specification of the
intraday strategy validated in this workspace as "OurRule" — renamed here
**DaysLowVolumnBreakout**. It covers stock selection, signal detection,
entry/exit mechanics, position sizing, cost modeling, timing conventions,
and the validated backtest results, including design choices that were
explicitly **tested and rejected** (so they are not re-introduced by
mistake in a reimplementation).

Instrument universe throughout: NSE equities, MIS (intraday) product,
5-minute candles.

---

## 1. Candle & data conventions

- **Interval**: 5-minute OHLCV candles.
- **Candle labeling**: a candle labeled `HH:MM` covers `[HH:MM, HH:MM+5min)`
  and is considered "closed"/known at `HH:MM+5min`. E.g. the `09:25`
  candle covers 09:25–09:30 and its close is the price at 09:30 real time.
  This convention matters everywhere below.
- **Indicators are CONTINUOUS, not session-reset** — computed once over
  a symbol's whole multi-day 5-minute history so they are properly
  "warmed up" and carry momentum across day boundaries:
  - `EMA21 = close.ewm(span=21, adjust=False, min_periods=21).mean()`
  - `ATR14` — Wilder's ATR, 14-period, computed as:
    ```
    prev_close = close.shift(1)
    TR = max(high - low, abs(high - prev_close), abs(low - prev_close))
    ATR14 = TR.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    ```
- **One exception — session-scoped, not continuous**: the "lowest volume
  so far today" reference (used in signal detection, §3) resets each
  day at 09:15. It is the running minimum of that day's own candle
  volumes from session open up to the current candle, inclusive.
- **Data needed per symbol**: enough historical 5-minute candles to seed
  EMA21/ATR14 properly (the validation used ~5 years of continuous
  history so the indicators are fully warmed; for live/paper trading, a
  minimum of several weeks of prior 5-min history per symbol is required
  before these indicators are trustworthy — do not start trading a
  symbol on day 1 of its own data history).

---

## 2. Daily candidate selection (which 2 stocks to watch each day)

Run once per day, using data available by 09:30 (the close of the
"09:25" candle) — this determines the day's bias and the day's 2
tradeable candidates. No look-ahead: only uses data through 09:30.

### 2.1 Universe

- All NIFTY50 index constituents, **union** the full F&O (futures &
  options eligible) equity universe. Call this `all_symbols`.
- The final tradeable candidate pool (step 2.4) is restricted to
  **F&O-eligible symbols only** (`fno_set`) — NIFTY50-only names that
  aren't F&O-eligible are used only for the breadth ratio in step 2.2,
  not as trade candidates.

### 2.2 Day bias (NIFTY50 breadth)

For every NIFTY50 constituent, compute its own "first 15 minutes"
return:
```
ret_first15(symbol, day) = (close_of_0925_candle / prev_day_close - 1) * 100
```
(`close_of_0925_candle` = that symbol's 5-min candle labeled `09:25`,
i.e. its price at 09:30 real time — this is the actual first-15-minute
return from the 09:15 open.)

```
advancers = count(NIFTY50 symbols where ret_first15 > 0)
decliners = count(NIFTY50 symbols where ret_first15 < 0)
nifty_ratio = advancers / decliners   (if decliners == 0: ratio = +infinity)
```

**Day bias gate — the day is only tradeable if the ratio clears a
threshold:**
```
if nifty_ratio > 2.0:   day_bias = LONG
elif nifty_ratio < 0.5: day_bias = SHORT
else:                    SKIP THIS DAY ENTIRELY (no trading)
```
This threshold gate was validated as-is; do not loosen it to "ratio ≥ 1
= LONG else SHORT" (that weaker version was tested with a sector filter
and found worse — see §9).

### 2.3 Candidate ranking

Within the F&O universe (`fno_set`), compute `ret_first15` for every
symbol the same way as above. Rank:
- **LONG day**: sort descending (best positive movers first).
- **SHORT day**: sort ascending (most negative movers first).

### 2.4 Final candidate list

Take the **top 2** symbols from the ranked F&O list. These are the
day's only 2 tradeable candidates, both carrying the same `day_bias`
direction. Record: date, nifty_ratio, bias, rank (1 or 2), symbol,
ret_first15_pct.

**No sector filter is applied anywhere in this pipeline.** A sector-
restricted version (top-3-sector, top-2 or top-5 stocks/day) was
explicitly tested and consistently performed worse in every
configuration tried — see §9. Do not add one.

---

## 3. Signal detection (per candidate, per day) — "DaysLowVolumnBreakout" signal

Applied independently to each of the day's 2 candidates, using that
symbol's own continuous 5-min EMA21 and ATR14 series (§1).

### 3.1 Constants

| Name | Value | Meaning |
|---|---|---|
| `SIGNAL_WINDOW_START` | `"09:35"` | earliest candle eligible to be a signal candle |
| `SIGNAL_WINDOW_END` (scan limit) | `"15:05"` | latest candle scanned for a signal/breakout |
| `NEW_SIGNAL_CUTOFF` | `"11:00"` | no *new* signal candle may form after this time |
| `VOL_THRESHOLD_PCT` | `0.05` (5%) | tolerance band on the "lowest volume so far" check |
| `ATR_PCT_BUFFER` | `0.05` (5%) | entry/stop buffer, as a fraction of the signal candle's ATR14 |
| `BREAKOUT_WINDOW` | `1` candle | only the immediate next candle after a signal is checked for the entry trigger |

### 3.2 Walk-forward loop (per candidate, per day)

Iterate the candidate's 5-min candles for that day, from
`SIGNAL_WINDOW_START` to `SIGNAL_WINDOW_END`, in chronological order.
Maintain: `invalidated` (bool, starts False), `active_signal` (starts
None), `breakout_counter` (starts 0), and `vol_so_far` (that day's
running min volume from 09:15 through the current candle, inclusive).

At **every** candle in the window:

1. **EMA21 day-invalidation gate** (checked every candle, regardless of
   any active signal):
   ```
   if day_bias == LONG  and candle.close < EMA21_at(candle): invalidated = True
   if day_bias == SHORT and candle.close > EMA21_at(candle): invalidated = True
   if invalidated: STOP -- no trade at all for this candidate today.
   ```
   This is a **whole-day kill switch** — a single candle closing on the
   wrong side of the continuous EMA21 at any point from 09:35 onward
   voids the entire candidate for the day, even if a signal had already
   formed. Mirrored by direction (SHORT's condition is the true mirror
   of LONG's — not a literal copy).

2. **If there IS an active signal** (from an earlier candle in this
   same loop):
   - `breakout_counter += 1`
   - `buffer = ATR14_at(signal_candle) * ATR_PCT_BUFFER`
   - **LONG**: `trigger_level = signal_candle.high + buffer`.
     Triggered if `candle.high >= trigger_level`.
   - **SHORT**: `trigger_level = signal_candle.low - buffer`.
     Triggered if `candle.low <= trigger_level`.
   - **If triggered**: entry fires. See §4 for entry/stop values. Stop
     scanning this candidate for today — this is the trade.
   - **If not triggered** and `breakout_counter >= BREAKOUT_WINDOW` (i.e.
     this was the 1 allotted follow-up candle and it didn't trigger):
     clear `active_signal`, reset `breakout_counter = 0` — the signal
     has expired unused. A **later** candle can still become a fresh
     signal (subject to the cutoff below).
   - Do not also evaluate this same candle as a brand-new signal
     candidate in the same iteration — move to the next candle.

3. **If there is NO active signal**, and the candle's time is `<=
   NEW_SIGNAL_CUTOFF` (11:00) — check whether THIS candle qualifies as
   a fresh signal candle:
   - **Candle color**: strictly RED (`close < open`) if `day_bias ==
     LONG`; strictly GREEN (`close > open`) if `day_bias == SHORT`.
   - **Volume**: `candle.volume <= vol_so_far.min() * (1 +
     VOL_THRESHOLD_PCT)` — i.e. this candle's volume is at or within 5%
     of the lowest volume seen so far *today* (inclusive of this
     candle in the running-min calculation).
   - **ATR14 valid**: `ATR14_at(candle)` must not be NaN and must be `>
     0` (guards against insufficient warm-up).
   - If all three hold: this candle becomes the new `active_signal`
     (store its time, high, low, and its own ATR14 value), reset
     `breakout_counter = 0`.
   - If the candle's time is `> NEW_SIGNAL_CUTOFF`: skip new-signal
     detection entirely for this candle (but an *already*-active signal
     from before 11:00 keeps running its breakout check normally, even
     past 11:00 — the cutoff only blocks new signal formation, not the
     completion of an existing one).

4. Continue to the next candle. At most **one** trade is ever produced
   per candidate per day — the moment an entry triggers, stop.

If the loop reaches `SIGNAL_WINDOW_END` with no trigger, the candidate
produces no trade for that day.

---

## 4. Entry & stop price

At the moment a breakout triggers (step 3.2.2 above):

- **Entry price = the trigger level itself** (not the triggering
  candle's open or close — a stop-order-style fill at the exact
  computed threshold):
  - LONG: `entry = signal_candle.high + buffer`
  - SHORT: `entry = signal_candle.low - buffer`
- **Stop price** (same buffer, opposite side of the signal candle):
  - LONG: `stop = signal_candle.low - buffer`
  - SHORT: `stop = signal_candle.high + buffer`
- `buffer = ATR14_at(signal_candle) * 0.05` in both cases (the SAME
  buffer value computed once, reused for both entry and stop).

---

## 5. Target & exit management

- `risk = abs(entry - stop)`
- `target = entry + 2 * risk` (LONG) / `entry - 2 * risk` (SHORT) — a
  flat **1:2 reward:risk** target.
- **Exit is a HALF-position split**:
  1. **First half (50% of quantity)** exits at `target` if/when the
     target price is reached. Reason: `target`.
  2. **If the stop is hit before the target** (for the full position,
     before any half has exited): the **entire** position closes at
     `stop`. Reason: `stop`.
  3. **After the first half exits at target**, the **remaining half's
     stop stays at the ORIGINAL stop price** — it is explicitly **NOT**
     moved to breakeven. If that remaining half later hits the original
     stop, it exits there. Reason: `stop` (on the remaining leg).
  4. **If neither the stop nor a forced-close condition triggers by
     15:10**, whatever quantity remains open is **force-closed at the
     close of the `15:10`-labeled candle** (i.e. the price at 15:15
     real time). Reason: `squareoff`.
  5. If the day's data ends before any exit condition fires (data gap
     only — should not happen with clean live data), close at the last
     available candle's close. Reason: `eod_data_end`.
- **A breakeven-stop-move on the remaining half (after the first half
  hits target) was explicitly tested and found to be marginally WORSE**
  (see §9) — do not add it. Leaving the original stop in place is the
  validated design.
- No trailing stop. No partial profit-booking beyond this single 50%
  split. No re-entry after an exit — one trade per candidate per day,
  maximum.

---

## 6. Live execution: candle-close events vs tick-by-tick (WebSocket)

The backtest evaluates entry triggers, stops, and targets against
**closed 5-minute candles' high/low** — e.g. "triggered if candle.high
>= trigger_level". That is a valid backtesting approximation, but a
live implementation should NOT literally wait for each 5-min candle to
close before checking these conditions — that adds up to 5 minutes of
unnecessary latency and materially worse fills. Split the strategy into
two clearly different timing domains:

### 6.1 Candle-close-driven (must wait for a closed candle)

Everything in §3 that reads a candle's **OHLCV as a completed bar** —
the EMA21 day-invalidation check, the signal-candle color/volume/ATR14
check, and the recording of `signal_candle.high` / `signal_candle.low`
— can only be evaluated once a 5-minute candle has actually closed.
This means:
- Build 5-min OHLCV bars from the WebSocket tick feed yourself (or pull
  them from your broker's own candle API), using the **exact same
  boundary convention** as your historical data (§1) — a candle
  labeled `09:25` must cover ticks from 09:25:00 up to but not
  including 09:30:00. Any mismatch here (e.g. off-by-one-tick boundary,
  or aggregating on a different clock) will make your live EMA21/
  ATR14/volume-so-far values silently diverge from what this backtest
  validated.
- Run the §3 signal-detection check **once per closed candle** (i.e.
  once every 5 minutes, at :00/:05/:10/... past the hour), not on every
  tick.

### 6.2 Tick-driven (should NOT wait for a candle close)

Once a signal candle is active (§3.2.2) or a position is open, the
following should be monitored **continuously from live ticks**, not
deferred to the next candle close — this is strictly more responsive
than the backtest and should only improve fills, never worsen them,
because the trigger/stop/target price levels themselves are unchanged
from §4/§5, only *how soon you notice* the price reached them changes:

- **Breakout/entry trigger** (§3.2.2): once a signal candle is locked
  in, watch live LTP (last traded price) against `trigger_level` for
  the following 5 minutes (the single `BREAKOUT_WINDOW` candle). Fire
  the entry the instant LTP crosses it — do not wait for that 5-minute
  candle to close. If 5 minutes pass with no cross, cancel/expire the
  signal exactly as §3.2.2 describes.
- **Stop-loss** (§5): once in a position, watch LTP against the
  current stop price (original or, on the remaining half after target,
  still the original — §5 explicitly keeps it unchanged) continuously;
  exit the instant it's touched.
- **Target** (§5): same — watch LTP against `target` continuously for
  the first half's exit.
- **15:10 squareoff** (§5.4): this one stays **time-driven**, not
  tick-driven — force-close whatever remains open at 15:10 real time
  regardless of price.

### 6.3 Recommended order mechanics

Two implementation options, in order of robustness:

1. **Exchange-side conditional orders (recommended)**: the moment a
   signal candle locks in, place a real **SL (stop-loss) buy/sell
   order** on the exchange at `trigger_level` for the entry, valid only
   for the 5-minute breakout window (cancel it if unfilled when the
   window expires). The moment that entry fills, immediately place a
   **SL-M (stop-loss market) order** at the stop price and a **LIMIT
   order** at the target price for the appropriate quantity split
   (§5.1-5.3), using an OCO-style ("one cancels other") bracket if your
   broker/API supports it, or by manually cancelling the counterpart
   order when one leg fills. This way the exchange itself enforces your
   trigger/stop/target even if your own application has a network hiccup
   or restarts — do not rely solely on your app polling WebSocket ticks
   and firing market orders reactively for stop-loss protection.
2. **Tick-monitored + reactive market/limit orders**: if exchange-side
   conditional orders aren't available for a given instrument/broker,
   monitor WebSocket ticks in your own application and fire a market
   (entry, stop) or limit (target) order the instant a level is
   crossed. This works but is strictly less robust than (1) — your
   stop-loss protection depends on your process staying up and your
   network connection staying live for the whole time you're in a
   trade.

Either way, the **price levels** (`trigger_level`, `stop`, `target`)
are computed identically per §3-§5 — only the delivery mechanism
(exchange-side order vs application-side tick monitoring) differs.

---

## 7. Position sizing & risk management

| Parameter | Value |
|---|---|
| `MAX_TRADES_PER_DAY` | 2 |
| `LEVERAGE` | 5.0× (MIS margin) |
| `MAX_RISK_PCT_PER_TRADE` | 0.5% of current capital, **per trade** (not divided further across the 2 daily slots) |

For each trade taken:
```
capital_alloc = (current_capital / MAX_TRADES_PER_DAY) * LEVERAGE   # buying-power cap
risk_budget   = current_capital * MAX_RISK_PCT_PER_TRADE            # rupee-risk cap

risk_per_share = abs(entry_price - stop_price)
qty_by_risk    = floor(risk_budget / risk_per_share)
qty_by_capital = floor(capital_alloc / entry_price)
quantity       = max(0, min(qty_by_risk, qty_by_capital))            # whichever binds first
```
If `quantity <= 0`, skip the trade (can't size it within both caps).

**If both of the day's 2 candidates produce a valid entry**, take both,
sized independently as above (each gets its own `capital_alloc` and
`risk_budget` computed off the SAME `current_capital` value for that
day — they do not deplete a shared pool sequentially). If more than 2
signals somehow exist for a day, take the 2 with the **earliest entry
timestamps** and discard the rest.

**Capital compounds day to day**: `next_day_capital = today_capital +
today's_total_net_P&L`. Start from whatever your actual account capital
is; the backtests here started from ₹1,000,000.

---

## 8. Transaction cost model (Zerodha intraday equity, as of this
validation — re-confirm against your current broker rate card before
going live)

Applied once per completed round-trip (one buy leg + one sell leg),
computed on the exact quantity of that leg (if a position is split into
a target-half and a runner-half, compute cost separately for each half
using its own entry/exit prices and quantity):

```
buy_turnover  = entry_price * qty
sell_turnover = exit_price  * qty

brokerage = min(0.03% * buy_turnover, Rs.20) + min(0.03% * sell_turnover, Rs.20)
stt       = 0.025% * sell_turnover                      # sell side only, intraday equity
exchange  = 0.00297% * (buy_turnover + sell_turnover)   # NSE, both sides
gst       = 18% * (brokerage + exchange)                # NOT applied to STT

total_cost = brokerage + stt + exchange + gst
net_pnl    = (exit_price - entry_price) * qty * (+1 for LONG, -1 for SHORT) - total_cost
```

No SEBI turnover fee, no stamp duty included in this model.

---

## 9. Validated design choices — what NOT to change

These were tested empirically in this workspace and found to make the
strategy **worse**. A reimplementation should treat all of these as
settled, not open questions to re-litigate without re-testing carefully:

1. **No sector filter.** Restricting candidates to the top-3
   performing sectors (by the same breadth-ratio method) and picking
   top-2 or top-5 stocks from within those sectors was tested in
   multiple configurations — every single one underperformed the plain
   no-sector-filter, top-2-from-the-whole-F&O-universe approach, in
   some configurations turning a profitable strategy negative.
2. **No breakeven-stop-move after the first half hits target.** Tested
   over the full 5-year set: reduces net P&L slightly (+₹1,513,895 →
   +₹1,487,518) because it prematurely stops out ~42 positions that
   would otherwise have ridden to a profitable 15:10 squareoff, and
   this outweighs the benefit on the ~102 positions it does help.
3. **No macro regime filter** (e.g. restricting to days where the
   NIFTY 50 *index* itself is below its own 200-day EMA). Tested
   SHORT-only and LONG+SHORT variants of this over the real 5-year
   NIFTY history — both substantially underperform the unfiltered
   strategy (CAGR 0.83% and 6.78% respectively, vs 20.36% unfiltered).
4. **Entry fill is the exact computed trigger price, not the
   triggering candle's open or close.** This was a deliberate,
   validated convention throughout — changing it changes both the
   entry price and the effective risk per share.
5. **The EMA21 day-invalidation gate checks EVERY candle from 09:35
   onward, not just the signal candle.** A single wrong-side close at
   any point kills the whole day for that candidate — this is a wide
   net, intentionally.
6. **Only the immediate next candle is checked for the breakout
   trigger** (`BREAKOUT_WINDOW = 1`). A wider window (3 candles) was
   tested and found worse (fewer, weaker signals converted).
7. **Volume tolerance is 5%, ATR buffer is 5%.** Both were swept
   (volume tolerance and buffer sizes were tested at several values)
   and 5%/5% was the best-performing combination found.

---

## 10. Backtest validation results (for reference / expectation-setting)

All results: ₹1,000,000 starting capital, 5X leverage, 0.5% max
risk/trade, realistic cost model above, no-sector-filter/top-2-per-day
candidate selection, standard (non-breakeven) exit management.

| Period | Positions | Win rate | Net P&L | Return |
|---|---|---|---|---|
| 1 year | 141 | 40% | +₹163,942 | +16.39% |
| 3 years | 376 | 43% | +₹856,590 | +85.66% |
| **5 years** (2021-09-20 → 2026-09-11) | **613** | **47%** | **+₹1,513,895** | **+151.39%** |

**5-year CAGR ≈ 20.36%** (total return 2.51×, over 4.97 years).

Yearly breakdown (5-year run):

| Year | Positions | Win rate | Return |
|---|---|---|---|
| 2021 (Sep–Dec, partial) | 34 | 38% | +3.21% |
| 2022 | 150 | 45% | +19.39% |
| 2023 | 97 | 43% | +21.24% |
| 2024 | 112 | 50% | **+35.51%** (best year) |
| 2025 | 119 | 34% | +6.69% (weakest full year) |
| 2026 (Jan–Sep, partial) | 101 | 44% | +16.40% |

By exit reason (5-year): `target` legs 100% win by definition; `stop`
legs 0% win by definition; `squareoff` legs (the runner half riding to
15:10) win ~90-95% of the time and are the single largest source of
total profit — this is a structural feature of the strategy, not
noise: the "let the winner run to close" half consistently outperforms
booking everything at 1:2.

**Verified for live/tick-by-tick fidelity**:
- No look-ahead bias — traced through the signal-detection logic; every
  check only ever reads the current candle or earlier state, never a
  future candle. (Unlike a separately-tested Fibonacci/swing-based
  strategy, this design has no centered-window indicators.)
- No same-candle stop/target ambiguity — checked directly against
  actual 5-min OHLC data: 0 of 863 legs (5-year set) had the stop level
  also sitting inside the exit candle that hit target, so no outcome
  depends on an assumed tie-break order.

**Not verifiable from backtesting alone — validate live/paper before
scaling size**: real order-fill slippage at the exact trigger price,
per-stock margin/leverage availability from your broker (5X was
assumed uniformly), whether 2 same-day positions can actually get 5X
buying power concurrently, and whether your live tick-to-5-min-candle
aggregation produces bars identical to your data provider's own
historical candles (mismatches here would cause the live EMA21/ATR14/
volume-so-far values to drift from what this backtest assumes).
