# DaysLowVolumnBreakout v5.3 — v5.2 + Stricter Re-Signal (lower volume, no chaining)

**Status: CURRENT BASELINE — v5.3, adopted 2026-09-19 at the user's
decision.** Builds directly on v5.2
(`DaysLowVolumnBreakout_v5.2_EMATrail_Spec.md`: v5.1 + the EMA5/EMA10
trail on the first half), which in turn builds on v5.1
(`DaysLowVolumnBreakout_v5_GatedSecondCandle_Spec.md`). **Everything in
those two specs is unchanged** — candidate selection, signal candle, EMA21
and first-candle gates, the gated-2nd-candle entry, stop, sector gate,
sizing, and the v5.2 EMA-trail exit — **except the re-signal rule (v5.1
§2.1), which this document tightens.** v5.2 remains a valid, higher-return
fallback (§3).

Reference implementation:
`scratch_v2_gated2nd_resignal_ematrail2_nextopen_resigvol_nochain.py`, run
against `result/top2_niftybais_first15_5year_FROZEN.csv`; trades in
`result/v2_gated2nd_resignal_ematrail2_nextopen_resigvol_nochain_5year_trades.csv`.

**All numbers below are from a single run each** (per the user's request
not to re-run every experiment); the earlier reproducibility bug that made
that matter is fixed in these scripts, but this exact variant has not been
re-run to confirm it byte-for-byte.

## 1. What changes versus v5.2

v5.1 §2.1 lets a window candle that fails to trigger — and fails the
continuation gate, or is candle #2 of an exhausted window — become a new
signal candle if its close extends the pullback beyond the prior signal
candle's close. That rule had **no volume test and no limit on chaining**.
v5.3 adds two conditions:

1. **Lower volume.** The re-signal candle's volume must be **strictly less
   than the volume of the signal candle it replaces**
   (`row.volume < active_signal.volume`). Without it, a re-signal could be a
   heavy-volume selling candle, which is not a low-volume pullback at all.
2. **No chaining.** A fresh (low-volume) signal candle can be replaced **at
   most once**. The re-signaled candle still gets its normal 2-candle
   breakout window and can trigger an entry, but it **can never re-signal
   again** (implemented as `chain_len == 0`, i.e. the currently active
   signal must be a fresh one, not itself a re-signal).

**Complete re-signal condition (LONG; SHORT mirrors the close comparison):**
all of the following on the window candle that just closed —
- it did not trigger the breakout, and failed the continuation gate (candle
  #1) or is candle #2 with the window exhausted;
- `close < prior_signal.close` (SHORT: `close > prior_signal.close`);
- `volume < prior_signal.volume`;
- the active signal is a fresh signal (no re-signal has happened yet for
  this signal);
- ATR14 at the candle is valid (not NaN, > 0).

Then that candle becomes the new signal (its own high/low/volume/ATR14/
close as the reference) and the rest of the rules apply unchanged. Look-ahead
discipline is the same as v5.1 §2.1: only this candle's own already-closed
facts and a fixed earlier signal's values are used.

**Motivating example:** ASIANPAINT, 27 Oct 2021 (LONG). v5.2's rule chained
four re-signals (09:50, 10:00, 10:05, 10:10) after the 09:45 low-volume
signal, ending on a 10:10 candle of 122,767 shares — over 3× the day's
running-minimum volume — and the resulting entry (10:15, ₹3106.86) was
stopped out at 11:40. Under v5.3 the 09:50 candle (121,723 shares vs the
signal's 68,462) is rejected and the chain never forms.

## 2. Look at what this does to the signal population

Versus v5.2 (861 positions): **218 positions removed** (their v5.2 results:
33% win rate, average +0.26R, i.e. still profitable on average) and **82
new positions** appeared (28% win rate, average +0.33R; these are later
fresh signals on the same stock-day once a chain no longer forms).
Re-signal events fell from 1,910 (longest chain 7) to **677 (longest chain
1)**.

## 3. Results (5-year, frozen cache, single run)

| | v5.1 | v5.2 | v5.2 + lower-volume rule only | **v5.3 (lower volume + no chain)** |
|---|---|---|---|---|
| Positions | 861 | 861 | 739 | **725** |
| Win rate | 41% | 35% | 35% | **35%** |
| Net P&L | +₹25,00,770 | +₹39,50,810 | +₹33,18,641 | **+₹34,18,391** |
| CAGR | 28.20% | 37.32% | 33.65% | **34.26%** |
| Profit factor | 1.42 | 1.53 | 1.51 | **1.53** |
| Max drawdown | -11.26% | -12.01% | -11.38% | **-10.23%** |
| Re-signal events / longest chain | 1,910 / 7 | 1,910 / 7 | 843 / 5 | **677 / 1** |

v5.3 outcome split (725 positions): **254 wins (35.0%)**, **420 complete
losses (57.9%)** (single exit, whole size lost; avg -0.99R), **51 partial
losses (7.0%)** (first half booked, position still net negative; avg
-0.24R). v5.2 had 304 / 510 / 47 of 861. Average return per position is
+0.214% of capital (v5.2: +0.194%); average R per position 0.503 (v5.2:
0.458) — **each trade is slightly better, but there are fewer of them.**

**Versus v5.2 honestly:** v5.3 gives up about **3 CAGR points**
(34.26% vs 37.32%) in exchange for the **shallowest drawdown of any variant
tested (-10.23%)**, an unchanged profit factor (1.53), and a cleaner signal
definition (every re-signal is a genuinely lower-volume pullback, no
multi-hop drift). The removed trades were profitable on average, which is
why the return falls. Because the position count differs (725 vs 861),
compounding differs too, so the CAGR gap should not be read as exact.

## 4. Caveats (inherited, all still apply)

- v5.2 §5 in full: the EMA-trail gain depends on a small number of large
  trend-day winners (top 20 positions supply the whole v5.2-over-v5.1
  improvement); lower win rate than v5.1; the 10-year window is **not**
  usable because the F&O universe is not point-in-time.
- v5 spec §0: squareoff at 15:15:00 real time (the "15:10" candle closes
  then), and the SHORT-trade STT leg attribution is still unfixed.
- Single-run results, as noted above.

## 5. Alternatives tried on the re-signal rule

| Variant | Result (5-year, single run) | Decision |
|---|---|---|
| Re-signal, any volume, unlimited chain (v5.1/v5.2 rule) | 861 positions, CAGR 37.32%, DD -12.01% | Replaced by v5.3; chains of up to 7 hops could end on heavy-volume candles |
| Lower-volume rule only (chains still allowed) | 739 positions, CAGR 33.65%, PF 1.51, DD -11.38% | Not chosen; no-chain adds a little return and cuts drawdown further |
| **Lower volume + no chaining (v5.3)** | 725 positions, CAGR 34.26%, PF 1.53, DD -10.23% | **Adopted** |
