# MAD Volatility Trail — Positional Strategy Reference

Reference documentation for `mad_trail_strategy.py`, an alternative
stop-loss mechanism for the momentum/positional (swing, CNC) strategy.
Written after directly tracing every call site in the codebase
(`mad_trail_strategy.py`, `backtest.py`, `indicators.py`,
`live_rebalance.py`, `screener.py`) — **`mad_trail_strategy.py`'s own
top-of-file docstring is stale** ("EXPERIMENTAL, backtest-only, not
wired into the live screener/scorer") and should not be trusted; this
document reflects what the code actually does, verified 2026-09-17.

**Status as of 2026-09-17**: `mad_stop_enabled = True` on the live VPS,
`auto_apply_stop_updates = True`, actively governing 5 real open
positions (DIVISLAB, LAURUSLABS, PAYTM, NYKAA, AUROPHARMA), each with a
live GTT stop-loss order.

---

## 1. What MAD means, and why use it instead of plain ATR

**Median Absolute Deviation** — a robust, outlier-resistant measure of
volatility. A plain standard deviation (or ATR) can be distorted by a
handful of extreme days; MAD instead takes the *median* of
`|price − median|` over a rolling window, so a few wild spikes barely
move it. The idea: a stop-loss band built from MAD should sit closer to
where price "normally" trades, rather than being blown wide by one or
two outlier candles.

## 2. Building the bands

Computed once per symbol over its full history (`precompute_mad_trail()`
in `mad_trail_strategy.py`), every value at row `i` depends only on
rows `[0..i]` — safe to precompute and look up by date later, same
convention as every other indicator in this codebase.

For each day, over a rolling window:

1. **Center line** = rolling median of closing price over
   `mad_stop_med_len` bars (default **21**) — a median, not a moving
   average, for the same outlier-resistance reason as MAD itself.
2. **MAD** = median of `|close − center|` over the trailing
   `mad_stop_mad_len` bars (default **21**), computed causally (each
   day's MAD uses that day's own center, not a fixed one).
3. **Scale factor** = `1.4826` — the standard constant that makes MAD
   comparable to a normal-distribution standard deviation.
4. **Band width** = `MAD × 1.4826 × mad_stop_dev_factor` (default
   `dev_factor = 2.0`), floored at `ATR14 × mad_stop_atr_floor_mult`
   (default `2.0`) — the floor prevents the band from getting
   dangerously tight during an unusually quiet stretch.
5. **Raw lower/upper bands** = `center ∓ width`.

## 3. The one-sided ratchet (the "trail")

The defining mechanic:

- The **lower (support) band only ever rises**, never falls, as long as
  the previous close stayed above it: `lower[i] = max(raw_lower[i],
  lower[i-1])` if `close[i-1] > lower[i-1]`, else it resets to that
  day's raw value.
- The **upper (resistance) band only ever falls**, never rises, as long
  as the previous close stayed below it — mirror logic.

This is exactly a trailing stop, but built from a volatility-adaptive
band instead of a fixed ATR multiple trailing the highest close.

## 4. Regime (bull / bear)

A sticky two-state machine, not re-evaluated from scratch each day:

- Once in **bull** (`regime = +1`), it only flips to **bear** (`-1`)
  when price closes *below* the (ratcheted) lower band.
- Once in **bear**, it only flips back to **bull** when price closes
  *above* the (ratcheted) upper band.

This prevents rapid flip-flopping on small wiggles — the regime only
changes on a genuine band breach.

`flip_bull` / `flip_bear` / `bars_since_bull_flip` are also precomputed
for any caller that wants "how recently did this turn bullish" (e.g. an
entry-freshness filter) without re-scanning history per day.

---

## 5. Where this actually feeds real trading decisions

Two **separate, independently-verified** call sites — confirmed by
reading the code, not inferred from any docstring:

### 5.1 Initial stop on a brand-new buy (live and backtest)

`indicators._suggested_stop()` (called from `compute_snapshot()`, which
`screener.build_technical_table()` calls for every candidate):

```
atr_stop = price - atr_stop_multiple * ATR
if not mad_stop_enabled:
    return atr_stop
if MAD regime is bull AND MAD lower band < price:
    return MAD lower band
return atr_stop   # regime not bullish, or band missing/above price
```

**Live wiring**: `live_rebalance.py`'s `propose_rebalance()` reads this
value directly — `stop = float(row["suggested_stop"])` — and that stop
is what `execute_buys()` places as the real GTT stop-loss order
(`kite_client.place_gtt_stoploss()`) the moment a new position is
opened. This is **not** a display-only field for live callers — it's
the actual stop that goes to the broker.

**Backtest wiring**: `backtest.py`'s own `_initial_stop()` reimplements
the identical rule independently (its own lazy `_get_mad()` cache, not
`compute_snapshot()`'s), so live and backtest can never quietly drift
apart even though they're separate code paths.

### 5.2 Ongoing ratchet for an already-held position

`live_rebalance.py`'s `_trailing_stop_candidate()`, called by
`compute_stop_updates()` every rebalance run for every currently-held
symbol:

```
if mad_stop_enabled:
    return today's MAD lower band  # no regime check, no ATR fallback here
elif trailing_stop_enabled:
    return highest_close_since_entry - trailing_atr_multiple * ATR
else:
    return None  # no ratchet mechanism active
```

Note this differs deliberately from §5.1's initial-stop rule: the
ongoing ratchet does **not** check regime and has **no** ATR fallback —
it just tries the current MAD lower band. This is safe because the
caller (`compute_stop_updates()`) only ever *applies* the result if
it's a genuine rise:

```python
if new_stop <= round(pos["current_stop"], 2):
    continue  # no ratchet this run
```

So even if the MAD band is temporarily below the existing stop (e.g.
regime just flipped bearish), nothing loosens — the position simply
keeps its prior, higher stop until MAD's band rises past it again.

When a genuine ratchet-up occurs and `auto_apply_stop_updates` is on
(default `True`), it's pushed immediately to the real broker GTT via
`kite_client.modify_gtt_trigger()` — same automation level as the
gap-down safety check, since raising a stop is one-directional and
low-risk (it only ever tightens risk, never loosens it or places a new
order).

`mad_stop_enabled` and `trailing_stop_enabled` are mutually exclusive
by construction (`if/elif`, never both) — turning MAD on fully replaces
the plain ATR chandelier trail, it doesn't combine with it.

### 5.3 What does *not* use this

`backtest.py`'s own trading decisions (`_initial_stop()`, the
trailing-ratchet block) use their **own** separate `_get_mad()` lazy
cache — not `compute_snapshot()`'s `suggested_stop` field. That field
is genuinely ranking-display-only *inside a backtest context* (shown
nowhere in the Backtest UI, in fact) — it's only the **live** Screener
page and the **live** `propose_rebalance()` path that read it for a
real decision. This distinction is why the module's blanket
"backtest-only" docstring claim is wrong: true for one of its three
consumers, not the other two.

---

## 6. Config reference

| Key | Default | Meaning |
|---|---|---|
| `mad_stop_enabled` | `False` | Master switch — off means zero behavior change anywhere (plain ATR stop throughout). |
| `mad_stop_med_len` | `21` | Rolling window (bars) for the median center line. |
| `mad_stop_mad_len` | `21` | Rolling window (bars) for the MAD calculation itself. |
| `mad_stop_dev_factor` | `2.0` | Multiplies the scaled MAD to get band width. |
| `mad_stop_atr_floor_mult` | `2.0` | ATR-multiple floor under the band width, so it never gets too tight in a quiet stretch. |

All Admin-editable (`config.ADMIN_EDITABLE_KEYS`), independent of the
plain ATR/trailing-stop settings.

## 7. Validation history

A standalone A/B test (`scripts/run_ema_stop_backtest_local.py`,
referenced in `_initial_stop()`'s docstring) found MAD-stop **raised
CAGR** (33.82% → 36.98%) **and shrank max drawdown at the same time**
over a 5.6-year backtest — a rare case of an improvement on both axes
at once, not a risk/return trade-off.

## 8. Open item

The top-of-file docstring in `mad_trail_strategy.py` still says
"EXPERIMENTAL, backtest-only, not wired into the live screener/scorer."
That was true when originally written; it no longer is (§5.1, §5.2).
Worth correcting so a future reader doesn't draw the same wrong
conclusion this document exists to correct.
