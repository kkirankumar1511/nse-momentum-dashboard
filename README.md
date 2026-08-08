# NSE Calendar-Entry Momentum Dashboard

A single-screen decision-support tool for 3–6 month positional trading on
NSE: a research-backed momentum + fundamental-quality screener fed by Kite
(Zerodha) historical data, a backtest engine, live positions/holdings, order
execution with GTT stop-losses, and a Stage-1 "propose, review, execute"
live-rebalance workflow — all in one Streamlit app. No AI/LLM anywhere in
this system; every score and gate is deterministic and reproducible.

> This is decision-support software, not investment advice. Momentum
> strategies have well-documented crash risk (e.g., sharp reversals after
> market bottoms — Daniel & Moskowitz 2016). Position sizing and stops are
> built in for exactly that reason. Trade at your own risk.

## The strategy and the research behind it

The 3–6 month holding period is precisely the window where **cross-sectional
momentum** is best documented:

1. **Jegadeesh & Titman (1993), "Returns to Buying Winners and Selling
   Losers", Journal of Finance.** Stocks that outperformed over the past 3–12
   months continue outperforming over the next 3–6 months. This is the core
   engine: we rank on 3-month and 6-month returns, skipping the most recent
   week to avoid the short-term reversal effect (Jegadeesh 1990).

2. **George & Hwang (2004), "The 52-Week High and Momentum Investing",
   Journal of Finance.** Nearness to the 52-week high predicts future returns
   better than raw past returns. Gate: price must be ≥ 85% of its 52-week
   high; proximity also contributes 20% of the composite score.

3. **Asness, Frazzini & Pedersen (2019), "Quality Minus Junk", Review of
   Accounting Studies.** Quality (profitability, safety, growth) earns a
   premium and, combined with momentum, cuts drawdowns. The Fundamentals
   page's primary-source XBRL value score is the quality gate.

4. **Relative strength vs the index.** Ranking on return *minus NIFTY return*
   rather than raw return keeps the screen focused on genuine leadership
   instead of beta (standard practice in the momentum literature; also
   supported by Indian-market studies — Sehgal & Balakrishnan, Vikalpa, who
   confirm momentum profits on NSE).

5. **Volume confirmation.** Lee & Swaminathan (2000, "Price Momentum and
   Trading Volume", JF): momentum is stronger and longer-lived when
   accompanied by volume. 20-day vs 60-day average volume expansion is 15% of
   the score.

6. **Risk management — Daniel & Moskowitz (2016), "Momentum Crashes", JFE.**
   Momentum's biggest weakness is violent reversals. Mitigations built in:
   RSI ceiling (no entries above 78), ATR-based stops (2.5×ATR) placed as
   **GTT orders** so they persist across days, and equal-risk position sizing
   (`risk_per_trade_pct` of capital risked per trade).

### Screening rules at a glance

| Layer | Rule |
|---|---|
| Trend gate | Close > 50 EMA > rising; Close > 200 EMA |
| 52-week high gate | Close ≥ 85% of 52-week high |
| RSI gate | 45 ≤ RSI(14) ≤ 78 |
| Quality gate | Fundamentals page's sector-aware XBRL value score ≥ `min_fundamental_score` |
| Score (rank) | 40% RS-6m + 25% RS-3m + 20% 52w-high proximity + 15% volume expansion (z-scores) |
| Sizing | qty = (`risk_per_trade_pct`% of capital) / (entry − 2.5×ATR stop) |
| Entries | Calendar: any gate-passer fills an open slot the instant it's free — at the monthly rebalance, or immediately when a stop frees a slot mid-month, rather than waiting for the next rebalance |
| Exits | GTT stop at 2.5×ATR; monthly rebalance exits anything below its 200 EMA or outside the top `2×max_positions` ranking |

All parameters live in `config.py → STRATEGY` and are meant to be tuned.

## Universe: NSE F&O stocks only

The universe is fetched live from NSE's own endpoint
(`https://www.nseindia.com/api/underlying-information` → `data.UnderlyingList`),
currently ~210 derivatives-eligible stocks, cached weekly in `cache/`. The
NSE API needs browser-like headers plus homepage cookies — `fno_universe.py`
handles that, and falls back to a bundled snapshot if NSE is unreachable, so
the dashboard never breaks because NSE is down.

```bash
python fno_universe.py            # show current list
python fno_universe.py --refresh  # force re-fetch from NSE
```

Why F&O-only: they're NSE's most liquid names (the exchange's eligibility
rules already screen for market cap and quarter-sigma order size), so
slippage stays low — and slippage is what quietly kills momentum edges in
live trading. They can also be hedged with options if a position sours.

Override the universe in `config.py` via `UNIVERSE_OVERRIDE = [...]`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in your keys
```

`.env`:

```
KITE_API_KEY=...
KITE_API_SECRET=...
KITE_ACCESS_TOKEN=          # filled by the daily login step below
DASHBOARD_USERNAME=...      # optional -- defaults to "Admin"
DASHBOARD_PASSWORD=...      # optional -- defaults to "Admin"
```

**`.env` only bootstraps these once.** The first time the app runs against
an empty `cache/state.db`, these values get seeded into the database (Kite
credentials plaintext since the OAuth exchange needs the real
`api_secret` back; the dashboard password as a salted PBKDF2 hash, never
the password itself) — after that, `state_db.py` is the live source of
truth, and editing `.env` again does nothing. Update credentials going
forward through the dashboard itself: Cockpit's **"Kite API settings"**
expander (if you ever regenerate keys in the developer console) and
**"Change dashboard password"** expander.

You need a **Kite Connect** developer app (₹2000/month from Zerodha,
https://developers.kite.trade). Set the redirect URL to anything you control
(even `http://127.0.0.1` — see the Live automation section below for the
one exception to that: it does have to be HTTPS everywhere except exactly
`127.0.0.1`).

### Daily login (Kite tokens expire every morning)

Easiest: open the dashboard — if the token's missing or expired, it
auto-redirects you straight to Kite's real login + 2FA page and exchanges
the returned token automatically, no copy-pasting. Or via CLI:

```bash
python kite_client.py login            # prints login URL — open it, log in
python kite_client.py token <request_token_from_redirect_url>
```

### Run the dashboard

```bash
streamlit run dashboard.py
```

First launch asks you to sign in (dashboard credentials, not Kite) —
defaults to `Admin`/`Admin`, change it via Cockpit's "Change dashboard
password" section before using this beyond your own machine.

**Remote access (optional)**: to reach the dashboard from your phone
instead of just `127.0.0.1`, Kite requires HTTPS for any non-loopback
address. A private network tool like [Tailscale](https://tailscale.com)
works well for this — it gives your machine a stable private address
reachable from your own enrolled devices only, and its built-in "HTTPS
Certificates" feature (`tailscale cert <your-device>.<tailnet>.ts.net`)
issues a real, publicly-trusted certificate for that private address at
no cost. Copy `.streamlit/config.toml.example` to `.streamlit/config.toml`
and point `sslCertFile`/`sslKeyFile` at the cert Tailscale generates, then
register that same `https://` address as your Kite app's Redirect URL.

## Using it

The app is a sidebar-navigated set of pages, not a flat row of tabs:

1. **🏠 Cockpit** — everything that matters at a glance: available cash,
   portfolio value, unrealized P&L, open positions vs your cap, **initial
   capital and overall return** (auto-captured from Kite's available cash
   the first time it's non-zero — see below), a **full funds breakdown**
   expander (Kite's complete `margins()` response, not just available cash),
   a locally logged portfolio-value chart (one snapshot per day you open the
   app), whether the last rebalance scan proposed any action, and a
   holdings table enriched with each position's entry date, days held,
   current trailing-stop level, and GTT status (✅/❌).
2. **🔍 Screener** — the full ranked universe (every gate-passer, not just
   what fits your open slots), plus a candlestick chart with EMA50/EMA200
   for any symbol.
3. **📡 Live Rebalance** — runs the daily scan, diffs it against your actual
   Kite holdings, and proposes sells (rebalance-rule failures) and buys
   (open slots, sized off real available cash). Running the scan never
   places an order; execution requires an explicit confirmation checkbox.
   Can also run headless on a schedule: `python live_rebalance.py`.
4. **💼 Positions & Trade** — live positions/holdings, P&L, today's orders,
   one-click confirmed square-off, and manual order entry (ATR-based
   suggested sizing, optional GTT stop-loss placed in the same click).
5. **🧪 Backtest** — the exact screener logic replayed point-in-time on 1–5
   years of real Kite data (deep history via chunked fetches — Kite's
   historical API caps a single request at ~2000 days).
6. **📊 Fundamentals** — primary-source XBRL value score across the full
   F&O universe (see below).

## Suggested workflow for a 3–6 month book

- Run the Fundamentals scan periodically (annual filings refresh once a
  year) and the Screener/Live Rebalance scan daily or weekly.
- Hold max `max_positions` positions, equal risk each.
- Rebalance monthly: exit anything that broke its stop, fell below the 200
  EMA, or dropped out of the ranking's top half; replace with new candidates
  — Live Rebalance proposes this diff against your real holdings for you.
- Let a freed slot refill immediately rather than waiting a month — that's
  what the backtest engine and Live Rebalance both already do.

## Notes & limitations

- Kite's historical-data API is rate-limited (~3 req/s); fetchers sleep
  between calls, and "day"-interval requests over ~2000 days are
  transparently split into chunks.
- GTT stop-losses are not guaranteed fills (they fire a limit order); gap-
  down risk remains.
- The fundamental quality gate is off by default in the backtest and opt-in
  (Backtest page checkbox, or `run_backtest(fundamentals_history=...)`).
  When enabled it's genuinely point-in-time — each filing's real broadcast
  timestamp gates whether it counts as "known" as of a given rebalance date
  — not lookahead. It doesn't universally improve results: in one real
  3-year test it produced more trades and slightly lower risk-adjusted
  returns than technical-only, since restricting the candidate pool to
  quality-passing names thins it out and increases rebalance turnover.
  Treat it as a real, tunable lever to test, not an assumed improvement.
- Today's F&O universe is used throughout the backtest window, so stocks
  that fell out of eligibility are invisible (survivorship bias). Treat
  absolute backtest returns as optimistic; parameter-sensitivity comparisons
  are what the tool is reliable for.

## Backtesting

```bash
python backtest.py --synthetic   # verify engine mechanics, no Kite needed
python backtest.py --years 3     # real NSE data via Kite (cached in ./cache)
python backtest.py --years 5     # deep history, chunked Kite fetches
```

Or use the **🧪 Backtest** page: equity curve vs NIFTY, key metrics,
drawdown chart, open positions marked to market at period end (nothing is
force-liquidated), and the full closed-trade log.

The engine reuses `indicators.py` / `screener.py` verbatim, point-in-time —
so what you backtest is literally the code that screens live.

### Sector relative-strength (opt-in)

`sector_universe.py` tilts ranking toward stocks in currently-outperforming
sectors. Most of NSE's ~35 tracked sector indices (Auto, Bank, IT, Energy,
Defence, PSE, Infra, ...) are themselves tradeable Kite instruments with
real historical daily candles — exactly like the NIFTY 50 benchmark — so
sector strength is computed point-in-time with the same
`indicators.relative_strength()` formula already used for every stock's
own momentum, not today's NSE heatmap snapshot applied retroactively.

Sector *classification* (which index is a stock's "sector") comes from
NSE's own ground-truth index-constituent data, not a heatmap scrape or a
hand-curated guess: for each F&O stock, `resolve_sector_profiles()` takes
its highest-weightage membership in a real "SECTORAL INDICES"-category
index (NSE's `allIndices` endpoint's own category label), falling back to
its highest-weightage "THEMATIC INDICES" membership only if it has none
— e.g. WIPRO is a member of both NIFTY IT (sectoral) and NIFTY IND
DIGITAL (thematic), and resolves to NIFTY IT. A handful of THEMATIC
indices are ownership/liquidity/compliance/recency screens rather than
real industries (NIFTY MNC, NIFTY CPSE, NIFTY SHARIAH*, NIFTY IPO, ...)
and are excluded from the fallback so they can't win on weightage alone.
Verified against a 208-symbol F&O resolution CSV, 2026-08-08.

```bash
python sector_universe.py   # rebuild sector profiles, print coverage
```

Off by default (`sector_bonus_weight: 0.0` in `config.py`) — use the
**🧪 Backtest** page's "Include sector relative-strength bonus" checkbox
and "Run A/B: baseline vs sector-aware" button to see the actual effect on
your own data before trusting it. An earlier scoring dimension built the
same way (long-year breakout priority) was A/B tested and found no edge,
and was removed entirely rather than left as unused config — treat this
one with the same scrutiny, not as an assumed improvement.

**Real result on a 3-year Kite backtest (this codebase's own test, not a
guarantee for your window)**: the sector bonus made things *worse* across
the board, monotonically with weight — CAGR 25.89% → 23.10% (weight 1.0) →
22.31% (weight 2.0); Sharpe 1.63 → 1.42 → 1.43; win rate 52.0% → 47.9% →
48.3%; profit factor 2.32 → 2.08 → 1.95. Only max drawdown improved
slightly (-14.98% → -13.53% → -12.43%), not enough to offset the rest.

A 5-year year-by-year breakdown (weight=1.0) shows this isn't uniform,
though: sector-aware actually wins or roughly ties baseline in 3 of 6 years
(2022 +0.25pp, 2025 +1.90pp, 2026 +2.49pp), and 2021/2024 are only modest
losses. The entire multi-year gap comes from **2023 alone** (-34pp), where
tilting toward strong sectors missed a concentrated rally in stocks outside
the leading sectors that year. One bad year dominating the aggregate, not
uniform harm every year — still left off by default for this reason,
re-run the A/B yourself before turning it on.

A market regime filter (pause new entries whenever NIFTY 50 itself fell
below its own 200 EMA / 50 EMA wasn't rising) was also tried, targeting the
same year-by-year gap: rebalance exits carry a ~76% win rate vs 0% for
stops by definition, and the stop-out rate nearly doubles in choppy years
(38-44% vs 21-29%). A real 5-year A/B found the hypothesis didn't hold —
only 1 of the 4 weak years improved, 2025 got notably worse (+9.28%→+2.35%
vs NIFTY's +10.51%), and aggregate CAGR/Alpha/Sharpe all fell (22.51%→
19.35%, 13.60%→10.45%, 1.50→1.43) despite a real drawdown improvement
(-18.06%→-13.56%). NIFTY's own trend health turned out to be a weaker
proxy for individual-stock whipsaw risk than hypothesized, and a binary
index-level gate risks whipsawing through a real chop itself — removed
entirely rather than left as unused config, same as the breakout-bonus
mechanic above.

### Trailing stop (opt-in)

Same underlying diagnostic (rebalance exits carry a ~76% win rate averaging
+15.1%, and winners already run 60-86 days once they survive that long),
approached from a different angle: the stop-loss is set once at entry
(`entry_price - atr_stop_multiple*ATR`) and never moves — confirmed by
inspection, `pos.stop` is only ever read at the daily stop-check and set
once in `try_enter`. A position that runs up 20-30% and then reverses has
to give back nearly the entire gain before the (unmoved, far-below-market)
stop triggers or the next monthly rebalance re-evaluates it.

The trailing stop (`config.STRATEGY["trailing_stop_enabled"]`, off by
default) ratchets each position's stop up daily, chandelier-style:
`highest_close_since_entry - trailing_atr_multiple*ATR`, monotonically
(never back down), using only that day's close — causal, no lookahead,
same decide-off-completed-bars model the rest of the engine already uses.
Uses close (not intraday high) for the running peak, consistent with
`relative_strength`/`momentum_return`/`pct_of_52w_high` elsewhere in this
codebase.

**Real result on a 5-year Kite backtest (2021-2026), swept across trailing
distances**: this is the first of the three ideas tried here (sector bonus,
regime filter, trailing stop) to show a genuine improvement — but it forms
a sharp inverted-U across the ATR multiple, not "wider is always better":

| Multiple | CAGR % | Alpha % | Sharpe | Max DD % | Trades | Final Capital |
|---|---|---|---|---|---|---|
| Baseline | 22.51 | 13.60 | 1.50 | -18.06 | 369 | 27,88,289 |
| 2.5x | 18.72 | 9.82 | 1.48 | -12.99 | 866 | 23,79,461 |
| 3.0x | 21.61 | 12.70 | 1.61 | -12.37 | 694 | 26,86,128 |
| 3.5x | 22.75 | 13.84 | 1.65 | -14.04 | 573 | 28,16,028 |
| **4.0x** | **24.30** | **15.39** | **1.73** | **-14.37** | 504 | **30,00,031** |
| 4.5x | 23.21 | 14.30 | 1.65 | -15.38 | 466 | 28,69,472 |
| 5.0x | 20.92 | 12.01 | 1.49 | -17.26 | 447 | 26,10,562 |
| 6.0x | 21.45 | 12.55 | 1.45 | -17.89 | 404 | 26,69,358 |
| 8.0x | 22.05 | 13.14 | 1.48 | -18.95 | 375 | 27,35,557 |

Too narrow (near the entry stop's own 2.5x) whipsaws out of real winners
early — more than double the trades (866 vs 369) and the worst CAGR of the
sweep. Too wide (6.0x+) barely trails at all, converging back toward
(and at 8.0x, past) baseline drawdown. **4.0x sits at the peak**: CAGR
+1.79pp, Sharpe +0.23, Alpha +1.79pp, and max drawdown improved by 3.7pp —
all better simultaneously, on essentially unchanged profit factor (2.23 vs
2.26). Year-by-year, 5 of 6 years improved or held steady at 4.0x (2021
+4.4pp, 2022 +0.6pp, 2023 +5.4pp, 2025 +1.2pp, 2026 +5.7pp, turning a
-2.02% year into +3.65%); only 2024 gave back some of its exceptional
+50.47% (down to +39.80%, still 4.5x NIFTY's +8.80% that year).

Shipped with `trailing_atr_multiple` defaulting to **4.0** (the identified
sweet spot). Unlike every other factor tried in this section,
`trailing_stop_enabled` now defaults to **True** — this is the one
experimental feature from this round that earned a spot in the production
default strategy ("Baseline + Trailing 4.0x"), everything else here stays
off. This is one 5-year window on today's F&O universe (survivorship bias
applies, see Known limitations in `backtest.py`) — re-run the **🧪
Backtest** page's "Run A/B: baseline vs trailing-stop" button on your own
window before trusting it, and note the peak's exact location may shift
with a different period.

A beta scoring bonus/gate (tilting or hard-filtering toward higher-beta
stocks) was also tried and discarded — same "helps in chop, hurts in
trend" pattern as the sector bonus at every weight tested, and a hard
beta>1 filter was worse still (CAGR 24.60%→20.25%, Sharpe 1.75→1.41, max
drawdown -14.43%→-18.99%). Not kept.

## Fundamentals: primary-source XBRL value score

```bash
python nse_api.py HCLTECH       # announcements, promoter trend, corp actions
python xbrl_parser.py HCLTECH   # quarterly/annual financials parsed from XBRL
```

`fundamentals_agent.fno_value_scan()` (the **📊 Fundamentals** page) computes
a 0-100 score per stock, entirely from the company's own audited XBRL
filings — no scraping, no LLM, cheap enough to run across the whole F&O
universe on every scan.

**Sector-aware scoring.** Banks and NBFCs file under structurally different
XBRL taxonomies — banks don't tag Revenue/Equity/Current Assets at all, and
general-company thresholds would flag every healthy NBFC as over-levered
(NBFCs run 3-6x leverage by design). Each symbol is routed via
`nse_api.filing_taxonomy()` to the rubric matching what its filings actually
contain:

| Rubric | Key metrics |
|---|---|
| `general` | ROE, Debt/Equity, Current Ratio, FCF growth, Revenue CAGR, PEG |
| `banking` | ROE, ROA, NIM proxy, Gross/Net NPA, Advances growth |
| `nbfc` | ROE, ROA, Debt/Equity, Loan-book growth (also covers AMCs per NSE's own filing classification) |
| `general_insurance` | ROE, ROA, Combined Ratio, Incurred Claims Ratio, Premium growth |
| `life_insurance` | ROE, Premium growth, PAT growth |

Missing sub-metrics are dropped from the total, not faked — the Fundamentals
page's "Rows with incomplete data" section shows exactly which pillars were
excluded and why (usually fewer than 2 years of annual filings retrievable
via NSE's endpoint for that name).

`/api/integrated-filing-results` returns the company's **XBRL** — the
*as-reported* financials straight from the filing, strictly better than
scraping a ratios page: primary source, timestamped, carries the
audited/unaudited flag, available the evening results drop.

### Point-in-time fundamentals in the backtest (opt-in)

Every NSE filing carries a real broadcast/publication timestamp distinct
from the period it reports on (a "year ended 31-Mar-2024" result is
typically not public until several weeks into April). `xbrl_parser.py`
tags every annual row with this `known_as_of` timestamp, so the backtest
can apply the fundamental gate *without* lookahead bias — only ever using
what was actually knowable on each historical rebalance date.

```bash
# From Python, or via the Backtest page's "Build/Refresh fundamentals
# history" button + "Include fundamental quality gate" checkbox:
history = fundamentals_agent.build_fundamentals_history(config.UNIVERSE, n_years=5)
res = backtest.run_backtest(candles, bench, fundamentals_history=history)
```

`build_fundamentals_history()` fetches each symbol's full annual history
once (same cost as a Fundamentals-page scan); `score_asof(history, date)`
then filters and scores purely in memory for any historical date, with no
further network calls, at every rebalance. Caveats: PEG is never computed
here (needs a live market price, which doesn't exist for a past date), and
this doesn't universally improve results — see Notes & limitations above.

## Stage 1 live automation: propose, review, execute

`live_rebalance.py` runs the exact same screener pipeline used live and in
the backtest, and diffs it against your **actual Kite holdings** — never
placing an order itself. It proposes:

- **Sells**: current holdings that now fail the rebalance rule (closed below
  the 200 EMA, or dropped out of the top-ranked zone).
- **Buys**: open slots after those sells, filled from gate-passers sized off
  your real available cash.
- **Stop updates**: recommended trailing-stop increases for currently held
  positions (see below) — proposal-only, same as sells/buys.

Run it from the **📡 Live Rebalance** page (with inline, confirmation-gated
execution buttons) or headless on a schedule:

```bash
python live_rebalance.py   # prints the proposal, saves it to cache/, places nothing
```

Proposed buys also show `fundamental_score`/`fundamental_rubric` alongside
the technical `score` — informational context for your own extra
confirmation, not an additional filter (the candidate list is already
gated on `min_fundamental_score` inside `screener.apply_gates`).

### Live trailing-stop updates (opt-in, approval-gated)

The backtest's trailing stop (`backtest.py` step 1b) only ever existed as
in-memory bookkeeping inside one simulation run — live, a GTT stop-loss was
placed once at entry and never revisited. `live_rebalance.py` now tracks
each held position's `entry_date`/`highest_close`/`current_stop` in
`cache/live_position_state.json` (created the moment a buy + GTT succeed,
in both the Live Rebalance page and the Trade tab) and recomputes the
trailing stop daily using the **exact same formula** as the backtest
(`highest_close_since_entry - trailing_atr_multiple*ATR`, ratchets up
only) — so live and backtest logic can't quietly drift apart.

This state file is a cache of *derived* state, never a source of truth: a
GTT can close a position without any of this app's code running, so every
read reconciles against your actual Kite holdings first and prunes stale
entries.

Recommended increases show up as a **"🔼 Recommended stop updates"** section
on the Live Rebalance page — same confirmation-gated pattern as sells/buys,
nothing modifies a live GTT until you explicitly check the box and click
**Apply stop updates** (`kite_client.modify_gtt_trigger`). An
**"⚠️ Unprotected holdings"** check cross-references your holdings against
Kite's own live GTT list (`kite_client.get_active_gtts`) and flags anything
with no active stop-loss — this also catches the case where a buy succeeds
but its GTT placement fails (previously reported as a single misleading
`❌ FAILED` for the whole order; now split into a distinct warning).

### Scheduled scan (opt-in, execution stays manual)

The scan itself — not execution — runs automatically on a Windows Task
Scheduler entry (`NSE-Momentum-DailyRebalanceScan`), weekdays at 4:00 PM
IST, right after market close, so a fresh proposal is always waiting the
next time you open the dashboard instead of needing you to click **Run
today's scan** yourself. It only ever calls `propose_rebalance()` — the
exact same function the dashboard button calls — and execution still
requires the dashboard's per-batch confirm checkbox + button, exactly as
before. Nothing about *placing* an order is automated.

Every run (success or failure) appends a timestamped block to
`cache/live_rebalance_log.txt` — check this first if a day's proposal
looks stale, since a headless scheduled run has no console to watch.

```powershell
# Inspect / modify / remove the scheduled task:
Get-ScheduledTask -TaskName "NSE-Momentum-DailyRebalanceScan"
Get-ScheduledTaskInfo -TaskName "NSE-Momentum-DailyRebalanceScan"   # last/next run time, last result
Unregister-ScheduledTask -TaskName "NSE-Momentum-DailyRebalanceScan" -Confirm:$false   # remove
```

**Important caveat**: Kite Connect access tokens expire daily and require
a manual login each morning (`python kite_client.py login`, then `python
kite_client.py token <request_token>`) — this is a constraint of
Zerodha's Connect API itself, not something this scheduled task can work
around. If the token has expired by 4 PM, the scan fails gracefully and
logs the failure (see `live_rebalance.py`'s `main()`) rather than crashing
silently — check the log if a day's run seems to be missing.

## Running everything as a Windows service (recommended over a manual terminal)

The whole app — dashboard + daily rebalance scan — can run as one Windows
service instead of a `streamlit run dashboard.py` terminal you have to keep
open: it starts automatically on machine reboot, and is controlled with one
command from anywhere (`trading-app ...`, no venv activation needed).

First, install the project itself (once):

```powershell
pip install -e .
```

This registers the `trading-app` command (via `trading_service.py`) and
pulls in `pywin32`. Everything below needs an **Administrator** command
prompt or PowerShell window (standard for any Windows service):

```powershell
trading-app install     # registers the service, sets auto-start on boot
trading-app start       # or: net start NSEMomentumTradingApp
trading-app status      # service state + is the dashboard port listening + last scan time
trading-app stop        # or: net stop NSEMomentumTradingApp
trading-app restart
trading-app remove      # unregisters the service entirely
```

Internally the service:
- launches `streamlit run dashboard.py` as a child process (auto-restarted
  if it ever crashes), and
- runs the exact same daily rebalance scan (`python live_rebalance.py`) at
  4:00 PM IST on weekdays, logged to `cache/service_log.txt` /
  `cache/service_rebalance_stdout.txt` / `cache/service_dashboard_stdout.txt`
  since a service has no visible console to watch.

**This duplicates the standalone `NSE-Momentum-DailyRebalanceScan` Task
Scheduler entry above** — once the service is installed and running, remove
that task so the scan doesn't run twice a day:

```powershell
Unregister-ScheduledTask -TaskName "NSE-Momentum-DailyRebalanceScan" -Confirm:$false
```

To opt out of auto-start-on-boot, pass `--startup` *before* the verb (pywin32's
argument parser requires options first): `trading-app --startup manual install`.
