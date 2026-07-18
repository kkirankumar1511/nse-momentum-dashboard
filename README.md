# NSE 3–6 Month Momentum Dashboard

A single-screen decision-support tool for 3–6 month positional trading on NSE:
a research-backed momentum + quality screener fed by Kite (Zerodha) historical
data, an AI agent that pulls fundamentals from the web, live positions and
holdings, and order execution / square-off with GTT stop-losses — all in one
Streamlit page.

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
   premium and, combined with momentum, cuts drawdowns. Gates: ROCE ≥ 15%,
   Debt/Equity ≤ 1, TTM profit growth ≥ 0. The AI agent fetches these from
   the web per stock.

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
   (1% of capital risked per trade).

### Screening rules at a glance

| Layer | Rule |
|---|---|
| Trend gate | Close > 50 EMA > rising; Close > 200 EMA |
| 52-week high gate | Close ≥ 85% of 52-week high |
| RSI gate | 45 ≤ RSI(14) ≤ 78 |
| Quality gate | ROCE ≥ 15%, D/E ≤ 1, TTM profit growth ≥ 0 |
| Score (rank) | 40% RS-6m + 25% RS-3m + 20% 52w-high proximity + 15% volume expansion (z-scores) |
| 🚀 Breakout priority | Recent (≤20d) break above a ~3-year high whose prior peak is ≥6 months old. Score bonus of 0.75 per year of base (capped at 2y); such stocks are flagged `priority` and listed first. Rationale: George & Hwang's 52-week-high effect strengthens at longer highs, and a multi-year base means no overhead supply of trapped sellers (the mechanism behind O'Neil/Minervini base-breakout systems). |
| Sizing | qty = (1% of capital) / (entry − 2.5×ATR stop) |
| Exits | GTT stop at 2.5×ATR; review monthly; exit if stock drops below 200 EMA or falls out of top-half of scores |

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
live trading. They can also be hedged with options if a position sours. Note
a full screen now fetches ~210 symbols from Kite (~75s at the rate limit;
the backtest caches them in `cache/`).

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
ANTHROPIC_API_KEY=...       # optional: enables AI briefs tab
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

1. **Screener tab** — click *Run screen*. Fetches ~50 stocks of daily candles
   from Kite (rate-limited, takes ~30s), scrapes fundamentals, and shows
   ranked candidates that pass every gate, each with a suggested ATR stop.
2. **Positions tab** — live positions, holdings, P&L, today's orders, and a
   confirmed square-off button per symbol.
3. **Trade tab** — pick a symbol; it shows LTP, the ATR stop, and a suggested
   quantity for 1% risk. Orders require an explicit confirmation checkbox.
   Optionally places a GTT stop-loss in the same click.
4. **AI Briefs tab** — Claude (with web search) summarizes recent results,
   catalysts, and red flags per candidate before you commit.

## Suggested workflow for a 3–6 month book

- Run the screen weekly (e.g., Saturday). Enter new names Monday.
- Hold max 8 positions (`max_positions`), equal risk each.
- Rebalance monthly: exit anything that broke its stop, fell below the 200
  EMA, or dropped out of the ranking's top half; replace with new candidates.
- Avoid initiating right before results — check the AI brief's catalysts.

## Notes & limitations

- Historical-data API is rate-limited (~3 req/s); the fetcher sleeps between
  calls.
- screener.in scraping is for personal use; keep the built-in delays and
  respect their terms.
- No backtest module is included; parameters come from the literature, not
  from curve-fitting to recent NSE data — you can add a backtest with the
  same `indicators.py` functions.
- GTT stop-losses are not guaranteed fills (they fire a limit order); gap-down
  risk remains.

## Backtesting

```bash
python backtest.py --synthetic          # verify engine mechanics, no Kite needed
python backtest.py --years 3            # real NSE data via Kite (cached in ./cache)
python backtest.py --years 3 --no-breakout   # A/B: is the breakout tier adding value?
```

Or use the **🧪 Backtest tab** in the dashboard: equity curve vs NIFTY,
metrics side-by-side with/without the breakout tier, drawdown chart, and the
full trade log (win rate split into breakout vs non-breakout trades).

The engine reuses `indicators.py` / `screener.py` verbatim, point-in-time —
so what you backtest is literally the code that screens live. Honest caveats
baked into the design: fundamental gates are disabled historically (can't
reconstruct past ROCE without lookahead), and today's universe carries
survivorship bias, so trust *relative* comparisons (parameters, A/B) more
than absolute CAGR.


## AI Filings Analyst (NSE primary sources)

```bash
python nse_api.py HCLTECH          # announcements, promoter trend, corp actions
python xbrl_parser.py HCLTECH      # quarterly financials parsed from XBRL
python filing_analyst.py HCLTECH   # full AI analysis
python filing_analyst.py HCLTECH --no-ar   # skip annual report PDFs (faster)
```
Or use the **📑 Filings Analyst tab**.

### Why these APIs beat scraping

`/api/integrated-filing-results` returns the company's **XBRL** — the
*as-reported* financials straight from the filing. That is strictly better
than screener.in: primary source, timestamped, carries the audited/unaudited
flag, available the evening results drop. `xbrl_parser.py` parses the Ind-AS
taxonomy (matching several element-name vintages) and correctly ignores
segment-level contexts so you get headline consolidated numbers, not a
business line.

`/api/corporate-share-holdings-master` gives promoter holding **as a time
series** — one of the few genuinely forward-looking free signals. Promoters
raising their stake with their own money, quarter after quarter, is costly
and hard to fake (cf. Jeng, Metrick & Zeckhauser 2003: insider *buying*
predicts returns; selling much less so).

`/api/corporates-corporateActions` matters more than it looks for a momentum
book: an ex-dividend gap is not a breakdown. Don't let a stop fire on a ₹12
ex-dividend gap and call it a trend break.

### The funnel (this is the important part)

An Indian annual report is 200–400 pages. 210 F&O stocks × 5 years ≈ 300,000
pages — sending that to any LLM costs more than most people's trading capital.
So the analyst is the **last stage of a funnel**:

```
210 F&O stocks
  → technical + gate screen (free, seconds)        → ~15-25 names
  → XBRL + announcements + promoter scan (cheap)   → ~10 names
  → AI deep-read of annual reports (expensive)     → final shortlist
```

Within a PDF it does **targeted section extraction** — auditor's report,
MD&A, related-party transactions, contingent liabilities — rather than
dumping the document. Those four sections are where decision-changing
information actually lives. The Filings tab defaults its symbol list to
whatever survived the Screener, so the funnel is the path of least resistance.

### LLM: open-source / local by default

No paid API required. `llm.py` abstracts the provider — set it in `.env`:

| `LLM_PROVIDER` | Cost | Notes |
|---|---|---|
| `ollama` **(default)** | Free | Local. **Your filings never leave your machine.** No rate limits. |
| `llamacpp` / `vllm` | Free | Local OpenAI-compatible servers |
| `groq` | Free tier | Hosted Llama 3.3 70B / Qwen, very fast |
| `openrouter` | Free tier | Look for `:free` model ids |
| `together` | Cheap | Hosted open models |
| `anthropic` | Paid | Optional fallback |

Quickstart (fully free, fully local):
```bash
# 1. Install Ollama from https://ollama.com
ollama pull qwen2.5:14b-instruct
# 2. In .env:
#    LLM_PROVIDER=ollama
python llm.py           # verify the provider is reachable
```

**Picking a model — context length matters more than intelligence.** A full
evidence prompt (12 quarters of XBRL + announcements + two annual reports'
sections) is 20k–40k tokens. 8k-context models (Gemma 2, older Llama)
physically cannot hold it and fall back to map-reduce chunking, which loses
cross-section reasoning — the model never sees the auditor's key-audit-matter
and the receivables spike at the same time. Prefer 32k+: Qwen2.5 (128k),
Llama 3.1/3.3 (128k), Mistral Nemo (128k).

Local hardware guide:

| Model | RAM | Verdict |
|---|---|---|
| `qwen2.5:7b-instruct` | ~5GB | Works; shallow reasoning |
| `qwen2.5:14b-instruct` | ~9GB | **Recommended default** |
| `qwen2.5:32b-instruct` | ~20GB | Noticeably better on financial nuance |
| `llama3.3:70b` | ~40GB | Best local, needs serious hardware |

**JSON reliability is the real problem with small models, not reasoning.**
They emit fences, preambles, trailing commas, single quotes, `None`/`True`,
`<think>` tags, prose containing braces, missing keys, and invented enum
values. `llm.py` defends with native JSON grammar (`format: json` on Ollama,
`response_format` on OpenAI-compatible), balanced-brace extraction that tries
*every* candidate object, a repair pass, schema coercion with fuzzy enum
matching, and retries with escalating strictness. All 12 of those failure
modes are covered by tests.

One honest regression: local models have **no web search**, so `ai_brief`
no longer free-associates about "recent news". It now reads NSE announcements
instead. That's arguably an upgrade — a model recalling half-remembered news
from training data is worse than one reading the company's actual filings.

### Division of labour: deterministic vs AI

**Deterministic code owns the safety-critical checks** — auditor/CFO
resignations, promoter pledging, SEBI actions, insolvency, rating
downgrades, tax-rate anomalies, other-income share of PBT, PAT growing
without revenue. **The AI owns judgement** — is this growth real or
engineered, is it durable, what would falsify the thesis.

The AI **cannot overrule a deterministic red flag**; it can only add context.
A verified example from the test suite: a stock with PAT +28% YoY,
accelerating growth, expanding margins, promoters buying, and a 94/100
fundamental score is still returned as **AVOID** because the CFO resigned.
An LLM that creatively explains away an auditor resignation is an
unacceptable failure mode; a regex that flags one is not.

**This is precisely why open-source models are viable here.** The LLM never
owns a safety-critical decision, so a weaker model degrades the *nuance* of
the analysis rather than letting a blowup through. Verified in the test
suite: with a mocked local model returning "strong" on a company whose
auditor had resigned, the final verdict was still **avoid**. A 14B model on
your laptop is a reasonable choice here in a way it would not be if the model
were the last line of defence.

The analyst outputs: `earnings_real` (real/mixed/engineered), auditor
concerns, `durability`, key risks, catalysts, verdict, **confidence**, and
`what_would_change_my_mind`. If evidence is thin it is instructed to say so
and lower confidence rather than generate narrative.

## Compounder watchlist (the "multibagger" question)

```bash
python compounder_scan.py            # scan the F&O universe
python compounder_scan.py --top 30   # quick partial scan
```
Or use the **🌱 Compounders tab**.

**Read this honestly.** This does not predict multibaggers — nothing does. It
scores stocks on the traits multibaggers demonstrably had *before* they ran,
so you get a research-grounded watchlist instead of a tip sheet.

Three things that must stay front of mind:

1. **Horizon mismatch.** A multibagger (2x–10x) is a 3–7 year outcome. Your
   momentum book is 3–6 months. These are kept in **separate sleeves** on
   purpose: the momentum sleeve has stops and monthly rebalancing; the
   compounder sleeve has neither and makes no timing claim. Do not let one
   contaminate the other — a stopped-out momentum trade is not a failed
   compounder thesis, and vice versa.
2. **F&O is the wrong pond for multibaggers.** F&O eligibility *requires*
   large cap and heavy liquidity. A ₹5,000cr company 10x-ing has happened
   often; a ₹5,00,000cr company 10x-ing would exceed India's entire market
   cap. Within F&O the scanner tilts to the smallest, fastest-growing names,
   but expect low scores — that is the correct answer, not a bug.
3. **Survivorship bias.** Every "multibagger trait" study picks winners in
   hindsight. Thousands of stocks had identical traits and went nowhere or to
   zero. A high score means *read the annual report*, never *buy*.

What it scores (Lynch; Marcellus Coffee Can; Greenblatt Magic Formula;
Fama–French RMW profitability factor):

| Signal | Weight | Why |
|---|---|---|
| Earnings growth **acceleration** (TTM vs 3Y) | 22% | The market re-rates acceleration; PE expansion × earnings growth is the multibagger engine |
| 3Y profit growth | 18% | The compounding base |
| ROCE | 18% | Above cost of capital = reinvestment actually creates value |
| 3Y sales growth | 12% | Real growth, not one-off margin |
| Margin trend | 10% | Operating leverage kicking in |
| Low debt | 8% | Survives downturns without dilution |
| Promoter holding | 6% | Skin in the game |
| Small mcap (runway) | 6% | Room left to compound |

Hard red flags (disqualify regardless of score): promoter pledging >5%, D/E
>1.5, ROCE <10%, PEG >2, promoter holding <25%.

The tab also shows an **⭐ Overlap** view: compounder-shortlist names that
also pass today's momentum gates — a long-term quality story the market is
repricing right now. Trade those with the momentum rules and stops anyway.


## Honest answer on "multibaggers in 3–6 months"

The filings agent makes the fundamental analysis genuinely good — primary
source, earnings-quality detection, promoter accumulation, event risk. It
does not change the arithmetic of the goal:

- **3–6 months**: momentum + quality is the evidence-backed play. That's the
  Screener + Backtest. Realistic aim is beating NIFTY by a few points with
  controlled drawdown — not 2×.
- **1 year**: fundamentals start to dominate. The Filings Analyst's
  `durability` and `earnings_real` fields matter most here.
- **Multibagger (2–10×)**: 3–7 years, and mostly outside F&O, because F&O
  eligibility *requires* large caps. The Compounders tab is the right sleeve
  for that money, and it has no stops and no timing claim.

The confluence view (momentum gates passed **and** filings verdict "strong")
is the closest honest thing to what you asked for: high-quality businesses
the market is repricing right now. Trade them on the 3–6 month rules with
stops. If one turns out to be a multibagger, that will be a 3-year outcome
you happened to enter early — not something the screen predicted.
