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
A **breakeven stop on the runner half** (§5b) was added in a later
revision and is now part of the adopted configuration.

Reference implementation (the recommended configuration, §5):
`scratch_v53_top5static_gap5_CHRONO_ematrail_be.py` (+ `_be_jun2024.py`)
— EMA-trail **with** the §5b breakeven stop. Its no-breakeven
predecessor `scratch_v53_top5static_gap5_CHRONO_ematrail.py` (+
`_jun2024.py`) remains on disk as the reference for §5's comparison
table. The lower-drawdown alternative (§5a, against-trend + dynamic
sector gate) is `scratch_v53_trendfilter_CHRONO_dynsector_ematrail.py`.
Plain-1:2-exit siblings of both (`..._plain12_gap5_CHRONO.py` and
`..._plain12_trendfilter_CHRONO_dynsector.py`, + their `_jun2024.py`
variants) remain on disk for comparison. The un-filtered look-ahead-
corrected, plain-exit baseline is `scratch_v53_top5static_plain12_
CHRONO.py`. Rejected variants are catalogued in §5d (exit rules) and
§5e (eligibility filters), with their scripts and trade CSVs named
there.

**All numbers below are single runs each**, per the standing "don't
re-run every experiment" preference.

> **READ §5i FIRST** for the currently-adopted configuration
> (45.96% CAGR, -15.84% DD, Calmar 2.90), then §5f for the squareoff
> correction that applies to every number in this document.
>
> **READ §5f.** Every CAGR in this document that is not explicitly
> labelled "(15:10 sq)" was produced by a backtest that squares off at
> the **15:10 candle's CLOSE = 15:15:00 real time**. The live paper-trade
> runner actually fires at **15:10:00**. §5f gives the corrected,
> live-matched numbers, which are **1.5-2.6 CAGR points lower**. Use the
> §5f figures for any expectation-setting; the older ones remain for
> variant-to-variant comparison only.

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

### 0.1 Reconciliation attempt (later revision) — partially resolved

A deliberate reconciliation pass was run. Findings, in order of
confidence:

**Cause (b), day-bias thresholds: ELIMINATED.** The frozen candidate
list's own thresholds were recovered empirically from its `nifty_ratio`
column: **LONG if ratio > 1.53, SHORT if < 0.66**. The dashboard uses
**1.5 / 0.66**. Applying the dashboard's thresholds to the same 869 days
reclassifies exactly **5 days (0.6%)** — a `<=` vs `<` boundary case.
Day-bias is not the explanation. Reference:
scratchpad `bias_threshold.py`.

**Every other rule parameter was verified identical** between this
project's scripts and the dashboard's `intraday_strategy.py`:
`SIGNAL_WINDOW_START` 09:25, `BREAKOUT_WINDOW` 2, `VOL_THRESHOLD_PCT`
0.05, sector gate 2.0/0.5, entry cutoff 11:00, `REWARD_RISK` 2.0,
`LEVERAGE` 5.0, `MAX_RISK_PCT_PER_TRADE` 0.005, `MAX_TRADES_PER_DAY` 2.

**Rule version: CONFIRMED as a major contributor, worth ~7.4 points.**
The dashboard's 17.93% was measured on its **v5** rule set (its own
top-2 baseline: 24.14%), not v5.3's (top-2 baseline: 34.26%). Stripping
v5.3's re-signal out of this project's top-5 pipeline to make it plain
v5 gives:

| | positions | win% | CAGR | PF | Max DD | Sharpe |
|---|---|---|---|---|---|---|
| ours, top-5, **v5.3** rules | 1,386 | 41.3% | **37.88%** | 1.41 | -15.47% | 2.44 |
| ours, top-5, **v5** rules (re-signal off) | 1,306 | 40.4% | **30.52%** | 1.35 | -16.88% | 2.15 |
| dashboard, top-5, v5 rules | ? | ? | **17.93%** | **1.25** | **-19.59%** | ? |

**All three metrics move toward the dashboard's** when the rule version
is matched — CAGR 37.88→30.52 (toward 17.93), PF 1.41→1.35 (toward
1.25), drawdown -15.47→-16.88 (toward -19.59). Same direction on every
axis; this is the same phenomenon, not two unrelated results. Reference:
`scratch_recon_top5_v5rules_noresignal.py`, trades in
`result/recon_top5_v5rules_noresignal_5year_trades.csv`.

**~12.6 CAGR points remain unexplained.**

**The most useful narrowing:** both projects agree *byte-for-byte* on
top-2 v5.3 (725 positions, 34.26%, PF 1.53, DD -10.23%). The shared
signal/entry/exit engine is therefore provably identical, and the
divergence exists **only in top-5** — i.e. entirely inside the
pool-to-slots reduction, which is §0's hypothesis (a). This could not be
tested directly: the dashboard's top-5 scratch scripts no longer exist
on disk (only `intraday_strategy.py`/`intraday_engine.py` remain), so
there is no code to diff against CHRONO.

**Open next step:** port the dashboard's `step_candidates_causal()`
selection into a script *in this project* and run it against this
project's identical frozen candidate list. If it reproduces ~30%, CHRONO
is correct. If it drops toward 18%, CHRONO is too permissive and every
top-5 number in this document needs revising.

**Note:** as of this revision the dashboard's live code is already
running `TOP_N_CANDIDATES = 5` and `GAP_FILTER_PCT = 5.0` — i.e. the
v5.4 pool is live in paper trading.

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

The drawdown cost shown here was later **partly recovered** by the §5b
breakeven stop on the runner half, which takes the primary config to
45.61% CAGR at -16.20% — i.e. better than plain-1:2 on CAGR/Sharpe/
Calmar while closing most of the drawdown gap. §5b has not been tested
on the §5a configuration. Two further exit rules (50% @1:2 + 50% @1:5,
and 50% @1:1.5 + 50% @1:2) were tested on the primary config and
**rejected** — see §5d.

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
| Overnight gap >6% (either direction, at 09:15 open) | causal, checked at 09:15 | 4.0% | 1,362 | 44.71% | 1.46 | -15.82% | âŒ pre-fix |
| **Overnight gap >5% (adopted, §5)** | same | 6.5% | **1,340** | **37.09%** | ~1.42 | **-14.55%** | ✅ |
| Overnight gap >3% | same | 17.6%* | 517* | 21.15%* | 1.29* | -13.75%* | ✅ (*jun2024 window, w/ dynsector) |
| >6% extension by 09:25 close (direction-signed) | causal, checked at 09:30 | 26-58% | 428/276 | 15.36%/9.72% | 1.51/1.54 | -9.08%/-8.52% | âŒ pre-fix |
| 09:25-vs-09:15-high only (no extension cap) | causal, checked at 09:30 | 45% | 361 | 20.04% | **1.79** | -8.50% | âŒ pre-fix, top-2 pool |
| Same-color-only volume (vs. any-color) | signal-rule change, not a filter | n/a (more signals, not fewer) | 1,472 | 33.96% | 1.35 | -10.60% | âŒ pre-fix |
| Against-own-daily-trend (20-day SMA, causal) | causal, checked at prior close | ~53% | 705 | 31.23% | 1.68 | -8.37% | âŒ pre-fix |
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
chronological slot-fill + breakeven stop on the runner half (§5b).**

| | Full 5-year | June 2024 → present |
|---|---|---|
| Positions | 1,340 | 589 |
| Win rate | 39% | 40% |
| **CAGR** | **45.61%** | **43.31%** |
| Final capital (from ₹1,000,000) | ₹6,651,520 | ₹2,271,854 |
| Profit factor | 1.49 | 1.49 |
| Max drawdown | **-16.20%** | **-10.39%** |
| Sharpe / Sortino / Calmar | 2.63 / — / 2.82 | 2.78 / 11.49 / **4.17** |

Reference: `scratch_v53_top5static_gap5_CHRONO_ematrail_be.py` (+
`_be_jun2024.py`); trades in
`result/v53_top5static_gap5_CHRONO_ematrail_be_5year_trades.csv` (+
`_be_jun2024_trades.csv`).

**Without the breakeven stop** (the previously-adopted configuration,
kept for reference — every other rule identical):

| | Full 5-year | June 2024 → present |
|---|---|---|
| Positions | 1,340 | 589 |
| Win rate | 35% | 36% |
| CAGR | 45.04% | 40.74% |
| Profit factor | 1.47 | 1.46 |
| Max drawdown | -16.81% | -12.37% |
| Sharpe / Sortino / Calmar | 2.53 / 12.04 / 2.68 | 2.57 / 11.83 / 3.29 |

Reference: `scratch_v53_top5static_gap5_CHRONO_ematrail.py` (+
`_jun2024.py`); trades in
`result/v53_top5static_gap5_CHRONO_ematrail_5year_trades.csv` (+
`_jun2024_trades.csv`). The June-2024+ window confirms the same pattern
as the 5-year run — CAGR up (36.08%→40.74%) with only a modest drawdown
cost (-10.43%→-12.37%) versus the plain-1:2 exit.

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

### 5b. Breakeven stop on the runner half (adopted)

**Rule:** the moment the first half is actually booked — at the fixed
1:2 target, or via the EMA5/EMA10 trail — the stop protecting the
remaining 50% moves from the original stop up (LONG) / down (SHORT) to
the **entry price**. Nothing else changes.

**Why.** Analysis of the v5.4 book found that of 504 positions that
booked a first half, **181 (36%) then gave the runner back at the
original -1R stop** — a position that had already paid 2R round-tripping
all the way to a full stop-out. That is ₹4.2 lakh of giveback in the
inside-range cohort alone (§5c), and proportionally more across all 504.

**Effect** (5-year / June-2024+):

| | 5-year | Jun-2024+ |
|---|---|---|
| CAGR | 45.04% → **45.61%** | 40.74% → **43.31%** |
| Max drawdown | -16.81% → **-16.20%** | -12.37% → **-10.39%** |
| Sharpe | 2.53 → **2.63** | 2.57 → **2.78** |
| Calmar | 2.68 → **2.82** | 3.29 → **4.17** |
| Profit factor | 1.47 → **1.49** | 1.46 → **1.49** |
| Win rate | 35% → **39%** | 36% → **40%** |

Runner-leg mechanics, 5-year: `323 squareoff | 181 stop` becomes
`257 squareoff | 247 breakeven`. The 181 full givebacks are eliminated,
at the cost of **66 positions that previously rode to a profitable
squareoff now being shaken out at breakeven instead**. Net runner P&L
still improves, ₹8,846,532 → ₹9,024,356.

Note the runner *leg* win rate falls (62.7% → 51.0%) while the
*position* win rate rises (35% → 39%). Both are correct: a breakeven
exit fills at the entry price, so after costs it books a small loss and
counts as a losing leg — but the position as a whole now keeps the first
half's 2R and finishes green instead of being dragged to a wash.

**Honesty about the CAGR gain.** Per calendar year the 5-year run is
**3 better / 3 worse** (2021 +17.78 vs +14.28, 2022 +42.10 vs +44.31,
2023 +25.75 vs +30.92, 2024 +36.75 vs +32.80, 2025 +69.70 vs +60.04,
2026 +36.18 vs +42.12) and the aggregate +0.57-point gain is largely one
year (2025). **Treat the +0.57 CAGR as noise.** The reasons to adopt are
the risk metrics — drawdown improved in 4 of 6 years, and the June-2024+
window improves on *every* metric — plus the fact that there is nothing
here to overfit: the breakeven level is the entry price, not a tuned
parameter.

**Causality.** The stop is tested at the top of the per-candle loop,
before the target/trail block, so the shift can only ever take effect
from the candle *after* the booking. No same-candle knowledge is used.

### 5c. Where the edge actually lives: the 1:2 target vs. the first-15-min range

A position's 1:2 target either sits **inside** the first-15-minute range
(09:15 + 09:20 + 09:25 candles, i.e. 09:15:00-09:30:00) — meaning price
had already traded through that level before the session was 15 minutes
old — or **beyond** it. On the v5.4 book:

| | n | share | W/L | win% | **1:2 hit-rate** | avg P&L | PF |
|---|---|---|---|---|---|---|---|
| target **inside** f15 range | 337 | 25.1% | 129/208 | 38.3% | **44.2%** | **₹6,529** | **1.79** |
| target **beyond** f15 range | 1,003 | 74.9% | 336/667 | 33.5% | 35.4% | ₹3,312 | 1.37 |

25.1% of trades supply **39.8% of net P&L**. By direction, almost all of
the edge is on the short side — SHORT + inside range is the single best
cohort in the strategy (130 trades, 43.8% win, 49.2% 1:2 hit, PF 1.99),
while LONG + inside is barely better than LONG + beyond (34.8% vs
33.6%).

**Critically, the edge is entirely in *reaching* 1:2, not in what
happens afterward.** Once the first half books, the runner leg averages
₹17,664 (inside) vs ₹17,506 (beyond) — identical. That is a more
believable finding than a blanket "these are better trades": it has one
specific mechanism.

This is **not yet implemented as a rule.** It is causal and usable (the
f15 range is fully closed at 09:30, before any entry can fill), and the
two obvious applications are (a) a filter taking only inside-range
targets, or (b) position sizing — full size inside, half size beyond.
Neither has been backtested. Recorded here so the finding is not lost.

**Runner-half economics, for reference** (inside-range positions that
booked at 1:2, 5-year): 149 runner legs, of which 88 reached squareoff
and 61 were stopped. The 88 that got there were **87 wins / 1 loss**,
averaging ₹34,720 (median ₹26,259). This asymmetry — nearly-certain
profit if the runner survives, total giveback if it doesn't — is what
motivated §5b.

Across both cohorts the runner carries **~55% of a booked position's
total profit** (inside: 45.4% first half / 54.6% runner; beyond: 45.1% /
54.9%). This is the single most important structural fact in the
strategy and it is why every rule that caps the runner early destroys
returns — see §5d.

### 5d. Exit variants tested and REJECTED

All on the §5 config (top-5 pool, gap5, static sector gate, CHRONO),
5-year window, so they are directly comparable to the 45.04% / -16.81%
pre-breakeven baseline.

| exit rule | positions | win% | CAGR | max DD | Calmar | verdict |
|---|---|---|---|---|---|---|
| EMA-trail + breakeven (**§5, adopted**) | 1,340 | 39.0% | **45.61%** | -16.20% | 2.82 | adopted |
| EMA-trail, no breakeven | 1,340 | 34.7% | 45.04% | -16.81% | 2.68 | superseded |
| 50% @1:2 + 50% squareoff (plain) | 1,340 | 41.3% | 37.09% | -14.55% | 2.55 | valid, simpler |
| 50% @1:2 + 50% @**1:5** | 1,340 | 41.3% | 29.79% | **-10.55%** | **2.82** | rejected |
| 50% @**1:1.5** + 50% @1:2 | 1,340 | 45.9% | **12.29%** | -11.88% | 1.03 | rejected |
| inside-f15 → EMA20 full-position ride | 1,340 | 36.8% | 42.42% | -16.59% | 2.56 | rejected |
| inside-f15 → EMA21 full exit + 50%@1:2 + conditional EMA10 trail | 1,340 | 39.6% | 38.36% | **-14.71%** | 2.61 | rejected |
| same, **EMA21 removed** (EMA10 trail only) | 1,340 | 39.7% | 40.43% | -15.38% | 2.63 | rejected |

**50% @1:2 + 50% @1:5** — reference
`scratch_v53_top5static_gap5_CHRONO_half12_half15.py`, trades in
`result/v53_top5static_gap5_CHRONO_half12_half15_5year_trades.csv`.
Costs 7.3 CAGR points. 220 of 1,340 positions (16.4%) did reach 1:5 —
42.3% of those that reached 1:2 — so the target fires often enough; it
simply doesn't pay enough when it does. Best single trade collapses from
₹153,216 to ₹63,483: the cap amputates exactly the trend days the
strategy depends on. Runner half earns *more legs for less money*
(407 legs / ₹6,649,010 vs 372 legs / ₹8,700,472). Its one merit is the
best Calmar of any variant (2.82) and the shallowest drawdown (-10.55%)
— **if drawdown control ever becomes the binding constraint, this is the
variant to revisit**, but §5a remains the better lower-drawdown answer.

**50% @1:1.5 + 50% @1:2** — reference
`scratch_v53_top5static_gap5_CHRONO_half115_half12.py`, trades in
`result/v53_top5static_gap5_CHRONO_half115_half12_5year_trades.csv`.
Dominated on every axis; loses to the plain baseline in all six years.
The diagnostic number: **of the 601 positions that reached 1:1.5, 520
(86.5%) went on to reach 1:2 anyway** — so booking early almost never
saves anything, it just takes 1.5R instead of 2R on half the size. Worse,
the whole position is now closed by 2R, so best single trade is ₹15,059
and squareoff exits collapse from 370 to 39. It does produce the
smoothest curve tested (71% of months positive, worst month -5.46%,
longest negative streak 3 months) — but smooth at 12.29% CAGR is not a
trade worth making.

#### 5d.1 The f15-conditional exit family (all rejected)

Three variants built on §5c's finding, each applying a different exit to
the positions whose 1:2 target sits inside the first-15-min range, while
leaving every other position on the baseline exit. **All three lose to
the §5b baseline.** SHORT is fully mirrored in all of them (verified
independently: 335 qualifying positions, 205 LONG / 130 SHORT, with both
new exit types firing on both sides).

**(i) EMA20 full-position ride** — hold the *full* position (no 50%
booking), square everything off on a close through EMA20 against the
trade, original stop live, next-candle-open fill.
**42.42% CAGR, -16.59% DD, Sharpe 2.39, Calmar 2.56** (vs 45.61% /
-16.20% / 2.63 / 2.82). 335 positions took this path, 152 exited on the
EMA20 cross. Only merit: best single trade rises to ₹297,756 (from
₹220,726) — riding full size does pay on the best day, not enough to
cover the rest. Reference
`scratch_v53_top5static_gap5_CHRONO_be_f15ema20.py`.

**(ii) EMA21 full exit + 50%@1:2 + conditional EMA10 trail** — all of:
original stop; a close through EMA21 squares off everything remaining
(live from entry, all day); 50% books at the fixed 1:2 touch; the runner
trails EMA10 but *only while price has not closed beyond the f15 high
(LONG) / low (SHORT)*; the first close beyond that level disables the
EMA10 trail permanently and the runner rides to squareoff.
**38.36% CAGR, -14.71% DD, Calmar 2.61.** Isolating the 335 changed
positions: LONG -₹772,500, SHORT -₹275,206 versus baseline — *both*
directions lose, and note the signature: **win rates rise while P&L
falls** (SHORT win 46.9%→51.5% while earning ₹2.75L less). Classic
cutting-winners-short. Reference
`scratch_v53_top5static_gap5_CHRONO_f15_ema21_ema10.py`.

**(iii) Same as (ii) with EMA21 removed** — isolates the EMA10 trail's
own contribution. **40.43% CAGR, -15.38% DD, Calmar 2.63.**

**Attribution, and a corrected hypothesis.** The working assumption was
that the EMA21 full-position exit was the main cost (it closed 73
positions, 29 of them before the 1:2 target was ever reached). **That
was wrong.** Removing EMA21 recovers only **+2.07 points** (38.36 →
40.43); the conditional EMA10 trail alone still costs **-5.18 points**
(45.61 → 40.43). The real mechanism is visible in the exit counts:

```
baseline:    ema5_trail_exit 502 | target   0
EMA10 only:  ema5_trail_exit 357 | target 152 | ema10_trail_exit 68
```

Instructing "close 50% at 1:2" **replaces the EMA5 defer-at-target** on
those 335 positions. 145 positions that would have deferred booking and
trailed EMA5 now take a fixed 2R instead. That defer is where the large
runs come from — avg win falls ₹32,887→₹27,956, best trade
₹220,726→₹185,378. **The problem is not which EMA trails the runner; it
is capping the first half at all.** Consistent with §5c's finding that
the runner carries ~55% of a booked position's profit.

All three lose 2024 badly (+14.07% / +16.88% vs baseline's +36.75%) —
the strongest trend year, where capped first halves hurt most. Each buys
0.8-1.5 points of drawdown but Calmar stays below baseline in every
case, so none is a risk-adjusted win.

**Not tested:** a variant that *keeps* the EMA5 defer on the qualifying
cohort and adds only the EMA21 protective exit — that would separate
"protective exit" from "cap the first half", and only the latter has
been shown to be expensive.

### 5e. Eligibility filter tested and REJECTED: first-5-min move cap

**Rule tested:** skip a candidate whose 09:15 candle *close* is more
than 7% from the previous day's close (this is gap + the first candle's
own move — a different measurement from the §4 gap filter, which caps
the 09:15 *open* only). Reference
`scratch_v53_top5static_gap5_CHRONO_ematrail_first5cap7.py`.

**Result: null.** CAGR 45.04% → 44.85%, max drawdown -16.81% → -17.39%
(*worse*), Calmar 2.68 → 2.58.

**Why it was tried.** Bucketing the v5.4 book by |09:15 close vs prev
close| showed 5-7% as the *best* bucket in the whole strategy (114
trades, 39.5% win, PF 2.10) and 7%+ as the only *losing* cohort anywhere
(35 trades, 31.4% win, PF 0.93, net -₹20,727).

**Why it failed.** The filter did remove exactly the intended 35
positions (11 wins / 24 losses, net -₹20,727) — but freeing those daily
slots caused CHRONO to backfill **16 replacement positions that went 2
wins / 14 losses**. And the final-capital drop (-₹42,245) exceeded the
direct P&L saved (+₹20,727), because removing trades shifts the whole
equity path and every later position sizes off the changed capital.

**Lesson.** The 7%+ cohort's -₹20,727 is 0.4% of the strategy's ₹5.5M
net P&L — inside the noise floor on a 35-trade sample. This is a second
confirmation of §6: no entry-time feature in this strategy has
predictive power over outcome.

**The useful by-product:** a >5% first-5-min move is a mildly *positive*
condition (149 trades, 37.6% win, PF 1.79, avg ₹5,778 — versus 34.3%,
PF 1.44, ₹3,913 for ≤5%), supplying 15.6% of net P&L from 11.1% of
trades. **This argues against ever tightening the §4 gap filter below
5%.** Also of note: 149 of those 149 moved *with* the trade direction —
only 5 positions in the entire 1,340 entered against their first-5-min
move, and those went 1/4. The strategy is structurally a continuation
play and essentially never fades the open.

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

### 5f. SQUAREOFF TIMING — live/backtest mismatch, and the corrected numbers

**Discovered from actual paper-trade fills.** A live squareoff printed at
`2026-09-23 15:10:00.831627`. The backtest books its squareoff leg at the
**15:10 candle's CLOSE**, which is **15:15:00 real time** (the candle
stamped 15:10 covers 15:10:00-15:14:59). The live runner fires at
**15:10:00** — the candle's *open*. **The backtest was holding five
minutes longer than the runner does.**

This is the same defect the inherited v3 §9 / v5 §0 caveat describes; it
had been documented but never quantified.

**Measured cost** (on the §5b 5-year run, 304 squareoff legs, all matched
to their 15:10 candle): re-pricing every squareoff leg at the candle's
open instead of its close gives **-₹188,030 — -3.33% of net P&L**.
Negative in all six years, worse on SHORT (-₹763/leg vs -₹527 LONG).
Per-share drift over that last 5 minutes: p25 -₹1.20, median ₹0.00,
p75 +₹0.60. Reference: scratchpad `squareoff_timing.py`.

**Decision: the BACKTEST was changed to match the runner**, not the
reverse — squareoff now books at the 15:10 candle's **open**. Scripts
carry the `_sq1510` suffix and write `..._sq1510_..._trades.csv`.

**Corrected, live-matched results** (these supersede every other CAGR in
this document):

| config | pos | win% | final capital | CAGR | PF | Sharpe | Max DD | Calmar |
|---|---|---|---|---|---|---|---|---|
| **top-5 v5.4 +BE (15:10)** | 1,340 | 39.0% | **₹6,077,989** | **43.02%** | 1.47 | 2.55 | -16.47% | 2.61 |
| **top-2 +gap5 (15:10)** | 534 | 37.5% | ₹3,294,510 | **27.08%** | 1.69 | **3.15** | -9.42% | 2.87 |
| **top-2 +gap5 +BE (15:10)** | 534 | **41.9%** | ₹3,204,281 | 26.37% | 1.69 | 3.13 | **-8.98%** | **2.94** |
| top-5 v5.4 +BE (15:15, old) | 1,340 | 39.0% | ₹6,651,520 | 45.61% | 1.49 | 2.63 | -16.20% | 2.82 |
| top-2 +gap5 (15:15, old) | 534 | 37.3% | ₹3,505,453 | 28.68% | 1.72 | 3.26 | -9.03% | 3.17 |
| top-2 +gap5 +BE (15:15, old) | 534 | 41.8% | ₹3,396,937 | 27.87% | 1.72 | 3.24 | -8.90% | 3.13 |

**Cost of the correction:** top-2 +gap5 **-1.60** pts, top-2 +gap5 +BE
**-1.49** pts, top-5 v5.4 +BE **-2.58** pts. Top-5 pays most because it
has more positions reaching squareoff. Drawdown worsens marginally in all
three.

**Per year, 15:10 squareoff:**

| year | top-2 +gap5 | top-2 +gap5 +BE | top-5 v5.4 +BE |
|---|---|---|---|
| 2021 | +8.21% / -4.39% | +9.07% / -4.17% | **+17.54%** / -6.66% |
| 2022 | +34.71% / -9.42% | +34.60% / -8.98% | **+37.14%** / -11.43% |
| 2023 | **+34.15%** / -5.09% | +31.78% / **-4.20%** | +23.72% / -16.47% |
| 2024 | +26.81% / **-3.38%** | +26.43% / -3.37% | **+33.48%** / -6.34% |
| 2025 | +17.52% / -6.27% | +18.54% / -6.27% | **+67.98%** / -10.47% |
| 2026 | +13.05% / -7.40% | +10.51% / -7.09% | **+35.93%** / -8.24% |

**If the runner is ever changed to square off at 15:15:00 instead, the
15:15 rows above become the applicable ones** — it is worth ~+1.5-2.6
CAGR points, and is the single cheapest improvement available.

### 5g. Breakeven on the TOP-2 pool — marginal, and sign-dependent

§5b's breakeven stop was ported to the top-2 pool. Reference
`scratch_v53_top2_gap5filter_be.py` (+ `_sq1510.py`).

| | CAGR | Max DD | Calmar | win% |
|---|---|---|---|---|
| top-2 +gap5, 15:15 | **28.68%** | -9.03% | **3.17** | 37.3% |
| top-2 +gap5 +BE, 15:15 | 27.87% | **-8.90%** | 3.13 | 41.8% |
| top-2 +gap5, 15:10 | **27.08%** | -9.42% | 2.87 | 37.5% |
| top-2 +gap5 +BE, 15:10 | 26.37% | **-8.98%** | **2.94** | **41.9%** |

**The sign flips with the squareoff convention.** At 15:15 breakeven is a
small net negative (Calmar 3.17→3.13). At the live-matched 15:10 it is a
small net *positive* on risk — Calmar 2.87→**2.94**, drawdown
-9.42%→**-8.98%**, win rate 37.5%→**41.9%** — for -0.71 CAGR points.

Either way the effect is far smaller than on top-5 (+0.57 CAGR there).
With only 2 candidates there are fewer runners, so breakeven's
shake-outs more nearly offset the givebacks it prevents. **Not a
decisive lever on this pool; adopt it for the smoother ride or omit it
for the extra 0.71%.**

### 5h. What the top-5 pool actually changes: deployment, not per-trade risk

A common mis-framing (including earlier in this document) is that the
top-5 pool is "riskier". It is not, per day: identical `MAX_TRADES_PER_DAY
= 2`, identical 0.5% risk per trade, identical sizing. It is a wider
*scan* for the same 2 slots — earliest valid signal/breakout wins,
slots freeze at 2. The real difference is **how often the strategy is
deployed at all**:

| | top-2 +gap5 | top-5 v5.4 +BE |
|---|---|---|
| Positions | 534 | 1,340 |
| **Days with a trade** | **423** | **776** |
| Trades per traded day | 1.26 | 1.73 |
| Days filling **both** slots | **111 (26%)** | **564 (73%)** |

**Top-5 trades on every day top-2 does** — zero days where top-2 trades
and top-5 does not — **plus 353 extra days**. Top-2 fills both slots only
26% of the time and finds nothing at all on ~350 otherwise-tradeable
days; it is chronically under-deployed rather than safer per trade.

**Quality of the extra deployment:**

| top-5 positions | n | net P&L | win% | avg |
|---|---|---|---|---|
| on days top-2 also traded | 796 | ₹3,881,250 | 40.8% | ₹4,876 |
| **on days top-2 sat out** | 544 | ₹1,196,739 | 36.4% | **₹2,200** |

The extra days supply **23.6% of top-5's P&L from 40.6% of its
positions** — genuinely thinner edge (half the average, 4.4 points worse
win rate) but **still positive**. They add money at worse efficiency,
they do not lose it.

**And that is exactly where the drawdown difference comes from:**

| | top-2 | top-5 |
|---|---|---|
| Daily P&L std dev | ₹28,809 | **₹43,186** |
| Worst day | -₹35,243 | **-₹59,644** |
| Best day | ₹238,426 | ₹320,574 |
| Negative days | 61% | 58% |
| **Longest losing-day streak** | **8** | **11** |

50% higher daily volatility and a 69% worse single day — because two
positions are held far more often, not because any one position risks
more. Reference: scratchpad `exposure.py`.

**Implication for the choice.** The trade-off is not "safe vs reckless".
It is *selective-but-idle* (top-2: -9.4% DD, but 13.05% in 2026 and a
five-year decay of 34.71→34.15→26.81→17.52→13.05) versus
*fully-deployed-at-a-thinner-edge* (top-5: -16.5% DD, no decay pattern,
strongest in the two most recent years). For a compounding account,
more deployment at a positive edge is usually correct — **but §0's 12.6
unreconciled CAGR points sit precisely inside this pool-to-slots logic**,
so that argument cannot be banked until §0.1's open step is done.

### 5i. ADOPTED (latest revision): one-sided signal gate + EMA10-only trail + entry-candle close rule

Three changes stack on top of §5/§5b/§5f. Reference
`scratch_v53_top5_ema10only_ecclose.py` (+ `_jun2024.py`); trades in
`result/v53_top5static_gap5_CHRONO_ematrail_ema10only_ecclose_5year_trades.csv`.

**Full rule chain:** top-5 pool → 5% overnight-gap filter → static
(09:25) sector gate → **one-sided signal gate (§5i.1)** → lowest-volume
signal candle → gated-2nd-candle entry → 11:00 new-signal cutoff →
CHRONO chronological slot-fill → max 2 trades/day → **EMA10-only trail
(§5i.2)** → breakeven stop on the runner (§5b) → **entry-candle close
rule (§5i.3)** → 15:10 squareoff at the candle OPEN (§5f).

| | Full 5-year | June 2024 → present |
|---|---|---|
| Positions | 1,263 | 552 |
| Trading days | 753 | 320 |
| Win rate | 37.1% | 36.4% |
| Final capital (from ₹1,000,000) | **₹6,734,567** | **₹2,211,397** |
| **CAGR** | **45.96%** | **41.62%** |
| Profit factor | 1.49 | 1.47 |
| Sharpe / Sortino | 2.58 / 12.35 | 2.54 / 11.57 |
| Max drawdown | **-15.84%** | **-9.36%** |
| **Calmar** | **2.90** | **4.44** |
| Expectancy | ₹4,540/trade | ₹2,195/trade |

Exit mix, 5-year: 711 stop / 459 ema10_trail_exit / 297 squareoff / 215
breakeven / 38 entry_candle_close / 2 eod.
Jun-2024+: 310 / 201 / 136 / 85 / 21.

**June-2024+ per year** (this is the window most representative of
current conditions):

| year | trades | win% | return | max DD |
|---|---|---|---|---|
| 2024 (Jun-Dec) | 126 | 31% | **-2.01%** | -7.30% |
| 2025 | 225 | 38% | **+70.00%** | -8.73% |
| 2026 (Jan-Sep) | 201 | 38% | **+32.75%** | -9.36% |

**Monthly, Jun-2024+:** 19 of 28 positive (68%), best +10.35%, worst
-6.02%, median **+4.01%**, longest negative streak **2 months**. The
opening seven months were flat-to-negative (Jun-Dec 2024 ended -2.01%)
before the strategy found its stride — worth knowing before judging a
short live sample.

**Note the Calmar asymmetry: 4.44 on the shorter window vs 2.90 on the
5-year.** That is the usual artifact — a 2.28-year window has fewer
chances to print a deep drawdown (-9.36% vs -15.84%). **Use the 5-year
figures for expectation-setting**; the Jun-2024 numbers are corroboration
that the edge persists recently, not a better estimate.

Against the prior §5f-adopted configuration on the same window
(43.31% CAGR, -10.39% DD, Calmar 4.17), §5i is slightly *lower* on CAGR
and Calmar over Jun-2024+ while being clearly better over 5 years
(45.96% vs 43.02%, Calmar 2.90 vs 2.61). The two windows disagree on the
margin, which is another reason to treat the §5i.1 gate's contribution
as small.

Progression from the previously-adopted §5f figure:

| step | CAGR | Max DD | Calmar |
|---|---|---|---|
| §5f adopted (EMA5/EMA10, 15:10 sq) | 43.02% | -16.47% | 2.61 |
| + one-sided signal gate | 43.50% | -14.66% | 2.97 |
| + EMA10-only trail | 47.60% | -15.76% | 3.02 |
| + entry-candle close rule (**adopted**) | **45.96%** | **-15.84%** | **2.90** |

#### 5i.1 One-sided signal gate

Reference range = the **09:20 and 09:25 candles**:
`ref_high = max(their highs)`, `ref_low = min(their lows)`.

A candle may only become a **signal candle** if its **body** (open and
close; wicks ignored) has not broken the reference level on the trade's
own side:

* **LONG** → `min(open, close) >= ref_low`
* **SHORT** → `max(open, close) <= ref_high`

The opposite side is unconstrained — a LONG signal may sit freely above
`ref_high`. In plain terms: *the pullback must not have cracked the
opening range's floor* (LONG), or *the bounce must not have cleared its
ceiling* (SHORT).

**Effect (before the EMA10 change):** 1,340 → 1,263 positions,
CAGR 43.02% → 43.50%, DD -16.47% → **-14.66%**, Calmar 2.61 → **2.97**,
Sharpe 2.55 → 2.65, expectancy ₹3,790 → **₹4,103**.

**Honesty about the +0.48 CAGR: treat it as noise.** Trade-level
attribution does not explain it — the gate dropped 133 positions worth
-₹133,320 (30.8% win) but CHRONO backfilled 56 worth -₹437,792 (25.0%
win), yet final capital rose ₹103,525. That is a compounding-path
effect. The durable gains are the **drawdown (-1.81 pts), Calmar (+0.36)
and expectancy (+₹313/trade)** — and unlike every other filter tested,
expectancy *rose*, meaning it removes genuinely worse trades rather than
merely reducing exposure.

**Worked example (caught):** GRASIM 2026-08-13 SHORT, signal candle
09:50. `ref_high` 3236.90 (09:20 H 3236.60 / 09:25 H 3236.90);
signal body 3246.10-3249.00. Fails by ₹12.10 — the whole body sits above
the reference high. The stock had closed higher on every candle from
09:30 (3234.50 → 3249.00) and the trade lost ₹24,193 shorting into it.

**Worked example (NOT caught):** COFORGE 2026-07-29 LONG, signal 11:00.
`ref_low` 1713.00; signal body 1737.90-1739.80, passing by ₹24.90. For a
LONG only the downside is tested, so an extended candle near the ceiling
passes freely. The trade still lost. **The gate is one-sided by design
and will not catch extended entries in the trade's own direction.**

**Two-sided variant REJECTED** (body must sit inside *both* bounds):
1,066 positions, CAGR 37.06%, DD -13.11%, Calmar 2.83 — better drawdown
but 6.4 CAGR points worse and lower expectancy (₹3,661). Reference
`scratch_v53_top5_sigrange.py`.

#### 5i.2 EMA10-only trail

EMA5 is removed entirely from the defer decision. At the 1:2 touch,
defer and trail **EMA10** whenever the target sits on the momentum side
of EMA10 (above for LONG, below for SHORT); otherwise book at the fixed
target. Reference `scratch_v53_top5_sigrange1side_ema10only.py`.

**Effect:** CAGR 43.50% → **47.60%**, Calmar 2.97 → **3.02**, PF 1.47 →
1.51, expectancy ₹4,103 → **₹4,848**, avg win ₹32,639 → **₹38,463**,
best trade ₹216,315 → **₹308,606**. Cost: win rate 39.3% → 37.1% and
drawdown -14.66% → -15.76%.

**Mechanism — note this corrects an intuition that proved wrong.** The
expectation was that removing EMA5 would cause *more* positions to
defer. It did the opposite: trail exits fell 478 → 459. Under both rules
**every** target touch already defers (neither run produces a single
`target` exit), so the change is purely *which* EMA is trailed. EMA10
sits further from price, so the trail is **looser**: positions ride
longer, a few more get stopped instead of booking early (stops 738 →
749, breakevens 229 → 215), and the survivors run much further.

Unlike §5i.1's +0.48, the **+4.10 points here is large enough to be a
real effect**, and it is corroborated by the trade statistics moving
together — bigger average win, bigger best trade, higher expectancy,
higher PF. A compounding artifact would not produce that pattern.

Per year it wins 5 of 6 (loses only 2022), but **drawdown is worse in 5
of 6** — small each time but systematic, exactly as expected from a
looser trail.

#### 5i.3 Entry-candle close rule — and the intrabar ambiguity it partly resolves

**THE DEFECT.** The exit loop ran `day[day.index > entry_time]`, so the
**entry candle was never tested against the stop**. Measured on the
§5i.2 book: **224 positions (17.7%) had an entry candle that traded
through the stop intrabar** and the backtest held every one of them. A
live runner holds a *resting* stop-loss order — confirmed by paper-trade
fills printing at exactly the stop price (GRASIM stop 3254.33 → exit
3254.33; COFORGE stop 1734.52 → exit 1734.52) — so those would have been
closed.

**THE AMBIGUITY.** OHLC cannot order events inside a candle. For a LONG
triggered by a rise through `entry`:

* if the low printed **before** the trigger → no position existed, no stop;
* if **after** → the stop fired.

A check for the decidable case (entry candle's open already past the
trigger, so the fill precedes any later extreme) found **0 of 224
certain — 100% ambiguous**, LONG 146/146 and SHORT 78/78. There is *not
one* confirmed instance in the whole book. For a lowest-volume pullback
setup, dip-then-reverse is the natural path, which argues the favourable
ordering is common — but that cannot be proven from the frozen cache
(5/10/15-minute and daily only; no minute data).

**THE BOUNDS.** Three runs differing only in this handling:

| | assumption | positions | CAGR | Max DD | Calmar | PF |
|---|---|---|---|---|---|---|
| no fix | favourable ordering 100%, and the 38 provable cases ignored | 1,263 | 47.60% | -15.76% | 3.02 | 1.51 |
| **B — entry-candle close (ADOPTED)** | corrects only the provable cases | 1,263 | **45.96%** | **-15.84%** | **2.90** | 1.49 |
| A — full stopfix (`>=` not `>`) | adverse ordering 100% | 1,263 | **17.41%** | -18.13% | **0.96** | 1.19 |

Reference for A: `scratch_v53_top5_ema10only_stopfix.py`.

**THE ADOPTED RULE (B).** If the **entry candle itself CLOSES beyond the
stop**, square off the **full** position at that candle's **close**:

```python
if entry_time in day.index:
    ec = day.loc[entry_time]
    breached = (ec["close"] <= stop) if direction == LONG else (ec["close"] >= stop)
    if breached:
        return [{"exit_time": entry_time, "exit_price": float(ec["close"]),
                 "reason": "entry_candle_close", "qty_frac": 1.0, "target": target}]
```

Normal intrabar stop rules resume from the next candle.

**Why B and not A.** The 38 cases B acts on are *path-independent*:
whatever happened intrabar, a candle that CLOSED beyond the stop leaves
the position past its stop at that close. No ordering assumption is
needed. A, by contrast, forces -1R on 186 further positions on an
assumption with **zero** confirmed instances — including ICICIPRULI
2026-09-07 (+₹256,606), WIPRO 2026-06-08 (+₹250,611) and ADANIPOWER
2025-01-14 (+₹213,773), each of which opened *between* entry and stop.
That is what costs A its 30 CAGR points.

**ACCEPTED RISK — the loss overshoots the sizing.** Exiting at the close
rather than the stop breaks the guarantee that `qty = risk_budget //
risk_per_share` caps the loss at 0.5% of capital. Measured over the 38:
loss at the stop would be ₹640,900; at the close it is **₹847,221** —
**32.2% more than budgeted**. R-multiples: mean **1.30R**, median 1.19R,
**max 2.40R** (DELHIVERY 2026-06-18: ₹27,737 → ₹66,700). This was raised
explicitly and accepted as a deliberate trade-off; the rule models a
close-based decision, not a resting stop order. **If the live runner
uses a resting SL order (it does), its real fills on these 38 will be
BETTER than this backtest shows** — so B is conservative on price while
remaining optimistic on the 186 ambiguous orderings.

**OPEN ITEM.** Fetching 1-minute candles for the recent subset of the
224 entry candles via the live Kite token would resolve which ordering
dominates. Kite's minute history reaches back only ~60 days, so it
cannot settle the full 5 years, but even 20-30 confirmed cases would
show which bound the truth sits nearer.

### 5j. Further variants tested and REJECTED (latest revision)

All on the §5i stack, 5-year window, so directly comparable to
45.96% / -15.84% / Calmar 2.90.

| variant | positions | CAGR | Max DD | Calmar | verdict |
|---|---|---|---|---|---|
| **3h EMA50 higher-timeframe gate** | 1,083 | **30.45%** | -15.09% | **2.02** | rejected |
| f15 breakout-close filter | 497 | 20.18% | **-9.14%** | 2.21 | rejected |
| f15 breakout-close + two-sided sig gate | **161** | **6.62%** | **-5.00%** | **1.32** | rejected |
| new-signal cutoff 10:30 (vs 11:00) | 1,205 | 39.13% | -14.26% | 2.74 | rejected |
| new-signal cutoff 10:00 (vs 11:00) | 1,041 | 33.06% | -12.51% | 2.64 | rejected |
| top-4 pool (vs top-5) | 1,113 | 33.65% | -14.83% | **2.27** | rejected |
| top-3 pool (vs top-5) | 888 | 32.36% | **-11.06%** | 2.92 | rejected (but see note) |

**3h EMA50 gate** — LONG only if the signal candle's close is above the
50-EMA of 3-hour candles, SHORT only if below. Built cleanly for 210/210
symbols; 3h bars were shifted forward a full 3h before alignment so only
CLOSED bars are ever visible (in practice the previous session's last
bar). Reference `scratch_v53_top5_ema10only_htf50.py`.
**-17.14 CAGR points.** The diagnostic: the 247 positions it discarded
won **45.3%** (vs the book's 37.1%) and averaged **₹6,174** (vs ₹4,848)
— *it removes the strongest cohort in the strategy*, then backfills 67
replacements winning 26.9%. Mechanically consistent: this is a
lowest-volume **pullback** entry, and requiring price on the "right"
side of a slow average strips out exactly the counter-trend snapbacks
the entry logic exists to catch. Same collision as the stacked gates.

**f15 breakout-close filter** — require some candle to have CLOSED
beyond the 09:15-09:30 range, in the trade's direction, **strictly
before** the entry candle. Reference `scratch_v53_top5_f15brkclose.py`.
Rejected: -23.3 CAGR points. It **structurally forbids early entries** —
the f15 range only closes at 09:30, so the earliest qualifying entry is
~09:40, destroying the 09:30/09:35/09:40 cohorts (579 positions, 43% of
the book). Trading days collapse 776 → 383. The 1,028 dropped positions
were net **+₹3,875,049** at avg ₹3,770, *better* than the filter's own
surviving ₹3,072.

> **A methodological warning worth keeping.** The exploratory analysis
> that motivated this filter used `first_breakout_ts <= entry_time`,
> which counts a breakout-close on the **entry candle itself** — whose
> close is unknown when the trigger fires. That leak made the cohort
> look like 45.9% win / PF 1.96. Corrected to strictly-before, it is
> 40.6% / PF 1.58 and loses badly. **Any entry-time condition must be
> re-checked with a strict inequality before being believed.**

**Cutoff tests** — both worse, monotonically (Calmar 2.97 → 2.74 →
2.64). The late signals are good trades: in the 11:00 run, signals after
10:00 are 223 positions worth **+₹773,723**, and signals after 10:30 are
60 positions at a **45.0% win rate** — the highest of any cohort in the
book. Independently corroborated: nse-momentum-dashboard's v5 spec
tested 10:30 on the top-2 pool and also found it clearly worse
(15.20% CAGR), concluding "11:00 remains the correct cutoff". **Second
finding on which the two projects agree.**

**Pool width** — top-5 wins, but note the anomaly: **top-4 is worse than
both top-3 and top-5** on nearly everything (Calmar 2.27; drawdown
-14.83%, *deeper* than top-5's despite 150 fewer trades; a 6-month
negative streak, the longest of any config). A genuine pool-width effect
should degrade monotonically. It does not. **That non-monotonicity is
itself evidence that pool-width differences in this code are
substantially noise and compounding-path artifacts — which bears
directly on §0's unresolved top-5-vs-top-2 dispute.**

*Note on top-3:* 32.36% CAGR at only **-11.06%** drawdown, Calmar 2.92,
worst month **-3.60%** (mildest of any config tested), and ₹5.9 lakh
less in brokerage over 5 years. Not adopted, but it is the sensible
lower-drawdown variant of the §5i stack if -15.84% proves too much.

### 5k. Breakeven and the top-2 pool, restated with the live-matched squareoff

See §5g for the full table. Summary at the 15:10 squareoff: top-2 + gap5
gives **27.08%** CAGR / -9.42% DD / Calmar 2.87, and adding breakeven
gives **26.37%** / **-8.98%** / **Calmar 2.94** with a 41.9% win rate and
a **2-month** longest losing streak — the smoothest ride of anything
tested.

**Top-2's yearly returns are decaying**: +34.71% → +34.15% → +26.81% →
+17.52% → **+13.05%** (2022→2026). Rupee profit held up (₹3.77L → ₹4.67L
→ ₹5.11L → ₹4.53L → ₹3.05L) — the percentage fell because the capital
base compounded 3.2×. The strategy takes ~100 trades a year worth
₹4-5 lakh regardless of account size; at ₹10L that is 40%+, at ₹30L it
is ~13%.

**That is the real argument for the wider pool** (§5h): top-5 finds
~2.5× as many trades to deploy growing capital into — 776 trading days
vs 423, both slots filled 73% of the time vs 26%. 2026 head-to-head at
equal capital: top-2 **+10.51%**, top-5 **+35.93%**.

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
   50% immediately at the fixed target. Either way the remaining 50%
   rides until square-off. **Square-off timing — see §5f:** the live
   runner fires at **15:10:00** and the backtest has been changed to
   match it (booking at the 15:10 candle's *open*). The alternative —
   holding to **15:15:00 real time**, the 15:10 candle's close — is
   worth **+1.5 to +2.6 CAGR points** and remains the single cheapest
   available improvement, but it is *not* what the corrected numbers in
   §5f assume. Whichever is chosen, the runner and the backtest must
   agree.
8b. **Breakeven stop on the runner (§5b — primary/§5 config only; §5a
   keeps the original stop):** the moment step 8's first half is
   *actually booked* — whether at the fixed target or via the EMA trail
   — amend the stop protecting the remaining 50% from the original stop
   to the **entry price**. In a live/paper runner this is a
   modify-order on the resting stop, issued once, immediately after the
   first-half fill confirms. It must not be issued on the mere *touch*
   of the 1:2 level: when the EMA-trail defers booking, the first half
   is still open and the original stop must stay live until the trail
   actually fills. Backtest equivalent: the stop is evaluated at the top
   of each candle loop, before the target/trail block, so the amended
   level can only take effect from the candle after the fill.
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
8. **Breakeven stop on the runner (§5b, added this revision)**: the
   amended stop level is the *entry price* — a value fixed at entry, not
   derived from anything later. The trigger for the amendment is the
   first half's own confirmed fill. In the backtest the stop is
   evaluated at the top of the per-candle loop, strictly before the
   target/trail block that can set `target_taken`, so the breakeven
   level can only ever be active from the candle *after* the booking —
   the booking candle itself is still checked against the original stop.
   No same-candle or forward knowledge. ✅
9. **First-5-min move measurement (§5e, analysis only — not adopted)**:
   the 09:15 candle closes at 09:20, five minutes before the earliest
   signal candle (09:25) and ten before the earliest entry, so it would
   have been causal had it been adopted. It was not. ✅
10. **First-15-min range vs. 1:2 target (§5c)**: the f15 range
   (09:15+09:20+09:25 candles) is fully closed at 09:30:00; the earliest
   possible entry is on the 09:30 candle, so the range is known before
   any fill. Causal. It has since been backtested as an exit-branch
   condition in three variants (§5d.1) — all rejected — so §5c remains a
   *finding*, not an adopted rule. ✅
11. **f15-conditional exit variants (§5d.1, all rejected)**: EMA20/EMA21/
   EMA10 tests all use a candle's own CLOSE and fill at the NEXT
   candle's open, matching the §2.2 trail convention. SHORT mirroring
   verified independently against raw candles (335 qualifying, 205 LONG
   / 130 SHORT, both exit types firing on both sides). ✅
12. **Squareoff timing (§5f)**: the corrected backtest books at the
   15:10 candle's **open** — a price known *at* 15:10:00, the same
   instant the live runner acts. This removes a 5-minute look-*forward*
   the previous convention had relative to the runner (booking at a
   15:15:00 close the runner never waited for). Strictly more
   conservative than before. ✅
13. **Reconciliation run (§0.1)**: `RESIGNAL_ENABLED = False` only
   *removes* a rule; it introduces no new data dependency. ✅

14. **One-sided signal gate (§5i.1)**: both reference candles (09:20,
   09:25) are closed by 09:30, and a signal is only ever evaluated at its
   own close. Causal. ✅
15. **EMA10-only trail (§5i.2)**: unchanged convention -- the defer
   decision uses the touch candle's own EMA10, and a trail-cross fills at
   the NEXT candle's open. ✅
16. **Entry-candle close rule (§5i.3)**: tests only the entry candle's
   own CLOSE, which is known at that candle's close; the exit is booked
   at that same close. No forward knowledge. It is *conservative on
   price* (books at the close rather than the better stop price) and
   makes no intrabar-ordering assumption at all. ✅
17. **3h EMA50 gate (§5j, rejected)**: 3h bars were shifted forward by a
   full bar-width before alignment, so only CLOSED higher-timeframe bars
   were ever visible. Causal -- it was rejected on performance, not
   causality. ✅

**A KNOWN REMAINING OPTIMISM, not a look-ahead:** for the 186 positions
whose entry candle traded through the stop intrabar but did NOT close
beyond it, this document's adopted numbers assume the favourable
intrabar ordering. §5i.3 documents the bound (CAGR 45.96% adopted vs
17.41% under the fully adverse assumption) and confirms **0 of 224 cases
are decidable from OHLC**. This is the single largest unquantified
risk to the absolute figures here.

**One caveat that is NOT a look-ahead issue but does affect
live-vs-backtest agreement:** observed paper-trade entries fill at
tick-level timestamps (e.g. `10:19:02`, `09:47:42`) via the live
engine's `check_tick_trigger()`, whereas this backtest only evaluates
trigger conditions once per closed 5-minute candle. That is a *fill
convention* difference, not a causality defect, and its sign has not
been measured. It is a second, unquantified source of live-vs-backtest
divergence alongside §5f's squareoff gap.

**The one item this audit does NOT resolve is §0's top-5-pool
discrepancy** — that is a disagreement about which absolute result is
*correct* on an already-look-ahead-corrected rule set, not a newly
discovered look-ahead problem in either project's correction itself. No
further look-ahead issue was found in anything reviewed for this
revision.
