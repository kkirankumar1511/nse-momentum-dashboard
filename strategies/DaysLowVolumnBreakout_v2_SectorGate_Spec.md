# DaysLowVolumnBreakout v2 (Sector-Gated) — Full Strategy Specification

This is the complete, implementation-ready spec for the **v2** evolution
of "OurRule"/DaysLowVolumnBreakout, validated in this workspace on top of
the original v1 design (see `DaysLowVolumnBreakout_Strategy_Spec.md` for
v1's full history/context). v2 adds three changes and sweeps the day-bias
threshold; this document is self-contained — you should not need to
cross-reference v1 to implement v2.

**Status as of this writing**: this exact configuration (day-bias
1.5/0.66, sector gate 2.0/0.5) is the **final recommended configuration
for paper trading**, after extensive additional stress-testing against
four alternative mechanisms (§13) — none beat it. Validated over 5
years:

| Metric | Value |
|---|---|
| Positions | 526 |
| Win rate | 44% |
| Net P&L | +₹17,52,626 |
| Cumulative return | +175.26% |
| **CAGR** | **22.29%** |
| Sharpe ratio | 3.14 |
| Profit factor | 1.58 |
| **Max drawdown** | **-6.64%** (2026-01-30 → 2026-03-25, recovered in 70 days) |
| Losing years (of 5) | **0** |

**A 10-year run has since completed and came back meaningfully weaker
(CAGR 14.07%)** — see §10 for the full breakdown and an important
caveat: a data-completeness bug was found in how the sector-gate
backtest re-fetches "not sufficiently cached" symbols across long runs
(see §10.3), meaning **the 14.07% figure itself is provisional and
needs a clean rerun before being fully trusted**.

**Recommendation**: 5 years / 526 trades, a Sharpe of 3.14, a shallow
-6.64% max drawdown, and zero losing years is a solid basis to **START
PAPER TRADING now** (paper trading is its own risk-free validation
layer, independent of backtest horizon) — but do not size real capital
off the 22.29% CAGR expectation until a verified 10-year number confirms
the edge holds across the 2016-2018 regime this 5-year window entirely
missed. Watch paper-trading results for consistency with the 5-year
backtest rather than assuming the number will hold as-is.

Instrument universe throughout: NSE equities, MIS (intraday) product,
5-minute candles.

---

## What changed from v1 → v2

1. **Signal window starts at 09:30 instead of 09:35** — the candle
   timestamped 09:30 (covering 09:30–09:35) can now itself be a signal
   candle. v1 excluded it.
2. **NEW: first-candle invalidation rule.** If any candle (from 09:30
   onward, same loop as the EMA21 gate) **closes** below the day's very
   first (09:15) candle's **low** (on a LONG day) / above its **high**
   (on a SHORT day), the whole day is invalidated for that candidate —
   exactly the same "whole day voided" semantics as the EMA21 gate, just
   a different reference level.
3. **NEW: entry-time sector confirmation gate.** Once a candidate's
   entry has actually triggered (signal + breakout confirmed, per §3-4),
   one more check must pass before the trade is taken: that candidate's
   own primary sector must show an A/D ratio confirming the same
   direction as the day's bias (§6). If it doesn't, that specific
   candidate's trade is dropped — the day's other candidate, if it
   separately passes, still trades normally.
4. **Day-bias threshold swept.** v1 used NIFTY50 ratio `> 2.0` LONG /
   `< 0.5` SHORT. v2's best-found result uses a more moderate
   `> 1.5` LONG / `< 0.66` SHORT (see §9.4 for the full sweep — neither
   the strict v1 threshold nor a fully relaxed `>1.0/<1.0` threshold
   performed as well as this moderate middle ground).

Everything else — entry/stop mechanics (§4 below), target/exit
management (§5), position sizing (§7), and the cost model (§8) — is
**unchanged from v1**.

---

## 1. Candle & data conventions

- **Interval**: 5-minute OHLCV candles.
- **Candle labeling**: a candle labeled `HH:MM` covers `[HH:MM, HH:MM+5min)`
  and is closed/known at `HH:MM+5min`. E.g. the `09:25` candle covers
  09:25–09:30; its close is the price at 09:30 real time.
- **Indicators are CONTINUOUS, not session-reset** — computed once over a
  symbol's whole multi-day 5-minute history:
  - `EMA21 = close.ewm(span=21, adjust=False, min_periods=21).mean()`
  - `ATR14` — Wilder's ATR, 14-period:
    ```
    prev_close = close.shift(1)
    TR = max(high - low, abs(high - prev_close), abs(low - prev_close))
    ATR14 = TR.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    ```
- **One exception — session-scoped**: the "lowest volume so far today"
  reference (§3) resets each day at 09:15.
- **Data needed per symbol**: enough historical 5-min candles to seed
  EMA21/ATR14 properly (the validation used ~5-10 years of continuous
  history). Do not trade a symbol on day 1 of its own data history.

---

## 2. Daily candidate selection

Run once per day, using data available by 09:30 (close of the "09:25"
candle). No look-ahead — only data through 09:30 is used.

### 2.1 Universe

- All NIFTY50 index constituents, **union** the full F&O-eligible equity
  universe (`all_symbols`).
- The final candidate pool (§2.4) is restricted to **F&O-eligible
  symbols only** — NIFTY50-only names not in F&O are used only for the
  breadth ratio (§2.2), never as trade candidates.

### 2.2 Day bias (NIFTY50 breadth) — **CHANGED threshold in v2**

For every NIFTY50 constituent:
```
ret_first15(symbol, day) = (close_of_0925_candle / prev_day_close - 1) * 100
```
(the actual first-15-minute return from the 09:15 open — close of the
5-min candle labeled `09:25`, i.e. price at 09:30 real time.)

```
advancers = count(NIFTY50 symbols where ret_first15 > 0)
decliners = count(NIFTY50 symbols where ret_first15 < 0)
nifty_ratio = advancers / decliners   (if decliners == 0: ratio = +infinity)

if nifty_ratio > 1.5:    day_bias = LONG
elif nifty_ratio < 0.66: day_bias = SHORT
else:                     SKIP THIS DAY ENTIRELY (no trading)
```
**This threshold (1.5 / 0.66) is v2's best-found value** — moderately
looser than v1's strict 2.0/0.5, but NOT as loose as a fully-relaxed
1.0/1.0 (that was tested and found worse — see §9.4). Do not casually
retune this without re-running the full sweep; the relationship between
this threshold and outcome was NOT monotonic (looser was not simply
"more trades = better" nor "fewer trades = better" — the moderate value
won specifically because it was consistently good across every year
tested, not because it was best in any single year).

### 2.3 Candidate ranking

Within the F&O universe, compute `ret_first15` for every symbol.
- **LONG day**: sort descending (best positive movers first).
- **SHORT day**: sort ascending (most negative movers first).

### 2.4 Final candidate list

Take the **top 2** symbols from the ranked F&O list — the day's only 2
tradeable candidates, both carrying the day's `day_bias`.

**No POOL-NARROWING sector filter is applied here** — i.e. step 2.3
ranks the WHOLE F&O universe, not just names from "top sectors". That
specific mechanism (restrict the ranking POOL to top-N sectors before
taking top-2) was tested extensively in both v1 and v2 exploration and
consistently hurt performance. **Do not confuse it with §6's sector
CONFIRMATION gate below — that is a different mechanism, applied later,
to already-selected candidates, and IS validated as a net improvement.**

---

## 3. Signal detection (per candidate, per day)

Applied independently to each of the day's 2 candidates, using that
symbol's own continuous 5-min EMA21 and ATR14.

### 3.1 Constants

| Name | Value | Meaning |
|---|---|---|
| `SIGNAL_WINDOW_START` | **`"09:30"`** (CHANGED from v1's `"09:35"`) | earliest candle eligible to be a signal candle |
| `SIGNAL_WINDOW_END` (scan limit) | `"15:05"` | latest candle scanned |
| `NEW_SIGNAL_CUTOFF` | `"11:00"` | no *new* signal candle may form after this |
| `VOL_THRESHOLD_PCT` | `0.05` (5%) | tolerance band on "lowest volume so far" |
| `ATR_PCT_BUFFER` | `0.05` (5%) | entry/stop buffer, as fraction of signal candle's ATR14 |
| `BREAKOUT_WINDOW` | `1` candle | only the immediate next candle checked for entry trigger |

### 3.2 Walk-forward loop (per candidate, per day)

Iterate 5-min candles from `SIGNAL_WINDOW_START` (09:30) to
`SIGNAL_WINDOW_END` (15:05), chronologically. Maintain: `invalidated`
(bool), `active_signal` (None or dict), `breakout_counter` (int), and
`vol_so_far` (day's running min volume from 09:15 inclusive).

Also capture, once, the day's **very first candle** (09:15) — its `low`
and `high` — before the loop starts. This is needed for step 1b (NEW).

At **every** candle in the window, in this order:

1. **EMA21 day-invalidation gate** (checked every candle):
   ```
   if day_bias == LONG  and candle.close < EMA21_at(candle): invalidated = True
   if day_bias == SHORT and candle.close > EMA21_at(candle): invalidated = True
   ```

1b. **NEW — first-candle-close-through invalidation gate** (checked
   every candle, same as 1):
   ```
   if day_bias == LONG  and candle.close < first_candle.low:  invalidated = True
   if day_bias == SHORT and candle.close > first_candle.high: invalidated = True
   ```
   Then, whichever of 1/1b fired:
   ```
   if invalidated: STOP -- no trade at all for this candidate today.
   ```
   Both are **whole-day kill switches** — either one firing, at any
   point from 09:30 onward, voids the entire candidate for the day, even
   if a signal had already formed.

2. **If there IS an active signal**:
   - `breakout_counter += 1`
   - `buffer = ATR14_at(signal_candle) * ATR_PCT_BUFFER`
   - **LONG**: `trigger_level = signal_candle.high + buffer`. Triggered
     if `candle.high >= trigger_level`.
   - **SHORT**: `trigger_level = signal_candle.low - buffer`. Triggered
     if `candle.low <= trigger_level`.
   - **If triggered**: entry fires (§4) — but see §6, the sector gate,
     before the trade is actually confirmed/placed. Stop scanning this
     candidate for today either way (triggered or gate-failed, the
     signal has resolved).
   - **If not triggered** and `breakout_counter >= BREAKOUT_WINDOW`:
     clear `active_signal`, reset counter — signal expired unused. A
     later candle can still become a fresh signal.

3. **If there is NO active signal**, and candle time `<=
   NEW_SIGNAL_CUTOFF` (11:00):
   - **Color**: strictly RED (`close < open`) if LONG day; strictly
     GREEN (`close > open`) if SHORT day.
   - **Volume**: `candle.volume <= vol_so_far.min() * 1.05`.
   - **ATR14 valid**: not NaN, `> 0`.
   - If all hold: this candle becomes `active_signal` (store time, high,
     low, its own ATR14), reset `breakout_counter = 0`.
   - If candle time `> NEW_SIGNAL_CUTOFF`: skip new-signal detection
     (an already-active signal from before 11:00 still resolves
     normally, even past 11:00).

4. Continue to next candle. At most one trade per candidate per day —
   the moment an entry triggers (and clears §6), stop.

---

## 4. Entry & stop price (unchanged from v1)

At the moment a breakout triggers:

- **Entry price = the trigger level itself**:
  - LONG: `entry = signal_candle.high + buffer`
  - SHORT: `entry = signal_candle.low - buffer`
- **Stop price** (same buffer, opposite side):
  - LONG: `stop = signal_candle.low - buffer`
  - SHORT: `stop = signal_candle.high + buffer`
- `buffer = ATR14_at(signal_candle) * 0.05` (same value, reused for both).

---

## 5. Target & exit management (unchanged from v1)

- `risk = abs(entry - stop)`
- `target = entry + 2*risk` (LONG) / `entry - 2*risk` (SHORT) — flat
  **1:2 reward:risk**.
- **Half-position split exit**:
  1. First half (50%) exits at `target` if reached. Reason: `target`.
  2. If stop hit before target (full position, no half exited yet):
     entire position closes at `stop`. Reason: `stop`.
  3. After first half exits at target, remaining half's stop **stays at
     the ORIGINAL stop** — explicitly NOT moved to breakeven (tested,
     found worse — see §9.2).
  4. If nothing triggers by 15:10: force-close remaining quantity at the
     close of the `15:10` candle (price at 15:15 real time). Reason:
     `squareoff`.
  5. Data-gap fallback: close at last available candle's close. Reason:
     `eod_data_end`.
- No trailing stop, no partial profit beyond the single 50% split, no
  re-entry after exit.

---

## 6. NEW in v2 — Sector confirmation gate

Applied **after** a signal+breakout has triggered (§3.2 step 2) but
**before** the trade is actually confirmed/placed. This is an
**entry-time confirmation check on an already-selected candidate**, not
a pool-narrowing filter (do not confuse with the rejected mechanism in
§2.4) — it can only ever REJECT an individual candidate's trade, never
substitute in a different stock.

### 6.1 Primary sector classification

Every stock needs exactly ONE ground-truth "primary sector":

1. Source real NSE sectoral/thematic index membership data — for each
   stock, every NSE index it belongs to, with its per-index weightage.
   (This project used a cached copy of NSE's own `allIndices` /
   `getConstituents`-style data; in your own workspace, source this
   fresh from NSE's public index APIs or a data vendor that mirrors
   them — do not substitute a hand-made sector mapping, the whole point
   is using NSE's own official classification.)
2. Each NSE index carries a category: `SECTORAL INDICES`,
   `THEMATIC INDICES`, or others (broad market, strategy, etc. — ignore
   those others for this purpose).
3. For a given stock, take its **highest-weightage SECTORAL INDICES
   membership** as its primary sector.
4. **Only if it has no sectoral membership at all**, fall back to its
   **highest-weightage THEMATIC INDICES membership** — but EXCLUDING
   thematic indices that are really ownership/liquidity/compliance/
   recency SCREENS rather than real industries. The exact exclusion list
   used in this project (re-verify against current NSE index listings
   before reusing blindly, but this was hand-inspected and is a
   reasonable starting point):
   ```
   NIFTY MNC, NIFTY CPSE, NIFTY PSE, NIFTY TATA 25 CAP,
   NIFTYCONGLOMERATE, NIFTY100 LIQ 15, NIFTY MID LIQ 15,
   NIFTY CORP MAATR, NIFTY SHARIAH 25, NIFTY50 SHARIAH,
   NIFTY500 SHARIAH, NIFTY100 ESG, NIFTY100 ENH ESG,
   NIFTY100ESGSECLDR, NIFTY IPO, NIFTY SME EMERGE
   ```
5. A stock with no resolvable primary sector (no sectoral or eligible
   thematic membership at all) simply cannot pass this gate — treat as
   automatic fail (drop the trade) rather than erroring.

This produces a single `symbol -> sector_name` map (e.g. `"HEROMOTOCO"
-> "NIFTY AUTO"`, `"INFY" -> "NIFTY IT"`). Rebuild/refresh this mapping
periodically (index membership and weightages change over time) — do
not treat it as permanently static.

### 6.2 Sector's own A/D ratio

For the candidate's primary sector, identify **all of that sector's own
constituent stocks** (from the same index-membership data source). Then,
for the SAME trading day, compute that sector's own breadth ratio using
the identical `ret_first15` definition as §2.2, but restricted to just
this sector's members:
```
sector_advancers = count(sector's constituents where ret_first15 > 0)
sector_decliners = count(sector's constituents where ret_first15 < 0)
sector_ratio = sector_advancers / sector_decliners  (inf if decliners == 0)
```

### 6.3 The gate itself

```
if day_bias == LONG:
    pass_gate = sector_ratio > 2.0
else:  # SHORT
    pass_gate = sector_ratio < 0.5
```
**Note this threshold (2.0 / 0.5) is intentionally the STRICT one** —
independent of, and unrelated to, whatever threshold §2.2 uses for the
day-bias itself. A looser sector-gate threshold (1.0/1.0) was tested and
found worse (see §9.4) — keep the sector gate strict even when the
day-bias threshold is relaxed.

If `pass_gate` is False: **drop this candidate's trade entirely** for
today (no entry is taken, no matter how good the signal/breakout was).
If the day had 2 candidates and only one passes, only that one trades.
If neither passes, no trade happens that day at all.

### 6.4 Timing — this is a CANDLE-CLOSE-driven, once-per-day check

Unlike §3's tick-relevant breakout monitoring, §6.2's `ret_first15` for
every sector-constituent stock only needs the SAME 09:30 checkpoint data
as the day-bias calculation (§2.2) — it does not need to be recomputed
continuously. In practice: **compute the sector ratio for every sector
you'll plausibly need ONCE at 09:30** (right after the day-bias
calculation, using the same batch of "09:25 candle close" data you
already pulled for every NIFTY50/F&O symbol — sector constituents will
mostly already be inside that same universe, plus whatever extra symbols
belong to sectors you haven't otherwise fetched), then look up the
already-computed ratio when a candidate's breakout later triggers
(anytime from 09:30 to 15:05). Do not literally wait to fetch fresh data
at the moment of breakout — the sector ratio is fixed for the day the
moment 09:30 data is in, exactly like the day bias itself.

---

## 7. Live execution: candle-close events vs tick-by-tick (WebSocket)

(Unchanged from v1 — repeated here for completeness since this document
is meant to be self-contained.)

### 7.1 Candle-close-driven (must wait for a closed candle)

Everything in §3 that reads a candle's OHLCV as a completed bar — EMA21
gate, first-candle gate (NEW), signal-candle color/volume/ATR14 check —
can only be evaluated once a 5-min candle has actually closed:
- Build 5-min OHLCV bars from your WebSocket tick feed (or your broker's
  own candle API) using the EXACT SAME boundary convention as your
  historical data (§1) — a candle labeled `09:25` covers ticks from
  09:25:00 up to but not including 09:30:00. Any mismatch here silently
  makes your live EMA21/ATR14/volume-so-far values diverge from what
  this backtest validated.
- Run the §3 signal-detection check once per closed candle (every 5
  minutes), not on every tick.
- The §6 sector gate is ALSO candle-close-driven, but only needs to run
  **once per day at 09:30** (§6.4) — not per-candle.

### 7.2 Tick-driven (should NOT wait for a candle close)

Once a signal candle is active or a position is open, monitor
continuously from live ticks:
- **Breakout/entry trigger**: watch live LTP against `trigger_level` for
  the single `BREAKOUT_WINDOW` candle's 5 minutes. Fire the instant LTP
  crosses it. If 5 minutes pass with no cross, expire the signal exactly
  as §3.2 describes.
- **Stop-loss**: watch LTP against the current stop continuously; exit
  the instant it's touched.
- **Target**: watch LTP against `target` continuously for the first
  half's exit.
- **15:10 squareoff**: stays **time-driven** — force-close whatever
  remains open at 15:10 real time regardless of price.

### 7.3 Recommended order mechanics

1. **Exchange-side conditional orders (recommended)**: the moment a
   signal candle locks in, place a real SL order at `trigger_level` for
   entry, valid only for the 5-minute breakout window (cancel if
   unfilled). The moment entry fills, immediately place an SL-M order at
   the stop and a LIMIT order at the target, OCO-bracketed if your
   broker/API supports it (or manually cancel the counterpart on fill).
   The exchange enforces your protection even through an app/network
   hiccup — don't rely solely on app-side reactive tick monitoring for
   stop-loss.
2. **Tick-monitored + reactive orders**: fallback if exchange-side
   conditional orders aren't available for an instrument — fire market
   (entry, stop) or limit (target) orders the instant a level crosses in
   your own app. Strictly less robust than (1).

Either way, price levels (`trigger_level`, `stop`, `target`) are
computed identically per §4-§5 — only delivery mechanism differs.

---

## 8. Position sizing & risk management (unchanged from v1)

| Parameter | Value |
|---|---|
| `MAX_TRADES_PER_DAY` | 2 |
| `LEVERAGE` | 5.0× (MIS margin) |
| `MAX_RISK_PCT_PER_TRADE` | 0.5% of current capital, per trade (not divided further) |

```
capital_alloc = (current_capital / MAX_TRADES_PER_DAY) * LEVERAGE
risk_budget   = current_capital * MAX_RISK_PCT_PER_TRADE

risk_per_share = abs(entry_price - stop_price)
qty_by_risk    = floor(risk_budget / risk_per_share)
qty_by_capital = floor(capital_alloc / entry_price)
quantity       = max(0, min(qty_by_risk, qty_by_capital))
```
If `quantity <= 0`, skip. If both of the day's candidates pass §3-§6,
take both, sized independently off the same `current_capital`. Capital
compounds day to day.

---

## 9. Validated design choices — what NOT to change

1. **No pool-narrowing sector filter at candidate-selection time**
   (§2.4) — tested extensively (v1 and v2 exploration alike), always
   hurt. Not the same thing as §6's confirmation gate (which helps).
2. **No breakeven-stop-move** after the first half hits target — tested
   over the full 5-year v1 set: net worse (+₹1,513,895 → +₹1,487,518).
3. **No macro regime filter** (e.g. NIFTY50 index below its own 200-day
   EMA) — tested SHORT-only and LONG+SHORT variants, both substantially
   underperform (CAGR 0.83% / 6.78% vs 20%+ unfiltered).
4. **Entry fill is the exact computed trigger price**, not the
   triggering candle's open/close.
5. **The EMA21 gate (and the NEW first-candle gate) check EVERY candle**
   from the signal window start onward, not just the signal candle —
   either firing at any point kills the whole day for that candidate.
6. **`BREAKOUT_WINDOW = 1`** — only the immediate next candle. A wider
   3-candle window was tested (v1) and found worse.
7. **Volume tolerance 5%, ATR buffer 5%** — both swept, this combination
   won.
8. **§6's sector-confirmation-gate threshold stays STRICT (2.0/0.5)**
   even though §2.2's day-bias threshold was relaxed to 1.5/0.66 —
   relaxing BOTH simultaneously (day-bias 1.0/1.0 + sector gate 1.0/1.0)
   was tested and was one of the weaker configurations found.
9. **Day-bias threshold: use 1.5 LONG / 0.66 SHORT, not the v1 strict
   2.0/0.5 nor a fully-relaxed 1.0/1.0** — see the sweep in §9.4. This
   was the single best-performing configuration found across this
   entire project's exploration (5yr CAGR 22.29%, Sharpe 3.14).

### 9.4 The day-bias threshold sweep (5-year results, all with the strict 2.0/0.5 sector gate applied)

| Day-bias threshold | Positions | Win rate | 5yr Return | CAGR |
|---|---|---|---|---|
| No sector gate at all (v1 baseline, threshold 2.0/0.5) | 613 | 43% | +151.39% | 20.36% |
| Strict 2.0/0.5 + sector gate | 496 | 44% | +154.78% | 20.68% |
| Relaxed 1.0/1.0 + sector gate | 594 | 42% | +144.17% | 19.39% |
| **Moderate 1.5/0.66 + sector gate** | **526** | **44%** | **+175.26%** | **22.29%** |

The moderate threshold doesn't win any single year outright but is
consistently good across all of them (never the worst performer in any
year), which is what drives the best cumulative compounded result. Do
not assume "more trades" or "fewer trades" trends monotonically with
performance here — it doesn't.

### 9.5 The SECTOR-GATE threshold sweep (5-year, separate from §9.4)

A second sweep tested loosening the *sector gate itself* (§6.3) to match
whatever the day-bias threshold was, instead of always keeping it at the
strict 2.0/0.5:

| Day-bias threshold | Sector gate threshold | 5yr CAGR |
|---|---|---|
| 2.0/0.5 (strict) | 2.0/0.5 (strict) | 20.68% |
| **1.5/0.66 (moderate)** | **2.0/0.5 (strict)** | **22.29%** (best found) |
| 1.5/0.66 (moderate) | 1.5/0.66 (moderate) | 16.77% |
| 1.0/1.0 (relaxed) | 2.0/0.5 (strict) | 19.39% |
| 1.0/1.0 (relaxed) | 1.0/1.0 (relaxed) | 19.28% |

**Conclusion: keep the sector gate at the strict 2.0/0.5 threshold no
matter what the day-bias threshold is set to** — relaxing the sector
gate to match a looser day-bias threshold consistently hurt in every
combination tested (e.g. 1.5/0.66 + 1.5/0.66 dropped to 16.77% CAGR,
profit factor 1.39, Sharpe 2.41 — the two gates are NOT meant to move
together).

---

## 10. Backtest validation results

All results: ₹1,000,000 starting capital, 5X leverage, 0.5% max
risk/trade, realistic Zerodha cost model (§11 below), day-bias threshold
1.5/0.66, sector-confirmation gate 2.0/0.5.

**5-year run** (2021-09 → 2026-09, span 5.03yr):
- 526 positions, 44% win rate, net P&L +₹1,752,626
- ₹1,000,000 → ₹2,752,626 (**+175.26%**, **CAGR 22.29%**)
- Profit factor 1.58, Sharpe ratio 3.14 (annualized daily-return based)

Yearly breakdown:

| Year | Positions | Win rate | Return |
|---|---|---|---|
| 2021 (Sep–Dec, partial) | 31 | 39% | +7.46% |
| 2022 | 125 | 44% | +21.33% |
| 2023 | 83 | 45% | +18.95% |
| 2024 | 101 | 45% | +28.05% (best full year) |
| 2025 | 89 | 40% | +14.23% |
| 2026 (Jan–Sep, partial) | 97 | 48% | +21.33% |

**10-year run** (2016-07 → 2026-09, span 10.13yr):
- 854 positions, 44% win rate, net P&L +₹2,795,981
- ₹1,000,000 → ₹3,795,981 (**+279.60%**, **CAGR 14.07%**)

This is meaningfully weaker than the 5-year-only snapshot (22.29% CAGR)
— confirming the concern raised above. Yearly breakdown:

| Year | Positions | Win rate | Return |
|---|---|---|---|
| 2016 (Jul-Dec, partial) | 28 | 43% | +2.27% |
| 2017 | 76 | 39% | +4.83% |
| 2018 | 71 | 35% | **-3.10%** (only losing year) |
| 2019 | 49 | 55% | +27.37% |
| 2020 | 74 | 51% | +21.45% |
| 2021 | 85 | 36% | +3.77% |
| 2022 | 131 | 44% | +18.34% |
| 2023 | 79 | 48% | +20.43% |
| 2024 | 88 | 43% | +14.61% |
| 2025 | 88 | 43% | +17.57% |
| 2026 (Jan-Sep, partial) | 85 | 46% | +18.56% |

2016-2018 (and 2021) were weak/flat years; 2019 onward was consistently
strong. The 5-year test window happened to catch mostly the strong
regime and miss the weak one entirely.

### 10.3 IMPORTANT — a data-completeness bug affecting these 10-year numbers

While cross-checking the 10-year run's trailing 5-year segment against
the standalone 5-year run (same candidate list, same date window — they
should produce IDENTICAL trades), they did NOT match: 501 positions /
+229.60% (10yr run's trailing segment, recomputed from a fresh
₹1,000,000 base) vs 526 positions / +175.26% (standalone 5-year run).

Root cause, confirmed by hand-recomputing one example (IEX,
2021-10-20, sector "NIFTY MS FIN SERV") against fresh ground-truth data:
the sector ratio for that (sector, date) should be 0.13 (3 advancers /
23 decliners among the sector's 30 constituents), which correctly PASSES
the SHORT gate (<0.5) — matching the standalone 5-year run's behavior.
The 10-year run's trailing segment, however, EXCLUDED this trade,
meaning its sector-panel data was incomplete at the moment that
particular run executed — almost certainly because one or more of that
sector's constituents failed the "not sufficiently cached, live-fetch"
top-up (§ load_cached_only helper, used by the reference implementation
scripts) partway through the run, silently dropping that symbol from
the day's adv/dec count and flipping the gate's pass/fail outcome for
some trades.

**Implication**: the §6 sector gate's correctness depends on having
COMPLETE price data for every one of a sector's constituents on the
day being evaluated. A live/production implementation must NOT silently
proceed with a partial sector-constituent dataset (e.g. if a live fetch
times out or errors for one member) — either retry until complete, or
explicitly fall back to skipping the gate (treat as "no data, drop the
trade") rather than computing a ratio off an incomplete member set that
silently differs run-to-run. **The 14.07% 10-year CAGR above should be
treated as provisional** until re-run with this completeness issue
fixed/guarded against; the true number could be somewhat higher (closer
to the standalone 5-year figure's implied trajectory) or could be
essentially unchanged — it has not yet been re-verified cleanly.

---

## 11. Transaction cost model (unchanged from v1 — Zerodha intraday
equity, re-confirm against your current broker rate card before going
live)

```
buy_turnover  = entry_price * qty
sell_turnover = exit_price  * qty

brokerage = min(0.03% * buy_turnover, Rs.20) + min(0.03% * sell_turnover, Rs.20)
stt       = 0.025% * sell_turnover                      # sell side only, intraday equity
exchange  = 0.00297% * (buy_turnover + sell_turnover)   # NSE, both sides
gst       = 18% * (brokerage + exchange)                # NOT applied to STT

total_cost = brokerage + stt + exchange + gst
net_pnl    = (exit_price - entry_price) * qty * (+1 LONG / -1 SHORT) - total_cost
```
No SEBI turnover fee, no stamp duty included.

---

## 12. What's NOT verifiable from backtesting alone

Same caveats as v1, plus one new one specific to v2:

- Real order-fill slippage at the exact trigger price.
- Per-stock margin/leverage availability from your broker (5X assumed
  uniformly).
- Whether 2 same-day positions can actually get 5X buying power
  concurrently.
- Whether your live tick-to-5-min-candle aggregation produces bars
  identical to your data provider's historical candles.
- **NEW**: whether your live sector-constituent/index-membership data
  source stays accurate and current — NSE periodically rebalances index
  membership and weightages (typically semi-annually); a stale sector
  map will silently misclassify stocks and corrupt the §6 gate. Build in
  a periodic refresh, and sanity-check a handful of well-known stocks
  (e.g. confirm `RELIANCE` still resolves to an energy/oil-and-gas-type
  sectoral index, `HDFCBANK` to a banking one) after each refresh.

---

## 13. Alternative candidate-pool mechanisms tested — all rejected

After the core §2/§6 design above was validated, four structurally
different mechanisms were tried against the SAME day-bias (1.5/0.66)
and sector-gate (2.0/0.5) thresholds, to see if widening beyond a fixed
top-2/day could do better. **None beat the plain top-2 design above.**
Do not re-implement any of these without a specific new reason to
revisit them — they were each given a genuine, correctly-implemented
try.

| Mechanism | Positions | Win% | CAGR | Max DD | Losing yrs |
|---|---|---|---|---|---|
| **This spec: top-2/day, sector gate after entry** | **526** | **44%** | **22.29%** | **-6.64%** | **0** |
| Top-5/day, rank-priority selection | 1,031 | 43% | 31.11%* | — | 0* |
| Top-5/day, causal time-ordered (all 5 watched live, first-2-to-confirm win) | 1,019 | 41% | 21.34% | — | 1 (2023: -5.96%) |
| Top-5/day causal + EMA200 trend filter added | 962 | 40% | 18.81% | — | 0 |
| Freeze exactly 2 stocks at 09:30 via sector gate FIRST, then watch only those 2 | 749 | 42% | 18.24% | -11.11% | 0 |

\* The rank-priority top-5 result (31.11%) was later found to have a
subtle look-ahead flaw — it let a higher-ranked candidate's LATE-firing
signal claim a slot ahead of a lower-ranked candidate that had already
confirmed EARLIER in the day, which a live system cannot replicate (you
can't wait to see if a better-ranked stock "eventually" fires before
committing to a worse-ranked one that already has). The **causal**
version (time-ordered, all 5 candidates watched together in real time,
whichever 2 confirm first win) is the methodologically correct fix, and
its honest 5-year CAGR (21.34%) came in below the plain top-2 design.
This is flagged prominently so nobody re-derives the inflated 31.11%
number and mistakes it for a validated result.

**Takeaway**: widening the candidate pool past 2/day does not help once
implemented without look-ahead bias — it either underperforms (causal
top-5, freeze-2) or only looked better because of a backtesting
artifact (rank-priority top-5). The simplest design — rank the F&O
universe, take the top 2, gate each independently on its own sector
ratio — remains the best **and** simplest mechanism found. Simplicity
is also an operational asset for a live/paper system: fewer moving
parts, one sector-ratio lookup per candidate instead of tracking up to
5 candidates' live state in parallel.

---

## 14. Paper-trading operational checklist

Use this as the go/no-go checklist before flipping the paper-trading
system live each morning, and as the build checklist for a first
implementation.

### 14.1 Pre-market / build-time (once, or on each data refresh)

- [ ] NIFTY 50 constituent list sourced and current.
- [ ] F&O-eligible equity universe sourced and current (candidate
      ranking pool, §2.3).
- [ ] NSE sectoral/thematic index constituent + weightage data sourced
      and current (§6.1) — confirm it's been refreshed within the last
      ~6 months (NSE's typical index-rebalance cadence).
- [ ] Primary-sector map rebuilt from that data (highest-weightage
      SECTORAL membership, falling back to eligible THEMATIC only if
      none) and spot-checked against 2-3 well-known stocks.
- [ ] Each candidate symbol has enough historical 5-min data locally to
      properly warm up EMA21 and ATR14 (several weeks minimum; this
      project used years of history — don't start trading a freshly
      listed or freshly-onboarded symbol on day 1).

### 14.2 09:15–09:30 (first 15 minutes, every trading day)

- [ ] Capture every candidate universe member's `ret_first15` (09:25
      candle close vs. prior day's official close) as each 09:25 candle
      closes.
- [ ] At 09:30: compute NIFTY50's own A/D ratio on `ret_first15`.
      Apply the 1.5 LONG / 0.66 SHORT gate — if neither threshold
      clears, **the whole day is off, stop here**.
- [ ] Rank the F&O universe by `ret_first15` in the day's direction,
      take the top 2.
- [ ] For each of those 2, resolve its primary sector and compute that
      sector's own `ret_first15` A/D ratio (same 09:30 snapshot). This
      value is now fixed for the rest of the day — no need to
      recompute it later.

### 14.3 09:30–11:05 (signal + entry window, per candidate)

- [ ] For each of the 2 candidates independently, run the candle-close-
      driven signal detection (§3): EMA21 gate + first-candle gate
      (checked every closed candle) and the signal-candle color/volume/
      ATR check (only up to 11:00 for a NEW signal).
- [ ] The moment a candidate's breakout confirms (§3.2 step 2, tick-
      driven per §7.2 for live responsiveness): check its **pre-
      computed** sector ratio (§14.2) against the 2.0/0.5 threshold. If
      it fails, **do not take the trade** — this candidate produces
      nothing today, even though its signal/breakout was otherwise
      valid.
- [ ] If it passes: place the entry per §7.3's recommended exchange-
      side conditional order mechanics.

### 14.4 Through the trading day

- [ ] Half-position exits at the 1:2 target (tick-driven).
- [ ] Remaining half's stop stays at the ORIGINAL level — do not move
      it to breakeven (§9.2/§14.5).
- [ ] 15:10 forced squareoff for whatever remains open (time-driven,
      not price-driven).

### 14.5 What NOT to change without re-validating (recap of §9 + §13)

- [ ] No pool-narrowing sector filter at candidate-selection time.
- [ ] No breakeven-stop-move.
- [ ] No macro NIFTY-below-200EMA regime filter.
- [ ] No EMA200 filter on the signal candle (§13 — tested, made things
      worse).
- [ ] Do not widen past top-2/day (§13 — every wider variant tried
      underperformed or relied on a look-ahead artifact).
- [ ] Day-bias threshold stays 1.5/0.66; sector-gate threshold stays
      2.0/0.5 — do not relax the sector gate to match the day-bias
      threshold (tested, hurt every time, §9.5).

### 14.6 Track these metrics from day one of paper trading

Compare against the 5-year backtest baseline (CAGR 22.29%, Sharpe 3.14,
profit factor 1.58, max drawdown -6.64%, 44% win rate) as your paper
trades accumulate — the earlier this diverges meaningfully, the earlier
you'll catch a live-implementation bug rather than a strategy problem:
- Running win rate (expect ~44%, wide swings are normal at low trade
  counts — don't react to fewer than ~50 trades).
- Average win vs. average loss size (the strategy's edge comes from
  asymmetric payoff, not high win rate — the `squareoff` legs, i.e. the
  runner half riding to 15:10, should be doing most of the heavy
  lifting per §10's exit-reason breakdown in v1's spec).
- Entry-price slippage vs. the computed trigger level (this is the
  single most likely source of live-vs-backtest divergence — track it
  explicitly per trade).
- Whether both daily slots are getting filled when 2 valid signals
  exist, or whether margin/capital constraints are silently blocking
  the 2nd trade.
