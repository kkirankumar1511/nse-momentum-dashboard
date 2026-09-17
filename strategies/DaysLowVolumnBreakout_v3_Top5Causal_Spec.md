# DaysLowVolumnBreakout v3 — Top-5 Causal + Vol10% + 2-Candle Breakout

**Status:** validated on 5-year and 10-year backtests. Best-known configuration
as of 2026-09-17, superseding `DaysLowVolumnBreakout_v2_SectorGate_Spec.md`.
Not yet paper-traded live.

Reference implementation: `scratch_baseline_top5_best.py`.

## 1. Summary of what changed from v2

v2 (the prior best, §CAGR 22.29%/5yr) used a fixed top-2/day F&O-ranked
candidate pool, a single-candle breakout window, and a 5% volume tolerance.
v3 changes three things simultaneously, each independently validated as an
improvement before being combined:

1. **Candidate pool widened to top-5/day**, walked with a genuinely
   time-ordered ("causal") mechanism — see §4.
2. **Signal-candle volume tolerance loosened from 5% to 10%**
   (`VOL_THRESHOLD_PCT = 0.10`).
3. **Breakout window widened from 1 to 2 candles**, with the added
   requirement that *every* candle in the window be the confirming
   color (green for LONG, red for SHORT) — a single wrong-colored
   candle drops the signal immediately, it does not wait out the window.

Two adjacent ideas were tested and explicitly rejected — see §9.

## 2. Candle and data conventions (unchanged from v1/v2)

- 5-minute candles, **open-time labeled**: a candle stamped `HH:MM`
  covers `[HH:MM, HH:MM+5min)`. E.g. the `09:25` candle covers
  09:25:00–09:29:59 and its close is the price at **09:30:00 real time**.
  This matters for live timing — see §8.
- Trading session: 09:15–15:30. Signal detection window starts 09:30.

## 3. Candidate selection (day-bias + F&O ranking, unchanged mechanism from v1/v2)

1. At the 09:30 checkpoint, compute `ret_first15` for NIFTY 50 = close of
   its `09:25` candle vs previous day's close.
2. Day bias: **LONG** if `NIFTY advance/decline ratio > 1.5`, **SHORT**
   if `< 0.66` (moderate threshold, validated best — see v2 spec §9.4
   for the sweep). Days that don't clear either threshold are skipped
   entirely (no candidates, no trades).
3. Within the F&O-tradable universe (~200-210 symbols), rank all
   symbols by their own `ret_first15` in the day's bias direction
   (most negative first for SHORT days, most positive first for LONG
   days).
4. Take the **top 5** ranked symbols as that day's candidate pool
   (`result/top5_niftybais_first15_mid_{5year,10year}.csv`, built by
   `scratch_niftybais_first15_relaxed.py 1.5 0.66 <lookback_days>
   <out_csv> 5`).

This candidate list is fixed and known by 09:30:00 — no look-ahead here.

## 4. The causal (time-ordered) multi-candidate walk

This is the mechanism that replaced an earlier, buggy "rank-priority"
version (documented as a cautionary case study in the v2 spec §13) —
**this is the single most important correctness property of the whole
strategy and must be reproduced exactly in any live implementation.**

The bug it fixes: checking candidates in rank order, each scanning its
*entire day* for a valid entry before moving to the next rank, lets a
late-firing higher-rank signal (e.g. at 13:00) claim a trade slot ahead
of an early-firing lower-rank signal (e.g. at 09:40) — which a live
system could never know in advance. The fix:

- All 5 candidates' 5-minute candles are walked **together, one
  timestamp at a time**, in ascending chronological order
  (`for ts in sorted(all_ts): ...`).
- Each candidate maintains its own independent state machine per day:
  `invalidated`, `active` (a pending signal awaiting breakout
  confirmation), `breakout_counter`, `resolved`.
- At every timestamp, **every unresolved candidate** is evaluated using
  only that timestamp's own candle and state set on strictly earlier
  timestamps — never a future candle.
- The moment a candidate's breakout confirms (trigger price touched AND
  its sector gate passes), it is added to a `fires_this_candle` list for
  that timestamp.
- **Ties at the exact same timestamp are broken by rank** (lower rank
  number wins) — this is the only place rank ordering matters, and only
  among candidates that already confirmed at that identical instant.
- The day stops taking new trades once `MAX_TRADES_PER_DAY = 2` slots
  are filled; the outer timestamp loop then breaks — no further candles
  are processed for that day, for any remaining candidate, regardless
  of what might have happened to them later.

**Live-trading requirement:** this needs an event-driven system
monitoring all 5 candidates' live 5-minute candles simultaneously and
reacting at each candle close in lockstep — not 5 independent
sequential scripts, and not a system with materially different latency
per symbol. Reintroducing sequential/rank-ordered processing here would
silently reintroduce the exact look-ahead bug this mechanism was built
to eliminate.

## 5. Signal detection (per candidate, once `active` in the walk above)

At each new (non-`active`, non-`invalidated`) timestamp `ts`, before
`NEW_SIGNAL_CUTOFF` (11:00:00, inclusive):

1. **First-candle gate**: if `close < first_candle.low` (LONG) or
   `close > first_candle.high` (SHORT), the candidate is invalidated
   for the rest of the day (checked every candle, not just at signal
   time).
2. **EMA21 gate**: if `close < EMA21` (LONG) or `close > EMA21` (SHORT),
   same invalidation. EMA21 is a continuous EWM over all 5-min closes
   (`span=21, min_periods=21`), evaluated using the candle's own close —
   legitimate at that candle's close, not before.
3. **Signal candle** = the candle is the confirming-signal color (red
   for LONG, green for SHORT — i.e. the *pullback* color, opposite the
   trade direction) **and** its volume is within 10% of the day's
   lowest volume-so-far (`row.volume <= vol_so_far.loc[:ts].min() *
   1.10`), evaluated causally (only candles up to and including `ts`).

If both conditions hold, `active = {time: ts, hi: candle.high,
lo: candle.low, atr: ATR14 at ts}` and the breakout window opens on the
*next* candle. **No same-candle re-signal**: a candle that fails to
confirm an active signal can never itself become a fresh signal candle
— only the candle after it can (validated: same-candle re-signal was
tested and found worse in every variant, top-2 and top-5 alike; see the
v2 spec and §9 below).

## 6. Entry: 2-candle, both-confirming-color breakout window

`BREAKOUT_WINDOW = 2`. For each candle after signal formation, in order:

1. **Color check first.** The candle must be the *confirming/
   continuation* color — green for LONG, red for SHORT (the opposite of
   the signal candle's color). If it is not, the signal is dropped
   **immediately** — it does not get a second candle to redeem itself,
   even if only 1 of the 2 allowed window candles has been used.
2. **Trigger check**, only if color passed: LONG triggers if
   `candle.high >= signal.high + 0.05 * ATR14@signal`; SHORT triggers if
   `candle.low <= signal.low - 0.05 * ATR14@signal`. Entry price is the
   exact trigger level (not the touched extreme).
3. If neither the window is exhausted (2 candles used) nor triggered,
   drop the signal.
4. **Stop** = the same ATR buffer on the opposite side of the signal
   candle (`signal.low - buffer` for LONG, `signal.high + buffer` for
   SHORT) — fixed at signal formation, does not move.

**Important asymmetry confirmed while building this**: the volume
condition (`is_lowest_volume`, 10% tolerance) is checked **only once**,
at signal-candle detection. It is **never** re-checked on the
confirmation/breakout candle(s) — those only need the correct color and
to touch the trigger price. This was tested as a deliberate variant
(requiring the confirmation candle to *also* be within 10% of the
day's-lowest volume) and found to be dramatically worse — see §9.

## 7. Exit (unchanged mechanism from v1/v2)

- **Target** = 1:2 reward:risk from entry, where risk = `|entry - stop|`.
- **Half-target-half-squareoff**: the first candle to touch the target
  books **half** the position at the target price. The remaining half
  rides until either the stop is hit or the `15:10`-labeled candle's
  close (price at **15:15:00 real time** — see §8) forces a squareoff
  of whatever remains, regardless of price. **Never moved to
  breakeven** — validated as worse in earlier testing (documented in
  the v2 spec).
- **Same-candle stop/target overlap**: if a single 5-min candle's
  high-low range contains both the stop and the target, the backtest
  always resolves **stop first** (the conservative assumption — see
  `pullback.py`'s `simulate_exit` docstring). Live trading resolves
  this automatically via actual tick sequence; live results can only
  match or beat this assumption, never be worse because of it.

## 8. Sector confirmation gate (unchanged mechanism from v2)

Identical infra and thresholds to v2 — reused here without
modification:

- Each candidate's NSE **primary sector** (`sector.primary_sector_map()`
  in `sector.py`) is the highest-weightage SECTORAL index it belongs to,
  falling back to an eligible THEMATIC index if it has no SECTORAL
  membership. Backed by `cache/nse_index_constituents.json` and
  `cache/nse_index_catalog.json`.
- At the 09:30 checkpoint, compute each touched sector's own
  advance/decline ratio using the *same* `ret_first15` method (each
  sector-constituent's `09:25`-candle close vs previous close) — this
  is a **single fixed value per sector per day**, computed once and
  reused for every gate check that day.
- Gate: **LONG requires sector ratio > 2.0; SHORT requires sector ratio
  < 0.5** (strict thresholds, validated best in the v2 threshold sweep).
  A candidate with no primary sector, or whose sector's ratio doesn't
  clear the gate, has its confirmed breakout discarded (never enters).

## 9. Live tick-vs-candle-close timing — cross-checked against genuine tick-by-tick trading

This section is the result of an explicit audit (2026-09-17) tracing
every decision point in `scratch_baseline_top5_best.py` against what a
real tick-by-tick live system would need to do differently.

**Directly replicable from candle closes, no changes needed:**
- Day-bias / candidate ranking, first-candle gate, EMA21 gate, sector
  gate, signal-candle detection — all evaluated once per 5-min candle
  close, never mid-candle.

**Require genuine continuous tick monitoring, not candle-close polling:**
- The breakout trigger (§6.2) and the stop/target checks (§7) use the
  candle's high/low — i.e. "did price touch this level *anywhere*
  within the 5 minutes." A live system must watch ticks continuously
  through the candle to catch the exact moment, not just poll at candle
  close.

**A genuine off-by-one timing trap — confirmed by tracing candle
labeling (§2):**
- The squareoff exit uses the **close of the `15:10`-labeled candle**,
  which (since candles are open-time labeled) is the price at **15:15:00
  real time**, not 15:10:00. A live system that naively places the
  squareoff order "at 15:10" would exit 5 minutes early on the wrong
  price. **The order must be placed at 15:15:00.**
- Note: this contradicted an earlier draft of the v2 spec (§9 line
  "force-close... at 15:10 real time"), which was imprecise relative to
  both the actual code and that same spec's own §5. Any live runner
  should follow this document's wording (15:15:00 real time), not that
  older line.

**A cost-model bug that understates/mischarges STT on SHORT trades:**
- `costs.round_trip_cost()` always applies STT to the *exit* leg
  (`sell_turnover = exit_price * qty`), which assumes a buy-then-sell
  (LONG) sequence. Indian intraday STT (0.025%) is charged on whichever
  leg is the actual **sell** order — for a SHORT trade that is the
  **entry** (short-sell), not the exit (cover-buy). This strategy is
  heavily SHORT-skewed (day-bias/sector-gate mechanics naturally
  produce more SHORT signals — e.g. one sampled month was 17/20 SHORT
  trades), so the backtest is silently charging STT on the wrong leg's
  turnover for the majority of trades. The rupee impact is small (entry
  and exit prices are close together given the tight ATR-buffered
  targets) but it is a real, fixable gap between backtested and actual
  live costs. **Not yet fixed in the codebase as of this document.**

**Infra requirement, not a bug:**
- The causal multi-candidate walk (§4) requires simultaneously
  monitoring all 5 ranked candidates' live candles in lockstep. Getting
  this wrong (sequential processing, uneven latency across symbols)
  reintroduces the exact look-ahead bug this mechanism was built to fix.

## 10. Position sizing (unchanged mechanism from v1/v2)

- `LEVERAGE = 5.0`, `MAX_RISK_PCT_PER_TRADE = 0.5%` of current capital.
- Once per day (not re-computed intraday): `capital_alloc = (capital /
  MAX_TRADES_PER_DAY) * LEVERAGE`, `risk_budget = capital *
  MAX_RISK_PCT_PER_TRADE`.
- Per trade: `qty = min(risk_budget // risk_per_share, capital_alloc //
  entry_price)` — whichever constraint binds first, floored to a whole
  share (`pullback.position_size`).
- Both of a day's trade slots use the *same* day-start capital snapshot
  even if trade 1 has already partially exited before trade 2 opens —
  a minor simplification vs a live system's true available margin.

## 11. Backtest results

### 11.1 5-year (`result/baseline_top5_best_5year_trades.csv`)

positions=1201, win_rate=45%, net P&L=+₹40,39,435 (₹10L → ₹50.39L,
+403.94%), **CAGR=37.81%**, profit factor 1.47, Sharpe 2.91, max
drawdown -10.75% (2022-07-28 → 2022-09-29, recovered in 22 days).

By exit reason: squareoff 344 legs/95% win/+₹73.4L; stop 856 legs/0%
win/-₹84.8L; target 505 legs/100% win/+₹51.9L.

By rank: rank1 234pos/48%/+₹11.63L, rank2 255pos/44%/+₹10.69L,
rank3 248pos/45%/+₹7.75L, rank4 249pos/39%/+₹3.04L,
rank5 215pos/47%/+₹7.29L — every rank contributes positively.

Year-wise: 2021 +12.83%, 2022 +43.21%, 2023 +24.86%, 2024 +9.22%,
2025 +40.87%, 2026 (partial) +62.36% — **zero losing years**.

### 11.2 10-year (`result/baseline_top5_best_10year_trades.csv`)

positions=2117, win_rate=46%, net P&L=+₹3,96,68,832 (₹10L → ₹4.07Cr,
+3966.88%), **CAGR=44.18%**, span=10.13yr, profit factor 1.47, Sharpe
3.45, max drawdown -10.81% (Aug 2025, recovered by Nov 2025).

By exit reason: squareoff 611 legs/96%/+₹7.22Cr; stop 1505 legs/0%/
-₹8.37Cr; target 929 legs/100%/+₹5.12Cr.

By rank: rank1 382pos/46%/+₹79.34L, rank2 442pos/48%/+₹1.34Cr,
rank3 437pos/48%/+₹73.65L, rank4 440pos/44%/+₹62.32L,
rank5 416pos/43%/+₹46.95L.

Per-year compounded return (start capital → end capital, that year's %):

| Year | Return | Trading days |
|---|---|---|
| 2016 (partial) | +15.80% | 54 |
| 2017 | +60.98% | 127 |
| 2018 | +10.04% | 113 |
| 2019 | +67.29% | 116 |
| 2020 | +100.94% | 147 |
| 2021 | +44.24% | 154 |
| 2022 | +52.50% | 164 |
| 2023 | +15.52% | 126 |
| 2024 | +10.97% | 117 |
| 2025 | +32.72% | 122 |
| 2026 (partial, ~8.5mo) | +57.59% | 111 |

**Every single one of the 10 years is positive** — the weakest year
(2018) still returned +10.04%. CAGR *improved* from 5yr to 10yr
(37.81% → 44.18%), the opposite of what happened when the v2 config was
extended to 10 years (22.29% → 14.07%, flagged there as likely
data-completeness-affected) — a meaningfully more reassuring result.

**Caveat carried over from v2**: both the 5yr and 10yr runs relied on a
`load_cached_only()` live-fetch fallback for symbols not fully cached
(15/210 candidate symbols and 56/393 sector-constituent symbols in the
10yr run) — the same non-deterministic-caching mechanism flagged as a
"data-completeness bug" in the v2 spec. This run's result is internally
consistent, but a re-run could in principle differ slightly if the
live-fetch pulls a different data window. Not observed to be a large
effect here (unlike v2's case), but not fully ruled out either.

**Profit concentration flag**: 2026 alone (partial year, ~8.5 months)
contributed ~37% of total 10-year profit (₹1.49Cr of ₹3.97Cr). Traced
the actual September 2026 trades (BOSCHLTD, SAIL, ICICIPRULI,
CHOLAFIN, HCLTECH, INFY, KAYNES, GODREJPROP, COCHINSHIP, GVT&D,
SOLARINDS) and found them legitimate — the large absolute P&L simply
reflects the compounded capital base (~₹2.5Cr+) by that point, not a
sizing or data bug. Still worth remembering when judging repeatability.

## 12. Alternatives tested and rejected

| Variant | Change from v3 baseline | Result | Verdict |
|---|---|---|---|
| Conditional 1-candle extension (+ same-candle re-signal) | top-2, vol10%, extend only if failed candle is confirming-color + exact day's-low-vol (0% tolerance) | CAGR 16.84% | Worse — rejected |
| Conditional extension, no re-signal | same, re-signal removed | CAGR 17.55% | Worse — rejected |
| Conditional extension on top-5 causal | same idea, top-5 pool | CAGR 20.67% | Worse — rejected |
| Same-candle re-signal only | top-2, vol5%, 1-candle window, re-signal allowed | CAGR 20.40% | Worse — rejected |
| **Volume check required on breakout/confirmation candle too** | top-5 causal, both window candles must *also* clear the 10% volume tolerance (not just color) | positions=433, win=33%, **CAGR=-1.83%** | **Much worse — clearly rejected.** Confirms volume should gate the signal candle only, never the confirmation candle. |
| Unconditional 2-candle-both-color, top-2 pool only | same signal/entry rule, but top-2 (not top-5) candidates | CAGR 23.37% | Better than v2 baseline, but inferior to the top-5 causal version — top-5 pool is a real, independent source of the edge |
| **New-signal cutoff 10:00 instead of 11:00** | signals only allowed to form up to 10:00 | positions=872 (-27%), win=47%, net P&L ≈ unchanged (+₹40.44L vs +₹40.39L), CAGR 37.85%, **profit factor 1.55, Sharpe 3.70**, max DD -10.44% | **Not adopted as the primary config, but a strong candidate**: nearly identical absolute return with meaningfully better risk-adjusted metrics (Sharpe 2.91→3.70) and 27% fewer trades. Only tested over 5 years so far — not yet validated over 10 years. Worth considering as the live default if lower turnover / smoother equity curve is valued over marginal extra return. |

## 13. Not verifiable from backtesting alone

(Carried over from v2, still applicable to v3 — none of these are
addressed by anything in this document, and remain open until actual
paper-trading data exists.)

- True intraday slippage / partial fills at the exact trigger price.
- Broker-side latency between a tick crossing the trigger and an actual
  order fill, especially across 5 simultaneously-monitored candidates.
- Whether the live data feed's candle timestamps and the historical
  cache's candle timestamps are labeled identically (open-time vs
  close-time) — confirmed as open-time labeled here (§2, §8), but must
  be re-verified against whatever live feed is actually used.
- Real margin/leverage availability at 5x for a SHORT-heavy,
  multi-symbol intraday book.

## 14. Paper-trading operational checklist

- [ ] Fix the squareoff timing to fire at **15:15:00 real time** (§9),
      not 15:10:00.
- [ ] Fix or explicitly accept the STT-leg mischarge in
      `costs.round_trip_cost()` for SHORT trades (§9) before trusting
      live P&L to match backtested cost assumptions closely.
- [ ] Build genuine simultaneous 5-symbol live monitoring for the
      causal walk (§4) — sequential/latency-skewed processing across
      candidates is not acceptable, it reintroduces the exact
      look-ahead bug this mechanism was built to eliminate.
- [ ] Confirm live candle labeling convention (open-time) matches this
      document before wiring up any time-based logic.
- [ ] 15:10 forced squareoff for whatever remains open (time-driven,
      not price-driven) at the corrected 15:15:00 real-time trigger.
- [ ] Decide whether to launch with the 11:00-cutoff primary config
      (higher absolute CAGR, more trades) or the 10:00-cutoff variant
      (near-identical return, meaningfully better Sharpe/turnover) —
      the 10:00 variant has not yet been validated over 10 years.
- [ ] Track live fill prices vs backtested trigger-level prices from
      day one to start quantifying real slippage (§13).
