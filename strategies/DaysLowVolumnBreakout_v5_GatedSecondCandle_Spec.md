# DaysLowVolumnBreakout v5.1 — v5 + Window-Candle Re-Signal-on-Deeper-Close

**Status: CURRENT BASELINE**, updated 2026-09-19 (adopted the re-signal
entry rule, see §2.1 and §3). Supersedes the plain v5 gated-2nd-candle
baseline (still fully documented below as the immediate prior baseline
— §3 shows both). Validated on the same frozen, reproducible cache
snapshot (`cache_frozen_20260918/`) — see §4 for why that matters, and
§3.3 for a fundamental reproducibility bug found and fixed in this same
session (not a cache problem — see below before trusting any number
from before 2026-09-19).

Reference implementation: `scratch_v2_gated2nd_resignal_close.py`, run
against `result/top2_niftybais_first15_5year_FROZEN.csv`. (Identical to
`scratch_v2_gated2nd_signal925.py` — the plain v5 baseline — except for
the re-signal addition in §2.1.)

> **This is the rule set to implement for paper trading.** The complete,
> unambiguous specification is §2 (plain v5 rules) + §2.1 (the v5.1
> re-signal addition) below. The numbers backing it — **861 positions,
> 41% win rate, CAGR 28.20%, profit factor 1.42, max drawdown -11.26%**
> (§3) — are verified stable across separate process re-runs (byte-for-
> byte identical output twice). The plain v5 baseline it supersedes
> (656 positions, 42% win, CAGR 24.14%, PF 1.47, DD -11.46%) is also
> still verified-stable and remains a legitimate, slightly more
> conservative fallback (higher profit factor, marginally better
> drawdown, fewer trades to manage) if v5.1's extra trade volume isn't
> wanted. `scratch_v2_gated2nd_resignal_close.py` reads from
> `cache_frozen_20260918/` and a pre-built candidate CSV for backtesting
> convenience — a live/paper-trading implementation obviously needs its
> own real-time data feed and order placement in their place, but the
> signal/entry/exit/sizing logic in §2 + §2.1 should be ported as-is.

## 0. Known live-implementation gaps (read before wiring this up)

These apply to any live/paper-trading port of §2 and are **not yet
fixed** anywhere in this codebase (documented, not resolved):

- **Squareoff timing**: this spec's "15:10" forced squareoff refers to
  the candle *labeled* 15:10, which — under this project's open-time
  candle convention — actually *closes* at 15:15:00 real time. A live
  implementation must square off at 15:15:00 real time, not 15:10:00,
  or it will exit 5 minutes early every day.
- **STT cost-model leg mischarge for SHORT trades**: flagged in v3 §9,
  applies identically here — see that section before wiring up live
  cost accounting, the entry/exit leg STT attribution needs a fix for
  SHORT positions specifically.
- **Point-in-time F&O universe**: §3.4 — the candidate ranking pool
  used for backtesting is today's F&O list, not history-correct. Not
  relevant for live/forward paper trading (today's list is exactly
  right for trading today), only matters if re-backtesting further.

## 1. Why this supersedes v3/v4

v3/v4 (the top-5 causal family, CAGR up to ~41%) depended on requiring
a breakout-window candle to close the confirming color — which, on
close inspection, cannot be evaluated in genuine real-time trading: it
needs the candle's own close before deciding whether to act on that
candle's own trigger touch, information that doesn't exist yet at the
moment the trigger fires. Stripping that requirement out (the
"trigger-first" fix) collapsed v3/v4's CAGR to ~18-22% — most of its
apparent edge was coming from the unrealistic part of the rule.

Separately, v2's own top-2/day baseline was re-examined and found to
already be genuinely live-valid from the start (its 1-candle breakout
window never checked color on the window candle, only the trigger).
Layering the **gated-2nd-candle** entry rule (§3) on top of v2's own
baseline — which extends the window to 2 candles while keeping every
single decision genuinely real-time-knowable — produced a clean,
verified improvement over plain v2, without reintroducing the v3/v4
problem. That combination is this document.

## 2. Full rule set

**Candidate selection**: NIFTY50 day-bias at the 09:30 checkpoint
(`ret_first15` = each stock's 09:25-candle close vs previous close).
Day bias: LONG if NIFTY A/D ratio > 1.5, SHORT if < 0.66. Rank the F&O
universe by their own `ret_first15` in the day's bias direction, take
the **top-2** per day.

**Signal window**: starts **09:25** — the candle covering 09:25:00–
09:29:59 (closing at 09:30:00 real time) is eligible as a signal candle.
This is safe/non-look-ahead: the day-bias ratio, the sector-gate ratio,
and this candle's own color/volume/ATR are all resolved from the exact
same close, at the exact same 09:30:00 instant — none of them depend on
each other's outcome, so there's nothing here that isn't genuinely known
the moment 09:30:00 arrives. **New-signal cutoff: 11:00** — no new
signal candle may form after this; an already-active signal still
resolves normally past it.

**Signal candle**: strictly red (LONG) / green (SHORT), volume ≤1.05×
the day's running minimum-so-far (**5% tolerance**), valid ATR14 (not
NaN, > 0).

**EMA21 invalidation gate**: continuous 5-min EMA21 (no reset); any
candle closing on the wrong side (below for LONG, above for SHORT)
from 09:30 onward voids the whole day for that stock.

**First-candle invalidation gate**: any candle closing below the day's
first (09:15) candle's low (LONG) / above its high (SHORT) voids the
whole day too — same "whole day voided" semantics as the EMA21 gate.

**Entry — the gated-2nd-candle rule:**

- **Window candle #1** (the candle immediately after the signal
  candle): check only whether price crosses the trigger level
  (`signal_high + 5%×ATR14` for LONG, `signal_low − 5%×ATR14` for
  SHORT) — a pure, real-time price-cross fact.
  - **Triggered → enter immediately**, at the trigger price. No color
    check needed — the trade is taken purely on the price cross.
  - **Not triggered** (only knowable once this candle has fully
    closed) → gate whether candle #2 gets a chance, using three facts
    about this *already-closed* candle:
    1. Confirming color (green for LONG continuation, red for SHORT)
    2. Volume strictly less than the signal candle's own volume
    3. High and low both contained within the signal candle's own
       high-low range (no new extreme of its own — a tight
       consolidation pause, not an independent breakout attempt)
    - All three pass → candle #2 gets a chance.
    - Any one fails → signal dies here; candle #2 is never checked.
- **Window candle #2** (only reached if candle #1 passed the gate):
  check only the trigger touch again, same trigger level as
  established at signal formation.
  - **Triggered → enter immediately.** No further color/volume/range
    check on candle #2 itself — the window ends after this candle
    regardless.
  - Not triggered → window exhausted, signal dies.

**Design principle**: every entry decision is made purely on a
real-time price-cross fact. Color/volume/range are only ever used to
decide whether to *keep watching* a candle that has *already closed* —
never on the candle currently being evaluated for its own trigger. This
is what makes the whole rule genuinely executable tick-by-tick in live
trading, unlike v3/v4's higher-CAGR-but-invalid version.

### 2.1 v5.1 addition: window-candle re-signal on a deeper close

When window candle #1 (or #2) fails to trigger **and** fails the
continuation gate above, v5 kills the signal outright. **v5.1 gives it
one more chance first**: that same already-closed candle becomes a
brand-new signal candle in its own right — using its own high/low/
volume/ATR14 as the new reference, and getting its own fresh 2-candle
breakout window — if its close extends the pullback beyond the *prior*
signal candle's close:

- **LONG**: `this_candle.close < prior_signal.close` (a lower pullback
  low, still fading, not yet reversing).
- **SHORT**: `this_candle.close > prior_signal.close` (a higher
  pullback high, mirrored).

If that also fails, the signal dies exactly as in plain v5 — no change
there. This can **chain**: a re-signaled candle's own failing window
candle can re-signal again, using the same condition against ITS OWN
close as the new reference. No cap on chain length is applied (none
was requested); observed up to 7 hops deep on the 5-year test with no
runaway degradation. 1,910 re-signal events occurred across the 5-year
run (656→861 final positions), so this fires often, not as a rare edge
case.

**Look-ahead check** (audited 2026-09-19): every fact this rule uses —
the candle's own close/high/low/volume, the prior signal's already-
established close, and ATR14 (Wilder's method, strictly backward-
looking via `shift(1)` + causal EWM, confirmed in `geopattern/core.py`)
— is either about the *current* candle (knowable the instant it
closes) or a *fixed, strictly-earlier* value. The re-signal check only
ever runs *after* that same candle's own trigger-touch check has
already failed (using that candle's own high/low, checked first,
returning immediately if triggered) — it never uses this candle's
close to retroactively influence a decision about its own trigger.
Structurally identical in discipline to the already-audited plain
gated-2nd-candle rule above; no forward-looking fact is used anywhere.

**Stop**: `signal_low − 5%×ATR14` (LONG) / `signal_high + 5%×ATR14`
(SHORT), fixed at signal formation, never moved. (Unchanged from v5 —
note this uses whichever signal candle the entry actually resolved
from, i.e. the last one in a re-signal chain if there was one.)

**Sector confirmation gate**: the candidate's own NSE primary sector
(`sector.primary_sector_map()`) must show an A/D ratio confirming the
day's direction (**strict: >2.0 LONG / <0.5 SHORT**), computed once at
the 09:30 checkpoint from that sector's own constituents' `ret_first15`,
checked at entry time on the already-picked candidate (not a
pool-narrowing filter). A failing candidate's trade is dropped; the
other top-2 candidate, if it separately passes, can still trade.

**Exit**: half the position books at the 1:2 reward:risk target — that
half's stop is **never moved to breakeven**; if the stop is hit first
(before target), the whole remaining position closes there. The other
half, once the target has fired, rides **unconditionally** to the
15:10 forced squareoff — no further stop/target check on it.

**Position sizing**: capital/2 per trade (2-per-day pool), 5x leverage,
0.5% max risk per trade, realistic Zerodha cost model (brokerage + STT
+ exchange charges + GST, `costs.py`).

## 3. Results (5-year, frozen cache, corrected candidate list)

### 3.0 v5.1 — CURRENT BASELINE (v5 + §2.1 re-signal)

`result/v2_gated2nd_resignal_close_5year_trades.csv`, span 5.04yr.
**Verified genuinely stable: re-run twice from separate process
launches, byte-for-byte identical output both times**:

- **positions=861, win_rate=41%, net P&L=+₹25,00,770**
- ₹10,00,000 → ₹35,00,770 (**+250.08%**)
- **CAGR = 28.20%**, profit factor **1.42**, max drawdown **-11.26%**
  (2025-08-25)
- Re-signal events: 1,910 total, longest observed chain 7 hops

Per-year compounded return — zero losing years:

| Year | Positions | Win rate | Return |
|---|---|---|---|
| 2021 (partial) | 61 | 34% | +6.00% |
| 2022 | 208 | 42% | +35.80% |
| 2023 | 143 | 47% | +34.76% |
| 2024 | 149 | 43% | +33.73% |
| 2025 | 150 | 35% | +6.15% |
| 2026 (partial, ~8.5mo) | 150 | 43% | +27.13% |

Adopted 2026-09-19 after: (a) the §2.1 look-ahead audit found nothing
forward-looking, (b) the improvement checked out as broad-based across
years rather than concentrated in one lucky stretch (2023/2024/2026
meaningfully better, 2021/2025 roughly flat, no year turns negative),
and (c) sample trades were manually verified against real candle data
(HINDZINC 2026-08-25 and MUTHOOTFIN 2026-08-20 — both zero-trade days
under plain v5 — resolved into a net-positive win and a small scratch
respectively once re-signal was allowed to chain through their early
failed attempts; see conversation history for the full candle-by-candle
trace). The tradeoff going in: profit factor is a genuine step down
(1.47→1.42) — v5.1 adds real trade volume at a slightly lower average
quality per trade, more than compensated for by the extra volume itself
on a CAGR basis, but worth knowing this isn't a free improvement on
every axis.

### 3.1 v5 — prior baseline (superseded, still a valid fallback)

`result/v2_gated2nd_signal925_5yearFINAL3_trades.csv`, span 5.04yr.
Verified stable the same way:

- **positions=656, win_rate=42%, net P&L=+₹19,75,397**
- ₹10,00,000 → ₹29,75,397 (**+197.54%**)
- **CAGR = 24.14%**, profit factor **1.47**, max drawdown **-11.46%**
  (2025-08-25)

Per-year compounded return — zero losing years:

| Year | Positions | Win rate | Return |
|---|---|---|---|
| 2021 (partial) | 45 | 38% | +6.42% |
| 2022 | 153 | 44% | +35.36% |
| 2023 | 113 | 42% | +22.87% |
| 2024 | 113 | 46% | +32.47% |
| 2025 | 120 | 35% | +8.97% |
| 2026 (partial, ~8.5mo) | 112 | 44% | +16.45% |

*(This 656/24.14% figure itself supersedes 514, 628, 591, 581, and 567
positions all separately reported at various points on 2026-09-18/19
before the §3.3 root cause was found and fixed — same code throughout,
hitting the same unseeded bug in different ways, not different rule
sets. Still fully valid as a more conservative fallback to v5.1: higher
profit factor, marginally better drawdown, ~24% fewer trades to manage
day to day.)*

### 3.2 Why the signal window moved from 09:30 to 09:25

The prior version of this baseline (§ history) used
`SIGNAL_WINDOW_START = "09:30"`, excluding the 09:25 candle from signal
consideration. Re-examined 2026-09-18: the day-bias ratio and the
sector-gate ratio are *already* computed from each relevant stock's
09:25-candle close (`ret_first15`) — so by the time those two gates are
known to have passed, the exact same close is available to also judge
whether the 09:25 candle itself qualifies as a signal candle (color,
day's-lowest-volume, valid ATR). None of the three facts (day-bias,
sector-bias, this candle's own shape) depend on each other's outcome —
they're all simultaneously knowable at 09:30:00 real time. Allowing
09:25 as an eligible signal candle is therefore not a look-ahead
change, just a genuinely earlier legitimate opportunity that the
09:30-start version was leaving on the table.

Verified result of the change:

| Metric | 09:30 start (previous v5) | **09:25 start (current v5)** |
|---|---|---|
| Positions | 528 | 514 |
| Win rate | 45% | 44% |
| CAGR | 21.51% | **23.93%** |
| Profit factor | 1.55 | **1.63** |
| Max drawdown | -8.34% | **-7.57%** |

Win rate dips a single point; every other metric improves. Adopted.

**Cutoff-time check** (against the 09:30-start version): 10:30 was also
tried on this same rule/pool and found clearly *worse* (CAGR 15.20%,
PF 1.42) — unlike the top-5 causal family, where 10:30 beat 11:00.
**11:00 remains the correct cutoff for the top-2 pool.** Do not port
the 10:30 finding across pool sizes without re-testing; the two don't
transfer.

### 3.3 The full reproducibility saga: 514 → 628 → 591 → 581 → 567 → 656 (stable)

The 514-position number (an earlier revision of §3) was reproducible on
paper but wrong, and fixing it took two genuinely separate bugs, found
in sequence, only the second of which was the real culprit.

**Bug #1 — incomplete sector cache (real, but not the full story).** The
sector confirmation gate needs 5-minute and daily candles for every
constituent of every NSE sector index ever touched by a candidate
(~1,130 symbols across the 33 sectors this candidate list touches) — a
much larger universe than the ~205 symbols in the top-2 candidate pool
itself. The original `cache_frozen_20260918/` snapshot only ever
covered the candidate-pool symbols; 711 of those 1,130 sector-
constituent symbols fell through to `load_cached_only()`'s live-fetch-
fallback path on every run, and with the project's Kite access token
dead all session, those fetches failed and fell back to serving stale
cache. Fixed by refreshing the token (see the project's VPS reference
memory) and live-fetching the missing symbols once into the frozen
snapshot. Two minor follow-on issues surfaced and were fixed in the
same pass: (a) most of the 711 "missing" symbols turned out to be
permanently-unresolvable junk tickers (SME-board/delisted/mismatched
names in the sector-index constituent lists that don't exist on Kite's
NSE instrument map at all) — harmless, they fail fast every time; (b)
four real symbols (COALINDIA, NMDC, ONGC, PETRONET) had a pre-existing
duplicate 2015-12-31 row in their daily cache CSV (logged once at
`00:00:00`, once at `09:15:00`, identical values) that crashed
`pd.DataFrame` construction — deduplicated, no data lost.

This produced a plausible-looking **628-position, 24.03% CAGR** result
that appeared stable at the time. **It was not.** Re-running the exact
same unmodified script minutes later, and again with a completely
disabled live-fetch path (to rule out network/timing effects), produced
**591, then 581, then 567 positions** — all from the identical script
and the identical on-disk cache, no network calls involved in the last
two. This ruled out caching, fetching, and even the initially-suspected
midnight-crossing theory (`load_cached_only()`'s old
`pd.Timestamp.now()`-based depth cutoff, which *is* a real latent bug —
now removed — but wasn't the actual cause here).

**Bug #2 — the real cause: hash-seed-randomized set iteration.**
Isolated by re-computing a single (sector, date) ratio directly from
disk, by hand, 5 times in one process (rock-solid, identical every
time) versus reading it back out of the full script's own output
across *separate* process launches (different every time). The
script built its sector-constituent → sector mapping as
`{sym: sec for sec, syms in sector_symbols.items() for sym in syms}`,
where `sector_symbols` was itself built by iterating
`sectors_touched`, a plain Python `set`. **Python randomizes string
hash seeds per process by default** (hash-flooding protection), so a
`set`'s iteration order over strings is stable *within* one process
but varies *across* separate launches. Any stock that's a constituent
of more than one *touched* sector's index got silently assigned to
whichever sector happened to be iterated last — different on every
fresh `python` invocation — which changed that sector's own A/D-ratio
computation for any date the ambiguous stock touched, which flipped
the sector gate's pass/fail for a few dozen unrelated candidates each
time, purely as a side effect of process startup, never touching the
actual price data or trading logic at all.

**Fix**: replaced the ad-hoc, set-order-dependent mapping with the
project's own `sector.primary_sector_map()` (already computed earlier
in the script as `sector_map`, and already used for the *candidate's*
own primary-sector check) — its tie-break is by index weight via
`sorted()` on a list, no set iteration involved, genuinely
deterministic. **Verified: re-run twice from separate process
launches, byte-for-byte identical result both times** — **656
positions, 42% win rate, CAGR = 24.14%** (§3). `scratch_v2_gated2nd_
ematrap.py` run with its trap-fallback toggle disabled reproduces this
exactly too, confirming the fix applies cleanly to both scripts and
what had looked like a bug specific to the EMA-trap script was, this
whole time, this same pre-existing, script-independent bug.

**Implication for every other multi-symbol/sector-gate result in this
project's history** (including the §3.4 10-year run, and anything from
earlier sessions that used the same `sym_to_sector` pattern): treat as
approximate until re-run against the fixed code. CAGR-level conclusions
have generally held up (23.93% → 24.03% → 24.14% is a narrow band), but
exact position counts and drawdown have not been reliable before this
fix, and shouldn't be quoted precisely from anything predating it.

### 3.4 10-year validation

**NOTE: this run predates BOTH §3.3 fixes** (the sector-cache extension
and, more importantly, the hash-randomization determinism bug) and is
now known to be unreliable at the position-count level for the same
reason the 5-year result swung between 514/628/591/581/567 before
settling at 656. Treat the position counts below as approximate and
the CAGR as directionally informative only, until this is re-run
against the fixed scripts. Not yet re-validated (open item, §6).

`result/v2_gated2nd_signal925_10year_trades.csv`, span 10.12yr:

- **positions=936, win_rate=43%, net P&L=+₹33,83,770**
- ₹10,00,000 → ₹43,83,770 (**+338.38%**)
- **CAGR = 15.72%**, profit factor **1.47**, max drawdown **-13.40%**
  (2021-09-03)

Meaningfully lower than the 5-year snapshot's 24.14% CAGR — 2016-2018
were weak/flat years (2018 the only loser, -1.21%), while 2019 onward is
consistently strong (every year ≥ +8.98%, several ≥ +15%). The 5-year
test window happened to catch mostly the strong regime.

| Year | Positions | Win rate | Return |
|---|---|---|---|
| 2016 (partial) | 31 | 45% | +4.64% |
| 2017 | 83 | 45% | +10.09% |
| 2018 | 82 | 35% | **-1.21%** (only losing year) |
| 2019 | 58 | 57% | +38.63% |
| 2020 | 99 | 42% | +8.98% |
| 2021 | 87 | 37% | +1.33% |
| 2022 | 127 | 47% | +36.39% |
| 2023 | 85 | 39% | +15.32% |
| 2024 | 91 | 44% | +16.98% |
| 2025 | 103 | 38% | +15.59% |
| 2026 (partial) | 90 | 44% | +18.31% |

**KNOWN LIMITATION — the 10-year candidate universe is not point-in-time
correct.** `nse_api.fetch_fno_universe()` only ever fetches the
*current* (as-of-today) list of ~210 F&O-eligible underlyings from
NSE's live `/api/underlying-information` endpoint — there is no
point-in-time/historical capability anywhere in that function. The
10-year candidate-ranking pool therefore used **today's (2026) F&O
list for every historical day back to 2016**, which is wrong in two
directions:

1. Stocks that are F&O-eligible *today* but weren't yet in 2016-2018
   (eligibility is typically earned years after listing, once
   liquidity/market-cap thresholds are cleared) were wrongly available
   as candidates in years they shouldn't have been.
2. Stocks that *were* F&O-eligible in earlier years but have since lost
   that status (delisting, merger, a periodic NSE eligibility revision)
   are silently missing from the candidate pool for those years, even
   though they were legitimate, tradable candidates at the time.

This is a genuine data-construction flaw, not a finding about the
strategy's real performance — and it's a plausible contributor to why
2016-2018 look weaker than 2019+ (wrong-composition candidate pools in
those years, independent of the trading rules themselves). A proper fix
needs point-in-time historical F&O membership data, which this project
does not currently source anywhere. Treat **15.72% CAGR as approximate,
potentially biased in an unknown direction**, until this is addressed.
The 5-year result (§3.1, 24.14% CAGR) is less affected by this
specific issue since F&O eligibility churn is slower relative to a
5-year window, but is not entirely immune either.

## 4. The reproducibility investigation that led here (important context)

*(§3.3 above documents a second, later round of this same investigation
— the sector-gate's own determinism, discovered independently of
the candidate-pool cache-freezing described below.)*

Earlier the same day, re-running what was believed to be "the v2
script" gave a very different number (18.48%, later corrected further)
from the originally documented 22.29%. Investigation found two distinct,
now-resolved problems:

1. **Wrong-script confusion**: the script initially re-run
   (`scratch_top2_niftybais_pullback_atr_rules_5year.py`) was *not*
   actually the source of the documented 22.29% number — it's missing
   the sector gate and the first-candle invalidation gate entirely. The
   true source is `scratch_baseline_plus3conditions_5year.py`.
2. **Genuine cache drift**: even re-running the *correct* script gave a
   different result days later (15.29%, then 19.78% on a frozen
   snapshot) — because `result/top2_niftybais_first15_5year.csv` (built
   Sep 13) and other candidate-list/price data are read from a shared,
   continuously-mutating on-disk cache, not a frozen dataset. A whole
   trading day (2026-09-10) was silently missing from the Sep-13
   candidate list, later confirmed present with the correct NIFTY ratio
   (0.63, SHORT) once rebuilt against a frozen cache snapshot.

**Resolution**: `cache_frozen_20260918/` is a full copy of the
candles cache (`cache/candles/` → `cache_frozen_20260918/candles/`)
plus the small JSON reference files, taken 2026-09-18 and never
touched again. Any script redirected to it (via monkeypatching
`kite_client._CANDLE_CACHE_DIR` and `sector._CATALOG_PATH` /
`_CONSTITUENTS_PATH` to the frozen paths — see
`scratch_v2_gated2nd_frozen.py`'s top few lines for the exact pattern)
now produces a genuinely reproducible result, rerun after rerun.
`result/top2_niftybais_first15_5year_FROZEN.csv` is the correspondingly
rebuilt, correct candidate list — **use this one going forward, not**
`result/top2_niftybais_first15_5year.csv` **(stale, missing at least
2026-09-10)**.

## 5. Alternatives tested and rejected on this rule

| Variant | Result | Verdict |
|---|---|---|
| 10:30 cutoff (instead of 11:00) | CAGR 15.20%, PF 1.42, DD -7.89% | Worse — rejected for this pool/rule combination |
| Dynamic sector-gate-at-signal (re-checked live at every signal candle's own timestamp, instead of the fixed 09:30 snapshot checked at entry) | CAGR 21.87%, PF 1.47, DD -11.41% (vs baseline's pre-09:25-update 21.51%/1.55/-8.34%) | Roughly tied CAGR, worse PF and much worse drawdown — rejected. Lets in more trades (593 vs 528) at lower average quality. |
| Unbounded continuation gate (window extends indefinitely as long as each candle's close reclaims the signal candle's own open) | CAGR 18.17%, PF 1.40, DD -10.79% | Worse — rejected. Only 14/612 positions (2.3%) ever used the extension beyond 2 candles, so window length wasn't the driver — the weaker continuation condition was. |
| Unbounded gate + window candle volume required > signal candle volume | CAGR 20.26%, PF 1.53, DD -9.02% | Better than the plain unbounded gate but still short of the baseline — rejected. |
| Fixed 2-candle cap + same "reclaim + rising volume" condition (isolating window-length from condition-strength) | CAGR 11.61%, PF 1.16, DD -15.32% (top-5 pool) | Confirms window length isn't the lever on top-5 either — see next row for why this number looks so much worse than the top-2 tests: different pool. |
| **Top-5 pool, same gated-2nd-candle rule** (originally reported 27.96% CAGR on a **stale** candidate list) | **17.93% CAGR, PF 1.25, DD -19.59%** once re-verified on the corrected, frozen candidate list | **Reversed finding**: top-5 is NOT better than top-2 — it's worse on both return and risk. The original 27.96% number was a stale-data artifact. Do not use top-5 pool for paper trading based on that earlier number. |
| Top-5 pool + dynamic sector-gate-at-signal (same live-recheck idea as the top-2 test above, applied to top-5) | CAGR 23.97%, PF 1.31, DD -18.52% (corrected list) | Meaningfully better than top-5's own corrected baseline (17.93%→23.97%), but still far worse drawdown than the top-2 baseline for similar or lower CAGR — not adopted. |
| **EMA-trap fallback** (`scratch_v2_gated2nd_ematrap.py`, adapted from an uploaded "MTF EMA Trap" scalping doc): on EMA21 breach, instead of outright invalidating the day, permanently disable the low-vol signal search for that stock and watch for a reclaim-with-rising-volume setup up to 11:30, entering at the reclaim candle's close with the tracked breach-to-reclaim extreme as stop | **Final, verified-stable numbers** (re-run twice from separate process launches, identical both times): **positions=712, win_rate=41%, CAGR=23.97%, PF=1.43, DD=-9.33%**, vs the plain baseline's 656/42%/24.14%/1.47/-11.46%. Of the 75 genuine trap-sourced signals (breach→reclaim, correct timing — this count is sector-gate-independent and was stable throughout), 55 survived the sector gate: **win rate 29%, net P&L only +₹3,659** — essentially breakeven, propped up by a couple of large squareoff-leg winners against a majority of quick, small stop-outs. | **Rejected, but more marginally than first thought.** Adds 56 more positions at lower win rate and CAGR, though with a smaller drawdown than the baseline — not a clear win on any axis, and the trap-sourced trades specifically are barely breakeven on their own (29% win rate). The reclaim condition (any close back above/below EMA21, no margin or confirmation-candle requirement) lets through thin, false reclaims that reverse immediately — e.g. IOC 2021-10-21 reclaimed by 2 paise and stopped out 5 minutes later. Every trap trade reclaimed on the very next candle (5 min) or not at all — the nominal 1-3 candle lookback window was never actually used. Do not enable `EMA_TRAP_FALLBACK_ENABLED` for paper trading. |

## 6. Not yet done (open items)

- [x] ~~10-year validation of this exact config~~ — done, see §3.4
      (CAGR 15.72%, PF 1.47, DD -13.40%, one losing year: 2018). **Now
      flagged stale** — predates both §3.3 fixes (sector-cache extension
      and, more importantly, the hash-randomization determinism bug)
      AND predates the §2.1 re-signal rule adopted as the new baseline,
      needs re-running against `scratch_v2_gated2nd_resignal_close.py`
      (see next item).
- [ ] **Re-run the 10-year validation** against the fixed, current
      (v5.1/re-signal) script. The 5-year result moved from 514 through
      several unstable intermediate numbers before settling at a
      verified-stable 656 positions / 24.14% CAGR (plain v5) once the
      real determinism bug was fixed, then to 861 / 28.20% once the
      re-signal rule was layered on top; the 10-year number needs the
      same re-verification (run twice from separate process launches,
      confirm identical output) on the CURRENT script before it can be
      trusted at the position-count level. Until re-run, treat §3.4's
      15.72% CAGR as directionally informative only, and note it
      doesn't reflect the re-signal rule at all.
- [x] ~~Re-run the EMA-trap-on result against the fixed scripts~~ —
      done, verified stable across two separate process launches. See
      §5: 712 positions, 41% win, CAGR 23.97%, still rejected, though
      more marginally than the pre-fix number suggested.
- [ ] **Point-in-time F&O universe data** — `nse_api.fetch_fno_universe()`
      only fetches the *current* F&O-eligible list, with no historical
      capability, so every multi-year backtest (5yr and 10yr alike) has
      used today's F&O membership retroactively for all past dates. See
      §3.4 for the full explanation. Needs a real historical F&O
      membership data source before any of this session's numbers
      (this baseline included) can be treated as fully correct rather
      than approximate. Not yet investigated whether such a source is
      obtainable at all (NSE circular archives, the VPS's own data, or
      elsewhere).
- [x] ~~Re-run plain v2 on the corrected candidate list~~ — done
      (19.78% CAGR, PF 1.57, DD -6.65%; still below this baseline).
- [ ] Live-tick audit specific to the gated-2nd-candle rule (the
      general principle in §2 was designed to be look-ahead-free, but
      it has not yet been through the same systematic candle-by-candle
      audit v3/v4 received).
- [ ] The squareoff-timing correction (15:15:00 real time, not
      15:10:00) and the STT cost-model leg mischarge for SHORT trades
      (both documented in v3 §9) apply identically here — still
      unfixed in the live-runner sense, only documented.
- [x] ~~Decide whether the top-5-pool variant is worth pursuing~~ —
      resolved: top-5's originally-reported 27.96% CAGR was a
      stale-candidate-list artifact. Re-verified on the corrected list
      it drops to 17.93% CAGR with a -19.59% drawdown, meaningfully
      worse than this top-2 baseline on every axis. Top-5 is not a
      pursued alternative going forward unless something changes that
      finding.
- [ ] Precomputed-cache infrastructure (`scratch_precompute_top2_cache.py`
      / `scratch_precompute_top5_cache.py`, pickled to
      `cache_frozen_20260918/top{2,5}_precomputed_5year.pkl`) exists and
      cuts data-loading time from minutes to ~15s, but this specific
      script (`scratch_v2_gated2nd_signal925.py`) was not yet ported to
      use it — still loads via `load_cached_only()` each run.
