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

> **READ §5l FIRST** for the currently-adopted configuration
> (47.79% CAGR, -14.96% DD, Calmar 3.19). §5i is its immediate
> predecessor and is retained for the comparison; §5f covers the
> squareoff correction that applies to every number in this document.
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

### 0.2 Code-level audit of the dashboard runner (2026-10-11) -- ACTION LIST

Prompted by a live discrepancy: the same window (2025-10-10 .. 2026-10-09)
produced **+38.00% on the VPS (dashboard runner, 335 positions, 35.8% win)**
against **+60.00% here (research engine, 485 positions, 35.3% win)**.

Win rates are nearly identical, so the rules are not in dispute -- the
divergence is in HOW MANY signals each pipeline generates per day
(**1.67/day on the VPS vs 2.57/day here**) and WHICH stocks it ranks.

Audit of `nse-momentum-dashboard/intraday_strategy.py` against this engine
found **16 of 18 strategy constants already identical**: `LEVERAGE` 5.0,
`MAX_RISK_PCT_PER_TRADE` 0.005, `TOP_N_CANDIDATES` 5, `VOL_THRESHOLD_PCT`
0.05, `ATR_PCT_BUFFER` 0.05, `BREAKOUT_WINDOW` 2, signal start 09:25,
`NEW_SIGNAL_CUTOFF` 11:00, `SQUAREOFF_TIME` 15:10, `REWARD_RISK` 2.0,
`GAP_FILTER_PCT` 5.0, `TRAIL_EMA_SLOW` 10, sector gate 2.0/0.5, and working
implementations of §5i's one-sided gate, §5m's EMA50 trend filter, §5n's FVG
veto and §5o's 09:45 bias retry. **`MAX_TRADES_PER_DAY` is already 4**, i.e.
the dashboard is at §5p while THIS engine still defaults to 2.

Three divergences remain. They are listed as an action list because fixing
them is what makes the two machines agree.

**ACTION 1 -- NONE. RETRACTED. The dashboard is already correct here.**

This section originally told the dashboard to apply the sector gate BEFORE
ranking, describing it as an adopted §5o change the live runner had missed.
**That was wrong and it is withdrawn.** §5o.5 of this same document tested
exactly that change and REJECTED it: -13 CAGR points on its own (38.90% vs a
51.89% matched control), Calmar 3.51 -> 1.85, and mean R +0.371 -> +0.247, a
33% fall in an account-size-neutral measure. §5o adopted **change 1 only**
(the 09:45 bias retry).

`select_candidates()` ranking the full F&O universe first and applying
`sector_gate_pass()` afterwards is therefore the ADOPTED behaviour, not a bug.
Per §5o.5's mechanism: the gate's "wasted" slots do quality-filtering work, not
just sector-filtering work -- pre-filtering reaches deeper down the return
ranking to find five names in qualifying sectors, and those replacements have
smaller opening moves. **Do not change `select_candidates()`.**

**How the error happened, because it has a second consequence.** Two builders
exist in `D:/.cloude/scratch/` and they differ in exactly this:

| builder | changes | status |
|---|---|---|
| `build_retry_list.py` | change 1 only (09:45 retry) | **ADOPTED -- use this** |
| `build_retry_sector_list.py` | change 1 + change 2 (sector gate first) | REJECTED per §5o.5 |

The adopted 5-year list `result/top5_retry0945_FROZEN.csv` was built with the
correct one, so **every 5-year result in §§5o-5q is unaffected.** They are
distinguishable at a glance: the rejected builder emits an extra `sector`
column, the adopted one does not.

The 2025-10-10..2026-10-09 one-year run quoted at the top of this section
(+60.00%, 485 positions) was built with the WRONG builder and is therefore a
§5o.5-rejected configuration, NOT the adopted baseline. It is retained here only
as the trigger for this audit; **do not treat +60.00% as the baseline for that
window.** §5o.5 predicts the rejected config inflates position count while
degrading per-trade edge, which is consistent with 485 positions against the
dashboard's 335 -- so part of the "gap" was this error, not dashboard drift.

**ACTION 2 -- settle the day-bias threshold.** The dashboard uses
`BIAS_RATIO_LONG_MIN = 1.5`; this engine's list builder uses **1.53**. Both use
0.66 for SHORT. Already flagged in §0.1 and still unresolved. A day whose NIFTY
50 advance/decline ratio lands in [1.50, 1.53) trades LONG on the VPS and is
skipped here. **Pick one value and set it in both places** -- this needs a
decision, not a guess, and whichever is chosen should be recorded here.

**ACTION 3 -- one authoritative copy of the universe and sector files.**
Measured drift between `nse-momentum-dashboard/cache/` (regenerated
2026-10-11) and `intraday-pullback-trading/cache/` (2026-08-21):

| file | divergence |
|---|---|
| `nse_index_constituents.json` | **46 of 64 index membership lists differ** |
| `fno_universe.json` | 213 vs 210 symbols (dashboard-only: ANANDRATHI, ENRIN, UJJIVANSFB) |
| `index_constituents.json` | 2026-09-14 vs the dashboard's 2026-10-11 NIFTY 50 list |

Why this moves stock SELECTION, not just a name or two: a sector passes on its
constituents' advance/decline ratio, so a changed member list changes the
RATIO, which can flip the gate verdict -- and a flipped sector makes **every**
stock in it eligible on one machine and invisible on the other. That is how an
independent day-by-day diff of the two candidate lists found top-5 agreement on
only **57 of 201 days (28%)**, 60% mean overlap, and **12 days with zero
overlap**. Examples of the drift: NIFTY AUTO +HYUNDAI -EXIDEIND; NIFTY
HEALTHCARE +GLAND,KIMS -PPLPHARMA,SYNGENE; NIFTY SERV SECTOR
+ADANIPOWER,FEDERALBNK -GAIL,NAUKRI.

**Kite cannot supply these files.** Kite is a broker API -- instruments,
quotes, orders, candles. Index membership and per-symbol WEIGHTAGE come from
NSE/niftyindices. This repo's `nse_api.fetch_index_constituents` returns symbols
with **no weightage**, and weightage is load-bearing: `sector.primary_sector_map()`
sorts a symbol's memberships by weightage to choose its PRIMARY sector, so
without it every multi-index symbol gets an arbitrary sector. The dashboard repo
does regenerate the weightage-bearing files; it is the only source.

**ACTION 4 -- change this engine's default `MAX_TRADES_PER_DAY` from 2 to 4**
so the default matches the adopted §5p config and the dashboard. Until then
every run here needs an explicit `--max-trades=4`, and omitting it silently
produces §5o.

### 0.3 THE ROOT CAUSE, FOUND (2026-10-12) -- sector-gate data completeness

**This supersedes every cause proposed in §0.2.** All of those were wrong. The
real divergence is a single disagreement about what to do when a sector's
constituent data is incomplete, and it is worth ~50 CAGR points over 5 years.

#### 0.3.1 What was eliminated first

Measured, not assumed. None of these is the cause:

| suspect | verdict |
|---|---|
| trade cap | both 4 |
| day-bias threshold | both 1.53/0.66; §0.1 measured it at 5 days of 869 |
| sector gate ORDERING | both rank-then-filter (§5o.5-correct) |
| position sizing / cost model | byte-identical formulas and constants |
| all 16 tunable parameters | identical (verified against the VPS run's own config panel) |
| universe / sector files | synced; book overlap 95.3%, moved the result the WRONG way |

**The decisive evidence:** on the 277 (date, symbol) trades both engines took,
**entry price matched 277/277 and stop price matched 277/277.** Signal
detection, the breakout trigger and stop placement are provably the same logic.
Quantity matched on only 3/277 -- but that is downstream, because once the two
equity curves separate every later `risk_budget = capital x 0.5%` differs.

#### 0.3.2 The actual gap

| | code | behaviour on incomplete constituent data |
|---|---|---|
| this engine | `sector_ratio_ok()` | **no check** -- compute the A/D ratio from whoever resolved |
| dashboard backtest | `_sector_ratio()` (commit 33dee03) | **any** unresolved constituent -> ratio None -> gate fails |

**Why the dashboard implemented strict: because of §5.1 of this document.**
Their commit cites the NAUKRI 2026-10-09 incident it records. §5.1 described the
SYMPTOM (a ratio scored off 4 of 27 constituents) but never defined the correct
RULE, so the strictest reading was taken. That omission is this document's
fault, not theirs.

**Why strict breaks over a backtest.** `members` is TODAY's index membership.
For a 2022 date that list contains AEQUS (listed 2025-12-10), TMCV (2025-11-12),
NTPCGREEN (2024-11-27), AFCONS (2024-11-04) and ~30 more. `m not in close_0925`
is True for all of them -- **not because data is missing, but because the
company did not exist.** The sector is rejected; when all five candidates'
sectors are rejected the whole day vanishes, and no CHRONO backfill can recover
it. The dashboard reported **787 skipped days ("no day bias / no candidates")**
against this engine's 227 neutral-bias days.

**The context split:** `intraday_engine.py`'s LIVE path has had strict
fail-closed since v2 Spec §10.3 and is **correct there** -- every current index
member exists today, so a missing constituent IS a failure. Porting that same
discipline into the BACKTEST module is where it breaks. **Live: strict is
right. Backtest: strict is wrong.**

#### 0.3.3 Measured, same window (2021-10-10 .. 2026-10-09, identical params)

| config | positions | CAGR |
|---|---|---|
| this engine, no check (control) | 1743 | **61.76%** |
| this engine, `--sector-coverage=listed --sector-min-members=8` | 770 | 15.90% |
| this engine, `--sector-coverage=strict` | 503 | 8.59% |
| **dashboard, strict** | **829** | **~11.8%** |

The control reproduces §5p exactly, so the coverage plumbing is inert when off.
This engine's `strict` lands in the same regime as the dashboard's, confirming
the mechanism beyond doubt.

Coverage is the whole story, and it is era-dependent:

| window | (sector, day) pairs with complete constituent data |
|---|---|
| 2021-08 .. 2023-12 | **33.1%** |
| 2024 onward | 70.6% |
| 2025-10 onward | **97.2%** |

This is why the ONE-YEAR comparison looked so different: at 97.2% coverage
strict costs only 32 positions and IMPROVES net by 1.2pp. Over 5 years it
removes 52% of positions and most of the return.

#### 0.3.4 Neither engine is correct -- the rule that is

- **No check (this engine)** scores ratios off partial samples. That is the
  NAUKRI bug. Inflates results.
- **Strict (dashboard)** rejects a sector because a 2025 IPO has no 2022 data.
  Not a data defect. Deflates results far more severely.

Correct rule, in three parts:

1. a constituent counts toward the denominator **only if its daily series spans
   that date** (it had a prior close, i.e. it existed);
2. **fail closed on unresolved members among those that DID exist** -- that is a
   genuine gap (stale cache, suspension) and is exactly the NAUKRI case;
3. a **floor on surviving constituent count** (`--sector-min-members`), so a
   4-of-27 sample can never pass whatever the tolerance.

**OPEN: the tolerance in part 2 is NOT yet settled.** Strict-among-listed
(run C) reached only 15.90%, because one stock in a 100-member sector that
printed no 09:25 bar still sinks the day. A percentage slack is required and
the threshold must be chosen by measuring where the GATE VERDICT destabilises
(90/100 resolved gives essentially the same ratio; 4/27 does not), not guessed.
**Do not ship a tolerance until that is measured.**

#### 0.3.5 Flags added to this engine

`--sector-coverage=off|strict|listed|pct:N` (default `off` = unchanged) and
`--sector-min-members=N`. Default behaviour is untouched, so every existing
§§5-5q result stands as measured.

#### 0.3.6 Side-finding: T2T/BE symbols silently poison their sectors

`kite_client.instrument_map()` keys strictly on Kite's `tradingsymbol`, but the
NSE constituent files use `cmSymbol`. A stock moved to the Trade-for-Trade (BE)
surveillance series trades as `HFCL-BE`, so a lookup for `HFCL` raises
"Unknown NSE symbol", the symbol never gets candles, and **its sectors are
incomplete on every single day** -- with only a `[warn]` line.

Four found in one session out of ~400 symbols (~1%): **HFCL** (blocked NIFTY
INFRALOG, NIFTY MS IT TELCM, NIFTY MULTI INFRA), **MTARTECH** (NIFTY IND
DEFENCE), **SIGMAADV**, **STLTECH**. Fetching HFCL and MTARTECH alone moved
5-year completeness 49.2% -> 52.6% and unblocked all four permanently-dead
sectors. NSE reshuffles the surveillance list periodically, so the suffix
fallback (`-BE`, `-BZ`, `-SM`, `-ST`, `-IL`) belongs in `instrument_map()`
permanently, not in a scratch script.

#### 0.2.1 Both pipelines fail SILENTLY -- verify, do not assume

Neither run printed anything to indicate its inputs were not what the operator
believed. Two silent failure modes are now confirmed:

- **Unknown CLI flags are discarded.** The engine parses positionals as
  `_pos = [a for a in sys.argv[1:] if not a.startswith("--")]`, so any
  unrecognised `--flag` is dropped with no error. A command written for a newer
  build runs on an older one at DEFAULT settings and still prints a plausible
  result.
- **`load_cached_only` omits symbols with no cached candles** instead of
  raising. Those symbols never become candidates AND never count toward a
  sector's advance/decline ratio, so a thin cache yields a confidently wrong
  gate verdict rather than an error. This is the same failure that let NAUKRI
  through the gate on 2026-10-09 (NIFTY INTERNET scored from 4 constituents).

`universe_fingerprint.py` (this repo) exists to close this. It prints SHA256
hashes of each universe/sector file, the engine's key constants, the full list
of CLI flags that build actually supports, per-symbol candle coverage over a
given window, and a single FINAL FINGERPRINT line. **Run it on both machines
before comparing any two results.** Matching fingerprint -> the inputs and
engine agree. Different -> the section hashes name the input that drifted.

Until Actions 1-4 are done, **cross-machine numbers are not comparable**, and
the gap should not be read as a problem with the strategy. Note also that a
single sector-membership snapshot applied across a multi-year backtest is
point-in-time biased whichever vintage is chosen; what matters is that both
machines use the SAME one and that it is declared.

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

## 5. Recommended configuration (adopted — see §5r for the current one)

> **Current adopted configuration is §5p** (§5o + four trades per day
> instead of two, risk unchanged at 0.5%): **62.68% CAGR / −10.78% max DD
> (quote as ~−14%, see §5p.4) / Calmar 5.81**, adopted for MONTHLY
> STABILITY — monthly Sortino 1.92 → 2.51, longest losing streak 4 months
> → 2, no negative rolling 6-month window. The chain is §5 → §5b → §5i →
> §5l → §5m → §5n → §5o → §5p; the sections below describe the earlier
> states and the reasoning that got there.

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

### 5i. SUPERSEDED BY §5l — one-sided signal gate + EMA10-only trail + entry-candle close rule

> **Superseded.** §5l removes the re-signal rule from this configuration and is
> the current adopted one (47.79% CAGR vs 45.96%). Everything below remains
> accurate and §5l inherits all of it except the re-signal.

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

### 5l. SUPERSEDED BY §5m — re-signal REMOVED, failed candle re-tested as a fresh signal

Supersedes §5i as the adopted configuration. Reference
`scratch_v53_top5_hybrid.py` (+ `_jun2024.py`); trades in
`result/v53_top5static_gap5_CHRONO_ematrail_hybrid_5year_trades.csv`.

**The single change from §5i:** v5.3's **re-signal rule is removed
entirely** — no close-extension test, no volume-vs-signal test, no
`chain_len` cap. In its place, whenever a signal dies (the gated-2nd-
candle gate fails on candle #1, or candle #2 exhausts the window) that
**same candle is immediately re-tested as an ordinary FRESH signal
candle** — day's-lowest volume + required colour + one-sided gate (§5i.1)
+ valid ATR. If it qualifies it becomes the new signal; otherwise nothing
is active and the scan continues.

Everything else is unchanged from §5i: 2-candle breakout window, the
gated-2nd-candle gate, top-5 pool, 5% gap filter, static sector gate,
11:00 cutoff, CHRONO, EMA10-only trail, breakeven, entry-candle close
rule, 15:10 squareoff at the open.

| | Full 5-year | June 2024 → present |
|---|---|---|
| Positions | 1,275 | 555 |
| Win rate | 36.2% | 36.2% |
| Final capital (from ₹1,000,000) | **₹7,170,362** | **₹2,596,127** |
| **CAGR** | **47.79%** | **51.94%** |
| Profit factor | 1.52 | 1.55 |
| Sharpe / Sortino | 2.58 / 12.51 | 2.81 / 13.77 |
| Max drawdown | **-14.96%** | **-12.81%** |
| **Calmar** | **3.19** | **4.05** |
| Per-trade (% of that day's capital) | 0.1630% | 0.1808% |

Exit mix, 5-year: 725 stop / 458 ema10_trail_exit / 294 squareoff /
213 breakeven / 41 entry_candle_close / 2 eod.

#### 5l.1 Why it works — and the clean decomposition

Three runs isolate the two rules independently:

| config | window | re-signal | CAGR | Max DD | Calmar |
|---|---|---|---|---|---|
| §5i (previous adopted) | 2 candles | **yes** | 45.96% | -15.84% | 2.90 |
| **§5l (adopted)** | 2 candles | **no** | **47.79%** | **-14.96%** | **3.19** |
| simplified | **1 candle** | no | 43.78% | -16.28% | 2.69 |

* **Removing the re-signal is worth +1.83 CAGR points** and 0.88 points
  of drawdown. It actively hurt.
* **The second window candle is worth +4.01 points.** Entry-lag counts
  confirm it: §5l takes **127 entries (10%) on candle #2**, the 1-candle
  variant takes none, and that is the entire gap between them.

**Mechanism.** The re-signal rule re-anchored to a candle that had not
proven itself a genuine low-volume pullback — it required only
`volume < the signal candle's volume`, not the day's running minimum. So
it manufactured weaker setups from failing ones. Requiring the failed
candle to qualify as a *proper fresh signal* is a strictly harder test,
and the numbers say the harder test is the right one.

This also removes the runaway-drift failure mode that `chain_len` was
introduced to cap (v5.3's "no-chain" fix, prompted by ASIANPAINT
2021-10-27's 4-hop chain ending on a 122,767-share candle, 3× the day's
running minimum). Under §5l such a chain is structurally impossible: every
signal must independently be a day's-lowest-volume candle.

**Live-trading benefit beyond the numbers.** The state machine loses an
entire rule with four sub-conditions plus its chain counter. Every signal
is now a fresh signal, which makes logs directly readable — the
2026-09-28 NATIONALUM episode, where a re-signal path had to be
reconstructed forensically from partial logs to explain why 09:45 was
skipped, could not arise under this rule.

#### 5l.2 Honest caveats

* **Consistency regresses.** Positive months fall to **38/62 (61%)** from
  §5i's 42/62 (68%), and the worst month deepens to **-7.20%** from
  -5.97%. Median month is better (+2.82% vs +2.75%), so the distribution
  widens rather than shifts.
* **2023 and 2024 are meaningfully worse** (+21.74% vs +28.96%, +22.48%
  vs +28.58%). The gain is concentrated in **2025 (+93.07% vs +71.46%)**
  and **2026 (+39.23% vs +33.41%)** — the two most recent years.
* **+1.83 CAGR is within the compounding-path noise** repeatedly observed
  in this document. Unlike §5i.1's +0.48 however, it is corroborated by
  PF (1.49→1.52), drawdown (-15.84%→-14.96%), Calmar (2.90→3.19) and
  per-trade return (0.1590%→0.1630%) all moving the same way — a path
  artifact does not produce that pattern.
* **Per-year drawdown is worse in 2024** (-12.53% vs -7.00%) despite the
  better 5-year figure.

**Per year:**

| year | §5l (adopted) | §5i (previous) |
|---|---|---|
| 2021 | +18.40% / -6.83% | **+19.33%** / **-5.89%** |
| 2022 | **+51.09%** / -10.92% | +48.81% / **-10.46%** |
| 2023 | +21.74% / **-14.96%** | **+28.96%** / -15.84% |
| 2024 | +22.48% / -12.53% | **+28.58%** / **-7.00%** |
| 2025 | **+93.07%** / **-8.26%** | +71.46% / -8.67% |
| 2026 | **+39.23%** / -10.37% | +33.41% / **-9.33%** |

**Deepest drawdowns:** -14.96% (2023-04-18 → 2023-11-24, trough
2023-10-26, 220 days); -12.53% (2024-07-24 → 2025-02-13, 204 days);
-10.92% (2022-08-01 → 2022-10-20, 80 days).

#### 5l.3 Variant also tested and REJECTED: 1-candle window

Dropping the breakout window to a single candle *as well* as removing the
re-signal — reference `scratch_v53_top5_simple.py`. **43.78% CAGR,
-16.28% DD, Calmar 2.69**, worse than both §5i and §5l. Confirms the
gated-2nd-candle window is load-bearing and should be kept.

**The §0 and §9 caveats continue to apply unchanged** — this is still a
top-5 result, and it still assumes the favourable intrabar ordering on the
positions whose entry candle dipped through the stop and recovered.

#### 5l.4 June-2024+ window — and a Calmar reversal worth noting

| | §5l (adopted) | §5i (previous) |
|---|---|---|
| Positions | 555 | 552 |
| Win rate | 36.2% | 36.4% |
| Final capital | **₹2,596,127** | ₹2,211,397 |
| **CAGR** | **51.94%** | 41.62% |
| Profit factor | **1.55** | 1.47 |
| Sharpe / Sortino | **2.81 / 13.77** | 2.54 / 11.57 |
| Max drawdown | -12.81% | **-9.36%** |
| **Calmar** | 4.05 | **4.44** |
| Per-trade | **0.1808%** | 0.1515% |

**+10.32 CAGR points on the recent window — but note the reversal: on
Jun-2024+ the adopted §5l has a WORSE drawdown (-12.81% vs -9.36%) and a
LOWER Calmar (4.05 vs 4.44), the opposite of the 5-year result** (where
§5l wins both). The two windows disagree on the risk side.

Reading them together: §5l's advantage is unambiguous on **return**
(CAGR, PF, Sharpe, Sortino and per-trade are all better in *both*
windows) but **ambiguous on risk** — better drawdown over 5 years, worse
over the recent 2.3. Treat the drawdown improvement as unproven.

Per year, Jun-2024+:

| year | trades | win% | return | max DD |
|---|---|---|---|---|
| 2024 (Jun-Dec) | 127 | 29% | **-2.35%** | -12.81% |
| 2025 | 224 | 40% | **+91.78%** | -8.33% |
| 2026 (Jan-Sep) | 204 | 36% | **+38.63%** | -10.47% |

(§5i on the same window: -2.01%, +70.00%, +32.75%.)

**Monthly, Jun-2024+:** 18 of 28 positive (64%), best +14.76%, worst
-6.00%, median **+4.06%**, longest negative streak **2 months**. As with
§5i, the opening seven months were flat-to-negative (Jun-Dec 2024 ended
-2.35%) before the strategy found its stride.

Exit mix: 311 stop / 204 ema10_trail_exit / 135 squareoff / 87 breakeven
/ 22 entry_candle_close.

**The same Calmar-window artifact applies as in §5i:** 4.05 on 2.28 years
versus 3.19 on 5.04 years is mostly the shorter window having fewer
chances to print a deep drawdown. **Use the 5-year figures for
expectation-setting.**

### 5m. SUPERSEDED BY §5n — EMA50 trend filter on the signal candle

Supersedes §5l as the adopted configuration. Same reference engine
`scratch_v53_top5_hybrid.py`; trades in
`result/v53_top5static_gap5_CHRONO_ematrail_hybrid_5year_trades.csv`.

**The single change from §5l:** the signal candle must now **close on the
trend side of EMA50** (5-min, continuous, `span=50, min_periods=50`):

```
LONG  -> signal candle close > EMA50
SHORT -> signal candle close < EMA50
```

A symbol with fewer than 50 candles of history REJECTS the signal rather
than passing it, so thin data cannot bypass the filter. Everything else
— the day's-lowest-volume signal rule, the one-sided body gate, the
gated-2nd-candle window, the fresh-signal re-test, the ATR×5% trigger and
stop, the half-at-1:2 + breakeven + EMA10-trail exit, the 15:10 squareoff,
the 5% gap filter, the sector gate and CHRONO slot filling — is unchanged.

Toggles: `--trend-filter=off` disables it, `--ema-span=N` changes the period.

**5-year result (2021-08-30 → 2026-09-15, frozen list and cache):**

| config | positions | win% | final capital | CAGR | PF | max DD | Calmar | expectancy |
|---|---|---|---|---|---|---|---|---|
| §5l (no trend filter) | 1275 | 36.2 | 7,170,362 | 47.79% | 1.52 | −14.96% | 3.19 | ₹4,839 |
| **§5m (EMA50)** | **1269** | **36.3** | **7,438,678** | **48.87%** | **1.53** | **−14.62%** | **3.34** | **₹5,074** |
| EMA200 instead of EMA50 | 1229 | 35.3 | 6,448,060 | 44.71% | 1.53 | −16.87% | 2.65 | ₹4,433 |

Every metric improves together — CAGR +1.08, max DD −0.34, Calmar
3.19 → 3.34 — and both directions benefit (LONG +₹133,045,
SHORT +₹135,271). **EMA200 was tested first and REJECTED**: it skipped
669 signals, cost 3.1 CAGR points and worsened drawdown by 1.9 points.
A 200-period average spans ~2.5 sessions and often contradicts the day's
move, which is the opposite of what this strategy trades.

**Year by year — EMA50 never underperforms the no-filter version:**

| year | §5l baseline | §5m EMA50 | EMA200 (rejected) |
|---|---|---|---|
| 2021 | +18.40% | +18.40% (identical) | +12.81% |
| 2022 | +51.09% | **+53.37%** | +45.73% |
| 2023 | +21.74% | **+23.01%** | +12.79% |
| 2024 | +22.48% | **+22.56%** | +21.36% |
| 2025 | +93.07% | **+93.13%** | +96.69% |
| 2026 | +39.23% | **+40.69%** | +45.69% |

This six-year consistency is why it was adopted. It is the test the
ATR risk-cap and the EMA200 filter both failed — both flipped sign
between 2021-24 and 2025-26.

**⚠ EFFECT SIZE — read this before trusting the +1.08 points.** The filter
is a very light touch: **69 signals skipped** out of ~5,900 evaluated, and
only **6 fewer positions** (1275 → 1269). The entire gain therefore rests
on six trades, which is too few to separate a real edge from variance.
It was adopted because it is directionally positive, never negative in any
year, and costs almost nothing — not because 1.08 points is established.
**Re-check it as more data accumulates rather than treating the gain as
settled.**

Two follow-ups were measured and are worth recording:

- **A stricter "whole candle beyond EMA50" test is not a useful stress
  test.** Requiring the far extreme (LONG: `low > EMA50`) rather than the
  close removes only **8 of the 1269** adopted trades (0.6%, worth
  +₹26,577). Signal candles are the day's lowest-volume candles and are
  therefore narrow, so they rarely straddle EMA50 — close and extreme are
  nearly the same test here.
- **To stress-test the filter properly, use a distance buffer**
  (`close > EMA50 × (1 + x)`) rather than changing which price is
  compared. That scales the rejection count and tests whether *how far*
  beyond EMA50 matters, not merely which side. NOT yet run.

**Why a trend filter helps so little either way:** v5.4 already has two
faster day-killers doing this job — a close through EMA21 ends the day,
and so does a close through the 09:15 candle's extreme. Both react sooner
than any 50- or 200-period average, so by the time a longer EMA disagrees
the position is usually already closed. EMA50 adds value only in the
narrow set of cases the EMA21 latch has not yet caught.

### 5n. SUPERSEDED BY §5o — fair-value-gap zone filter on the signal candle

Supersedes §5m as the adopted configuration. Same reference engine
`scratch_v53_top5_hybrid.py`; trades in
`result/v53_top5static_gap5_CHRONO_ematrail_hybrid_5year_trades.csv`.

**The single change from §5m:** a signal candle is **vetoed when its CLOSE
falls inside the fair value gap** left between the day's 1st and 3rd candles.

```
bullish gap   high(09:15) <  low(09:25)   ->  zone [ high(09:15) , low(09:25) ]
bearish gap   low (09:15) >  high(09:25)  ->  zone [ high(09:25) , low(09:15) ]
no gap        the two candles overlap     ->  filter inert for that day

veto when     fvg_lo <= signal candle close <= fvg_hi
```

Bounds are INCLUSIVE, so a close landing exactly on a zone edge counts as
inside (2 of 689 vetoes). Everything else — the day's-lowest-volume signal
rule, the one-sided body gate, the §5m EMA50 trend filter, the gated-2nd-
candle window, the ATR×5% trigger and stop, the half-at-1:2 + breakeven +
EMA10-trail exit, the 15:10 squareoff, the 5% gap filter, the sector gate
and CHRONO slot filling — is unchanged. `--fvg=off` disables it.

**5-year result (2021-08-30 to 2026-09-15, frozen list and cache):**

| config | positions | win% | final capital | CAGR | PF | max DD | Calmar | expectancy |
|---|---|---|---|---|---|---|---|---|
| §5m (no FVG filter) | 1269 | 36.3 | 7,438,678 | 48.87% | 1.53 | -14.62% | 3.34 | Rs.5,074 |
| **§5n (FVG filter)** | **1233** | **36.8** | **8,859,088** | **54.12%** | **1.59** | -14.77% | **3.66** | **Rs.6,374** |

Sharpe 2.62 to 2.83, Sortino 12.74 to 14.23, avg win Rs.40,293 to Rs.46,645.
Both directions improve: LONG expectancy Rs.4,955 to Rs.6,435 (+30%), SHORT
Rs.5,270 to Rs.6,274 (+19%). Drawdown is marginally worse (-0.15) and costs
rise Rs.150,483 on the larger account.

#### 5n.1 Why it works -- the mechanism is NOT the removed trades

**689 signals are vetoed but only 36 positions disappear.** The 689 counts
candle-level veto EVENTS, which fall on **351 distinct candidate-days** -- a
single day can have several candles vetoed in succession, so 689 is not a count
of lost opportunities. The veto acts at
SIGNAL level, and most vetoed signals sat on days where the 2-trade cap would
have discarded something anyway; rejecting them early lets CHRONO take the
next chronological trigger instead. Decomposition of the +Rs.1,420,410:

| | trades | net |
|---|---|---|
| removed by the veto | 75 | **-Rs.312,206** |
| backfilled replacements | 39 | +Rs.133,015 |
| present in BOTH runs | 1194 | Rs.6,750,884 to **Rs.7,726,074** |

The shared 1,194 trades earn Rs.975,190 more purely because the account
compounds from a higher base once the losers are gone. So the gain is mostly
COMPOUNDING, not trade selection.

Exit mix shows better quality rather than more volume -- **fewer** trail and
squareoff legs earning Rs.2.6M more between them:

| reason | legs §5m to §5n | net change |
|---|---|---|
| squareoff | 294 to 288 | **+Rs.1,410,518** |
| ema10_trail_exit | 459 to 453 | **+Rs.1,192,213** |
| stop | 719 to 693 | -Rs.1,115,210 |

The stop bucket worsening on 26 FEWER stops is compounding again: median R is
unchanged at -1.04, so per-trade risk is identical; each 1R loss is simply
more rupees on a bigger account.

**Why the rule makes sense:** the gap band is a price zone that traded only
once. A day's-lowest-volume candle sitting INSIDE it is not exhaustion waiting
to resume -- it is price sagging back into territory it still has to reclaim,
so the breakout trigger fires into supply. Worked example, DELHIVERY
2026-06-18 LONG: bullish zone [457.20, 461.75], signal 09:35 closed 460.75
(inside), entry 09:40 at 461.02, and the entry candle closed 459.00 -- below
the stop -- for a -2.4R `entry_candle_close` loss of Rs.75,217. Vetoing it cut
that day from -Rs.108,836 to -Rs.38,348.

#### 5n.2 Robustness -- what separates it from the rejected max-4 test

Drop-top-N winners and recompute. The edge is flat across every level:

| removed | §5m CAGR / Calmar | §5n CAGR / Calmar | edge |
|---|---|---|---|
| none | 48.87% / 3.34 | 54.12% / 3.66 | +5.25 |
| top 1 | 47.61% / 3.26 | 52.88% / 3.58 | +5.26 |
| top 3 | 45.29% / 3.10 | 50.51% / 3.42 | +5.22 |
| top 5 | 42.92% / 2.94 | 48.14% / 3.26 | +5.23 |
| top 10 | 36.95% / 2.32 | 42.05% / 2.51 | +5.10 |
| top 20 | 23.55% / 0.77 | 28.94% / 1.40 | +5.40 |

Between +5.10 and +5.40 throughout, and at top-20 removal §5n's drawdown is
far better (-20.70% vs -30.46%). Concentration is also slightly improved
(top-20 trades = 66.9% of net vs 70.4%). Compare the max-4-positions/day
test, which showed +3.76 CAGR and a -4.09 drawdown improvement that VANISHED
at drop-top-3 (-10.53% to -14.42%) -- that is why max-4 was not adopted and
this was.

#### 5n.3 Year and month wise

| year | pos | W/L | win% | net P&L | return | yr maxDD | capital end |
|---|---|---|---|---|---|---|---|
| 2021* | 96 | 38/58 | 39.6 | +182,592 | +18.26% | -5.83% | 1,182,592 |
| 2022 | 282 | 105/177 | 37.2 | +713,768 | +60.36% | -11.34% | 1,896,359 |
| 2023 | 221 | 84/137 | 38.0 | +629,968 | +33.22% | -14.77% | 2,526,327 |
| 2024 | 214 | 67/147 | 31.3 | +758,891 | +30.04% | -11.10% | 3,285,218 |
| 2025 | 220 | 84/136 | 38.2 | +2,671,539 | +81.32% | -8.53% | 5,956,758 |
| 2026* | 200 | 76/124 | 38.0 | +2,902,331 | +48.72% | -8.82% | 8,859,088 |

(*) partial years. Every year positive; worst single-year drawdown -14.77%
(2023), which is also the overall maximum. 2024 again earns +30.04% on the
LOWEST win rate (31.3%) -- win size, not hit rate.

Monthly: **62 months, 41 positive (66%)**, best +23.16% (Nov 2023), worst
-6.48% (Mar 2022), mean +3.81%, median +2.73%, std 7.10%, longest losing
streak 4 months (Jun-Sep 2023 -- the same stretch that holds the annual
drawdown).

**WARNING -- READ THE YEAR-BY-YEAR IN RUPEES, NOT PERCENT.** §5n's 2025 shows
+81.32% against §5m's +93.07%, which looks like a loss -- but §5n enters 2025
with a larger account, so the same gain is a smaller fraction. In rupees 2025
is **+Rs.121,947 BETTER**. Per-year rupee differences (§5n minus §5m):
2021 -Rs.1,406 | 2022 +Rs.81,880 | 2023 +Rs.212,092 | 2024 +Rs.254,878 |
2025 +Rs.121,947 | 2026 +Rs.751,019. Five of six years positive, 2021 flat.

#### 5n.4 No-look-ahead audit

The zone is complete at candle 3's close (09:30), which is the SAME instant
the earliest eligible signal (the 09:25 bar, index 2) is evaluated -- the
identical timing convention the candidate ranking already uses (see §9).
Six checks, all clean:

| check | result |
|---|---|
| earliest possible signal | index 2; `SIGNAL_WINDOW_START="09:25"` excludes candles 0 and 1 |
| vetoed signals at index < 2 | **0** |
| zone reading a candle LATER than the signal | **0** -- code reads only `iloc[0]` and `iloc[2]` |
| entries at or before the 09:25 bar | **0** in both runs; earliest entry 09:30 |
| removed positions re-derived from raw candles | **75 of 75**, 0 unexplained |
| index-2 self-reference | 2 cases, both exact boundary touches |

The 2 index-2 vetoes are candles that closed exactly on their own extreme,
which the inclusive bound counts as inside: NAUKRI 2022-10-10 (bearish zone
[761.50, 763.50], closed 761.50 = its own high) and OIL 2026-01-28 (bullish
zone [475.70, 485.30], closed 485.30 = its own low). Every input comes from
candles 0 and 2, both known at candle 2's close.

#### 5n.5 Honest caveats

- **The effect size was initially mis-estimated by 30x.** The cohort analysis
  found 89 TRADED positions whose signal closed inside the zone, worth
  -Rs.47,301 -- which predicted a negligible change. The filter actually
  vetoes 689 SIGNALS and moves the result by +Rs.1,420,410, because the CHRONO
  backfill dominates. Reason about signal-level filters at signal level, not
  from the traded subset.
- 689 candle-level vetoes across 351 candidate-days is a large intervention
  for a rule found by inspecting cohorts of
  a single 5-year sample. The drop-top-N stability and the six-check causality
  audit are what justify adoption; it has NOT been tested out-of-sample.
- Tightening the bounds to strict inequalities would change 2 of 689 vetoes --
  immaterial, but recorded deliberately.

### 5o. SUPERSEDED BY §5p — second nifty-bias attempt at 09:45

Supersedes §5n as the adopted configuration. Same reference engine
`scratch_v53_top5_hybrid.py`; the change is to the CANDIDATE LIST, not the
signal logic.

**The single change from §5n:** when the day's NIFTY 50 breadth check lands
in the neutral gap, try once more 15 minutes later instead of discarding the
day.

```
primary check   breadth from the close AT 09:30 (the 09:25-labelled candle)
                ratio >= 1.53 -> LONG,  <= 0.66 -> SHORT
retry           if neutral, recheck at the close AT 09:45 (the 09:40 candle)
                same thresholds; if it passes, the day trades
skip            only if BOTH attempts land in the neutral gap
```

A rescued day takes its bias AND its top-5 ranking from the 09:45 close --
not mixed timestamps. The list carries a `checkpoint` column ("09:30" or
"09:45") and the engine reads it: on a 09:45 day **no signal may be
evaluated before the 09:40-labelled candle**, because the shortlist was not
knowable earlier. Without that guard the extra days would be look-ahead.
Builder: `D:\.cloude\scratch\build_retry_list.py`.

**How many days this recovers (2021-08-30 .. 2026-10-07):**

| | days |
|---|---|
| decided at the primary 09:30 check | 875 |
| **rescued by the 09:45 retry** | **158** |
| still neutral after both attempts | 229 |

That is **+18% more trading days**.

#### 5o.1 Measured result

**FROZEN-CACHE RESULT (directly comparable to §5n), 2021-08-30 .. 2026-09-15:**
865 days decided at the primary check, **157 rescued by the 09:45 retry**,
225 still neutral. List: `result/top5_retry0945_FROZEN.csv`; trades:
`result/v53_top5static_gap5_CHRONO_ematrail_hybrid_chg1_frozen_trades.csv`.

| config | positions | win% | net | CAGR | max DD | Calmar | PF | mean R |
|---|---|---|---|---|---|---|---|---|
| §5n (adopted predecessor) | 1233 | 36.8 | +7,859,088 | 54.12% | -14.77% | 3.66 | 1.59 | +0.381 |
| **§5o (09:45 retry)** | **1362** | 36.6 | **+10,171,746** | **61.37%** | -16.64% | **3.69** | 1.55 | **+0.389** |

**+7.25 CAGR points** on the frozen cache, Calmar 3.66 -> 3.69, trading days
740 -> 835. Note §5o's list is a REBUILD, so its primary-day set (865) differs
slightly from the original frozen list's (869) -- a handful of days move on
data-availability edges. The live-cache run below is retained as an
independent confirmation on a longer window.

Also run on the LIVE cache through 2026-10-07 against a MATCHED CONTROL --
same cache, window, engine and 2-trade cap, with the rescued days removed:

| config | positions | win% | net | CAGR | max DD | Calmar | PF | mean R |
|---|---|---|---|---|---|---|---|---|
| matched control (§5n rules) | 1242 | 36.6 | +7,441,323 | 51.89% | -14.77% | 3.51 | 1.54 | +0.371 |
| **§5o (09:45 retry)** | **1378** | 36.5 | **+9,715,761** | **59.16%** | -16.64% | **3.55** | 1.50 | **+0.379** |

**+7.27 CAGR points.** Rs.10L -> Rs.1,07,15,761 (+971.58%) over 5.10 years.

#### 5o.2 Why this is the cleanest change adopted so far -- it is purely additive

```
control primary-day positions 1242    §5o primary-day positions 1242
overlap 1242    control-only 0    §5o-only 0
```

Every pre-existing trade is **identical**. §5o only ADDS 136 positions on the
158 rescued days; it reshuffles nothing, so there is no CHRONO knock-on and
no selection effect to disentangle. Contrast §5n, whose 689 candle-level
vetoes moved 36 positions and reshuffled the rest.

The added trades are better than the ones already there:

| cohort | n | win% | net | mean R |
|---|---|---|---|---|
| retry (09:45) days | 136 | 36.0 | +234,645 | **+0.446** |
| primary (09:30) days | 1242 | 36.6 | +9,481,116 | +0.371 |

**Better in 6 of 6 years IN RUPEES** (read rupees, not percent -- §5o enters
later years with a larger account, so 2023 and 2026 look worse in % while
being better in rupees):

| year | control | §5o | diff |
|---|---|---|---|
| 2021 | +174,758 | +204,102 | +29,344 |
| 2022 | +707,678 | +982,334 | +274,655 |
| 2023 | +620,768 | +699,656 | +78,889 |
| 2024 | +751,172 | +981,764 | +230,592 |
| 2025 | +2,629,282 | +4,046,503 | **+1,417,221** |
| 2026 | +2,557,665 | +2,801,402 | +243,738 |

Robustness -- drop-top-N on mean R, flat or improving:

| removed | §5o mean R | control mean R | edge |
|---|---|---|---|
| none | +0.379 | +0.371 | +0.008 |
| top 3 | +0.329 | +0.323 | +0.006 |
| top 10 | +0.255 | +0.247 | +0.009 |
| top 20 | +0.178 | +0.166 | **+0.012** |

#### 5o.3 Year and month wise

| year | pos | win% | net | return | yr maxDD | capital end | mean R |
|---|---|---|---|---|---|---|---|
| 2021* | 103 | 40.8 | +204,102 | +20.41% | -6.85% | 1,204,102 | +0.368 |
| 2022 | 307 | 38.1 | +982,334 | +81.58% | -11.66% | 2,186,435 | +0.434 |
| 2023 | 239 | 37.2 | +699,656 | +32.00% | -16.64% | 2,886,092 | +0.251 |
| 2024 | 243 | 32.1 | +981,764 | +34.02% | -7.78% | 3,867,856 | +0.267 |
| 2025 | 254 | 37.0 | +4,046,503 | +104.62% | -8.24% | 7,914,359 | **+0.631** |
| 2026* | 232 | 35.8 | +2,801,402 | +35.40% | -9.10% | 10,715,761 | +0.281 |

(*) partial years. Every year positive; worst single-year drawdown -16.64%
(2023), which is also the overall maximum.

Monthly: **63 months, 42 positive (67%)**, best +27.92% (Dec 2022), worst
-7.29% (Mar 2022), mean +4.11%, median +2.76%, std 7.81%, longest losing
streak 4 months (May-Aug 2023 -- the stretch holding the max drawdown).

#### 5o.4 Honest caveats

- **Most of the +Rs.2,274,438 is COMPOUNDING, not trade quality.** The 136
  added positions earn only +Rs.234,645 directly; the remaining ~Rs.2.04M
  comes from the account compounding off a higher base so later trades size
  larger. The direct per-trade edge is **+0.008R**. Real, but the headline
  CAGR gain overstates the mechanism -- the same decomposition as §5n.1.
- **Drawdown is worse: -14.77% -> -16.64%** (+1.87 points), and PF slips
  1.54 -> 1.50. Calmar still improves marginally (3.51 -> 3.55) only because
  CAGR rises more than drawdown.
- **2025 carries a disproportionate share** (+104.62%, mean R +0.631 vs the
  book's +0.379). Jan 2025 alone was +25.67%, and THREE trades in that month
  (BAJFINANCE +28.68R, ADANIPOWER +13.85R, INOXWIND +11.75R) produced
  Rs.848,891 of its Rs.992,742. Tail dependence is unchanged from §5n.
- **Not yet tested out-of-sample.** The frozen cache holds 5-min data back
  to 2015-10-05 but the candidate list only starts 2021-08-30, so roughly
  six years of untouched data remain available for a genuine out-of-sample
  test (subject to historical NIFTY 50 membership being available).

#### 5o.5 REJECTED alongside it: sector gate applied BEFORE ranking

Tested in the same batch and **rejected**. The idea was that all five
selected candidates should already pass the sector gate, rather than ranking
by first-15-min return first and then losing whichever of the five fail.

| config | positions | win% | net | CAGR | max DD | Calmar | mean R |
|---|---|---|---|---|---|---|---|
| matched control | 1242 | 36.6 | +7,441,323 | 51.89% | -14.77% | 3.51 | +0.371 |
| sector-gate-first alone | 1448 | 35.1 | +4,349,018 | **38.90%** | -20.99% | 1.85 | +0.247 |
| sector-gate-first + 09:45 retry | 1656 | 34.8 | +5,792,860 | 45.56% | -23.42% | 1.95 | +0.259 |

**-13 CAGR points on its own**, Calmar nearly halved, and mean R down 33%
(+0.371 -> +0.247) -- an account-size-neutral measure, so this is genuine
per-trade degradation, not a compounding artefact.

**Why it fails:** pre-filtering does not merely protect the five slots, it
FILLS them with deeper-ranked names. Previously a strong mover in a weak
sector was selected and then discarded, leaving 2-3 high-quality candidates
(436 of 1583 signals dropped at the gate). Pre-filtered, almost nothing
fails (74 dropped) because the list has already reached further down the
return ranking to find five names in qualifying sectors -- and those
replacements have smaller opening moves. **The sector gate's "wasted" slots
were doing quality-filtering work, not just sector-filtering work.**

### 5p. SUPERSEDED BY §5r — four trades per day instead of two

Supersedes §5o as the adopted configuration. One parameter changes:

```
MAX_TRADES_PER_DAY   2  ->  4          (--max-trades=4)
MAX_RISK_PCT_PER_TRADE      0.5%       UNCHANGED
```

**Adopted for MONTHLY STABILITY, not for return.** Return is effectively
unchanged; the reason to take it is that it halves the length of losing
stretches and removes the negative half-year. Read §5p.4 before acting on it --
the drawdown headline does NOT survive a robustness check and worst-case daily
risk doubles.

Everything else is untouched: the §5o 09:45 bias retry, the §5n FVG-zone
filter, the §5m EMA50 trend filter, the §5l re-signal rule, the §5i one-sided
gate and entry-candle-close rule, the §5b breakeven stop, the EMA10 trail,
the 15:10 squareoff, the 5% gap filter, the sector gate and CHRONO slot
filling. Reference engine `scratch_v53_top5_hybrid.py` with `--max-trades=4`;
list `result/top5_retry0945_FROZEN.csv`; trades
`result/v53_top5static_gap5_CHRONO_ematrail_hybrid_max4only_trades.csv`.

**5-year result (frozen cache, 2021-08-30 .. 2026-09-15):**

| config | positions | win% | net | CAGR | max DD | Calmar | PF | mean R |
|---|---|---|---|---|---|---|---|---|
| §5o (max 2) | 1362 | 36.6 | +10,171,746 | 61.37% | -16.64% | 3.69 | 1.55 | +0.389 |
| **§5p (max 4)** | **1694** | 36.6 | **+10,636,697** | **62.68%** | **-10.78%** | **5.81** | 1.51 | +0.365 |

#### 5p.1 Why it was adopted -- the stability case

| monthly metric | §5o | §5p | change |
|---|---|---|---|
| mean monthly return | +4.25% | +4.25% | unchanged |
| median monthly return | +2.89% | +2.97% | +0.08 |
| standard deviation | 7.89% | **6.87%** | **-13%** |
| downside deviation | 2.22% | **1.69%** | **-24%** |
| monthly Sharpe | 0.54 | **0.62** | +15% |
| monthly Sortino | 1.92 | **2.51** | **+31%** |
| average winning month | +7.85% | +7.62% | -0.23 |
| average LOSING month | -3.31% | **-2.33%** | **+0.98** |
| months >= +2% | 35 | 35 | unchanged |
| months <= -3% | 10 | **8** | -2 |
| worst month | -7.29% | **-6.33%** | +0.96 |
| **longest losing streak** | **4 months** | **2 months** | **halved** |
| worst rolling 3-month | -10.86% | **-2.82%** | **+8.04** |
| worst rolling 6-month | -6.52% | **+0.68%** | **no losing half-year** |

Positive months fall 42 -> 41, the only metric that worsens.

#### 5p.2 The mechanism -- the "one winner rescues it" bucket

The change is **purely additive**: 332 positions added, **0 removed**. Every
§5o trade is still present and unchanged, so there is no selection effect to
disentangle (contrast §5n, whose 689 vetoes reshuffled the book).

Day-fill distribution across 835 trading days:

| slots filled | §5o | §5p |
|---|---|---|
| 1 | 308 days | 308 days |
| 2 | 527 days | 280 days |
| 3 | - | 162 days |
| 4 | - | 85 days |

So the cap only binds on the 247 days that produced a 3rd or 4th signal.
Win/loss mix on §5p's four-slot days:

| mix | days | net | avg day as % of capital |
|---|---|---|---|
| 4W-0L | 5 | +748,553 | +3.66% |
| 3W-1L | 15 | +2,903,977 | +3.31% |
| 2W-2L | 27 | +1,768,716 | +1.41% |
| 1W-3L | 24 | -216,705 | **-0.21%** |
| 0W-4L | 14 | -1,237,624 | **-2.01%** |

**The 1W-3L row is the whole point.** 24 days lost three of four trades and
still came in at only -0.21%, because one trailed winner covers three 1R
stops. In a 2-slot book those same days register as clean 0W-2L losses at
-0.93%. More slots push days out of the "everything lost" bucket and into the
"one winner rescues it" bucket -- which is why the losing streak halves.

Corroborating: **all-lose days FALL** from 421 of 835 (50.4%) to 386 (46.2%),
and the longest run of consecutive all-lose days shortens from **10 days to
8**. A 4th trade gives each day one more chance to find a winner.

#### 5p.3 Robustness -- what separates it from the rejected max-4 test

Drop-top-N winners and recompute the monthly metrics. Unlike the drawdown
figure (§5p.4), the DISTRIBUTIONAL metrics hold at every level:

| removed | §5o std / dnDev / Sortino | §5p std / dnDev / Sortino |
|---|---|---|
| none | 7.89 / 2.22 / 1.92 | **6.87 / 1.69 / 2.51** |
| top 3 | 7.91 / 2.37 / 1.71 | **7.22 / 2.17 / 1.89** |
| top 10 | 8.41 / 3.03 / 1.18 | **7.39 / 2.57 / 1.43** |
| top 20 | 8.44 / 4.21 / 0.65 | **7.94 / 3.78 / 0.80** |

Lower volatility, lower downside deviation and higher Sortino throughout; the
advantage narrows but never inverts. That is the test that killed the Fib
retracement cut, the VWAP discount gate and the max-4 drawdown claim.

#### 5p.4 Honest caveats -- READ BEFORE LIVE USE

- **The -10.78% max drawdown is NOT robust. Quote it as ~-14%.** Drop the top
  3 winners and it becomes -14.26%; by drop-top-10 the edge over §5o is
  +0.24, i.e. gone. Probe: only **21 of the 332 added positions** fall inside
  the drawdown window, but they average **+1.206R**, and they move the
  drawdown PEAK forward three months (2023-04-12 -> 2023-07-17), shortening
  the measured window from 155 days to 59. Causal, but it rests on a lucky
  cluster. The stability metrics in §5p.3 are what justify adoption, NOT this.
- **Worst-case daily risk DOUBLES.** Theoretical 4 x 0.5% = 2.0% vs §5o's
  1.0%. Measured (position size summed across BOTH exit legs -- see the
  warning below):

  | | §5o | §5p |
  |---|---|---|
  | risk per trade, median | 0.500% | 0.499% |
  | risk per trade, mean | 0.495% | 0.443% |
  | daily risk, median | 0.997% | 0.883% |
  | daily risk, **max** | **1.086%** | **2.017%** |

  Per-trade risk at the MEDIAN is unchanged at ~0.5% -- §5p does NOT run at
  lower per-trade risk. Only the MEAN falls (0.495% -> 0.443%), because the
  four-way allocation split makes the capital cap bind ~40% of the time
  instead of ~5%, and those positions size below the full budget. On the 85
  four-slot days the full 2% is genuinely at risk. 14 of those days lost all
  four; worst was **-2.30% of capital** (2023-08-14). All 14 landed between
  -1.66% and -2.30%, i.e. no blowups.
- **MEASUREMENT WARNING for anyone re-deriving these numbers.** The trades CSV
  has one row PER EXIT LEG, not per position (1070 single-leg and 624 two-leg
  positions in §5p). Position size is the SUM of the leg quantities; using
  `qty.max()` halves it for every two-leg position, which understates risk per
  trade (0.378% instead of 0.499%) and inflates mean R by ~3.7x (+1.344
  instead of +0.365). Always `groupby("position_id")` with qty SUMMED.
- **The added trades are lower quality.** The 332 extra positions average
  **+0.271R against the book's +0.389R**. Portfolio variance improves through
  diversification, not through better selection.
- **Single-sector concentration on four-slot days is unhedged.** 2023-05-19
  filled all four slots with Adani-group names (ADANIENSOL, ADANIGREEN,
  ADANIPOWER, GVT&D) for -1.88%. The sector gate passes one sector, so four
  slots can all land in it. A sharper group-wide move could exceed the 2%
  expectation. NOT yet measured -- how often four slots share a sector is an
  open question.
- Not tested out-of-sample, same as §5o. ~6 years of untouched 5-min data
  (back to 2015-10-05) remain available.

#### 5p.5 Capital sensitivity -- §5p scales down well

Run at three starting capitals. Position count is **1694 in all three** -- no
trade is lost to zero-qty rounding -- and the risk/capital binding ratio is
identical (60.2% / 39.8%), so the structure is scale-invariant.

| capital | CAGR | max DD | Sortino | worst rolling 6-month | cost % of gross | cost in R |
|---|---|---|---|---|---|---|
| Rs.10L | 62.68% | -10.78% | 2.51 | **+0.68%** | 18.69% | 0.0957 |
| Rs. 5L | 61.27% | -10.89% | 2.43 | **+0.27%** | 19.59% | 0.1034 |
| Rs. 2L | 56.73% | -11.26% | 2.16 | **-1.10%** | 22.72% | 0.1228 |

Rs.10L -> Rs.5L costs 1.41 CAGR points; Rs.5L -> Rs.2L costs 4.54 more. The
entire gap is Zerodha's near-fixed per-order charges, which do not shrink with
position size. **Rs.5L keeps essentially the whole benefit. At Rs.2L the
negative half-year returns (-1.10%) and costs cross 22% of gross**, so Rs.2L
is workable but is where the drag becomes material.

#### 5p.6 REJECTED alongside it

| config | positions | CAGR | max DD | Sortino | losing streak | worst roll-6m |
|---|---|---|---|---|---|---|
| §5o: 2 x 0.50% | 1362 | 61.37% | -16.64% | 1.92 | 4 months | -6.52% |
| **§5p: 4 x 0.50%** | **1694** | **62.68%** | -10.78% | **2.51** | **2 months** | **+0.68%** |
| 3 x 0.50% | 1609 | **63.88%** | -15.52% | 2.21 | 4 months | **-7.58%** |
| 4 x 0.25% | 1694 | 32.44% | -6.53% | 2.25 | 2 months | +1.84% |
| 4 x 0.50%, 2 entries/symbol | 2118 | 68.86% | -21.10% | - | - | - |

- **max 3 REJECTED.** Highest CAGR of any configuration (63.88%) but it does
  NOT deliver stability: losing streak stays at 4 months, worst rolling
  6-month is **-7.58% (the worst of all four)**, and worst single month is
  -8.55%. The stability gain is specifically the **4th slot** -- the 85
  busiest days -- not slot-widening in general.
- **4 x 0.25% REJECTED.** Lowest absolute volatility, but every RATIO is worse
  than 4 x 0.50% (Sortino 2.25 vs 2.51, Calmar 4.97 vs 5.81) and it costs 30
  CAGR points. It wins no more often (41 positive months, identical) -- it
  wins SMALLER: months >= +2% collapse 35 -> 28, months >= +5% 24 -> 13, while
  cost share rises to 21.2%. Halving per-trade risk is equivalent to trading a
  smaller account; it does not change the shape of the distribution.
- **2 entries per symbol REJECTED.** +7.5 CAGR but mean R is BELOW §5o at
  every drop-top-N level, drawdown worsens to -21.10%, and the re-entry trades
  earn **+0.203R against first entries' +0.353R**. Of 478 second entries, 325
  opened while trade #1 was still open and only 138 after a stop. Flag
  retained as `--per-symbol=N`.

### 5q. Signal-candle filters tested on top of §5p and REJECTED

Two further entry filters were proposed and tested against the adopted §5p
configuration (top-5 pool, 09:45 bias retry, max 4 trades/day, 1 per symbol,
₹10L, 5x, 0.5% risk/trade, frozen cache, 2021-08-30 -> 2026-10-09).
**Neither was adopted. Nothing in §§5-5p changes.**

Reference baseline: **1694 positions, 36.6% win, +₹10,636,697, CAGR 62.68%,
max DD -10.78%, mean R +0.365, Sortino 2.51, losing streak 2 months,
worst rolling 6-month +0.68%.**

#### 5q.1 09:25 candle volume > 1.5x SMA50(volume) -- REJECTED

The 09:25 bar's volume must exceed 1.5x the mean of the 50 preceding 5-minute
bars (a continuous series, so it reaches into the prior session). Measured
independently of the engine, this fails on only **640 of 5110 candidate 09:25
bars (12.5%)**. Two penalties were tested:

| mode | on failure | positions | win | net | CAGR | max DD | mean R |
|---|---|---|---|---|---|---|---|
| (baseline) | - | 1694 | 36.6% | +10,636,697 | **62.68%** | -10.78% | +0.365 |
| `candidate` | whole symbol-day discarded | 1549 | 37.1% | +9,651,179 | 59.85% | -10.26% | **+0.393** |
| `signal` | only the 09:25 bar barred | 1675 | 36.7% | +10,439,447 | 62.13% | -10.92% | +0.367 |

- **`candidate` mode is a DAY filter** -- the stock leaves the top-5 pool, so a
  different stock takes the slot (4 positions appear that the baseline never
  had). It drops 149 positions that were genuinely marginal (31.5% win, mean R
  +0.112) and per-trade quality improves at every drop-top-N depth
  (+0.365 -> +0.393 at N=0, +0.183 -> +0.199 at N=20). Monthly metrics are
  mildly better: Sortino 2.68, worst month -5.08%, worst rolling 3-month
  -1.94%, 42 positive months, 37 months >= +2%.
  **Rejected anyway:** it costs 2.8 CAGR points and ₹986k, and it is worse in
  **4 of 6 years**. The per-trade gain is real but small and does not reach the
  monthly distribution.
- **`signal` mode is a CANDLE filter and is effectively a no-op.** It bars 461
  of the 09:25 bars but only **28 positions change** (9 appear). Once a
  symbol-day has passed the gap filter, the §5m EMA50 trend filter and the
  §5n FVG gate, the 09:25 bar is rarely the surviving signal candle -- barring
  it simply defers the same stock to a later candle.
- **Reading note:** "SMA 50 volume" was implemented as 50 preceding 5-MINUTE
  bars. The alternative reading -- the mean of the last 50 DAYS' 09:25 bars --
  is a different gate and was NOT tested.

#### 5q.2 Signal candle must pierce EMA5 -- REJECTED, and it damages stability

Rule tested: every signal candle after the 09:25 bar must have pierced the
5-minute EMA5 (LONG: `low < EMA5`; SHORT: `high > EMA5`), the 09:25 bar exempt.
Five variants, including a tolerance that loosens the test and a window that
lets the gated-2nd-candle ("sub-signal") supply the pierce instead:

| variant | positions | CAGR | max DD | mean R | Sortino | streak | roll-6m |
|---|---|---|---|---|---|---|---|
| (baseline, no gate) | 1694 | **62.68%** | **-10.78%** | **+0.365** | **2.51** | **2** | **+0.68%** |
| strict | 1205 | 32.04% | -12.92% | +0.334 | 1.42 | 4 | -3.35% |
| window=1 only | 1219 | 31.04% | -13.05% | +0.321 | 1.40 | 4 | -2.15% |
| tol ₹0.05 (1 tick) + w1 | 1264 | 35.08% | - | - | - | - | - |
| tol 0.05% | 1325 | 37.96% | -13.41% | +0.346 | 1.62 | 4 | -3.04% |
| tol 0.05% + window=1 | 1343 | 37.91% | -13.88% | +0.339 | 1.67 | 4 | -0.84% |

- **Worse in all 6 years, in all 5 variants.** No subperiod where it helps.
- **It is an ANTI-stability filter, and the reason is precise.** Std dev of
  monthly returns falls 6.87% -> ~5.3-5.7%, which looks like stabilisation, but
  **downside deviation is UNCHANGED (1.69% -> 1.67-1.73%)**. All the removed
  volatility comes out of the UPSIDE. Hence Sortino collapses 2.51 -> 1.4-1.7
  while Sharpe barely moves (0.62 -> 0.45-0.49). Every §5p property breaks:
  losing streak 2 -> 4 months, worst rolling 3-month roughly triples
  (-2.82% -> -7.9/-9.3%), worst rolling 6-month turns negative in every
  variant, and **max drawdown gets WORSE** (-10.78% -> -12.9/-13.9%) despite
  trading 350-490 fewer positions.
- **Why it is structurally wrong for this strategy.** The v5.4 signal candle is
  the day's LOWEST-VOLUME bar inside a narrow one-sided range -- a
  volume-contraction signal. Requiring it to also stab through EMA5 selects for
  candles that have already travelled away from the mean, which is a different
  and worse setup. Loosening the tolerance only helps by making the gate
  progressively inert: CAGR is strictly monotone in the slack given
  (0 -> 32.04%, ₹0.05 -> 35.08%, 0.05% -> 37.96%, gate absent -> 62.68%),
  with no interior optimum found.
- **The deferred-pierce window is dead weight at every setting** (-1.0 CAGR
  points alone, -0.05 on top of the tolerance). The counters show why: with
  window=1 it rescued 1458 signals where the sub-signal candle supplied the
  pierce, but 1136 setups were SPENT -- the breakout fired before any pierce
  was established, killing a signal that the strict rule would have rejected
  early enough to be re-tested as a fresh one. Roughly a wash.
- Flags retained but default OFF: `--ema5-pierce=on`, `--ema5-pierce-tol=N`
  (percent), `--ema5-pierce-abs=N` (rupees), `--ema5-pierce-window=N`.

#### 5q.3 ENGINE BUG found during §5q and fixed -- invalidates no adopted result

`find_entry` takes 11 parameters; the call site passed only **9 positional
arguments**, so `volsma` and `ema5` never reached it and defaulted to `None`.
Both gates treat a missing lookup as a rejection, so the first run of each was
silently testing something else entirely:

- `--v0925=signal` did not test volume at all -- it **unconditionally barred the
  09:25 bar**. Confirmed by that run having ZERO entries at 09:30 (baseline: 167).
- `--ema5-pierce=on` did not test EMA5 at all -- it **unconditionally rejected
  every post-09:25 signal candle**. Confirmed by all 183 of its positions
  entering at 09:30/09:35, i.e. exactly the 09:25-signal entries the rule exempts.

**No adopted result is affected.** Both flags default off, and `volsma_cache` /
`ema5_cache` are consumed only by these two test gates -- §5o and §5p never
touch that path. `--v0925=candidate` was also unaffected, because it reads
`volsma_cache` directly at module level rather than through `find_entry`.
Fixed by passing both caches at the call site.

**Useful by-product.** The two broken runs happen to implement a clean
partition of the signal space, and the numbers are worth keeping:

| what was actually tested | positions | win | CAGR |
|---|---|---|---|
| 09:25 signal candle ONLY | 183 | 31.7% | **1.95%** |
| 09:25 signal candle EXCLUDED | 1602 | 37.0% | **61.15%** |

The opening bar contributes almost nothing; essentially all of the edge lives
in the 09:30-11:00 signals. This was not a designed experiment and should be
re-run deliberately before being relied on.

### 5r. ADOPTED (latest revision): signal-candle volume tolerance 5% -> 10%

Supersedes §5p as the adopted configuration. **One constant changes:**

```
VOL_THRESHOLD_PCT   0.05  ->  0.10        (--vol-tol=0.10)
MAX_TRADES_PER_DAY        4               UNCHANGED (see 5r.4 -- REQUIRED)
```

The signal candle qualifies as the day's lowest-volume bar when
`volume <= running_min * (1 + VOL_THRESHOLD_PCT)`. Widening that tolerance from
5% to 10% ADMITS MORE SIGNALS -- it is a loosening, not a filter. Everything
else is untouched: §5p's 4 slots, §5o's 09:45 bias retry, §5n's FVG veto,
§5m's EMA50 trend filter, §5l, §5i, §5b's breakeven stop, the EMA10 trail, the
15:10 squareoff, the 5% gap filter, the sector gate and CHRONO slot filling.

Reference run: engine `scratch_v53_top5_hybrid.py --cache-dir=cache
--max-trades=4 --vol-tol=0.10`, list `result/top5_5y_CLEAN.csv`, trades
`result/v53_top5static_gap5_CHRONO_ematrail_hybrid_vt10_trades.csv`.
Inputs fingerprint `b3803a8af41c4edf` (see `universe_fingerprint.py`).

#### 5r.1 Measured, 2021-08-30 .. 2026-10-09, Rs.10L, 5x, 0.5%/trade

| metric | §5p (5%) | **§5r (10%)** |
|---|---|---|
| positions | 1743 | **1811** |
| win rate | 36.72% | **37.38%** |
| mean R | +0.360 | **+0.369** |
| mean R, drop-top-20 | +0.183 | **+0.204** |
| mean R, drop-top-50 | +0.044 | **+0.071** |
| CAGR | 61.76% | **69.17%** |
| max drawdown | -11.42% | **-9.87%** |
| costs as % of gross | 19.5% | **18.5%** |
| months >= +2% | 34/63 | **36/63** |
| NEGATIVE months | 23 | **20** |
| monthly win rate | 63.5% | **68.3%** |
| downside deviation | 1.82% | **1.63%** |
| monthly mean/std | 0.595 | **0.685** |
| Sortino | 2.31 | **2.80** |
| losing streak | 2 months | **2 months** |
| worst rolling 6-month | -0.36% | **+3.99%** |

Year by year: **+19.33 / +84.42 / +38.98 / +43.67 / +122.04 / +50.37** --
better than §5p in **5 of 6 years** (only 2021, a 4-month stub, is worse).

#### 5r.2 Why this was adopted when eleven other variants were not

**It is the only change in the session that PASSED a paired bootstrap.**
10,000 resamples of the same 63 months, candidate vs §5p:

| metric | difference | 95% CI | wins | p |
|---|---|---|---|---|
| monthly mean/std | +0.092 | **[+0.035, +0.161]** | 100.0% | **0.000** |
| Sortino | +0.539 | [-0.055, +1.556] | 96.0% | **0.039** |
| negative months | -2.99 | [-7.0, 0.0] | 95.4% | **0.046** |
| months >= +2% | +2.01 | [0.0, +5.0] | 86.9% | 0.131 |

**Better on BOTH mean/std and Sortino in 5 of 5 full years.** Every other
candidate managed 1 of 5, with its gain concentrated in a single year.

**The mechanism is the one the factor study identified as the only real lever:
it ADDS trades without diluting them.** +68 positions AND a higher mean R at
every drop-top depth. Contrast every rejected filter, which removed trades and
cut mean R.

#### 5r.3 METHODOLOGY -- headline CAGR cannot adopt a variant on this sample

Three variants looked as good as this one on headline numbers and died on the
same bootstrap: rank-weighting 1.5:0.7 (p=0.34), dual-direction + |ret15|>=3.0
(p=0.46), avg3 volume seed (p=0.39). Each was better in only 1 of 5 years, and
each traced its gain to a single year (2023 for the first two, 2026 for the
third).

At 63 monthly observations a swing of +-0.04 in mean/std is indistinguishable
from noise, which implies **a CAGR difference below roughly 10 points is not
decidable on this sample.** §5r clears that bar (+7.4 CAGR, ratio CI excluding
zero, 100% of resamples won); the others did not. **Require a paired bootstrap
plus year-by-year consistency before adopting anything from here on.**

#### 5r.4 REQUIRED: this needs §5p's 4 slots -- it is an INTERACTION

| cap | tolerance | positions | CAGR | Sortino |
|---|---|---|---|---|
| 4 | 5% | 1743 | 61.76% | 2.31 |
| **4** | **10%** | **1811** | **69.17%** | **2.80** |
| 2 | 5% | 1393 | 61.53% | 1.96 |
| 2 | 10% | 1423 | 61.90% | 1.88 |

At `--max-trades=2` the tolerance is worth **+0.4 CAGR, not +7.4**, and Sortino
FALLS. The extra signals only pay when there are four slots to take them; with
two they compete for seats already occupied. **Do not adopt §5r without §5p.**
(This also re-confirms §5p independently: every max-2 column carries a 4-month
losing streak and a negative worst-half-year.)

#### 5r.5 REJECTED alongside it -- eleven consecutive trade-REMOVING filters

All measured on top of §5r unless noted, same window and list:

| variant | positions | CAGR | note |
|---|---|---|---|
| **§5r (adopted)** | **1811** | **69.17%** | |
| + early shapes WIDENED (red/doji/hammer, <=09:40) | 1945 | 59.74% | mean R +0.369 -> +0.311 |
| + early shapes STRICT (red AND hammer) | 1453 | 49.99% | drop-top-50 -0.005 |
| + early shapes SHAPEONLY (hammer, any colour) | 1524 | 45.82% | drop-top-50 **-0.020** |
| + RSI divergence, classic | 1467 | 55.63% | 15,024 signals skipped |
| + RSI divergence, hidden | 1498 | 58.81% | see 5r.6 |
| + prev-close vs daily EMA5 | 1031 | 30.12% | 2,140 candidates skipped |
| + strict hammer + prev-close EMA5 | 812 | 22.42% | worst of the session |
| RSI classic alone (5% tol) | 1401 | 47.76% | |
| RSI hidden alone (5% tol) | 1429 | 50.92% | |

**Candle SHAPE carries no usable information.** Widening, restricting and
substituting for the colour rule all lose. `strict` posts a higher headline mean
R (+0.390) but drop-top-50 is -0.005 against §5r's +0.071 -- that mean is
carried by a handful of outliers.

**"Avoid trading against the daily trend" is BACKWARDS for the LONG side.**
Splitting §5r's book by whether the prior close sat above its daily EMA5:

| group | n | win% | mean R | net |
|---|---|---|---|---|
| LONG, WITH the daily trend | 650 | 34.9% | +0.333 | +3,960,109 |
| **LONG, AGAINST the trend** | **454** | **40.1%** | **+0.507** | **+4,061,951** |
| SHORT, with the trend | 378 | 37.8% | +0.400 | +3,526,962 |
| SHORT, against the trend | 329 | 38.0% | +0.213 | +2,122,288 |

Longs taken when yesterday closed BELOW its daily EMA5 are the best cell in the
book. The candidate list already ranks on first-15-min return, so demanding the
daily trend agree keeps only continuation moves and discards the reversals --
which is where the long edge concentrates. The rule helps SHORTS only; a
short-side-only version is UNTESTED.

#### 5r.6 The one variant with a better CONSISTENCY profile -- not adopted

`§5r + --rsi-div=hidden` (1498 positions, CAGR 58.81%):

| | §5r | +RSI hidden |
|---|---|---|
| NEGATIVE months | 20 | **15** |
| monthly win rate | 68.3% | **76.2%** |
| months >= +2% | 36 | **37** |
| months >= +5% | **27** | 22 |
| Sortino | **2.80** | 2.64 |
| CAGR | **69.17%** | 58.81% |

Its negative-month reduction IS statistically real (-7.9 months, 95% CI
[-13, -3], p=0.000). But its RATIO metrics are not (mean/std p=0.102, Sortino
p=0.276) and it beats §5p in only **2 of 6 years**. It trims the left tail and
the right tail together -- months >= +5% fall 27 -> 22. Recorded because it is a
defensible choice if a smoother equity curve is worth 10 CAGR points;
**not adopted**, because the ratio gain is unproven.

### 5s. ADOPTED -- DAILY VOLATILITY-REGIME FLOOR (`--min-rng-atr=0.90`)

Adopted 2026-10-18. The first change in this project to reduce NEGATIVE
MONTHS rather than raise CAGR, and the first to clear a 95% bootstrap gate on
a stability metric.

#### 5s.1 The rule

```
rng_atr(D) = mean(high - low over sessions D-5 .. D-1) / ATR14(as of D-1)
require      rng_atr(D) >= 0.90        else DROP THE CANDIDATE
```

Both the 5-session mean range and ATR14 are shifted one session, so only data
through **D-1** is read. Missing/NaN daily history **FAILS CLOSED**.

Applied as a **day-level CANDIDATE gate immediately after the 5% overnight-gap
filter**, i.e. BEFORE any intraday signal scanning. This placement is load
bearing: a dropped candidate frees its slot and CHRONO refills it from the
same day's pool. The post-hoc study, which could not model refill, showed a
near-zero effect; the engine run with refill shows the full gain.

Everything else is untouched: 5r's 10% volume tolerance, 5p's 4 slots, 5o's
09:45 bias retry, 5n's FVG veto, 5m's EMA50 trend filter, 5b's breakeven
stop, the EMA10 trail, the 15:10 squareoff, the sector gate.

Reference run: `scratch_v53_top5_hybrid.py 2.0 0.5 <label>
result/top5_5y_CLEAN.csv --cache-dir=cache --max-trades=4 --vol-tol=0.10
--min-rng-atr=0.90 --risk-pct=0.60`; trades
`result/v53_top5static_gap5_CHRONO_ematrail_hybrid_rngatr090_r060_trades.csv`.

#### 5s.2 Measured, 2021-08-30 .. 2026-10-09, Rs.10L, 5x

| metric | 5r @0.50% | **5s @0.60%** | 5s @0.50% |
|---|---|---|---|
| positions | 1811 | **1198** | 1198 |
| win rate | 37.38% | **38.98%** | 38.98% |
| mean R | +0.369 | **+0.468** | +0.467 |
| payoff (avg win / avg loss) | 2.61 | **2.72** | 2.72 |
| CAGR | 69.17% | **67.17%** | 58.39% |
| max drawdown | -9.87% | **-9.34%** | **-8.02%** |
| month-end drawdown | -6.80% | -6.54% | **-5.35%** |
| Calmar | 7.01 | 7.19 | **7.28** |
| Sortino | 9.69 | **12.11** | 12.05 |
| downside deviation | 1.63 | 1.26 | **1.13** |
| **NEGATIVE months** | **20** | **13** | 14 |
| months < -5% | 1 | 1 | **0** |
| months >= 2% | 36 | 35 | 33 |
| sum of all monthly losses | -46.6 | **-29.2** | **-26.1** |
| monthly win rate | 68.3% | **79.4%** | 77.8% |
| final equity | Rs.1.467cr | Rs.1.379cr | Rs.1.047cr |

**0.60% is the ADOPTED risk setting.** 0.615% was only ever the scaling
factor that made drawdown match 5r exactly (0.50 x 9.87/8.02); 0.60% is a
round, statable number that beats 0.615% on drawdown (-9.34% vs -9.50%),
downside deviation, Sortino and Calmar for 1.04pp of CAGR.

**CAVEAT on "13".** The month separating 13 from 14 is 2025-05, which reads
-0.52% at 0.50%, **+0.07% at 0.60%**, +0.17% at 0.615%. At +0.07% it is
indistinguishable from zero. Quote the result as **13-14 negative months**;
the robust claim is 20 -> ~13.5, not a precise 13.

#### 5s.3 Paired bootstrap, 63 months, N=20,000, vs 5r

```
                   mean monthly  P(better)  neg months  P(fewer)  P(lower ddev)
5s @0.50%            -0.634pp       6.3%       -6         95.2%       98.7%
5s @0.60%  ADOPTED   -0.141pp      36.5%       -7         97.1%       92.5%
5s @0.615%           -0.083pp      42.8%       -7         97.2%       90.9%
```

Mean return is a statistical dead heat (36.5%). **The negative-month
reduction clears at 97.1%** -- this is the metric the change was adopted on.

#### 5s.4 Why the threshold is 0.90 -- the sweep (all at 0.50% risk)

| floor | trades | meanR | neg | ddev | Sortino | maxDD | Calmar | CAGR |
|---|---|---|---|---|---|---|---|---|
| none (5r) | 1811 | +0.369 | 20 | 1.63 | 9.69 | -9.87% | 7.01 | 69.17% |
| 0.82 | 1454 | +0.443 | 19 | 1.14 | 13.11 | -9.92% | 6.55 | 65.01% |
| **0.90** | 1198 | +0.467 | **14** | 1.13 | 12.05 | -8.02% | **7.28** | 58.39% |
| 1.00 | 868 | +0.488 | 14 | 1.07 | 9.74 | -8.21% | 5.20 | 42.74% |

**0.82 REJECTED** -- its negative-month gain is noise, P(fewer) only 56.8%.
**1.00 REJECTED** -- identical stability to 0.90 (same 14 negative months,
same 33 months >=2%) for 16 points less CAGR. The curve flattens at 0.90.

#### 5s.5 Why this works when ~26 entry filters did not

Every previous filter tried to PREDICT the outcome. Trade-level IC is
measurably zero: out-of-sample AUC 0.5134 (win) / 0.4943 (R>=2) for intraday
control features, 0.4936 / 0.5354 for daily directional features, 0.5018 in
the earlier factor study.

**5s predicts nothing.** It enforces a NECESSARY CONDITION on the payoff.
Median R is -1.04; the mean is positive only because 116 of 1811 trades reach
+5R or better (max +30.86R). A stock compressed to 0.3-0.8x its own ATR14
cannot travel far enough intraday to produce such a move. That is a property
of the stock's current state, not a forecast of the outcome.

Quintile evidence (1,799 5r trades, `D:\.cloude\scratch\an_daily5.py`):

| Q | n | win% | mean R | **P(R>=2)** | net Rs. | rng_atr range |
|---|---|---|---|---|---|---|
| Q1 | 360 | 33.6% | +0.065 | **11.7%** | **+13,274** | 0.29-0.82 |
| Q2 | 360 | 35.3% | +0.370 | 14.7% | +21,80,134 | 0.82-0.93 |
| Q3 | 359 | 39.8% | +0.465 | 18.4% | +32,30,092 | 0.93-1.04 |
| Q4 | 360 | 38.1% | +0.478 | 21.9% | +52,25,944 | 1.04-1.16 |
| Q5 | 360 | 40.3% | +0.483 | 17.5% | +30,42,918 | 1.16-1.92 |

**Q1 is 20% of the book and produced 0.1% of the profit.** `rng_atr` is the
only feature found in two full studies with a significant relationship to
**P(R>=2)** (p=0.0026), positive in **6 of 6 years**.

Literature: Gao, Han, Li & Zhou, *Market Intraday Momentum*, JFE 129(2) 2018
-- intraday predictability is stronger on volatile and high-volume days, R^2
rising from 1.1% on low-volume days to 3.1% on the highest.

#### 5s.6 Validated on three independently-built candidate pools

| pool | no-floor meanR | with-floor meanR | gain | maxDD no floor -> floor |
|---|---|---|---|---|
| top-5 | +0.369 | +0.467 | +0.098 | -9.87% -> -8.02% |
| top-8 | +0.308 | +0.386 | +0.078 | -13.91% -> -12.43% |
| top-10 | +0.261 | +0.343 | +0.082 | -23.97% -> -10.78% |

A consistent +0.08..+0.10 mean-R lift on pools it was never tuned against,
halving top-10's drawdown. Strongest evidence 5s is a real effect, not a fit.

#### 5s.7 Where the gain comes from -- the 10 rescued months

vs 5r: 10 months rescued, 3 made worse, 10 negative in both.

| month | 5r | 5s | swing | floor removed | sum R removed |
|---|---|---|---|---|---|
| 2025-05 | -4.17 | +0.17 | +4.34 | 11 trades | **-12.1R** |
| 2023-01 | -3.26 | +1.09 | +4.36 | 13 trades | -10.4R |
| 2021-11 | -1.86 | +6.54 | +8.41 | 12 trades | -5.9R |
| 2023-06 | -1.76 | +2.45 | +4.21 | 8 trades | -5.7R |
| 2024-12 | -0.46 | +1.40 | +1.87 | 7 trades | -5.2R |
| 2024-05 | -0.42 | +1.54 | +1.96 | 4 trades | -4.4R |
| 2023-12 | -0.03 | +1.49 | +1.52 | 6 trades | -3.5R |
| 2023-10 | -0.45 | +0.27 | +0.72 | 4 trades | -2.4R |
| 2024-08 | -1.59 | +0.35 | +1.94 | 5 trades | -2.3R |
| 2022-06 | -1.29 | +0.51 | +1.79 | 8 trades | -1.8R |

In EVERY rescued month the removed block had negative sum R. The removed
trades are overwhelmingly clean -1.1R stop-outs in compressed names (SUZLON,
PGEL, PHOENIXLTD, HINDPETRO, UNOMINDA, DELHIVERY).

Made worse: 2022-04 (+4.90 -> -2.21), 2024-04 (+0.67 -> -0.98), 2026-04
(+4.05 -> -1.25). All three are April; at n=3 presumed coincidence.

Among the 10 months negative in BOTH, 5s is shallower in 7 of 10 and its
aggregate is -25.27 vs 5r's -31.28.

#### 5s.8 Data integrity -- the fail-closed drops

25 of 5,185 candidate rows (0.48%) failed closed for short history: IREDA 5,
PAYTM 3, POLICYBZR 3, WAAREEENER 3, JIOFIN 2, SAGILITY 2, SWIGGY 2, VMM 2,
ATHERENERG / ENRIN / NYKAA 1 each. Spread 2021-11 to 2025-06, only 28% before
2022. All are recent IPOs that genuinely lacked 5 sessions of range plus 14
of ATR -- refusing them is correct, not survivorship bias. Below-floor drops
are evenly spread by year (115/379/315/310/297/312).

#### 5s.9 OPEN -- live/VPS parity

Full implementation guide: `IMPLEMENT_5s_VolRegimeFloor_HANDOFF.md`.

**PLACEMENT -- post-selection, NOT the acceptance loop.** `intraday_engine.py`
has two gate stages that behave differently on failure:

| stage | example | on failure |
|---|---|---|
| acceptance loop (~line 152) | overnight-gap filter | SUBSTITUTES -- walks `ranked_all`, next-best name takes the slot |
| post-selection (~line 961) | `resolve_sector_gates()` | MARKS AND DROPS -- `sector_gate_pass=False`, then `tracker.done = True` at trigger; slot NOT refilled |

The backtest does the second thing: a candidate that fails the floor stays
in the day's list and simply never produces a signal. **So 5s goes at the
post-selection stage**, resolving `rng_atr` once at 09:30 beside the sector
gate, persisting the VALUE as well as the boolean, and dropping at trigger
with status `rng_atr_gate_failed`.

(An earlier revision of this section said "candidate stage so slots refill".
That was WRONG -- it describes the acceptance loop. At the floor's ~33%
rejection rate (1,728 of 5,185 on the full candidate list; 1,629 of the
4,864 reaching the floor after the gap filter; positions 1,811 -> 1,198
= -33.8%) a substituting implementation would draw 28.2% of its book
from ranks 6+ and land near 16-18 negative months instead of 13, while being
reported as 5s.)

**Replicate the computation exactly** -- both rolling windows shifted one
session, simple `rolling(14).mean()` not Wilder/EWM, 14 prior sessions
sufficient (the first bar's TR falls back to high-low), duplicate dates
dropped `keep="last"`, fail-closed on missing history, and today's partial
daily bar excluded explicitly (the backtest's `.shift(1)` does this
structurally; live must filter).

**Two live constants must change** in `intraday_strategy.py`: `MIN_RNG_ATR`
(absent -> 0.90) and `MAX_RISK_PCT_PER_TRADE` (0.005 -> 0.006). Everything
else already matches. Until both land, live runs 5r, not 5s.

**Verification:** `audit_lookahead_5s.py` against the live computation
(proves causality in seconds); `rng_atr` for ~200 symbols on one date local
vs VPS diffed to 6 decimals (0.31% of decisions sit within +/-0.001 of the
0.90 threshold); `universe_fingerprint.py` both sides. Acceptance test: the
set of candidates marked `rng_atr_gate_failed` live must EXACTLY equal the
set the backtest drops for that date.

This is the same class of change that caused the 0.3 local/VPS divergence.
NOT STARTED.

---

### 5t. REJECTED alongside 5s (2026-10-17/18)

Recorded so these are not silently retried.

| idea | measured | verdict |
|---|---|---|
| Rejection-tail shape signal, volume gate off | 5y CAGR 22.13% vs 69.17%, bootstrap P(better) **0.0%** | REJECTED |
| Same + volume > SMA20 | 1y looked excellent (meanR +0.508, DD -3.31%); 5y collapsed to +0.300 / -12.22% | REJECTED -- 1y was noise |
| Close-confirmed entry + full close at 1:2 | CAGR 15.32%, P(R>=2) 0.2%, max R +2.06 | REJECTED |
| Close-confirmed entry alone | CAGR 26.09%, meanR +0.224, two drawdowns > 380 days | REJECTED |
| FVG-in-FVG (HTF 1H -> LTF 5m, separate engine) | CAGR -3.89%, profit factor 0.499, 41 trades/yr | REJECTED |
| Ratcheting stop, 3 ladders | breakevens 326->47, win rate 37.4%->42.6%, but >=5R bucket 116->77/62/84; neg 20->22/24/18 | REJECTED |
| Risk-normalised breadth (6/8/12 slots) | 4->12 slots added **22 trades**; neg frozen at 20/20/20/20 | REJECTED -- slot cap never binding |
| max-trades=2 (both pools, matched risk) | neg 13->20 (top5), 16->20 (top10); 8 months < -5% | REJECTED |
| top-8 pool + floor | neg 18 (worse than top-10's 16), maxDD -12.43%, Calmar 6.05 | REJECTED |
| Intraday buyer/seller control features (18) | OOS AUC 0.5134 / 0.4943; all 32 filter variants lose money | REJECTED |
| Daily DIRECTIONAL features (last 5 days) | all abs(IC) < 0.04, none consistent across years | REJECTED |

**top-10 pool + floor is NOT rejected** -- it is a legitimate alternative
(CAGR 79.16%, Calmar 7.35, 41 months >=2%) but has 16 negative months, 3
months < -5%, and Sortino 9.54 vs 5s's 12.11. It optimises RETURN where 5s
optimises CONSISTENCY. Not adopted because the stated objective is monthly
consistency.

The 1:2 full-close, the volume-expansion gate and the ratchet are THREE
independent confirmations that truncating the right tail destroys the edge,
consistent with Acar & Toffel (2000): stop-losses are the necessary element
of a trend-following rule; take-profit rules are optional add-ons. **5b's
breakeven stop must be left alone.**

---

### 0.4 Deflated Sharpe Ratio validation of 5r (2026-10-18)

Bailey & Lopez de Prado (2014). 101 comparable 5-year runs exist in
`result/`. 5r monthly SR **0.6850** (annualised 2.373), T=63, skew +0.625,
kurtosis 2.790. Variance of trial Sharpes V = 0.009945.

| N trials | E[max SR] | SR*/SR | DSR |
|---|---|---|---|
| 2 (eigenvalue est.) | 0.0518 | 0.08 | 1.0000 |
| 12 (clusters @0.70) | 0.1660 | 0.24 | 1.0000 |
| 28 (literal count) | 0.2039 | 0.30 | **1.0000** |
| 60 | 0.2339 | 0.34 | 1.0000 |

**5r passes at every plausible N with ~3x margin.** Minimum track-record
length for significance is 5.5 months; we have 63. Positive skew HELPS -- it
reduces the PSR denominator, so the fat right tail makes the Sharpe more
reliable, not less.

**Caveat:** DSR assumes trial Sharpes come from a SKILL-FREE population. Ours
come from 101 variants of an already-selected strategy, all clustered in
0.60-0.70, so the true deflation is harsher than this table shows. Honest
wording: passes with a wide margin under a flattering assumption.

**The more useful finding:** mean off-diagonal correlation of the 101 trials
is **0.735** and the eigenvalue-based effective N is **1.7**. All 101 variants
are effectively ONE BET -- the search has not explored the strategy space.
Pursue structurally different constructions (e.g. a beta-neutral book) over
further variants of this design.

---

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

**DATA PREREQUISITES -- BOTH OF THESE FAIL SILENTLY AND CORRUPT STEP 3.**
Found the hard way: the sector gate passed a candidate (NAUKRI, 2026-10-09)
that should have failed it, because NIFTY INTERNET's advance/decline ratio was
computed from only **4 of its constituents**.

1. **Refresh the SECTOR-UNIVERSE candles, not just the F&O list.** The sector
   gate polls every constituent of each TOUCHED sector index, and many of those
   are NOT in the 210-symbol F&O universe. A refresh that covers only the F&O
   list leaves them stale (19 of 64 relevant symbols had no data for the day in
   question, out of 187 of 393 sector symbols stale overall). `load_cached_only`
   **silently excludes** a symbol with no data rather than raising, so the gate
   returns a confidently wrong ratio computed from a partial sample instead of
   an error. Always verify constituent coverage per sector for the target date
   before trusting a gate verdict.
2. **Regenerate sector MEMBERSHIP before live use.** `nse_index_catalog.json`
   and `nse_index_constituents.json` in `cache/` were **manually copied from
   the nse-momentum-dashboard repo and are dated 2026-08-21**. This repo cannot
   regenerate them: `nse_api.fetch_index_constituents` returns no weightage, and
   the weightage is what these files carry. Membership drifts with index
   reviews, so a stale file silently mis-assigns sectors. Re-copy from the
   dashboard repo and check the file dates before any live or paper run.

A staleness guard that prints the age of both files, and per-sector constituent
coverage for the date being evaluated, is still TODO.

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

18. **Re-signal removal (§5l)**: removing a rule introduces no new data
   dependency. The replacement — re-testing the failed candle against the
   ordinary fresh-signal criteria — uses only that candle's own already-
   closed facts (its colour, its volume vs the day's running minimum, its
   body vs the fixed 09:20/09:25 reference range), exactly as a fresh
   signal always did. ✅

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
