# DaysLowVolumnBreakout v5.4 — Top-5 Candidate Pool, EMA-Trail Exit, Look-Ahead-Corrected Selection ("CHRONO")

**Status: PARALLEL TRACK to v5.3 — under evaluation, not yet adopted as
the live baseline.** v5.3 (`DaysLowVolumnBreakout_v5.3_NoChainReSignal_
Spec.md`, current baseline: top-2 candidates, EMA-trail exit) is
unchanged and still the production reference. v5.4 is a separate branch
built on the same v5/v5.1 core signal-and-entry rules
(`DaysLowVolumnBreakout_v5_GatedSecondCandle_Spec.md`: EMA21/first-
candle gates, gated-2nd-candle window, lower-volume+no-chain re-signal),
but swaps **which candidates are traded** (top-5 pool instead of top-2)
— and, critically, **fixes a real look-ahead bug in how the day's 2
traded slots get filled** that was silently inflating every top-5-pool
backtest run this session before the fix (§2.3). v5.4 was originally
built and tested with the EMA-trail exit disabled (plain fixed 1:2); §2.2
covers both, and the EMA-trail version is now the adopted exit (§5) —
re-enabling it gave a clean improvement on the primary configuration.

Reference implementation (the recommended configuration, §5):
`scratch_v53_top5static_gap5_CHRONO_ematrail.py` (full 5-year). The
lower-drawdown alternative (§5a, against-trend + dynamic sector gate)
is `scratch_v53_trendfilter_CHRONO_dynsector_ematrail.py`. Plain-1:2-
exit siblings of both (`..._plain12_gap5_CHRONO.py` and
`..._plain12_trendfilter_CHRONO_dynsector.py`, + their `_jun2024.py`
variants) remain on disk for comparison. The un-filtered look-ahead-
corrected, plain-exit baseline is `scratch_v53_top5static_plain12_
CHRONO.py`.

**All numbers below are single runs each**, per the standing "don't
re-run every experiment" preference.

## 0. CRITICAL — unresolved conflict with the nse-momentum-dashboard's own prior top-5 investigation (read this before deploying anything from this document)

`E:\trading-workspace\nse-momentum-dashboard` already has a live-trading-
ready implementation of this exact strategy family
(`intraday_strategy.py` + `intraday_engine.py`, backed by its own
`strategies/DaysLowVolumnBreakout_*.md` spec history, which is
otherwise identical to this project's — its v5.3 spec's numbers, 725
positions / 34.26% CAGR / PF 1.53 / DD -10.23% on the top-2 pool, are
byte-for-byte the same finding as this project's v5.3 spec). That
project **independently discovered the identical look-ahead problem**
this document's §2.3 calls "CHRONO" — its v3 spec (`DaysLowVolumnBreakout_
v3_Top5Causal_Spec.md`) names it explicitly: *"a late-firing higher-
rank signal could claim a trade slot ahead of an early-firing lower-
rank one — impossible for a live system to know in advance"* — and
built a genuinely causal, time-ordered multi-candidate walk
(`step_candidates_causal()`) to fix it, years/sessions before this
document's CHRONO fix was found independently in this session. This is
strong cross-validation that the CHRONO *principle* is real and
necessary — but the two projects' investigations **disagree sharply on
whether the top-5 pool is actually better than top-2 once the bug is
fixed**:

| | This session (v5.4, §2.3/§5) | nse-momentum-dashboard (v5 spec §5) |
|---|---|---|
| Top-5, corrected-selection, plain 1:2 exit, gated-2nd-candle rule | **37.88% CAGR**, PF 1.41, DD -15.47% | **17.93% CAGR**, PF 1.25, DD -19.59% |
| Verdict | Top-5 beats top-2 (34.26%) | "Top-5 is NOT better than top-2 — it's worse on both return and risk. Do not use top-5 pool for paper trading." |

Both numbers claim to be look-ahead-corrected on the same underlying
rule set, yet they disagree by more than 2x on CAGR and go in opposite
directions on whether top-5 even beats top-2. **This has not been
reconciled.** Plausible causes, none yet confirmed: (a) the dashboard's
"corrected candidate list" fix (its own §3.3, a hash-seed-randomized
`set`-iteration sector-mapping bug) is a *different* bug from CHRONO,
and it's unclear from its spec whether its top-5 re-test also carried
over v3's `step_candidates_causal()` walk, or reverted to a simpler
selection that might reintroduce a bug of its own; (b) possible
day-bias-threshold differences (dashboard uses NIFTY ratio >1.5/<0.66;
this session's candidate CSV's own threshold was not re-verified
against that); (c) different candidate-list construction/frozen-cache
snapshots between the two projects.

**Practical conclusion — until this is reconciled:**
- **Do not deploy v5.4's top-5-pool finding (§5) to live/paper
  trading.** Treat the 37.88-45.04% CAGR numbers in this document as
  informative for this session's own internal comparisons (filter
  effects, EMA-trail effects, etc. — those relative comparisons, all
  run under the same candidate list and selection code, are internally
  consistent regardless of the absolute-CAGR discrepancy) but not as
  validated for real capital.
- **The dashboard's existing v5.3 top-2 implementation
  (`intraday_strategy.py`/`intraday_engine.py`, already live-trading-
  ready — EMA-trail is wired into the live engine via `choose_trail_ema`/
  `trail_crossed`, not just defined) is the safer, already-vetted
  choice to actually run paper trades on.**
- **This session's one genuinely low-risk, well-corroborated addition
  on top of that existing top-2 implementation**: the 5% overnight-gap
  filter (§4-top2, new this session) — tested directly on v5.3's own
  top-2/EMA-trail rules (not the disputed top-5 pool) and found to
  improve win rate, profit factor, AND drawdown simultaneously (534
  positions, 37% win, CAGR 28.68%, **PF 1.72**, DD **-9.03%**, **Sharpe
  3.26** — the best risk-adjusted result found across *both* projects'
  histories). This is the one recommendation from this whole document
  that's safe to actually add to the existing live engine: skip a
  candidate whose 09:15 open already gapped >5% from the prior close,
  checked once, causally, before that candidate is even searched for a
  signal. No new runner was written for this (per instruction) — it
  should be implemented as one extra pre-market filter step inside the
  existing `intraday_strategy.py`/`intraday_engine.py` candidate-
  selection code, alongside its existing day-bias/sector-gate checks.

The rest of this document (§1 onward) is retained as-is for the
internal-comparison value described above, but every top-5 conclusion
in it should be read with §0's caveat attached.

## 1. What's unchanged from v5/v5.1/v5.3

- Signal candle: red (LONG) / green (SHORT), volume at-or-near the
  day's running minimum (**any color**, not same-color-only — v5.4
  keeps v5.3's rule here; a same-color-only variant was tried and
  rejected, §4).
- EMA21 + first-candle (09:15) close-through invalidation, checked
  every candle.
- Gated-2nd-candle breakout window, ATR-buffered trigger/stop.
- Re-signal: close beyond the prior signal's close, lower volume, no
  chaining (`chain_len == 0`).
- `entry_cutoff = 11:00:00` for fresh-signal search (re-signal itself
  has no cutoff).
- 5x leverage, 0.5% max risk/trade, Zerodha cost model,
  `MAX_TRADES_PER_DAY = 2`.

## 2. What v5.4 changes

### 2.1 Candidate pool: top-5, not top-2

Candidate source: `result/top5_niftybais_first15_mid_5year_FROZEN.csv`
(rank 1-5 per day by first-15-min return, same day-bias construction as
the top-2 CSV, just not cut down to 2). All (up to) 5 ranked candidates
are independently scanned candle-by-candle every day; only 2 are
actually traded.

### 2.2 Exit: EMA-trail (v5.3's rule, re-enabled) vs. plain 1:2

v5.4 was originally built and tested with v5.2/v5.3's EMA5/EMA10 trail
on the first half **disabled** — the first half booked unconditionally
at the fixed 1:2 target the instant it was touched, with the second
half riding to 15:10 squareoff (the "plain 1:2" scripts, `..._plain12_
..._CHRONO...py`). That was not a deliberate optimization choice, just
the exit v5.4 happened to be built with first.

**EMA-trail was then re-enabled** (transplanting v5.3's exact rule —
defer booking at 1:2 if the target sits on the momentum side of EMA5/
EMA10, trail that EMA instead, next-candle-open fill — onto the top-5
pool for the first time) and tested on both the primary config (§5) and
the §5a alternative:

| | Primary (gap5-only, static sector) | §5a (against-trend + dynamic sector) |
|---|---|---|
| Positions | 1,340 (same set either exit) | 619 (same set either exit) |
| | Plain 1:2 → **EMA-trail** | Plain 1:2 → **EMA-trail** |
| Win rate | 41% → **35%** | 44% → **37%** |
| CAGR | 37.09% → **45.04%** | 22.99% → **25.39%** |
| Profit factor | ~1.42 → **1.47** | — → 1.59 |
| Max drawdown | -14.55% → **-16.81%** | -7.28% → -9.05% |
| Sharpe | 2.43 → **2.53** | 3.02 → 2.81 |
| Sortino | — → 12.04 | 14.29 → 14.93 |
| Calmar | — → 2.68 | 3.16 → 2.81 |

Same trade-off shape as the original v5.1→v5.2 EMA-trail adoption:
fewer, larger wins (win rate down) in exchange for meaningfully higher
CAGR. **On the primary config, this is a clean win — CAGR and Sharpe
both improve simultaneously** (drawdown is the only metric that
worsens), so EMA-trail is adopted there (§5). **On §5a, it's a genuine
trade-off, not a clean win** — CAGR improves but Sharpe and Calmar both
get *worse* (3.02→2.81, 3.16→2.81) alongside a deeper drawdown — so §5a
keeps the plain-1:2 exit as its primary number, with EMA-trail noted as
an available higher-CAGR/lower-Sharpe option.

### 2.3 THE LOOK-AHEAD BUG AND ITS FIX ("CHRONO")

**The bug.** With up to 5 candidates scanned per day but only 2 tradeable
slots, every script this session (including the original v5/v5.3 top-2
scripts, though there the pool size already equals the slot count so it
never mattered) filled those slots by **iterating candidates in rank
order and taking the first 2 that found ANY valid entry that day** —
regardless of what time that entry actually triggered. Measured: **on
59.6% of all signal days** (pre-sector-gate), 3 or more candidates found
a valid signal. On any such day, picking "the top-ranked 2 of however
many signalled" requires knowing — before deciding whether to take an
earlier, lower-ranked entry — whether a higher-ranked candidate is going
to trigger *later that same morning*. A live/paper system cannot know
that; it can only react to what has already happened.

**The fix.** Sort each day's surviving (sector-gate-passed) candidates
by their **actual entry_time**, and take the first `MAX_TRADES_PER_DAY`
in that real chronological order. A later-triggering signal — no matter
how highly ranked, or how strongly it passes any other filter — simply
never gets a chance once the day's slots are already full. This is the
only selection rule that's genuinely executable in real time, and it's
what every "CHRONO" script name in this document refers to.

**Measured impact** (unfiltered baseline, full 5 years): **238 of 792
signal days (30%)** had their actual traded-2 change once sorted by real
entry time instead of rank. Position count is unchanged (1,386 either
way — the fix changes *which* 2 trade each day, not *how many* days
have 2), but the flawed rank-based selection had quietly been picking a
better-looking 2 more often than a live system honestly could:

| | Flawed (rank-order selection) | **CHRONO-corrected** |
|---|---|---|
| Positions | 1,386 | 1,386 |
| Win rate | 42% | 41% |
| CAGR | 44.95% | **37.88%** |
| Profit factor | 1.46 | 1.41 |
| Max drawdown | -16.46% | -15.47% |
| Sharpe | — (not computed) | 2.44 |

**~7 points of CAGR were an artifact of look-ahead, not a real edge.**
Every filter/variant result produced *before* this fix was discovered
(§4, marked "pre-CHRONO, not re-verified") should be treated as
directionally informative only, not as a number that would hold up
live. Every result marked CHRONO-corrected has this fix applied and is
believed look-ahead-free.

## 3. The sector confirmation gate: static vs. dynamic

v5/v5.3's sector gate snapshots each sector's advance/decline ratio
**once, at the fixed 09:25 candle**, and applies that single (sector,
day) verdict to every signal that sector produces that day, regardless
of when the signal candle itself actually forms (which can be as late
as 11:00+). v5.4 additionally tested a **dynamic** version: the ratio is
recomputed continuously and looked up at **each candidate's own
signal-formation time** instead.

On the recommended configuration (§5: against-trend + 5% gap filter),
switching to the dynamic sector gate was a clean, unambiguous
improvement on every metric simultaneously — not the usual
quality-for-quantity trade-off:

| | Static sector gate | **Dynamic sector gate** |
|---|---|---|
| Positions | 667 | 619 |
| Win rate | 43% | **44%** |
| CAGR | 22.37% | **22.99%** |
| Max drawdown | -8.22% | **-7.28%** |
| Sharpe | 2.74 | **3.02** |
| Sortino | 13.23 | **14.29** |
| Calmar | 2.72 | **3.16** |

Confirmed on the June-2024-onward window too (static: 313 pos / 19.05%
CAGR / -8.30% DD → dynamic: 297 pos / 21.62% CAGR / -7.32% DD, PF 1.51).

**However this is NOT a universal upgrade**: applied to the un-trend-
filtered "gap-only" configuration (no against-trend filter), the
dynamic sector gate was a wash-to-slightly-worse (CAGR 36.08%→36.12%,
but max DD -10.43%→-12.08%). It appears to specifically synergize with
the against-trend filter's already-small, selective candidate pool, not
to be a context-free improvement. **Recommendation: use the dynamic
sector gate only alongside the against-trend filter.**

### 3.1 Case study: "Baseline + 5% gap filter" (no trend filter)

This configuration — the plain CHRONO baseline with *only* the
overnight-gap filter, no against-trend filter — was tested in detail on
its own, since the gap filter alone is nearly free (§4) and worth
knowing precisely.

| | Full 5-year | June 2024 → present |
|---|---|---|
| Positions (static sector gate) | 1,340 | 589 |
| CAGR (static) | 37.09% | 36.08% |
| Max DD (static) | -14.55% | -10.43% |
| Positions (dynamic sector gate) | — (not run) | 557 |
| Win rate (dynamic) | — | 42% |
| CAGR (dynamic) | — | 36.12% |
| Profit factor (dynamic) | — | 1.45 |
| Max DD (dynamic) | — | **-12.08%** (worse) |

Reference: `scratch_v53_top5static_plain12_gap5_CHRONO.py` (static
sector, 5yr) and `scratch_v53_top5static_plain12_gap5_CHRONO_dynsector_
jun2024.py` (dynamic sector, June 2024+); trades in
`result/v53_top5static_plain12_gap5_CHRONO_5year_trades.csv` and
`result/v53_top5static_plain12_gap5_CHRONO_dynsector_jun2024_trades.csv`.
June-2024+ monthly breakdown available in the conversation history (28
months, no losing month streak longer than 1, best month 2026-09 at
+10.24% / PF 5.51).

On this configuration, the dynamic sector gate is the confirmed
wash-to-worse case referenced above — this is the specific evidence for
"don't pair the dynamic sector gate with the gap-only configuration."

**A tighter 3% gap threshold was also tested on this configuration**
(June 2024+, with dynamic sector gate): 517 positions, 40% win,
CAGR=21.15%, PF=1.29, DD=-13.75% — **worse than 5% on every single
metric** despite discarding more than twice as many candidates (17.6%
vs 6.8%). Confirms the gap filter isn't discriminating good from bad
trades; discarding more of them at random just removes more good ones
along with the bad. **5% (or no gap filter at all) is better than 3%.**

## 4. Eligibility filters tested (all discard candidates before
`find_entry()` is even called; none change entry mechanics)

| Filter | Basis | Discard rate | Positions | CAGR | PF | Max DD | CHRONO-corrected? |
|---|---|---|---|---|---|---|---|
| *(none)* | — | — | 1,386 | **37.88%** | 1.41 | -15.47% | ✅ |
| First-15-min breakout (09:25 close beyond 09:15 high/low) | causal, checked at 09:30 | 43% | 1,000 | 26.77% | 1.40 | -12.56% | ✅ |
| Overnight gap >6% (either direction, at 09:15 open) | causal, checked at 09:15 | 4.0% | 1,362 | 44.71% | 1.46 | -15.82% | ❌ pre-fix |
| **Overnight gap >5% (adopted, §5)** | same | 6.5% | **1,340** | **37.09%** | ~1.42 | **-14.55%** | ✅ |
| Overnight gap >3% | same | 17.6%* | 517* | 21.15%* | 1.29* | -13.75%* | ✅ (*jun2024 window, w/ dynsector) |
| >6% extension by 09:25 close (direction-signed) | causal, checked at 09:30 | 26-58% | 428/276 | 15.36%/9.72% | 1.51/1.54 | -9.08%/-8.52% | ❌ pre-fix |
| 09:25-vs-09:15-high only (no extension cap) | causal, checked at 09:30 | 45% | 361 | 20.04% | **1.79** | -8.50% | ❌ pre-fix, top-2 pool |
| Same-color-only volume (vs. any-color) | signal-rule change, not a filter | n/a (more signals, not fewer) | 1,472 | 33.96% | 1.35 | -10.60% | ❌ pre-fix |
| Against-own-daily-trend (20-day SMA, causal) | causal, checked at prior close | ~53% | 705 | 31.23% | 1.68 | -8.37% | ❌ pre-fix |
| Against-trend + gap5 (combined) | both above | ~65% | 667 | 22.37% | 1.50 | -8.22% | ✅ |
| **Against-trend + gap5 + dynamic sector gate (§5a alternative)** | all above + §3 | ~68% | 619 | 22.99% | — | -7.28% | ✅ |

**Consistent pattern across every hard-discard filter**: each one
improves win rate and/or drawdown modestly, at a CAGR cost that's
disproportionate to how many candidates it removes — because none of
them are precise enough to remove only bad trades (verified explicitly
for the extension filter: the discarded >6%-extended group's own win
rate, 38%, was *higher* than the baseline's 35%, meaning the filter was
cutting the strategy's biggest trend-day winners along with the losers).
**The only exceptions that improve return, not just risk, are the
dynamic sector gate (§3) and the small, nearly-free overnight-gap
filter** (discards <7% of candidates for a near-wash on CAGR).

A ranking **preference** (soft reorder instead of hard discard, e.g.
"prefer against-trend signals for the 2 slots when more than 2 exist")
was tried and found to be **invalid under the CHRONO-corrected
architecture** — see §2.3: a soft preference can't decide between two
candidates unless it already knows both of their outcomes, which is
exactly the look-ahead the fix removes. The only way to express a
preference honestly is as a hard eligibility filter (as in this table).

## 5. Recommended configuration (adopted)

**Overnight-gap >5% filter only, no against-trend filter, static
(09:25-snapshot) sector gate, EMA-trail exit (§2.2) + CHRONO
chronological slot-fill.**

| | Full 5-year | June 2024 → present |
|---|---|---|
| Positions | 1,340 | 589 |
| Win rate | 35% | 36% |
| **CAGR** | **45.04%** | **40.74%** |
| Profit factor | 1.47 | 1.46 |
| Max drawdown | -16.81% | -12.37% |
| Sharpe / Sortino / Calmar | 2.53 / 12.04 / 2.68 | 2.57 / 11.83 / **3.29** |

Reference: `scratch_v53_top5static_gap5_CHRONO_ematrail.py` (+
`_jun2024.py`); trades in
`result/v53_top5static_gap5_CHRONO_ematrail_5year_trades.csv` (+
`_jun2024_trades.csv`). The June-2024+ window confirms the same pattern
as the 5-year run — CAGR up (36.08%→40.74%) with only a modest drawdown
cost (-10.43%→-12.37%) versus the plain-1:2 exit, and its Calmar ratio
(3.29) is actually the best of any variant tested at either window
length.

**Plain-1:2-exit version** (§2.2's "before" column — still fully valid,
slightly lower CAGR/Sharpe but shallower drawdown, and the only one
with a verified June-2024+ number):

| | Full 5-year | June 2024 → present |
|---|---|---|
| Positions | 1,340 | 589 |
| Win rate | 41% | 42% |
| CAGR | 37.09% | 36.08% |
| Final capital (from ₹1,000,000) | ₹4,908,817 (+390.88%) | ₹2,018,892 (+101.89%) |
| Max drawdown | -14.55% | -10.43% |

Reference: `scratch_v53_top5static_plain12_gap5_CHRONO.py` (+
`_jun2024.py`); trades in
`result/v53_top5static_plain12_gap5_CHRONO_5year_trades.csv` (+
`_jun2024_trades.csv`).

Either exit is barely different from the plain unfiltered baseline in
candidate/position count (§2.3: 1,386 pos, since the 5% gap filter only
discards ~6.5% of candidates) — the gap filter is adopted because it's
a strictly-better-or-equal trade on drawdown at effectively no CAGR
cost, on top of whichever exit is chosen.

### 5a. Lower-drawdown alternative

**Against-own-daily-trend filter + overnight-gap >5% filter + dynamic
(signal-time) sector gate + CHRONO, plain-1:2 exit.** Trades roughly
half the raw CAGR away in exchange for a meaningfully shallower
drawdown and the best risk-adjusted ratios of anything tested — worth
choosing instead if drawdown control matters more than maximizing CAGR:

| | Full 5-year | June 2024 → present |
|---|---|---|
| Positions | 619 | 297 |
| Win rate | 44% | 43% |
| CAGR | 22.99% | 21.62% |
| Profit factor | — | 1.51 |
| Max drawdown | -7.28% | -7.32% |
| Sharpe / Sortino / Calmar | 3.02 / 14.29 / 3.16 | — |

Reference: `scratch_v53_top5static_plain12_trendfilter_CHRONO_dynsector.py`
(+ `_jun2024.py`); trades in
`result/v53_top5static_plain12_trendfilter_gap5_CHRONO_dynsector_5year_trades.csv`
(+ `_jun2024_trades.csv`). Note this variant uses the **dynamic** sector
gate (§3), unlike the primary recommendation above, which uses the
static one — the dynamic gate only pays off when paired with the
against-trend filter (§3.1).

**EMA-trail option on this config** (§2.2): higher CAGR (25.39%) but
*worse* Sharpe (2.81) and Calmar (2.81) and a deeper drawdown (-9.05%)
— unlike the primary config, EMA-trail is **not** a clean win here, so
plain-1:2 remains the recommended exit for §5a specifically. Reference:
`scratch_v53_trendfilter_CHRONO_dynsector_ematrail.py`; trades in
`result/v53_trendfilter_gap5_CHRONO_dynsector_ematrail_5year_trades.csv`.

## 5.1 Implementation guide for live/paper trading (kept for reference — see §0 before acting on it)

**Per §0, do not build this against the top-5 pool for real capital
until the dashboard discrepancy is reconciled.** This section is kept
because its non-pool-specific content (the chronological-selection
principle, the sector-gate timing, the exit-fill convention) is still
correct and reusable — but step 2/6's *top-5* framing specifically
should not be treated as a green light on its own.

A live system never has the "which 2 of many" problem CHRONO fixes in
the backtest — it only ever sees candidates trigger one at a time, in
real order, which *is* chronological by construction. CHRONO's role was
purely to make the **backtest** simulate that same honest, in-order
behavior instead of secretly picking with hindsight. So implementing
this live is simpler than the backtest, not harder — just take trades
as they happen:

This describes the **primary recommendation (§5: gap5-only, static
sector gate)**; §5a's alternative differs only in steps 1/4 (add the
against-trend pre-market check) and step 6 (use the dynamic sector
check instead of the static one) — both called out inline below.

1. **09:15:00 (candle open):** as soon as each stock's opening trade
   prints, compute the overnight gap `%` vs yesterday's close for every
   F&O stock; discard (mark ineligible for the day) any stock gapping
   beyond 5%, either direction (§4).
   *(§5a only: also, pre-market, using yesterday's close, compute each
   stock's 20-day SMA and flag its daily trend — uptrend if
   `close > SMA20`, else downtrend; this feeds step 4 below.)*
2. **09:25:00 (09:15 candle closes) → 09:30:00 (09:25 candle closes):**
   compute each remaining stock's return since prev-close through both
   candles; rank descending (LONG day) / ascending (SHORT day); take
   the **top 5** — this is the day's candidate pool, same construction
   as `top5_niftybais_first15_mid_5year_FROZEN.csv`. Apply the §2.1
   day-bias (NIFTY50 advance/decline ratio at this same 09:25 point) to
   decide LONG vs SHORT for the day.
3. **Sector confirmation snapshot (static, §3):** at this same 09:25
   point, compute each touched sector's advance/decline ratio among its
   index constituents (using each constituent's own return-since-prev-
   close as of 09:25); a candidate's sector must confirm (ratio >
   `SECTOR_LONG_MIN_RATIO` for LONG, < `SECTOR_SHORT_MAX_RATIO` for
   SHORT) to be eligible. This single (sector, day) verdict applies to
   that sector's signals all day — it is **not** re-checked later.
4. *(§5a only)* **Cross the against-trend filter against this top-5**:
   a candidate is eligible only if its direction is *against* its own
   pre-market trend flag from step 1 (LONG signal + stock in a daily
   downtrend, or SHORT signal + stock in a daily uptrend). Typically
   leaves 0-3 of the 5 eligible.
5. **09:25 through 11:00:00, continuously:** for every eligible
   candidate (passed steps 1/3, and 4 if §5a) in parallel, run the
   unchanged v5/v5.1 signal-and-entry state machine (§1) candle-by-
   candle: EMA21/first-candle invalidation check, lowest-any-color-
   volume signal detection, gated-2nd-candle breakout trigger,
   re-signal-on-deeper-close (lower volume, no chaining). Identical
   logic to the existing v5.3 live runner; only the *candidate list*
   and *exit* differ.
6. **On every trigger event** (a candidate's breakout condition fires):
   if 2 trades have already been taken today, ignore it — the day is
   done. Otherwise: it already passed the sector check in step 3, so
   take the trade. *(§5a only: instead, compute the sector confirmation
   fresh at that exact moment — that trigger's own sector's live
   advance/decline ratio at signal time, not the stale 09:25 number —
   and only take the trade if it still confirms; if not, skip this
   trigger and keep monitoring the other eligible candidates.)*
7. **Entry/stop**: exactly as triggered — entry at the ATR-buffered
   breakout level, stop at the signal candle's opposite extreme minus/
   plus the same buffer (unchanged from v5).
8. **Exit (§2.2 — EMA-trail for the primary/§5 config, plain 1:2 for
   §5a)**: for §5, at the 1:2 target touch, check whether the target
   sits on the momentum side of EMA5 (or EMA10); if so, defer booking
   and trail that EMA instead, filled at the next candle's open once a
   close crosses back through it; otherwise (and always, for §5a) book
   50% immediately at the fixed target. Either way, the remaining 50%
   rides, with the original stop still live, until square-off. Per the
   inherited v5 §0 caveat, square off at **15:15:00 real time**, not
   the "15:10" candle label (the candle stamped 15:10 covers 15:10-
   15:15; its close is only known at 15:15).
9. **Sizing**: unchanged from v5 — `qty = min(risk_budget //
   risk_per_share, capital_alloc // entry)`, `risk_budget = capital ×
   0.5%`, `capital_alloc = (capital / 2) × 5.0` (5x leverage, 2-way
   split for the day's 2 slots).

Steps 1-4 can be fully scripted and run unattended each morning; steps
5-6 are the part that needs a live/paper execution loop watching 5-
minute candles as they close (or the underlying tick/quote feed, if
finer-grained trigger detection is wanted — the backtest only ever
checks trigger conditions once per closed 5-minute candle, so matching
that cadence live keeps behavior identical to what's been tested here).

## 6. Deep-dive analytics: no predictive entry-time signal found

Extensive testing (causal feature engineering across 15-18 features —
first-30-min price action, momentum, volume acceleration, RSI, range
position, risk %, day-level NIFTY-ratio magnitude, sector-strength
magnitude — cross-validated RandomForest classification, out-of-fold
"star rating" tiers, ATR-relative-extension regime analysis) found
**no feature or combination that predicts win/loss above a coin flip**
once look-ahead is properly excluded (cross-validated AUC ≈ 0.49-0.50
across every honest test). A naive first-30-minute momentum signal
looked strong (p=0.0001, AUC 0.588) until rebuilt causally, at which
point it evaporated (p=0.10, AUC 0.501) — a clear look-ahead artifact.
A 5-star/4-star/3-star position-sizing scheme built from an out-of-fold
model score was tested directly and found **actively harmful**: the
top-scored tier had the *lowest* win rate and P&L of the three tiers
tested, non-monotonic across a 5-tier split too. **Conclusion: this
strategy's edge is in its payout structure and trade frequency, not in
being able to identify better-vs-worse individual setups in advance.**
One genuinely real (though weaker, p≈0.05, and not yet independently
re-validated post-CHRONO) finding: trades taken *against* a stock's own
higher-timeframe (daily 20-SMA) trend outperformed trades *with* it
(44.5% vs 40.1% win rate, PF 1.69 vs 1.28) — this is the origin of the
against-trend filter in §4/§5.

## 7. Caveats

- §2.3's CHRONO fix should be treated as the definitive correction; any
  number in §4 marked "pre-fix, not re-verified" could shift if
  re-tested (most likely downward, following the unfiltered baseline's
  pattern, though the magnitude of the shift will vary by how
  concentrated that variant's candidate pool already is — see the
  breakout filter's smaller 26.77%→26.77%-no-change-needed gap, already
  corrected, versus the trend-filter's originally-uncorrected 31.23%).
- v5's other inherited caveats still apply: squareoff at 15:15:00 real
  time vs the "15:10" candle close; SHORT-trade STT leg attribution
  unfixed; single-run results throughout.
- The EMA-trail exit (§2.2) has been tested with the top-5 pool and
  CHRONO fix on both windows for §5, and is now the adopted exit for
  §5's primary recommendation. It has only been tested on the full
  5-year window for §5a (where it was found to be a worse choice than
  plain-1:2, §2.2) — its June-2024+ number for §5a specifically is not
  yet run, though given §5a's full-5-year result this is not expected
  to change the recommendation there.
- The 3% gap threshold was tested once (on the gap-only + dynsector,
  no-trend, June-2024 configuration) and was clearly worse than 5% on
  every metric — narrower is not better here.

## 8. The v5.3 top-2 + 5% gap filter finding (§0's recommended addition)

Applying this session's gap filter (checked once, causally, at 09:15:00
against yesterday's close) to v5.3's **actual, unmodified top-2 pool**
(not the disputed top-5 one) — no CHRONO fix needed there, since pool
size already equals `MAX_TRADES_PER_DAY`, so there is no "which N of
many" selection ambiguity to begin with:

| | v5.3 unfiltered (spec baseline) | **v5.3 + 5% gap filter** |
|---|---|---|
| Candidates discarded | — | 174 of 1,336 (13.0%) |
| Positions | 725 | 534 |
| Win rate | 35% | **37%** |
| CAGR | 34.26% | 28.68% |
| **Profit factor** | 1.53 | **1.72** |
| **Max drawdown** | -10.23% | **-9.03%** |
| **Sharpe / Sortino / Calmar** | — (not computed for the spec baseline) | **3.26 / 21.15 / 3.17** |

Reference: `scratch_v53_top2_gap5filter.py`; trades in
`result/v53_top2_gap5filter_5year_trades.csv`. Every year 2021-2026 is
individually profitable (PF ≥1.46 every year, peak 2.27 in 2023) — see
conversation history for the full year table.

Win rate, profit factor, and drawdown **all improve simultaneously** —
same "clean win, not a trade-off" pattern as the top-5 dynamic sector
gate (§3), at the cost of ~5.6 CAGR points from the reduced trade
volume (13% of candidates lost, with no backup candidate to fall back
on since the pool is already exactly 2 wide). **This is the safest,
most directly comparable enhancement in this whole document** — it
touches nothing about the disputed pool-size question (§0), reuses
v5.3's exact, already-live-ready entry/exit/EMA-trail logic unmodified,
and only adds one new pre-market eligibility check.

### 8.1 Top-2 vs. top-5: cost of the extra trade volume

For context on what the top-5 pool would cost in transaction fees
*if* its CAGR advantage over top-2 turns out to be real once §0 is
reconciled — comparing the two gap-5%-filtered variants directly
(`v53_top2_gap5filter` vs `v53_top5static_gap5_CHRONO_ematrail`, both
EMA-trail, both 5% gap filter, full 5-year):

| | Top-2 | Top-5 | Extra (top-5 − top-2) |
|---|---|---|---|
| Positions | 534 | 1,340 | +806 |
| Legs (fills) | 750 | 1,844 | +1,094 |
| Total brokerage/transaction cost | ₹442,901 | ₹1,420,290 | **+₹977,390** |
| Cost as % of gross profit | 15.02% | 20.46% | — |
| Sharpe | **3.26** | 2.53 | — |

Top-5 pays roughly ₹195,000/year more in transaction costs (over double
top-2's total) and is proportionally more cost-heavy relative to its
own gross profit (20.46% vs 15.02%) — consistent with §0's concern that
its extra trades are, on average, lower-conviction than top-2's
original picks. Top-2 remains the better risk-adjusted and
lower-friction choice on every measure checked; top-5's only case is
raw absolute CAGR, and that case is exactly the one §0 flags as
unresolved.

## 9. Final no-look-ahead audit (as of this revision)

Every component actually used by §8's recommended addition (v5.3 top-2
+ 5% gap filter) has been individually checked for look-ahead:

1. **Gap filter**: uses only the 09:15 candle's own open price and the
   previous day's official close — both known at 09:15:00, before any
   other check runs. ✅
2. **Day-bias / candidate ranking (v5.3, unchanged)**: NIFTY50 A/D
   ratio and each F&O stock's own `ret_first15`, both resolved from the
   09:25 candle's close (known at 09:30:00) — the top-2 list is fixed
   the moment 09:30:00 arrives, before any signal search begins. ✅
3. **Signal/entry state machine (v5.3, unchanged)**: EMA21/first-candle
   invalidation, gated-2nd-candle trigger-first-then-gate ordering, and
   the no-chain re-signal condition all use only a candle's own already-
   closed facts or an earlier-fixed reference value — this exact rule
   set has now been audited independently by **two** separate projects
   (this session, and nse-momentum-dashboard's v3/v5 specs) and both
   concluded it's genuinely real-time-executable. ✅
4. **Sector gate (static, v5.3)**: computed once at the fixed 09:25
   snapshot, before `entry_cutoff` (11:00) — always known well before
   any candidate's actual trigger. ✅
5. **Slot selection**: not applicable here — top-2 pool size already
   equals `MAX_TRADES_PER_DAY`, so there is no multi-candidate selection
   step for CHRONO to correct. This is precisely *why* §8's addition is
   safe to deploy without further reconciliation: it inherits none of
   §0's open question.
6. **EMA-trail exit (v5.3, unchanged)**: the defer/trail decision at
   the 1:2 touch uses only that same candle's EMA5/EMA10 (known at its
   own close); the fill on a trail-cross is booked at the *next*
   candle's open, never the crossing candle's own close (both audited
   in v5.2's original spec and re-confirmed unchanged here). ✅
7. **Position sizing**: uses only the day's starting capital (known at
   day open) and the entry/stop prices from the already-triggered
   signal. ✅

**The one item this audit does NOT resolve is §0's top-5-pool
discrepancy** — that is a disagreement about which absolute result is
*correct* on an already-look-ahead-corrected rule set, not a newly
discovered look-ahead problem in either project's correction itself. No
further look-ahead issue was found in anything reviewed for this
revision.
