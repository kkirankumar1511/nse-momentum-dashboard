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
```

You need a **Kite Connect** developer app (₹2000/month from Zerodha,
https://developers.kite.trade). Set the redirect URL to anything you control
(even `http://127.0.0.1`).

### Daily login (Kite tokens expire every morning)

```bash
python kite_client.py login            # prints login URL — open it, log in
python kite_client.py token <request_token_from_redirect_url>
```

### Run the dashboard

```bash
streamlit run dashboard.py
```

## Using it

The app is a sidebar-navigated set of pages, not a flat row of tabs:

1. **🏠 Cockpit** — everything that matters at a glance: available cash,
   portfolio value, unrealized P&L, open positions vs your cap, a locally
   logged portfolio-value chart (one snapshot per day you open the app), and
   whether the last rebalance scan proposed any action.
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

Run it from the **📡 Live Rebalance** page (with inline, confirmation-gated
execution buttons) or headless on a schedule:

```bash
python live_rebalance.py   # prints the proposal, saves it to cache/, places nothing
```

Stop-losses aren't covered by this job — if you placed a GTT at entry, your
broker already enforces it intraday without this needing to run.
