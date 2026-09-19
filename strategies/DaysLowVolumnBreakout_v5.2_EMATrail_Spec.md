# DaysLowVolumnBreakout v5.2 — v5.1 + EMA-Trail on the First Half (v2, alone)

**Status: v5.2, created 2026-09-19 at the user's request** (EMA-trail v2
*alone* — the tiered-stop variant was tested and not chosen, §4). Builds
directly on v5.1 (`DaysLowVolumnBreakout_v5_GatedSecondCandle_Spec.md`:
v5 gated-2nd-candle entry + 09:25 signal window + window-candle
re-signal). **Everything in v5.1 §2 / §2.1 (candidate selection, signal
candle, EMA21 and first-candle invalidation gates, entry, re-signal,
stop, sector confirmation gate, position sizing) is unchanged** and is
not repeated here — this document specifies only what changes: how the
first half of the position is exited.

**Read §5 (caveats) before implementing.** The headline improvement
rests on ~20 outlier trades, the backtest fills the trail exit at the
crossing candle's own close (optimistic), and only the 5-year window has
been tested (the 10-year window is not trustworthy — see §5).

Reference implementation (close-fill backtest):
`scratch_v2_gated2nd_resignal_ematrail2.py`, run against
`result/top2_niftybais_first15_5year_FROZEN.csv`.
Realistic-fill variant (trail exit at the NEXT candle's open):
`scratch_v2_gated2nd_resignal_ematrail2_nextopen.py` — result in §3.2.

## 1. What changes versus v5.1

**v5.1 exit (for reference):** at the 1:2R target price, book the first
half (50%) there; the second half rides to the 15:10 squareoff; the
original stop protects whatever is still open.

**v5.2 exit:** the first half is **no longer booked at the fixed 1:2R
price**. Instead, once price first touches the 1:2R level:

1. **Defer** the first-half booking. The original stop still protects
   the **whole** position (100%) while it is deferred. Nothing else
   changes on the touch candle.
2. **Choose the trail EMA** from where the (fixed) target price sits
   relative to the continuous 5-minute EMA5 and EMA10 at the touch
   candle:
   - **LONG**: target **above EMA5** → trail **EMA5**; otherwise, if
     EMA10 < target ≤ EMA5 → trail **EMA10**; otherwise book at the
     fixed target exactly as v5.1.
   - **SHORT** (mirror): target **below EMA5** → trail **EMA5**;
     otherwise, if EMA5 ≤ target < EMA10 → trail **EMA10**; otherwise
     book at the fixed target as v5.1.
3. **Trail**: from the candle *after* the touch candle onward, watch each
   candle's **close** against the chosen EMA (that same candle's own
   EMA value). The first candle that closes **below** the EMA (LONG) /
   **above** it (SHORT) triggers the **first-half booking (50%)**.
4. The **second half** then behaves exactly as in v5.1: rides to the
   15:10 squareoff, protected by the original stop.
5. If no candle ever closes through the trail EMA before squareoff, the
   first half is simply never booked separately — the full position
   squares off together at 15:10 (or is stopped at the original stop).

EMA5 / EMA10 are continuous, non-reset 5-minute EMAs of the close
(`ewm(span=N, adjust=False, min_periods=N)`), same convention as the
existing EMA21 used elsewhere in this rule set.

**In practice the EMA10 tier almost never applies.** Target-above-EMA5
is true for essentially every trade (all 329 target-touch trades in the
5-year test deferred; 311 trailed out on EMA5, 4 on EMA10, 14 never
crossed), so the rule behaves as "replace the fixed 1:2R first-half
exit with an EMA5 close-cross trailing exit". It is still specified with
the EMA10 branch because that is what was tested.

## 2. Look-ahead / real-time notes

- The trail check compares a candle's **own close** to that same
  candle's **own EMA** — both known simultaneously at candle close, the
  same convention used by every other close-based rule in this project.
- The **defer/trail-EMA choice** uses the touch candle's EMA5/EMA10,
  which technically includes that candle's own close (not yet known at
  the moment of the intrabar touch). Because the condition is true on
  effectively every trade (above), this is immaterial to results, but a
  live implementation should decide using the most recent *completed*
  candle's EMA values, or simply defer unconditionally.
- **Fill price**: the close-fill backtest assumes the first-half sell
  executes at the crossing candle's own close. In live trading the
  crossing is only known after that candle closes, so the order goes in
  at the next candle's open. §3.2 tests exactly that.

## 3. Results (5-year, frozen cache, verified stable)

### 3.1 Close-fill backtest (`scratch_v2_gated2nd_resignal_ematrail2.py`)

`result/v2_gated2nd_resignal_ematrail2_5year_trades.csv`, span 5.04yr.
**Verified: re-run from a separate process, byte-for-byte identical.**

| | v5.1 baseline | **v5.2 (EMA-trail v2)** |
|---|---|---|
| Positions | 861 | 861 |
| Win rate | 41% | **35%** |
| Net P&L | +₹25,00,770 | **+₹39,01,189** |
| CAGR | 28.20% | **37.05%** |
| Profit factor | 1.42 | **1.52** |
| Max drawdown | -11.26% | **-11.86%** |

Per-year compounded return:

| Year | Positions | Win rate (v5.1 → v5.2) | v5.1 | v5.2 | Change |
|---|---|---|---|---|---|
| 2021 (partial) | 61 | 34% → 31% | +6.00% | +6.51% | +0.51 |
| 2022 | 208 | 42% → 36% | +35.80% | +47.21% | +11.41 |
| 2023 | 143 | 47% → 42% | +34.76% | +45.08% | +10.32 |
| 2024 | 149 | 43% → 32% | +33.73% | +42.41% | +8.68 |
| 2025 | 150 | 35% → 31% | +6.15% | +18.67% | +12.52 |
| 2026 (partial) | 150 | 43% → 38% | +27.13% | +27.48% | +0.35 |

Exit-reason breakdown (legs):

| Reason | Legs | Win rate | Net P&L |
|---|---|---|---|
| ema5_trail_exit | 311 | 95% | +₹45,21,918 |
| ema10_trail_exit | 4 | 100% | +₹1,67,395 |
| squareoff | 272 | 94% | +₹65,64,912 |
| stop | 588 | 0% | -₹73,48,097 |
| eod_data_end | 1 | 0% | -₹4,938 |

Exit paths (positions): stop only 499; EMA5-trail → squareoff 222;
EMA5-trail → stop 89; squareoff only 46; EMA10-trail → squareoff 4;
eod 1. (v5.1 for comparison: stop only 494; target → squareoff 235;
target → stop 94; squareoff only 37; eod 1.)

**How the first half changed versus the old fixed 1:2R exit** (315
trail exits): mean **+0.47R**, but median **-0.11R**; **53% of trail
exits were worse than the fixed target would have been**; 14 exited
below entry (first half lost money); the top 5% gained +4.6R or more,
best +14.7R. This is a trend-following payoff — many small give-backs,
a few large gains — which is why the win rate falls while profit rises.

### 3.2 Realistic-fill test — trail exit at the NEXT candle's open

`scratch_v2_gated2nd_resignal_ematrail2_nextopen.py`,
`result/v2_gated2nd_resignal_ematrail2_nextopen_5year_trades.csv`.

**RESULT PENDING at the time this file was created** — the run was
launched the same moment as this document. This section is to be
filled in with positions / win rate / CAGR / PF / DD and the
verification-rerun outcome. **Do not rely on §3.1's numbers for
paper-trading expectations until this is filled in and compared.**

## 4. Alternatives tested alongside this (not adopted)

| Variant | Result (5-year, verified stable) | Decision |
|---|---|---|
| **v5.2 + tiered stop** on the second half (signal open at 1:2R, signal close at 1:3R, entry at 1:4R; applied only once the first half is booked) | 861 positions, win **38.3%**, CAGR **35.44%**, PF 1.53, DD **-12.92%** | **Not adopted.** Raises the win rate ~3 points (27 positions flip loss→win, 2 the other way — all from "trail exit then original stop" round trips) but costs ~1.6 CAGR points and ~1 point of drawdown: 130 positions hit a tier stop and made ₹9.9L combined versus ₹11.7L without it (it clips second halves that would have run on). |
| **v5.2 first attempt** (target price *below* EMA5 for LONG — direction inverted) | 861 positions, CAGR 29.58% | **Superseded (bug).** The inverted condition was met on only 5 of 329 target-touch trades (EMA5 almost never sits beyond a target that has only just been touched), so the rule was effectively switched off. The tested-and-specified direction is §1. |
| Tiered stop alone on v5.1 | CAGR 26.77% vs 28.20% | Rejected. |

## 5. Caveats (read before implementing)

1. **Outlier-dependent.** Compared trade by trade on return-on-capital
   (removing compounding), the top 10 improved positions supply **65%**
   of the total improvement and the top 20 supply **100%**. With the top
   10 reverted to v5.1 results, CAGR is **31.18%**; with the top 20
   reverted, **28.09%** (v5.1 is 28.20%). Of ~248 positions that changed
   materially (>0.1% of capital), **119 were better and 129 worse**; the
   median change was **-0.12% of capital**. The 20 biggest winners are
   spread across every year 2022–2026 (5/3/5/5/2), so it is not a
   one-year fluke, but it is a small number of large trend days
   (e.g. ADANIENSOL, POLICYBZR, ADANIPOWER, ADANIGREEN, KAYNES, CDSL).
2. **Optimistic fill in §3.1** (close of the crossing candle) — see
   §3.2 for the realistic fill.
3. **Lower win rate, slightly deeper drawdown.** 35% wins vs 41%; max
   drawdown -11.86% vs -11.26%. Five positions that used to bank a small
   guaranteed win at target now hit the original stop on the full size
   (deferred first half never trailed out).
4. **5-year window only, and the 10-year window is not usable.**
   `nse_api.fetch_fno_universe()` only returns today's F&O list, so any
   multi-year backtest retroactively uses the current F&O universe (v5
   spec §3.4). The 10-year result is therefore not trustworthy and has
   deliberately **not** been run for v5.2.
5. **Inherited live-implementation gaps** from v5 spec §0 still apply
   unchanged: squareoff must be at 15:15:00 real time (the "15:10"
   candle closes then), and the STT leg attribution for SHORT trades is
   unfixed in the cost model.
6. Run-to-run stability: `scratch_v2_gated2nd_resignal_ematrail2.py`
   reproduces byte-for-byte across separate process launches (the
   sector-gate hash-seed bug described in v5 spec §3.3 is fixed in it,
   and it includes the memory-reduction changes — sparse per-candidate-day
   indicator dictionaries and 09:25-only sector loading — validated to
   reproduce the v5.1 baseline exactly: 861 positions, 28.20% CAGR).

## 6. Implementation checklist for a paper-trading port

- Entry, re-signal, stops, sector gate, sizing: port from v5.1 as-is.
- Track, per open position: `target` (1:2R price), `target_touched`
  (bool), `trail_ema` (5 or 10), `first_half_booked` (bool).
- On the candle that first touches `target` intrabar: set
  `target_touched`, pick `trail_ema` per §1 (decide from the last
  completed candle's EMAs), do **not** sell.
- On every subsequent **candle close**: if `target_touched` and not
  `first_half_booked` and close crosses the trail EMA against the
  position → sell 50% at the next candle's open, set
  `first_half_booked`.
- Original stop remains live on the full remaining quantity throughout.
- At the 15:15:00 real-time squareoff, close whatever is left.
