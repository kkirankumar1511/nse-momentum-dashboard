"""
NSE Calendar-Entry Momentum Cockpit (Streamlit)

Run:  streamlit run dashboard.py

Pages (sidebar navigation):
  Overview          - everything that matters at a glance: cash, portfolio
                      value, realized/unrealized P&L, XIRR, open positions,
                      today's pending actions
  Screener          - full ranked universe (all gate-passers, not just what
                      fits your open slots), plus a symbol chart
  Live Rebalance    - run the daily scan, review proposed sells/buys, execute
  Positions & Trade - live holdings/positions, square-off, manual order entry
  Backtest          - calendar-entry engine on real Kite data, 1-5 years
  Fundamentals      - primary-source XBRL value score, all F&O stocks
  Admin             - dashboard password, Kite API settings, deposit/
                      withdrawal ledger

Single strategy: calendar-entry momentum (buy the instant a slot opens,
monthly rebalance + daily stop checks). No AI/LLM anywhere in this app.
"""

from __future__ import annotations

import datetime as dt
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import backtest as bt
import config
import fundamentals_agent as fa
import indicators
import kite_client
import live_rebalance as lr
import screener
import sector_universe as su
from background_jobs import clear_background_job, get_background_job, start_background_job


# Windows' asyncio ProactorEventLoop logs a spurious traceback whenever a
# client's TCP connection resets mid-request (e.g. a mobile browser over
# Tailscale losing signal) -- _call_connection_lost's socket.shutdown() raises
# ConnectionResetError on a socket the peer already reset. It's cosmetic
# noise, not a crash (Streamlit/Tornado already handles a disconnected client
# through its own websocket layer).
#
# A per-loop exception handler (asyncio.get_event_loop().set_exception_
# handler(...)) does NOT work here: Streamlit re-executes this script inside
# a per-session ScriptRunner worker thread, not the thread that owns the
# actual server event loop where the connection-lost callback fires --
# asyncio.get_event_loop() in that worker thread either raises RuntimeError
# (Python 3.12+, no loop set for a non-main thread) or, if it didn't, would
# still only patch a throwaway loop nobody's connections use. Patching the
# transport class method directly works regardless of which thread applies
# it, since the class is one shared object across every thread. Guarded by
# a sentinel attribute so re-running this script (every Streamlit rerun)
# doesn't stack another wrapper on top of the last one.
if os.name == "nt":
    from asyncio.proactor_events import _ProactorBasePipeTransport
    if not getattr(_ProactorBasePipeTransport, "_connection_reset_silenced", False):
        _orig_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost

        def _call_connection_lost_quietly(self, exc=None):
            try:
                _orig_call_connection_lost(self, exc)
            except ConnectionResetError:
                pass

        _ProactorBasePipeTransport._call_connection_lost = _call_connection_lost_quietly
        _ProactorBasePipeTransport._connection_reset_silenced = True
import state_db

st.set_page_config(page_title="KK Trading System", layout="wide", page_icon="📈")

def _redirect_to_kite_login(error: str | None = None) -> None:
    """Auto-redirects the browser to Zerodha's real login + 2FA page --
    same tab, no click needed (HTML meta-refresh, immediate). Since this
    app's redirect URL is registered as this dashboard's own address,
    completing login there lands back on this app with request_token in
    its query params, which the block below auto-exchanges. A visible
    fallback link is also shown in case a browser/extension blocks the
    auto-redirect. `error` is only set for a genuine unexpected failure
    (e.g. token exchange itself failing) -- a routine expired/missing
    token redirects silently, no alarming red banner for something that
    happens every single day by design."""
    url = kite_client.login_url()
    if error:
        st.error(error)
    else:
        st.info("Kite session expired — redirecting to login...")
    st.markdown(f'<meta http-equiv="refresh" content="0; url={url}">',
               unsafe_allow_html=True)
    st.caption("Opens Zerodha's real login + 2FA page. Nothing here ever "
              "sees your password — only the one-time token Kite sends "
              f"back after you log in. Not redirected automatically? "
              f"[Click here]({url}).")
    st.stop()


# ---------------------------------------------------------------------------
# Kite request_token exchange -- MUST run before the dashboard login gate
# below, not after. An external OAuth redirect (leaving to Zerodha's site
# and back) tears down and recreates the browser's Streamlit session, which
# resets st.session_state -- so if the dashboard login gate ran first, it
# would always intercept the return trip and show the sign-in form again,
# with the one-time request_token sitting unprocessed in the URL (and lost,
# since it's single-use, the moment anything else consumes this page load).
# Checking it here, first, means it gets exchanged immediately regardless
# of dashboard-session state.
#
# Guarded on session_state["_kite_token_exchanged_for"] == request_token,
# NOT on `not config.KITE_ACCESS_TOKEN` (a prior version's guard) -- that
# earlier guard broke the very first time this server process stayed alive
# across Kite's daily token expiry (~6 AM): config.KITE_ACCESS_TOKEN held
# yesterday's now-expired token, still a non-empty string, so `not
# config.KITE_ACCESS_TOKEN` was False and a genuinely fresh request_token
# from a brand-new login got silently discarded without ever being
# exchanged -- landing back on this app's own sign-in form with no valid
# Kite session, looking like a broken redirect.
#
# Which specific token was already exchanged stays in session_state, and an
# external OAuth redirect (leaving to Zerodha and back) tears down and
# recreates st.session_state anyway -- so a genuinely new request_token from
# a fresh Kite login always arrives in a fresh session_state with no
# matching "_kite_token_exchanged_for" entry, and gets exchanged regardless
# of whatever's cached in config.KITE_ACCESS_TOKEN.
# What this guard still protects against: the same already-used
# request_token lingering in the URL within the SAME session (over an
# unstable mobile/Tailscale connection, st.query_params.clear() can fail to
# propagate to the browser's actual address bar before the next
# interaction fires) -- that case DOES still have the matching session_state
# entry from the successful exchange moments earlier, so it's correctly
# skipped instead of retried (Kite tokens are single-use; retrying would
# fail and bounce back to Kite's login page for no reason).
# ---------------------------------------------------------------------------
request_token = st.query_params.get("request_token")
if request_token and request_token != st.session_state.get("_kite_token_exchanged_for"):
    try:
        token = kite_client.exchange_request_token(request_token)
        config.KITE_ACCESS_TOKEN = token
        st.session_state["_kite_token_exchanged_for"] = request_token
        # Completing Kite's own login+2FA on Zerodha's site is at least as
        # strong a proof of identity as this app's own password -- and by
        # definition, only someone who'd already passed the dashboard login
        # gate in some browser session could have reached the "Login to
        # Kite" redirect in the first place. So a successful exchange here
        # also authenticates this (fresh, redirect-reset) session directly,
        # rather than depending on a cookie surviving the external round
        # trip to prove the same thing less reliably.
        st.session_state["dashboard_authenticated"] = True
        st.query_params.clear()
        st.rerun()
    except Exception as e:
        st.query_params.clear()
        _redirect_to_kite_login(f"Token exchange failed (request_token is "
                                f"single-use and may have already been used): {e}")
elif request_token:
    st.query_params.clear()

# ---------------------------------------------------------------------------
# Dashboard login gate. A real credential check backs it: state_db.
# dashboard_auth stores a salted PBKDF2-HMAC-SHA256 hash, never the
# password itself -- seeded once from config.DASHBOARD_USERNAME/PASSWORD
# (which still default to the "Admin"/"Admin" placeholder in .env, but
# only ever used to seed the hash on first run, never compared against
# directly afterward). This app places real orders and shows real fund
# balances, so change the password via the Cockpit's "Change dashboard
# password" section before using this beyond your own machine -- a loud
# warning shows until you do.
#
# Note this session resets on a full external page reload (leaving to
# Kite's login page and back tears down and recreates it, Streamlit's
# design, unrelated to cookies) -- see the request_token block above,
# which re-authenticates that fresh session directly on a successful
# exchange rather than depending on this form again.
# ---------------------------------------------------------------------------
state_db.ensure_dashboard_auth_seeded(config.DASHBOARD_USERNAME, config.DASHBOARD_PASSWORD)

if not st.session_state.get("dashboard_authenticated", False):
    st.title("🔒 KK Trading System — sign in")
    with st.form("login_form", clear_on_submit=True):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        if state_db.verify_dashboard_login(u, p):
            st.session_state["dashboard_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect username or password.")
    st.stop()

# ---------------------------------------------------------------------------
# Kite connection health check -- only reached after the dashboard login
# above succeeds. Shows the Kite login redirect ONLY when the token is
# actually missing/expired; a still-valid token falls straight through to
# the normal dashboard pages below.
# ---------------------------------------------------------------------------
if not config.KITE_ACCESS_TOKEN:
    _redirect_to_kite_login()

try:
    margins = kite_client.get_margins()
    available_cash = margins["equity"]["available"]["live_balance"]
except Exception:
    _redirect_to_kite_login()

SCREEN_CACHE = os.path.join("cache", "screen.pkl")
VALUE_SCORE_CACHE = os.path.join("cache", "fno_value_scores.pkl")
BACKTEST_CACHE = os.path.join("cache", "backtest_result.pkl")
FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _pnl_color(val) -> str:
    """Green for gains, red for losses -- used on every P&L-shaped column
    (pnl, ret_pct, unrealized_*, Alpha %, ...) across the whole app so a
    losing row is never just a plain number next to a winning one."""
    if pd.isna(val):
        return ""
    if val > 0:
        return "color: #16a34a; font-weight: 600"
    if val < 0:
        return "color: #dc2626; font-weight: 600"
    return ""


def colored_metric(label: str, value: float, fmt: str = "₹{:,.0f}") -> None:
    """A st.metric substitute for headline P&L-shaped numbers. st.metric
    only ever colors its DELTA text green/red, never the main value --
    which meant Realized/Unrealized/Total P&L sat there in plain,
    sign-less text no different from a loss to a gain. Same green/red
    convention as _pnl_color/pnl_style (#16a34a / #dc2626), rendered as
    markdown since no dataframe/delta is involved here. Only the number
    is colored, not the label, so it still reads fine in both light and
    dark themes."""
    color = "#16a34a" if value >= 0 else "#dc2626"
    st.markdown(
        f"<div style='font-size:0.875rem; opacity:0.75; margin-bottom:0.1rem'>{label}</div>"
        f"<div style='font-size:1.75rem; font-weight:600; color:{color}; "
        f"line-height:1.2'>{fmt.format(value)}</div>",
        unsafe_allow_html=True)


def _status_color(val) -> str:
    """Open positions get a green badge, closed a neutral gray -- used on
    the Tradebook's status column so open/closed reads at a glance instead
    of as plain text next to everything else."""
    if val == "open":
        return "background-color: #16a34a; color: white; font-weight: 600; border-radius: 4px"
    if val == "closed":
        return "background-color: #6b7280; color: white; font-weight: 600; border-radius: 4px"
    return ""


# Raw/snake_case field name -> human-readable table header, applied by
# pnl_style() (and readable_df() for the few tables that don't need P&L
# coloring or number formatting) so no table in this app ever shows a
# header like `pnl_pct` or `tradingsymbol`. One shared dict rather than a
# per-page copy, since the same fields (symbol, qty, entry/exit price, P&L,
# ATR-based stops...) repeat across Overview, Live Rebalance, Positions &
# Trade, Backtest, and Fundamentals.
COLUMN_LABELS = {
    "symbol": "Symbol", "tradingsymbol": "Symbol",
    "qty": "Qty", "quantity": "Qty",
    "avg_price": "Avg price", "average_price": "Avg price",
    "ltp": "LTP", "last_price": "LTP",
    "pnl": "P&L", "pnl_pct": "P&L %",
    "entry_date": "Entry date", "exit_date": "Exit date",
    "entry_price": "Entry price", "exit_price": "Exit price",
    "current_price": "Current price",
    "current_stop": "Current stop", "recommended_stop": "Recommended stop",
    "suggested_stop": "Suggested stop", "stop": "Stop",
    "gtt_active": "GTT active", "gtt_trigger_id": "GTT trigger ID",
    "days_held": "Days held", "holding_days": "Days held",
    "source": "Source", "reason": "Reason",
    "product": "Product", "status": "Status",
    "transaction_type": "Type", "order_timestamp": "Time",
    "unrealized_pnl": "Unrealized P&L", "unrealized_ret_pct": "Unrealized return %",
    "ret_pct": "Return %",
    "date": "Date", "amount": "Amount (₹)", "note": "Note",

    # Screener / momentum
    "score": "Score", "price": "Price", "rs_3m": "RS 3M", "rs_6m": "RS 6M",
    "pct_52w_high": "% of 52W high", "rsi": "RSI",
    "vol_expansion": "Vol expansion", "atr_pct": "ATR %",
    "fundamental_score": "Fundamental score", "fundamental_rubric": "Sector rubric",
    "trend_ok": "Trend OK", "near_high_ok": "Near high OK", "rsi_ok": "RSI OK",
    "quality_ok": "Quality OK", "quality_fails": "Quality fails",

    # Fundamentals (Value Score)
    "total_score": "Score (0-100)", "rubric": "Sector", "roe": "ROE %",
    "roa": "ROA %", "debt_to_equity": "Debt / equity",
    "current_ratio": "Current ratio", "revenue_cagr_pct": "Revenue CAGR %",
    "fcf_yoy_pct": "FCF growth %", "peg": "PEG ratio",
    "gross_npa_pct": "Gross NPA %", "net_npa_pct": "Net NPA %",
    "nim_proxy_pct": "NIM (approx.) %", "advances_yoy_pct": "Advances growth %",
    "pat_yoy_pct": "Profit growth %", "combined_ratio_pct": "Combined ratio %",
    "incurred_claim_ratio_pct": "Claims ratio %",
    "premium_yoy_pct": "Premium growth %", "loan_yoy_pct": "Loan book growth %",
    "fiscal_year_end": "As of", "missing_pillars": "Data gaps",

    # Job execution log
    "job_type": "Job", "trigger_type": "Trigger", "started_at": "Started",
    "finished_at": "Finished", "duration_sec": "Duration (s)",
    "summary": "Summary", "error_message": "Error",

    # Tradebook
    "initial_stop": "Initial stop", "entry_score": "Entry score",
    "entry_rsi": "Entry RSI", "entry_pct_52w_high": "Entry % of 52W high",
    "entry_vol_expansion": "Entry vol expansion",
    "entry_fundamental_score": "Entry fundamental score",
    "exit_reason": "Exit reason", "entry_reason": "Why this trade",
    "latest_recommended_stop": "Latest stop (daily 8:30AM calc)",
    "extra_qty": "Extra qty",
    "trigger_price": "GTT trigger price", "updated_at": "Last updated",
    "apply_error": "Why it needs attention",
    "realized_pnl": "Realized P&L",
    "realized_ret_pct": "Realized return %",
}


def readable_df(df: pd.DataFrame) -> pd.DataFrame:
    """Renames every column present in COLUMN_LABELS to its human-readable
    label. Columns not in the dict (already-readable, author-chosen labels
    like a yearly-performance table's "Strategy %") pass through unchanged."""
    return df.rename(columns=COLUMN_LABELS)


def pnl_style(df: pd.DataFrame, cols: list[str] | None = None, fmt: dict | None = None,
             na_rep: str = "—"):
    """Renames columns via readable_df(), then applies optional numeric
    formatting and green/red P&L coloring -- the one function every styled
    table in this app goes through. `cols`/`fmt` are given in the ORIGINAL
    (pre-rename) field names -- remapped internally, so call sites don't
    need to track the renamed labels themselves."""
    df = readable_df(df)
    cols = [COLUMN_LABELS.get(c, c) for c in (cols or []) if COLUMN_LABELS.get(c, c) in df.columns]
    fmt = {COLUMN_LABELS.get(k, k): v for k, v in (fmt or {}).items()}
    styler = df.style
    if fmt:
        styler = styler.format(fmt, na_rep=na_rep)
    if cols:
        styler = styler.map(_pnl_color, subset=cols)
    return styler


def merged_holdings() -> pd.DataFrame:
    """Positions + holdings merged into one live table with current P&L.
    A same-day CNC buy can transiently appear in both Kite endpoints until it
    settles into holdings overnight -- acceptable for an at-a-glance cockpit
    view, not used anywhere money actually moves."""
    pos = kite_client.get_positions()
    hold = kite_client.get_holdings()
    rows = []
    if not pos.empty and "quantity" in pos.columns:
        for _, r in pos[pos["quantity"] != 0].iterrows():
            rows.append({"symbol": r["tradingsymbol"], "qty": r["quantity"],
                        "avg_price": r["average_price"], "ltp": r["last_price"],
                        "pnl": r["pnl"], "source": "position"})
    if not hold.empty and "quantity" in hold.columns:
        for _, r in hold[hold["quantity"] > 0].iterrows():
            rows.append({"symbol": r["tradingsymbol"], "qty": r["quantity"],
                        "avg_price": r["average_price"], "ltp": r["last_price"],
                        "pnl": r["pnl"], "source": "holding"})
    return pd.DataFrame(rows)


def log_equity_snapshot(value: float, invested_amount: float | None = None) -> pd.DataFrame:
    """Upserts today's portfolio value (and cost basis, for the chart
    overlay), so the Cockpit can chart account growth over time -- Kite
    has no such history endpoint for a specific strategy's slice of the
    account. See state_db.py."""
    return state_db.log_equity_snapshot(value, invested_amount)


def _annualized_returns(portfolio_value: float,
                        equity_log: pd.DataFrame) -> tuple[float | None, float | None]:
    """Returns (current_year_xirr, overall_xirr) as fractions (0.12 = 12%),
    or None for either if there isn't enough data yet to annualize.

    Overall XIRR: the full cash_flows ledger (deposits negative, i.e. money
    going in; withdrawals positive) plus today's portfolio value as a final,
    hypothetical-liquidation cash flow.

    Current-year XIRR mirrors backtest.py's yearly_performance() pattern
    (the prior available snapshot's value as this year's start value, not
    inflated by starting exactly at the first snapshot of a partial year):
    the equity_log's last value before this year, or the earliest snapshot
    this year if the log doesn't go back further, seeds the series alongside
    this year's cash flows and today's value."""
    cash_flows = state_db.get_cash_flows()
    today = dt.date.today()
    year_start = dt.date(today.year, 1, 1)

    overall_series = [(dt.date.fromisoformat(r["date"]), -float(r["amount"]))
                      for _, r in cash_flows.iterrows()]
    overall_series.append((today, portfolio_value))
    overall = indicators.xirr(sorted(overall_series))

    start_date = start_value = None
    if not equity_log.empty:
        log = equity_log.copy()
        log["date"] = pd.to_datetime(log["date"]).dt.date
        before_year = log[log["date"] < year_start]
        if not before_year.empty:
            start_date = before_year.iloc[-1]["date"]
            start_value = float(before_year.iloc[-1]["value"])
        else:
            start_date = log["date"].iloc[0]
            start_value = float(log["value"].iloc[0])

    current_year = None
    if start_date is not None and start_date < today:
        this_year_series = [(start_date, -start_value)]
        for _, r in cash_flows.iterrows():
            d = dt.date.fromisoformat(r["date"])
            if start_date < d <= today:
                this_year_series.append((d, -float(r["amount"])))
        this_year_series.append((today, portfolio_value))
        current_year = indicators.xirr(sorted(this_year_series))

    return current_year, overall


# ---------------------------------------------------------------------------
# Page: Overview
# ---------------------------------------------------------------------------

def page_cockpit():
    st.subheader("🏠 Overview")
    st.caption("Calendar-entry momentum system — everything that matters, at a glance.")

    merged = merged_holdings()
    invested_amount = float((merged["qty"] * merged["avg_price"]).sum()) if not merged.empty else 0.0
    holdings_value = float((merged["qty"] * merged["ltp"]).sum()) if not merged.empty else 0.0
    unrealized_pnl = float(merged["pnl"].sum()) if not merged.empty else 0.0
    portfolio_value = available_cash + holdings_value

    log = log_equity_snapshot(portfolio_value, invested_amount)
    state_db.ensure_first_cash_flow_captured(available_cash)
    realized_pnl = state_db.get_realized_pnl()
    total_pnl = realized_pnl + unrealized_pnl
    current_xirr, overall_xirr = _annualized_returns(portfolio_value, log)

    unrealized_pct = (unrealized_pnl / invested_amount * 100) if invested_amount else None
    pct_deployed = (invested_amount / portfolio_value * 100) if portfolio_value else None
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Invested amount", f"₹{invested_amount:,.0f}",
             help="Cost basis of everything you currently hold (qty × avg. buy "
                  "price) -- how much cash is actually tied up in stocks right now.")
    k2.metric("Current holdings value", f"₹{holdings_value:,.0f}",
             delta=(f"₹{unrealized_pnl:+,.0f} ({unrealized_pct:+.1f}%)"
                    if unrealized_pct is not None else None),
             help="Same holdings, marked to today's price -- the delta is your "
                  "unrealized P&L vs the invested amount.")
    k3.metric("Available cash", f"₹{available_cash:,.0f}",
             help="Sitting uninvested -- not in any stock right now.")
    k4.metric("Total capital", f"₹{portfolio_value:,.0f}",
             help="Available cash + current holdings value -- everything the "
                  "strategy has to work with.")
    k5.metric("% deployed", f"{pct_deployed:.0f}%" if pct_deployed is not None else "—",
             help="Invested amount ÷ total capital -- how much of your money is "
                  "actually working vs sitting idle as cash. Naturally lower "
                  "while max_positions is small relative to your account size, "
                  "or right after a sell before the next buy fills the slot.")

    k6, k7, k8, k9 = st.columns(4)
    with k6:
        colored_metric("Realized P&L", realized_pnl)
    with k7:
        colored_metric("Unrealized P&L", unrealized_pnl)
    with k8:
        colored_metric("Total P&L", total_pnl)
    k9.metric("Open positions", f"{len(merged)} / {config.STRATEGY['max_positions']}")

    k10, k11 = st.columns(2)
    with k10:
        st.caption("Annualized return (XIRR) — this year")
        if current_xirr is not None:
            colored_metric("", current_xirr * 100, fmt="{:+.1f}%")
        else:
            st.markdown("—")
    with k11:
        st.caption("Annualized return (XIRR) — overall")
        if overall_xirr is not None:
            colored_metric("", overall_xirr * 100, fmt="{:+.1f}%")
        else:
            st.markdown("—")
    if overall_xirr is None:
        st.caption("XIRR needs at least one logged deposit and one portfolio "
                  "value snapshot — log a deposit on the Admin page (or fund "
                  "the account) to start tracking annualized return.")

    if len(log) > 1:
        plot_log = log.copy()
        plot_log["date"] = pd.to_datetime(plot_log["date"])
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=plot_log["date"], y=plot_log["value"], name="Total capital (₹)",
            mode="lines", line=dict(color="#16a34a", width=2),
            fill="tozeroy", fillcolor="rgba(22,163,74,0.08)",
            hovertemplate="₹%{y:,.0f}<extra>Total capital</extra>"))
        if plot_log["invested_amount"].notna().any():
            fig.add_trace(go.Scatter(
                x=plot_log["date"], y=plot_log["invested_amount"],
                name="Invested amount (₹)", mode="lines",
                line=dict(color="#6b7280", width=1.5, dash="dot"),
                hovertemplate="₹%{y:,.0f}<extra>Invested amount</extra>"))
        fig.update_layout(
            height=380, margin=dict(l=10, r=10, t=10, b=10),
            hovermode="x unified",
            xaxis=dict(rangeslider=dict(visible=True), rangeselector=dict(buttons=[
                dict(count=1, label="1m", step="month", stepmode="backward"),
                dict(count=6, label="6m", step="month", stepmode="backward"),
                dict(count=1, label="YTD", step="year", stepmode="todate"),
                dict(count=1, label="1y", step="year", stepmode="backward"),
                dict(step="all", label="All"),
            ])),
            yaxis=dict(tickprefix="₹", separatethousands=True),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
        st.plotly_chart(fig, width="stretch")
        if plot_log["invested_amount"].isna().any():
            st.caption("Invested amount only started being logged recently — "
                      "earlier days show a gap until enough history builds up.")
    else:
        st.caption("Portfolio value is logged once a day when you open this page — "
                  "the chart builds up over time as you keep using the dashboard.")

    with st.expander("Full funds breakdown (from Kite margins API)"):
        def _breakdown_table(section: dict) -> pd.DataFrame:
            rows = []
            for k, v in section.items():
                try:
                    v = f"{float(v):,.2f}"
                except (TypeError, ValueError):
                    v = str(v)
                rows.append({"Metric": k.replace("_", " ").title(), "Value": v})
            return pd.DataFrame(rows)

        try:
            m = kite_client.get_margins()["equity"]
            fc1, fc2 = st.columns(2)
            with fc1:
                st.write("**Available**")
                st.dataframe(_breakdown_table(m["available"]),
                            width="stretch", hide_index=True)
            with fc2:
                st.write("**Utilised**")
                st.dataframe(_breakdown_table(m["utilised"]),
                            width="stretch", hide_index=True)
        except Exception as e:
            st.warning(f"Could not fetch funds breakdown: {e}")

    st.divider()
    st.subheader("Action needed today")
    prop = state_db.get_last_rebalance_run()
    if prop is not None:
        age_hr = (dt.datetime.now() - prop["run_time"]).total_seconds() / 3600
        n_sell = len(prop["sells"])
        n_buy = len(prop["buys"])
        n_stop = len(prop.get("stop_updates", pd.DataFrame()))
        if n_sell or n_buy or n_stop:
            st.warning(f"**{n_sell} sell(s), {n_buy} buy(s), {n_stop} stop "
                      f"update(s) proposed** (scan run {age_hr:.1f}h ago).")
        else:
            st.success(f"No action needed (scan run {age_hr:.1f}h ago).")
    else:
        st.info("No rebalance scan run yet.")
    st.page_link(page_live_rebalance_p, label="Go to Live Rebalance →", icon="📡")

    st.divider()
    st.subheader("Holdings")
    if merged.empty:
        st.caption("No open positions or holdings.")
    else:
        held = set(merged["symbol"])
        stale = state_db.get_stale_open_symbols(held)
        exit_prices = kite_client.get_ltp(stale) if stale else {}
        state = state_db.reconciled_positions(held, exit_prices)
        merged["entry_date"] = merged["symbol"].map(
            lambda s: state.get(s, {}).get("entry_date", "—"))
        merged["days_held"] = merged["symbol"].map(
            lambda s: (dt.date.today() - dt.date.fromisoformat(state[s]["entry_date"])).days
            if s in state else None)
        merged["current_stop"] = merged["symbol"].map(
            lambda s: state.get(s, {}).get("current_stop"))
        merged["gtt_active"] = merged["symbol"].map(
            lambda s: "✅" if state.get(s, {}).get("gtt_trigger_id") else "❌")
        st.dataframe(
            pnl_style(merged.sort_values("pnl", ascending=False), ["pnl"],
                     {"avg_price": "{:.2f}", "ltp": "{:.2f}", "pnl": "{:,.0f}",
                      "current_stop": "{:.2f}"}, na_rep="—"),
            width="stretch", hide_index=True)

        st.caption("'Current stop'/'GTT active' above are this app's own "
                  "tracked values.")
        st.page_link(page_positions_trade_p,
                    label="Full GTT details + actions →", icon="🛑")

# ---------------------------------------------------------------------------
# Page: Admin
# ---------------------------------------------------------------------------

def page_admin():
    st.subheader("⚙️ Admin")
    st.caption("Dashboard/Kite settings, the deposit/withdrawal ledger used "
              "for XIRR, and strategy configuration.")

    if state_db.is_using_default_dashboard_password(config.DASHBOARD_USERNAME, config.DASHBOARD_PASSWORD):
        st.warning(
            "⚠️ Using the default Admin/Admin login — this app places real "
            "orders and shows real fund balances. Change it in the "
            "\"Change dashboard password\" section below before using this "
            "beyond your own machine."
        )

    st.subheader("💰 Deposits / withdrawals")
    st.caption("Kite's API has no visibility into bank transfers, so each "
              "deposit/withdrawal is logged here manually — this ledger is "
              "what makes the Overview page's XIRR figures accurate once "
              "you start adding money over time, instead of just a single "
              "starting-capital snapshot.")
    with st.form("cash_flow_form", clear_on_submit=True):
        cf_date = st.date_input("Date", value=dt.date.today())
        cf_amount = st.number_input(
            "Amount (₹) — positive for a deposit, negative for a withdrawal",
            step=1000.0, format="%.2f")
        cf_note = st.text_input("Note (optional)")
        cf_submitted = st.form_submit_button("Log cash flow")
    if cf_submitted:
        if cf_amount == 0:
            st.error("Amount can't be zero.")
        else:
            state_db.record_cash_flow(cf_date.isoformat(), float(cf_amount), cf_note)
            st.success("Logged.")
            st.rerun()

    ledger = state_db.get_cash_flows()
    if ledger.empty:
        st.caption("No cash flows logged yet.")
    else:
        st.dataframe(
            pnl_style(ledger.sort_values("date", ascending=False), fmt={"amount": "{:,.0f}"}),
            width="stretch", hide_index=True)

    st.divider()
    st.subheader("🎯 Strategy configuration")
    st.caption("Stored in state.db (`strategy_config` table) — takes effect "
              "immediately for this dashboard process (no restart needed), "
              "and for the next scheduled/manual rebalance scan. See the "
              "README for the research behind each default.")
    cfg = config.STRATEGY
    with st.form("strategy_config_form"):
        st.markdown("**Portfolio & risk**")
        c1, c2, c3 = st.columns(3)
        max_positions = c1.number_input(
            "Portfolio size (max open positions)", min_value=1, max_value=50,
            value=int(cfg["max_positions"]), step=1)
        risk_per_trade_pct = c2.number_input(
            "Risk per trade (% of capital)", min_value=0.1, max_value=10.0,
            value=float(cfg["risk_per_trade_pct"]), step=0.1)
        atr_stop_multiple = c3.number_input(
            "Initial stop (× ATR)", min_value=0.5, max_value=10.0,
            value=float(cfg["atr_stop_multiple"]), step=0.1)

        st.markdown("**Trailing stop**")
        c4, c5 = st.columns(2)
        trailing_stop_enabled = c4.checkbox(
            "Enabled", value=bool(cfg["trailing_stop_enabled"]))
        trailing_atr_multiple = c5.number_input(
            "Trailing stop (× ATR)", min_value=0.5, max_value=10.0,
            value=float(cfg["trailing_atr_multiple"]), step=0.1)

        st.markdown("**Automation**")
        c4b, c4c = st.columns(2)
        auto_apply_stop_updates = c4b.checkbox(
            "Auto-apply trailing-stop ratchets", value=bool(cfg["auto_apply_stop_updates"]),
            help="Push a ratcheted stop straight to the real broker GTT as "
                 "soon as it's computed, instead of waiting for a manual "
                 "'Apply stop updates' click. Low-risk (only ever tightens "
                 "an existing stop) -- on by default.")
        auto_execute_trades = c4c.checkbox(
            "Auto-execute sells/buys/top-ups", value=bool(cfg["auto_execute_trades"]),
            help="Have the SCHEDULED daily rebalance job place proposed "
                 "sells/buys/top-ups as real orders automatically, with no "
                 "confirmation step (never affects the dashboard's manual "
                 "'Run today's scan' button, which always stays "
                 "review-first). Off by default -- this deploys new "
                 "capital and exits real positions, so it's a deliberate "
                 "opt-in once you trust the proposal quality.")

        st.markdown("**Momentum & trend**")
        c6, c7, c8 = st.columns(3)
        mom_lookback_days_short = c6.number_input(
            "Momentum lookback — short (days)", min_value=5, max_value=252,
            value=int(cfg["mom_lookback_days_short"]), step=1)
        mom_lookback_days_long = c7.number_input(
            "Momentum lookback — long (days)", min_value=5, max_value=504,
            value=int(cfg["mom_lookback_days_long"]), step=1)
        skip_recent_days = c8.number_input(
            "Skip most recent (days)", min_value=0, max_value=30,
            value=int(cfg["skip_recent_days"]), step=1)
        c9, c10, c11 = st.columns(3)
        near_high_threshold = c9.number_input(
            "52-week-high proximity (%)", min_value=50.0, max_value=100.0,
            value=float(cfg["near_high_threshold"]) * 100, step=1.0,
            help="Price must be at least this % of its 52-week high to qualify.")
        ema_fast = c10.number_input(
            "EMA (fast)", min_value=5, max_value=100,
            value=int(cfg["ema_fast"]), step=1)
        ema_slow = c11.number_input(
            "EMA (slow)", min_value=50, max_value=400,
            value=int(cfg["ema_slow"]), step=1)

        st.markdown("**RSI & volume**")
        c12, c13, c14 = st.columns(3)
        rsi_min = c12.number_input(
            "RSI min", min_value=0, max_value=100, value=int(cfg["rsi_min"]), step=1)
        rsi_max = c13.number_input(
            "RSI max", min_value=0, max_value=100, value=int(cfg["rsi_max"]), step=1)
        volume_expansion_min = c14.number_input(
            "Min volume expansion (20d/60d)", min_value=0.0, max_value=5.0,
            value=float(cfg["volume_expansion_min"]), step=0.1)

        st.markdown("**Fundamental gate & sector bonus (opt-in features)**")
        fundamental_gate_enabled = st.checkbox(
            "Filter candidates on fundamental score (Live Rebalance + Screener)",
            value=bool(cfg["fundamental_gate_enabled"]),
            help="On by default (5-year A/B, equal-weight sizing, "
                 "max_positions=10): trades ~2pp CAGR for a real ~4pp max "
                 "drawdown reduction (-28.65%->-24.47%) — see config.py's "
                 "comment for the full year-by-year breakdown. The "
                 "fundamental score still shows for every candidate "
                 "wherever fundamentals data is fetched regardless of this "
                 "toggle, for your own reference.")
        c15, c16, c17, c18 = st.columns(4)
        min_fundamental_score = c15.number_input(
            "Min fundamental score (0-100)", min_value=0.0, max_value=100.0,
            value=float(cfg["min_fundamental_score"]), step=1.0,
            help="Only enforced when the checkbox above is on.")
        fundamental_bonus_weight = c16.number_input(
            "Fundamental score ranking tilt", min_value=0.0, max_value=2.0,
            value=float(cfg["fundamental_bonus_weight"]), step=0.1,
            help="0 = off (gate only, no ranking effect). 5-year A/B "
                 "(equal-weight sizing, max_positions=10) found an "
                 "inverted-U peaking near 0.5 -- CAGR 43.70%->43.03%, "
                 "Sharpe 1.62->1.64, max drawdown -24.61%->-20.30%. "
                 "Anything above 0.5 tested worse across the board.")
        sector_bonus_weight = c17.number_input(
            "Sector bonus weight", min_value=0.0, max_value=1.0,
            value=float(cfg["sector_bonus_weight"]), step=0.05,
            help="0 = off. Backtested to underperform as of this default; "
                 "see README's Sector relative-strength section.")
        history_days = c18.number_input(
            "Candle history fetched (days)", min_value=300, max_value=3000,
            value=int(cfg["history_days"]), step=100)

        strategy_submitted = st.form_submit_button("Save strategy settings", type="primary")

    if strategy_submitted:
        updates = {
            "max_positions": int(max_positions),
            "risk_per_trade_pct": float(risk_per_trade_pct),
            "atr_stop_multiple": float(atr_stop_multiple),
            "trailing_stop_enabled": bool(trailing_stop_enabled),
            "trailing_atr_multiple": float(trailing_atr_multiple),
            "auto_apply_stop_updates": bool(auto_apply_stop_updates),
            "auto_execute_trades": bool(auto_execute_trades),
            "mom_lookback_days_short": int(mom_lookback_days_short),
            "mom_lookback_days_long": int(mom_lookback_days_long),
            "skip_recent_days": int(skip_recent_days),
            "near_high_threshold": float(near_high_threshold) / 100,
            "ema_fast": int(ema_fast),
            "ema_slow": int(ema_slow),
            "rsi_min": int(rsi_min),
            "rsi_max": int(rsi_max),
            "volume_expansion_min": float(volume_expansion_min),
            "fundamental_gate_enabled": bool(fundamental_gate_enabled),
            "min_fundamental_score": float(min_fundamental_score),
            "fundamental_bonus_weight": float(fundamental_bonus_weight),
            "sector_bonus_weight": float(sector_bonus_weight),
            "history_days": int(history_days),
        }
        state_db.update_strategy_config(updates)
        config.STRATEGY.update(updates)  # live for this process -- no restart needed
        st.success("Strategy settings saved — in effect immediately.")

    st.divider()
    st.subheader("🚫 Skip stocks from scanner")
    st.caption("Manually excluded symbols are removed from `config.UNIVERSE` "
              "— the shared candidate list the Screener, Live Rebalance, "
              "and backtest.py all fetch candles for — so a skip here takes "
              "effect everywhere at once. Tick/untick Skip, optionally edit "
              "the reason, then click Update — nothing changes until you do.")

    _skipped_df = state_db.get_skipped_symbols_df()
    skipped_reasons = (_skipped_df.set_index("symbol")["reason"] if not _skipped_df.empty
                      else pd.Series(dtype=str))
    all_syms_for_skip = sorted(set(config.UNIVERSE) | set(skipped_reasons.index))
    try:
        skip_prices = kite_client.get_ltp(all_syms_for_skip)
    except Exception as e:
        st.warning(f"Couldn't fetch live prices: {e}")
        skip_prices = {}

    skip_table = pd.DataFrame({
        "symbol": all_syms_for_skip,
        "price": [skip_prices.get(s) for s in all_syms_for_skip],
        "skip": [s in skipped_reasons.index for s in all_syms_for_skip],
        "reason": [skipped_reasons.get(s, "") for s in all_syms_for_skip],
    })
    edited_skip_table = st.data_editor(
        skip_table, hide_index=True, width="stretch", key="skip_table_editor",
        disabled=["symbol", "price"],
        column_config={
            "symbol": st.column_config.TextColumn("Symbol"),
            "price": st.column_config.NumberColumn("LTP (₹)", format="₹%.2f"),
            "skip": st.column_config.CheckboxColumn("Skip?"),
            "reason": st.column_config.TextColumn("Reason (optional)"),
        })
    if st.button("Update skip list", type="primary", key="skip_update_btn"):
        changed = 0
        for _, row in edited_skip_table.iterrows():
            sym, reason = row["symbol"], row["reason"] or ""
            currently_skipped = sym in skipped_reasons.index
            if row["skip"]:
                if not currently_skipped or skipped_reasons.get(sym, "") != reason:
                    state_db.add_skipped_symbol(sym, reason)
                    changed += 1
            elif currently_skipped:
                state_db.remove_skipped_symbol(sym)
                changed += 1
        config.refresh_universe()
        st.success(f"Updated {changed} symbol(s) — in effect immediately." if changed
                  else "No changes to apply.")
        st.rerun()

    st.divider()
    with st.expander("🔑 Change dashboard password"):
        st.caption("Stored as a salted hash in state.db — the password "
                  "itself is never saved anywhere, not even here.")
        with st.form("change_password_form", clear_on_submit=True):
            new_user = st.text_input("Username", value=config.DASHBOARD_USERNAME)
            new_pw = st.text_input("New password", type="password")
            confirm_pw = st.text_input("Confirm new password", type="password")
            change_submitted = st.form_submit_button("Update credentials")
        if change_submitted:
            if not new_user or not new_pw:
                st.error("Username and password can't be empty.")
            elif new_pw != confirm_pw:
                st.error("Passwords don't match.")
            else:
                state_db.update_dashboard_password(new_user, new_pw)
                st.success("Credentials updated — use the new username/password "
                          "next time you sign in.")

    with st.expander("🔑 Kite API settings"):
        st.caption("Stored in state.db, not .env — only needed if you "
                  "regenerate keys in the Kite developer console. Unlike "
                  "the dashboard password, these are kept plaintext (Kite's "
                  "own login flow needs the real api_secret value back), "
                  "so this is a convenience move, not a security upgrade.")
        masked_key = (config.KITE_API_KEY[:4] + "…" + config.KITE_API_KEY[-4:]
                     if len(config.KITE_API_KEY) > 8 else "(not set)")
        st.caption(f"Current API key: `{masked_key}`")
        with st.form("kite_api_settings_form", clear_on_submit=True):
            new_api_key = st.text_input("New API key (leave blank to keep current)")
            new_api_secret = st.text_input("New API secret (leave blank to keep current)",
                                           type="password")
            api_submitted = st.form_submit_button("Update Kite API credentials")
        if api_submitted:
            if not new_api_key and not new_api_secret:
                st.error("Enter at least one value to update.")
            else:
                state_db.update_kite_api_credentials(
                    new_api_key or config.KITE_API_KEY,
                    new_api_secret or config.KITE_API_SECRET)
                st.success("Kite API credentials updated — restart the "
                          "dashboard for this process to pick them up.")


# ---------------------------------------------------------------------------
# Page: Screener
# ---------------------------------------------------------------------------

def page_screener():
    st.subheader("🔍 Screener — full ranked universe")
    st.caption(
        "Every F&O stock passing the technical gates (trend structure, "
        "52-week-high proximity, RSI regime) and the fundamental quality "
        "gate, ranked by momentum score. This is the broader browse/chart "
        "view; Live Rebalance shows only what actually fits your open "
        "position slots."
    )

    if "screen" not in st.session_state and os.path.exists(SCREEN_CACHE):
        st.session_state["screen"] = pd.read_pickle(SCREEN_CACHE)
        st.session_state["screen_time"] = dt.datetime.fromtimestamp(
            os.path.getmtime(SCREEN_CACHE))
        st.session_state["screen_is_cached"] = True

    def _run_and_cache_screen(with_fund, fundamentals, progress_cb):
        result = screener.run_screen(with_fund, fundamentals=fundamentals,
                                     progress_cb=progress_cb)
        os.makedirs("cache", exist_ok=True)
        result.to_pickle(SCREEN_CACHE)
        return result

    screen_job = get_background_job("screen_run")
    screen_running = screen_job is not None and not screen_job["done"]

    colA, colB = st.columns([1, 3])
    with colA:
        with_fund = st.checkbox(
            "Fetch fundamental score", value=True,
            help="Shows each candidate's fundamental score/sector rubric for "
                 "reference (uses the Fundamentals page's primary-XBRL score, "
                 "reusing the on-disk cache if present rather than re-scanning "
                 "NSE). Whether it also FILTERS candidates is a separate "
                 "toggle — see Admin → Strategy configuration → 'Filter "
                 "candidates on fundamental score' (off by default).")
        if screen_running:
            st.info(f"⏳ Scan running since {screen_job['started_at']:%H:%M:%S} — safe to switch tabs.")
        if st.button("Run screen", type="primary", disabled=screen_running):
            start_background_job(
                "screen_run", _run_and_cache_screen, with_fund,
                st.session_state.get("value_scores"), job_type="screen_run",
                summarize_fn=lambda r: f"{len(r)} candidates")
            st.rerun()

    @st.fragment(run_every="1s" if screen_running else None)
    def _screen_job_status():
        job = get_background_job("screen_run")
        if job is None:
            return
        if not job["done"]:
            frac, stage = job["progress"]
            st.progress(frac, text=f"{stage} — started {job['started_at']:%H:%M:%S}, "
                              "keeps running even if you switch tabs")
            return
        if job["error"]:
            st.error(f"Scan failed: {job['error']}")
        else:
            st.session_state["screen"] = job["result"]
            st.session_state["screen_time"] = dt.datetime.now()
            st.session_state["screen_is_cached"] = False
        clear_background_job("screen_run")
        st.rerun()

    _screen_job_status()

    if "screen" not in st.session_state:
        st.info("Click **Run screen** to fetch Kite data and rank the universe.")
        return

    t: pd.DataFrame = st.session_state["screen"]
    cached_note = " 📁 (from cache — click Run screen to refresh)" \
        if st.session_state.get("screen_is_cached") else ""
    st.caption(f"Last run: {st.session_state['screen_time']:%d %b %Y %H:%M}{cached_note}")

    candidates = t[t["all_gates"]].copy()  # already sorted by score, descending
    candidates.insert(0, "rank", range(1, len(candidates) + 1))
    show_cols = ["rank", "score", "price", "rs_3m", "rs_6m", "pct_52w_high", "rsi",
                "vol_expansion", "atr_pct", "suggested_stop",
                "fundamental_score", "fundamental_rubric"]
    show_cols = [c for c in show_cols if c in candidates.columns]
    if "fundamental_score" not in t.columns:
        # apply_gates() only attaches these two columns at all when it was
        # given non-empty fundamentals -- silently absent otherwise, which
        # used to look like a bug rather than a consequence of the checkbox
        # (or this exact result being cached from a run where it was off).
        st.caption("ℹ️ No fundamental score column — this result was scanned "
                  "with **Include fundamental quality gate** off, or "
                  "fundamentals data wasn't available at scan time. Check the "
                  "box above and click **Run screen** again to include it.")
    # fundamental_rubric is a string column ("general"/"nbfc"/...) and rank is
    # already a plain int -- a single global "{:.2f}" format spec would crash
    # on the former and add pointless decimals to the latter.
    num_fmt = {c: "{:.2f}" for c in show_cols if c not in ("fundamental_rubric", "rank")}

    keep_zone_size = config.STRATEGY["max_positions"] * 2
    st.subheader(f"✅ Candidates passing all gates ({len(candidates)})")
    st.caption(f"Sorted by score, highest first — Live Rebalance keeps a held "
              f"position only while it's ranked in the top {keep_zone_size} "
              f"here (max_positions × 2); dropping below that rank is what "
              f"triggers a proposed sell.")
    st.dataframe(
        pnl_style(candidates[show_cols].rename_axis("Symbol"), fmt=num_fmt),
        width="stretch")

    with st.expander("Full universe (including gate failures)"):
        all_cols = show_cols + ["trend_ok", "near_high_ok", "rsi_ok",
                                "quality_ok", "quality_fails"]
        all_cols = [c for c in all_cols if c in t.columns]
        st.dataframe(readable_df(t[all_cols].rename_axis("Symbol")), width="stretch")

    st.divider()
    sym = st.selectbox("Chart a symbol", list(t.index))
    if sym:
        df = kite_client.fetch_daily_candles(sym, days=config.STRATEGY["history_days"])
        if not df.empty:
            cfg = config.STRATEGY
            fig = go.Figure()
            fig.add_trace(go.Candlestick(
                x=df.index, open=df["open"], high=df["high"],
                low=df["low"], close=df["close"], name=sym))
            fig.add_trace(go.Scatter(
                x=df.index, y=indicators.ema(df["close"], cfg["ema_fast"]),
                name="EMA50", line=dict(width=1)))
            fig.add_trace(go.Scatter(
                x=df.index, y=indicators.ema(df["close"], cfg["ema_slow"]),
                name="EMA200", line=dict(width=1)))
            fig.update_layout(height=500, xaxis_rangeslider_visible=False,
                              margin=dict(l=10, r=10, t=30, b=10))
            st.plotly_chart(fig, width="stretch")

    st.divider()
    with st.expander("🏭 Current sector rankings"):
        st.caption(
            "Today's relative strength (vs NIFTY 50, same lookback as the "
            "6-month momentum score) for every tracked sector index — "
            "separate from the Backtest page's sector bonus, this is just "
            "for browsing which sectors are currently strong. Own button "
            "since it needs its own ~35 index fetches on top of the "
            "candles already fetched by Run screen.")
        if st.button("Fetch current sector rankings"):
            with st.spinner("Fetching sector membership + index history..."):
                membership = su.get_sector_membership()
                days = config.STRATEGY["history_days"]
                sector_candles = su.fetch_sector_index_candles(days=days)
                bench_sec = kite_client.benchmark_candles(days)
                rank = su.sector_rs_asof(
                    sector_candles, bench_sec, dt.date.today(),
                    config.STRATEGY["sector_rs_lookback_days"])
            st.session_state["sector_rank"] = rank
            st.session_state["sector_rank_time"] = dt.datetime.now()
        if "sector_rank" in st.session_state:
            rt = st.session_state["sector_rank_time"]
            st.caption(f"Last fetched: {rt:%d %b %Y %H:%M}")
            st.dataframe(
                st.session_state["sector_rank"].rename("Relative strength (pct pts)")
                  .to_frame().style.format("{:.2f}"),
                width="stretch")


# ---------------------------------------------------------------------------
# Page: Live Rebalance
# ---------------------------------------------------------------------------

def page_live_rebalance():
    auto_exec = bool(config.STRATEGY.get("auto_execute_trades", False))
    st.subheader(
        "📡 Live Rebalance — review, then execute" if not auto_exec
        else "📡 Live Rebalance — auto-executing",
        help="Runs the exact same screener pipeline as Screener/Backtest, "
             "diffs it against your actual Kite holdings, and proposes "
             "sells (closed below 200 EMA, or dropped out of the "
             "top-ranked zone), buys (open slots, sized off your real "
             "available cash), and trailing-stop updates. Can also be "
             "scheduled externally via `python live_rebalance.py`.")
    if auto_exec:
        st.warning(
            "**auto_execute_trades is ON** — the scheduled daily scan places "
            "these sells/buys/top-ups as real orders automatically, with no "
            "confirmation step. The buttons below still work as a manual "
            "override for whatever's left (e.g. a manual 'Run today's scan'). "
            "Turn this off in Admin → Strategy configuration to go back to "
            "manual-only.")
    else:
        st.info(
            "Manual mode — running the scan never places an order by "
            "itself. Execution below requires an explicit confirmation "
            "checkbox per batch. Enable **auto_execute_trades** in "
            "Admin → Strategy configuration to have the scheduled daily "
            "scan place these automatically instead.")

    if "rebalance_proposal" not in st.session_state:
        last_run = state_db.get_last_rebalance_run()
        if last_run is not None:
            # holdings is never persisted (see state_db.get_last_rebalance_run
            # -- it's a live snapshot, not historical), so always re-fetch it
            # fresh here regardless of when the underlying proposal ran.
            last_run["holdings"] = lr.get_live_holdings().reset_index().rename(
                columns={"tradingsymbol": "symbol"})
            st.session_state["rebalance_proposal"] = last_run

    rebalance_job = get_background_job("rebalance_run")
    rebalance_running = rebalance_job is not None and not rebalance_job["done"]

    if rebalance_running:
        st.info(f"⏳ Scan running since {rebalance_job['started_at']:%H:%M:%S} — safe to switch tabs.")
    if st.button("Run today's scan", type="primary", disabled=rebalance_running):
        fundamentals = st.session_state.get("value_scores")
        start_background_job(
            "rebalance_run", lr.propose_rebalance, available_cash,
            fundamentals=fundamentals, job_type="rebalance_scan",
            summarize_fn=lambda r: (f"{len(r['buys'])} buys, {len(r['sells'])} sells, "
                                    f"{len(r['stop_updates'])} stop updates"))
        st.rerun()

    @st.fragment(run_every="1s" if rebalance_running else None)
    def _rebalance_job_status():
        job = get_background_job("rebalance_run")
        if job is None:
            return
        if not job["done"]:
            frac, stage = job["progress"]
            st.progress(frac, text=f"{stage} — started {job['started_at']:%H:%M:%S}, "
                              "keeps running even if you switch tabs")
            return
        if job["error"]:
            st.error(f"Scan failed: {job['error']}")
        else:
            st.session_state["rebalance_proposal"] = job["result"]
        clear_background_job("rebalance_run")
        st.rerun()

    _rebalance_job_status()

    if "rebalance_proposal" not in st.session_state:
        st.info("Click **Run today's scan** to generate a proposal.")
        return

    result = st.session_state["rebalance_proposal"]
    st.caption(f"Last run: {result['run_time']:%d %b %Y %H:%M}")
    if rebalance_running:
        st.warning("⏳ A new scan is running — actions below are locked until "
                  "it finishes, so you can't execute against this now-stale "
                  "proposal while a fresh one is being computed.")

    rc1, rc2, rc3 = st.columns(3)
    rc1.metric("Current holdings", len(result["holdings"]))
    rc2.metric("Proposed sells", len(result["sells"]))
    rc3.metric("Open slots after sells", result["open_slots"])

    if result.get("cash_shortfall") is not None:
        target = result.get("target_per_slot") or 0
        pool = result.get("cash_pool") or 0
        shortfall = result.get("cash_shortfall") or 0
        if shortfall > 0:
            st.warning(
                f"💰 **₹{shortfall:,.0f} more needed** to fully equal-weight every "
                f"open slot and under-target holding (target ₹{target:,.0f}/slot, "
                f"₹{pool:,.0f} available including proposed sell proceeds) — "
                "buys below may be partial or fewer than ideal until more cash "
                "is added.")
        else:
            st.success(
                f"✅ Enough cash (₹{pool:,.0f} available including proposed sell "
                f"proceeds) to fully equal-weight every open slot and "
                f"under-target holding at ₹{target:,.0f}/slot.")
        unsettled = result.get("unsettled_proceeds") or 0
        if unsettled:
            st.caption(
                f"ℹ️ ₹{unsettled:,.0f} of today's sell proceeds is from a "
                "same-day position or T1 (BTST) holding — already excluded "
                "from the available figure above, since Zerodha won't treat "
                "it as usable cash until that settlement cycle completes "
                "(next trading day for T1, the day after for a same-day sale).")

    st.subheader(f"🔴 Proposed sells ({len(result['sells'])})")
    if st.session_state.get("sell_exec_log"):
        st.success("Sell(s) executed and removed from the list below.")
        for line in st.session_state["sell_exec_log"]:
            st.write(line)
        del st.session_state["sell_exec_log"]
    if not result["sells"].empty:
        st.dataframe(pnl_style(result["sells"], fmt={"avg_price": "{:.2f}"}),
                    width="stretch", hide_index=True)
        confirm_sell = st.checkbox(
            "I confirm I want to execute ALL proposed sells at market",
            key="confirm_sell_all")
        if st.button("Execute all sells", disabled=not confirm_sell or rebalance_running):
            log, succeeded = lr.execute_sells(result["sells"])
            st.session_state["sell_exec_log"] = log
            if succeeded:
                result["sells"] = result["sells"][
                    ~result["sells"]["symbol"].isin(succeeded)].reset_index(drop=True)
                st.session_state["rebalance_proposal"] = result
            st.rerun()
    else:
        st.caption("No current holdings fail the rebalance rule today.")

    st.subheader(f"🟢 Proposed buys ({len(result['buys'])})")
    if st.session_state.get("buy_exec_log"):
        st.success("Buy(s) executed and removed from the list below.")
        for line in st.session_state["buy_exec_log"]:
            st.write(line)
        del st.session_state["buy_exec_log"]
    if not result["buys"].empty:
        st.caption("Fundamental score/sector rubric are shown for your own "
                  "reference, not as a filter — the candidate list above is "
                  "technicals-only unless you've turned on 'Filter candidates "
                  "on fundamental score' in Admin → Strategy configuration.")
        st.dataframe(
            pnl_style(result["buys"], fmt={
                "price": "{:.2f}", "stop": "{:.2f}", "score": "{:.2f}",
                "fundamental_score": "{:.1f}"}),
            width="stretch", hide_index=True)
        place_gtt = st.checkbox("Also place a GTT stop-loss for each buy",
                                value=True, key="rebal_gtt")
        confirm_buy = st.checkbox(
            "I confirm I want to execute ALL proposed buys at market",
            key="confirm_buy_all")
        if st.button("Execute all buys", disabled=not confirm_buy or rebalance_running):
            log, succeeded = lr.execute_buys(result["buys"], place_gtt=place_gtt)
            st.session_state["buy_exec_log"] = log
            if succeeded:
                result["buys"] = result["buys"][
                    ~result["buys"]["symbol"].isin(succeeded)].reset_index(drop=True)
                st.session_state["rebalance_proposal"] = result
            st.rerun()
    else:
        st.caption("No open slots, or no candidates today.")

    top_ups = result.get("top_ups", pd.DataFrame())
    st.subheader(f"⬆️ Proposed top-ups ({len(top_ups)})")
    if st.session_state.get("topup_exec_log"):
        st.success("Top-up(s) executed and removed from the list below.")
        for line in st.session_state["topup_exec_log"]:
            st.write(line)
        del st.session_state["topup_exec_log"]
    if not top_ups.empty:
        st.caption(
            "Additional shares for positions you already hold that are below "
            "their equal-weight target, funded by cash left over after the "
            "buys above — the position's existing stop-loss carries over "
            "unchanged, only its GTT quantity gets updated to cover the new "
            "total.")
        st.dataframe(
            pnl_style(top_ups, fmt={"price": "{:.2f}"}),
            width="stretch", hide_index=True)
        confirm_topup = st.checkbox(
            "I confirm I want to execute ALL proposed top-ups at market",
            key="confirm_topup_all")
        if st.button("Execute all top-ups", disabled=not confirm_topup or rebalance_running):
            log, succeeded = lr.execute_top_ups(top_ups)
            st.session_state["topup_exec_log"] = log
            if succeeded:
                result["top_ups"] = top_ups[
                    ~top_ups["symbol"].isin(succeeded)].reset_index(drop=True)
                st.session_state["rebalance_proposal"] = result
            st.rerun()
    else:
        st.caption("No under-target holdings, or no cash left over to top up with.")

    stop_updates = result.get("stop_updates", pd.DataFrame())
    st.subheader(f"🔼 Stop updates needing attention ({len(stop_updates)})")
    if st.session_state.get("stopupdate_exec_log"):
        st.success("Stop update(s) applied and removed from the list below.")
        for line in st.session_state["stopupdate_exec_log"]:
            st.write(line)
        del st.session_state["stopupdate_exec_log"]
    if not stop_updates.empty:
        st.caption("The trailing stop ratchets up automatically as soon as it's "
                  "computed (same as the gap-down safety check) — the rows "
                  "below are ones that **couldn't** auto-apply (no active GTT, "
                  "or the update to Kite failed) and need your attention.")
        st.dataframe(
            pnl_style(stop_updates, fmt={"current_stop": "{:.2f}", "recommended_stop": "{:.2f}"}),
            width="stretch", hide_index=True)
        confirm_stops = st.checkbox(
            "I confirm I want to raise ALL these GTT stop-losses",
            key="confirm_stop_updates")
        if st.button("Apply stop updates", disabled=not confirm_stops or rebalance_running):
            log = []
            succeeded = []
            for _, r in stop_updates.iterrows():
                if pd.isna(r["gtt_trigger_id"]):
                    log.append(f"⚠️ {r['symbol']}: no active GTT to update — "
                              "place one manually first (Trade tab).")
                    continue
                try:
                    ltp = kite_client.get_ltp([r["symbol"]])[r["symbol"]]
                    kite_client.modify_gtt_trigger(
                        int(r["gtt_trigger_id"]), r["symbol"], int(r["qty"]),
                        r["recommended_stop"], ltp)
                    # Only now does the recommended stop become the applied
                    # (real, broker-side) stop -- see apply_stop_update()'s
                    # docstring for why this must never happen earlier.
                    state_db.apply_stop_update(r["symbol"])
                    log.append(f"✅ {r['symbol']}: stop raised to "
                              f"₹{r['recommended_stop']:.2f}")
                    succeeded.append(r["symbol"])
                except Exception as e:
                    log.append(f"❌ {r['symbol']}: FAILED — {e}")
            st.session_state["stopupdate_exec_log"] = log
            if succeeded:
                result["stop_updates"] = stop_updates[
                    ~stop_updates["symbol"].isin(succeeded)].reset_index(drop=True)
                st.session_state["rebalance_proposal"] = result
            st.rerun()
    else:
        st.caption("No trailing-stop increases needed attention today — "
                  "either nothing ratcheted, or it all auto-applied cleanly.")

    st.caption("For GTT coverage/details and a full trade history, see:")
    lc1, lc2 = st.columns(2)
    with lc1:
        st.page_link(page_positions_trade_p,
                    label="GTT / Stop-Loss Management →", icon="🛑")
    with lc2:
        st.page_link(page_tradebook_p, label="Tradebook →", icon="📒")


# ---------------------------------------------------------------------------
# Page: Positions & Trade
# ---------------------------------------------------------------------------

def page_positions_trade():
    st.subheader("💼 Positions, Holdings & Trade")
    cfg = config.STRATEGY

    refresh_choice = st.selectbox(
        "Auto-refresh positions/holdings from Kite", ["Off", "10s", "30s", "1m", "5m"],
        index=2, key="pt_refresh_interval",
        help="Only the positions/holdings tables below refresh on this timer -- "
             "the rest of this page (square-off, orders, trade forms) isn't "
             "affected, so nothing you're typing gets reset by it.")
    run_every = None if refresh_choice == "Off" else refresh_choice

    @st.fragment(run_every=run_every)
    def _live_positions_holdings():
        st.markdown("**Open positions**")
        live_pos = kite_client.get_positions()
        if live_pos.empty or (live_pos.get("quantity") == 0).all():
            st.caption("No open positions.")
        else:
            live = live_pos[live_pos["quantity"] != 0]
            st.dataframe(
                pnl_style(
                    live[["tradingsymbol", "quantity", "average_price",
                         "last_price", "pnl", "product"]],
                    ["pnl"], {"average_price": "{:.2f}", "last_price": "{:.2f}",
                             "pnl": "{:,.0f}"}),
                width="stretch", hide_index=True)
            st.metric("Total position P&L", f"₹{live['pnl'].sum():,.0f}")

        st.markdown("**Holdings (CNC)**")
        live_hold = kite_client.get_holdings()
        if live_hold.empty:
            st.caption("No holdings.")
        else:
            live_hold = live_hold.copy()
            live_hold["pnl_pct"] = ((live_hold["last_price"] / live_hold["average_price"]) - 1) * 100
            st.dataframe(
                pnl_style(
                    live_hold[["tradingsymbol", "quantity", "average_price",
                              "last_price", "pnl", "pnl_pct"]],
                    ["pnl", "pnl_pct"],
                    {"average_price": "{:.2f}", "last_price": "{:.2f}",
                     "pnl": "{:,.0f}", "pnl_pct": "{:.2f}"}),
                width="stretch", hide_index=True)
            st.metric("Total holdings P&L", f"₹{live_hold['pnl'].sum():,.0f}")

        st.caption(f"Last refreshed {dt.datetime.now():%H:%M:%S}"
                  + (f" · auto-refreshing every {refresh_choice}" if run_every else ""))

    _live_positions_holdings()

    # Separate fetch for the sections below (square-off, stop-loss, orders) --
    # these don't live inside the auto-refreshing fragment above, since their
    # widgets/forms shouldn't get reset every refresh tick; a second cheap
    # positions/holdings call here keeps them independent of that cadence.
    pos = kite_client.get_positions()
    hold = kite_client.get_holdings()

    st.divider()
    st.subheader("Place an order")
    st.caption("Sizing uses your ATR stop so every BUY risks the same % of capital. "
               "Pick SELL on something you currently hold to square off the whole "
               "position at market in one click.")

    all_syms = []
    if not pos.empty:
        all_syms += list(pos[pos["quantity"] != 0]["tradingsymbol"])
    if not hold.empty:
        all_syms += list(hold[hold["quantity"] > 0]["tradingsymbol"])
    all_syms = sorted(set(all_syms))

    symbol_choices = sorted(set(all_syms) | set(config.UNIVERSE))
    col1, col2, col3 = st.columns(3)
    with col1:
        symbol = st.selectbox("Symbol", symbol_choices, key="trade_symbol")
        side = st.radio("Side", ["BUY", "SELL"], horizontal=True, key="trade_side")

    held_qty, held_avg_price = 0, 0.0
    if not pos.empty and symbol in pos["tradingsymbol"].values:
        r = pos[pos["tradingsymbol"] == symbol].iloc[0]
        held_qty, held_avg_price = int(r["quantity"]), float(r["average_price"])
    elif not hold.empty and symbol in hold["tradingsymbol"].values:
        r = hold[hold["tradingsymbol"] == symbol].iloc[0]
        held_qty, held_avg_price = int(r["quantity"]), float(r["average_price"])

    square_off_mode = False
    if side == "SELL" and held_qty > 0:
        square_off_mode = st.checkbox(
            f"Square off entire position ({held_qty} shares at market)",
            value=True, key="trade_square_off")

    with col2:
        capital = st.number_input("Capital for sizing (₹)",
                                  value=float(available_cash), step=10000.0,
                                  key="trade_capital", disabled=square_off_mode)
        order_type = st.radio("Order type", ["MARKET", "LIMIT"], horizontal=True,
                              key="trade_order_type", disabled=square_off_mode)
        limit_price = st.number_input("Limit price", value=0.0, step=0.05,
                                      key="trade_limit") \
            if (order_type == "LIMIT" and not square_off_mode) else None

    try:
        ltp = kite_client.get_ltp([symbol])[symbol]
        df = kite_client.fetch_daily_candles(symbol, days=120)
        atr_now = float(indicators.atr(df, cfg["atr_period"]).iloc[-1])
        stop = ltp - cfg["atr_stop_multiple"] * atr_now
        suggested_qty = screener.position_size(capital, ltp, stop)
    except Exception as e:
        st.warning(f"Couldn't fetch live data: {e}")
        ltp, stop, suggested_qty = 0.0, 0.0, 0

    with col3:
        st.metric("LTP", f"₹{ltp:,.2f}")
        st.metric("ATR stop", f"₹{stop:,.2f}")
        if square_off_mode:
            qty = held_qty
            st.metric("Quantity", qty)
        else:
            default_qty = held_qty if (side == "SELL" and held_qty > 0) else int(suggested_qty)
            qty = st.number_input("Quantity", value=default_qty, min_value=0,
                                  help=f"Suggested for {cfg['risk_per_trade_pct']}% risk"
                                       if side == "BUY" else "Currently held quantity",
                                  key="trade_qty")

    place_gtt = False
    if side == "BUY" and not square_off_mode:
        place_gtt = st.checkbox("Also place GTT stop-loss at the ATR stop", value=True,
                                key="trade_place_gtt")

    if square_off_mode:
        st.info(f"Order preview: **SELL {qty} × {symbol}** at market "
                f"(square-off) ≈ ₹{qty * ltp:,.0f}")
    else:
        est_value = qty * ltp
        st.info(f"Order preview: **{side} {qty} × {symbol}** ≈ ₹{est_value:,.0f} "
                f"({order_type}{f' @ ₹{limit_price}' if limit_price else ''})"
                + (f" + GTT SL at ₹{stop:,.1f}" if place_gtt else ""))

    confirm = st.checkbox(
        f"I confirm I want to close my entire {symbol} position at market"
        if square_off_mode else "I confirm this order",
        key="trade_confirm")
    if st.button("Square off" if square_off_mode else "Execute order", type="primary",
                disabled=not confirm or qty == 0, key="trade_execute"):
        if square_off_mode:
            try:
                order_id = kite_client.square_off_position(symbol)
                st.success(f"Square-off order placed: {order_id}")
                try:
                    exit_ltp = kite_client.get_ltp([symbol])[symbol]
                except Exception:
                    exit_ltp = None
                state_db.close_trade(symbol, exit_ltp, "manual_square_off")
                # A stale GTT left pointing at a position you no longer
                # hold can trigger and attempt to sell shares that aren't
                # there, or just confusingly linger in the Kite GTT list.
                tracked_pos = state_db.get_open_positions().get(symbol)
                gtt_id = tracked_pos.get("gtt_trigger_id") if tracked_pos else None
                if gtt_id:
                    try:
                        kite_client.delete_gtt(int(gtt_id))
                        st.success(f"GTT {gtt_id} deleted")
                    except Exception as e:
                        st.warning(f"⚠️ GTT delete failed: {e} — remove it "
                                  "manually in Kite or re-check here.")
            except Exception as e:
                st.error(f"Order failed: {e}")
        else:
            try:
                oid = kite_client.place_order(symbol, qty, side,
                                              order_type=order_type, price=limit_price)
            except Exception as e:
                st.error(f"Order failed: {e}")
            else:
                st.success(f"Order placed: {oid}")
                if side == "BUY":
                    gtt_id = None
                    if place_gtt:
                        try:
                            gtt_id = kite_client.place_gtt_stoploss(symbol, qty, stop, ltp)
                            st.success(f"GTT stop-loss placed: trigger {gtt_id} at ₹{stop:,.1f}")
                        except Exception as e:
                            st.warning(f"⚠️ Buy succeeded but GTT stop-loss FAILED: {e} "
                                      "(no stop-loss in place — check the GTT / "
                                      "Stop-Loss Management section below)")
                    # Recorded even when the GTT failed (gtt_id=None), same
                    # reasoning as the Live Rebalance buy flow.
                    position_id = state_db.record_new_position(
                        symbol, float(ltp), int(qty), float(stop), gtt_id)
                    # No screener row here (this is a manually-picked symbol, not
                    # a candidate from the scan) -- entry snapshot is just
                    # price/qty/stop, same as record_new_position itself gets.
                    state_db.record_trade_entry(
                        symbol, float(ltp), int(qty), float(stop),
                        snapshot={"entry_reason": "Manually placed order (Trade tab), "
                                 "not from the automated scan"},
                        position_id=position_id)

    st.divider()
    st.subheader(
        "🛑 GTT / Stop-Loss Management",
        help="The one place for GTT visibility and actions: every current "
             "position/holding, its live GTT status straight from Kite "
             "(the source of truth, not just this app's own tracking), "
             "and -- for anything unprotected -- the action to place one. "
             "Computes the same ATR-based stop this app always uses for a "
             "position bought outside its own buy flow (e.g. placed "
             "directly on Kite), and backfills this app's own bookkeeping "
             "so future trailing-stop updates pick it up too.")

    try:
        active_gtts = kite_client.get_active_gtts()
        gtt_by_symbol = {}
        if not active_gtts.empty and "condition" in active_gtts.columns:
            for _, g in active_gtts.iterrows():
                cond = g.get("condition") or {}
                gsym = cond.get("tradingsymbol") if isinstance(cond, dict) else None
                if not gsym:
                    continue
                trigger_vals = cond.get("trigger_values") if isinstance(cond, dict) else None
                gtt_by_symbol[gsym] = {
                    "trigger_price": trigger_vals[0] if trigger_vals else None,
                    "updated_at": g.get("updated_at"),
                }
    except Exception as e:
        st.warning(f"Could not fetch GTTs from Kite: {e}")
        gtt_by_symbol = {}

    gtt_symbols = set(gtt_by_symbol)
    gtt_rows = []
    for sym in all_syms:
        g = gtt_by_symbol.get(sym)
        gtt_rows.append({
            "symbol": sym, "gtt_active": "✅" if g else "❌",
            "trigger_price": g["trigger_price"] if g else None,
            "updated_at": g["updated_at"] if g else None,
        })
    gtt_table = pd.DataFrame(gtt_rows)
    if not gtt_table.empty:
        st.dataframe(
            pnl_style(gtt_table.sort_values("symbol"), fmt={"trigger_price": "{:.2f}"},
                     na_rep="—"),
            width="stretch", hide_index=True)

    unprotected_syms = [s for s in all_syms if s not in gtt_symbols]

    if not unprotected_syms:
        st.success("Every current position/holding has an active GTT.")
    else:
        st.warning(f"{len(unprotected_syms)} unprotected: "
                  f"{', '.join(unprotected_syms)} — place a stop-loss below.")
        sl_symbol = st.selectbox("Symbol", unprotected_syms, key="manual_sl_symbol")

        sl_qty, sl_avg_price = 0, 0.0
        if not pos.empty and sl_symbol in pos["tradingsymbol"].values:
            r = pos[pos["tradingsymbol"] == sl_symbol].iloc[0]
            sl_qty, sl_avg_price = int(r["quantity"]), float(r["average_price"])
        elif not hold.empty and sl_symbol in hold["tradingsymbol"].values:
            r = hold[hold["tradingsymbol"] == sl_symbol].iloc[0]
            sl_qty, sl_avg_price = int(r["quantity"]), float(r["average_price"])

        try:
            sl_ltp = kite_client.get_ltp([sl_symbol])[sl_symbol]
            sl_df = kite_client.fetch_daily_candles(sl_symbol, days=120)
            sl_atr = float(indicators.atr(sl_df, cfg["atr_period"]).iloc[-1])
            sl_stop = sl_ltp - cfg["atr_stop_multiple"] * sl_atr
        except Exception as e:
            st.warning(f"Couldn't fetch live data: {e}")
            sl_ltp, sl_stop = 0.0, 0.0

        slc1, slc2, slc3 = st.columns(3)
        slc1.metric("Quantity", sl_qty)
        slc2.metric("LTP", f"₹{sl_ltp:,.2f}")
        slc3.metric("ATR stop", f"₹{sl_stop:,.2f}",
                   help=f"{cfg['atr_stop_multiple']}× ATR({cfg['atr_period']}) below LTP")

        sl_confirm = st.checkbox("I confirm this GTT stop-loss", key="manual_sl_confirm")
        if st.button("Place stop-loss", type="primary",
                    disabled=not sl_confirm or sl_qty == 0 or sl_stop <= 0,
                    key="manual_sl_place"):
            try:
                gtt_id = kite_client.place_gtt_stoploss(sl_symbol, sl_qty, sl_stop, sl_ltp)
            except Exception as e:
                st.error(f"GTT placement failed: {e}")
            else:
                state_db.upsert_manual_position(sl_symbol, sl_avg_price, sl_qty,
                                                sl_stop, gtt_id)
                st.success(f"GTT stop-loss placed: trigger {gtt_id} at ₹{sl_stop:,.1f}")
                st.rerun()

    st.divider()
    st.subheader("Today's orders")
    orders = kite_client.get_orders()
    if not orders.empty:
        st.dataframe(
            pnl_style(orders[["order_timestamp", "tradingsymbol", "transaction_type",
                             "quantity", "average_price", "status"]],
                     fmt={"average_price": "{:.2f}"}),
            width="stretch", hide_index=True)
    else:
        st.caption("No orders today.")


# ---------------------------------------------------------------------------
# Page: Backtest
# ---------------------------------------------------------------------------

def page_backtest():
    st.subheader("🧪 Backtest — calendar-entry momentum system")
    st.caption(
        "Replays the exact screener logic point-in-time with monthly "
        "rebalancing (any slot freed by a stop gets redeployed immediately, "
        "not just at the next rebalance), daily ATR-stop checks, and "
        "transaction costs. The fundamental quality gate is off by default "
        "and opt-in below — when enabled it's genuinely point-in-time (only "
        "uses filings that were actually public as of each rebalance date, "
        "via each filing's real broadcast timestamp), not lookahead. Today's "
        "universe implies some survivorship bias regardless — treat "
        "parameter-sensitivity comparisons as more reliable than absolute "
        "returns."
    )

    range_mode = st.radio("Date range", ["Trailing years", "Custom dates"],
                          horizontal=True)
    b1, b2 = st.columns(2)
    if range_mode == "Trailing years":
        years = b1.slider("Years of history", 1.0, 5.0, 3.0, 0.5,
                          help="Up to 5 years supported via chunked Kite "
                               "fetches (Kite's historical API caps a single "
                               "request at ~2000 days).")
        start_date, end_date = None, None
    else:
        default_start = dt.date.today() - dt.timedelta(days=3 * 365)
        start_date = b1.date_input("Start date", value=default_start,
                                   max_value=dt.date.today())
        end_date = b2.date_input("End date", value=dt.date.today(),
                                 max_value=dt.date.today())
        years = None
    bt_capital = st.number_input("Starting capital (₹)", value=1_000_000.0,
                                 step=100000.0)
    st.caption("No per-trade cost is modeled — Zerodha charges no brokerage "
              "on equity delivery (CNC). Statutory costs (STT, stamp duty, "
              "exchange/SEBI charges) still apply in reality (~5-7 bps round "
              "trip) but aren't broker-specific; use `--cost-bps` on the CLI "
              "if you want a more conservative run that includes them.")

    use_fundamentals = st.checkbox(
        "Include fundamental quality gate (point-in-time)", value=False,
        help="Uses each filing's real broadcast timestamp to only count "
             "what was actually public knowledge as of each rebalance date "
             "— not today's fundamentals applied retroactively. Needs a "
             "fundamentals history built below first (a one-time or "
             "periodic scan across the universe, same cost as the "
             "Fundamentals page's scan).")
    if use_fundamentals:
        if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
            hist_cached = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)
            age_hr = (dt.datetime.now() - hist_cached["run_time"]).total_seconds() / 3600
            st.caption(f"📁 Fundamentals history built {age_hr:.1f}h ago "
                      f"({len(hist_cached['history'])} symbols).")
        else:
            st.warning("No fundamentals history built yet — the gate will "
                      "have no effect until you build one.")
        if st.button("Build/Refresh fundamentals history"):
            bar = st.progress(0.0, text="Starting...")
            history = fa.build_fundamentals_history(
                config.UNIVERSE, n_years=5,
                progress_cb=lambda s, f: bar.progress(f, text=s))
            bar.empty()
            os.makedirs("cache", exist_ok=True)
            pd.to_pickle({"history": history, "run_time": dt.datetime.now()},
                        FUNDAMENTALS_HISTORY_CACHE)
            st.rerun()

    use_sector = st.checkbox(
        "Include sector relative-strength bonus", value=False,
        help="Tilts ranking toward stocks in currently-outperforming "
             "sectors, using each sector index's own real historical price "
             "data (point-in-time, not today's NSE heatmap applied "
             "retroactively) — see sector_universe.py. Needs sector data "
             "built below first. Off by default: an earlier scoring "
             "dimension built the same way (long-year breakout priority) "
             "was A/B tested and found no edge, so treat this the same way "
             "— verify with the A/B run below before trusting it.")
    sector_weight = 1.0
    if use_sector:
        if os.path.exists(SECTOR_DATA_CACHE):
            sec_cached = pd.read_pickle(SECTOR_DATA_CACHE)
            age_hr = (dt.datetime.now() - sec_cached["run_time"]).total_seconds() / 3600
            st.caption(f"📁 Sector data built {age_hr:.1f}h ago "
                      f"({len(sec_cached['sector_candles'])} sector indices, "
                      f"{len(sec_cached['sector_membership'])} symbols mapped).")
        else:
            st.warning("No sector data built yet — the bonus will have no "
                      "effect until you build it.")
        if st.button("Build/Refresh sector data"):
            with st.spinner("Fetching sector membership + index history..."):
                sector_membership = su.get_sector_membership(force_refresh=True)
                sector_candles = su.fetch_sector_index_candles(
                    days=int(years * 365) + 400 if range_mode == "Trailing years"
                    else (dt.date.today() - start_date).days + 400)
            os.makedirs("cache", exist_ok=True)
            pd.to_pickle({"sector_membership": sector_membership,
                         "sector_candles": sector_candles,
                         "run_time": dt.datetime.now()}, SECTOR_DATA_CACHE)
            st.rerun()
        sector_weight = st.slider("Sector bonus weight", 0.0, 3.0, 1.0, 0.25,
                                  help="0 = no effect (same as unchecked). "
                                       "Higher = sector strength matters more "
                                       "relative to the 4 existing technical "
                                       "score terms.")

    use_trailing = st.checkbox(
        "Trail the stop up as a position gains (chandelier-style)",
        value=False,
        help="Ratchets each position's stop up to "
             "highest_close_since_entry - multiple*ATR as it gains, never "
             "back down — instead of leaving the stop fixed at entry for "
             "the whole holding period. Existing winners already run "
             "60-86 days on average and capture solid gains via the "
             "rebalance exit; a fixed stop never locks in any of that "
             "along the way, so a reversal can give back most of an open "
             "profit before the stop or next month's rebalance catches "
             "it. Verify with the A/B run below before trusting it.")
    trailing_mult = 4.0
    if use_trailing:
        trailing_mult = st.slider(
            "Trailing ATR multiple", 1.0, 8.0, 4.0, 0.25,
            help="Distance below the running peak close, in ATRs. A real "
                 "5-year sweep found this forms an inverted-U — too narrow "
                 "(near the entry stop's own 2.5x) whipsaws out of "
                 "winners early, too wide (6.0x+) barely trails at all — "
                 "peaking at 4.0x (default here): CAGR 24.30% vs baseline "
                 "22.51%, Sharpe 1.73 vs 1.50, max drawdown -14.37% vs "
                 "-18.06%. Re-verify with the A/B run below on your own "
                 "window before trusting it.")

    if "bt_result" not in st.session_state and os.path.exists(BACKTEST_CACHE):
        cached = pd.read_pickle(BACKTEST_CACHE)
        st.session_state["bt_result"] = cached["result"]
        st.session_state["bt_bench"] = cached["bench"]
        st.session_state["bt_run_time"] = cached["run_time"]
        st.session_state["bt_is_cached"] = True

    run_disabled = range_mode == "Custom dates" and start_date >= end_date
    if run_disabled:
        st.error("Start date must be before end date.")

    if st.button("Run backtest", type="primary", disabled=run_disabled):
        with st.spinner("Loading candles (cached daily, first run is slow)..."):
            if range_mode == "Custom dates":
                days = (dt.date.today() - start_date).days + 400
                candles_bt, bench_bt = bt.load_candles_cached(
                    config.UNIVERSE, days, end_date=end_date)
            else:
                days = int(years * 365) + 400
                candles_bt, bench_bt = bt.load_candles_cached(config.UNIVERSE, days)
        fundamentals_history = None
        if use_fundamentals and os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
            fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]
        run_cfg, sector_candles, sector_membership = None, None, None
        if use_sector and os.path.exists(SECTOR_DATA_CACHE):
            sec_data = pd.read_pickle(SECTOR_DATA_CACHE)
            sector_candles = sec_data["sector_candles"]
            sector_membership = sec_data["sector_membership"]
            run_cfg = dict(config.STRATEGY)
            run_cfg["sector_bonus_weight"] = sector_weight
        if use_trailing:
            run_cfg = run_cfg or dict(config.STRATEGY)
            run_cfg["trailing_stop_enabled"] = True
            run_cfg["trailing_atr_multiple"] = trailing_mult
        with st.spinner("Simulating..."):
            res = bt.run_backtest(candles_bt, bench_bt, run_cfg,
                                  initial_capital=bt_capital,
                                  fundamentals_history=fundamentals_history,
                                  sector_candles=sector_candles,
                                  sector_membership=sector_membership)
            run_time = dt.datetime.now()
            st.session_state["bt_result"] = res
            st.session_state["bt_bench"] = bench_bt
            st.session_state["bt_run_time"] = run_time
            st.session_state["bt_is_cached"] = False
            os.makedirs("cache", exist_ok=True)
            pd.to_pickle({"result": res, "bench": bench_bt, "run_time": run_time},
                        BACKTEST_CACHE)

    st.divider()
    st.subheader("Sector A/B comparison")
    st.caption("Runs the backtest twice with identical settings — once with "
              "the sector bonus off (baseline), once with it on at the "
              "chosen weight above — so you can see the actual effect "
              "instead of assuming one.")
    if st.button("Run A/B: baseline vs sector-aware", disabled=run_disabled
                or not os.path.exists(SECTOR_DATA_CACHE)):
        with st.spinner("Loading candles (cached daily, first run is slow)..."):
            if range_mode == "Custom dates":
                ab_days = (dt.date.today() - start_date).days + 400
                candles_ab, bench_ab = bt.load_candles_cached(
                    config.UNIVERSE, ab_days, end_date=end_date)
            else:
                candles_ab, bench_ab = bt.load_candles_cached(
                    config.UNIVERSE, int(years * 365) + 400)
        sec_data = pd.read_pickle(SECTOR_DATA_CACHE)
        # Explicit trailing_stop_enabled=False on both arms: isolates the
        # sector effect against a pure-technical baseline (matching the
        # already-published README numbers) regardless of what
        # config.STRATEGY's own default is -- config.STRATEGY now ships
        # with trailing_stop_enabled=True, so leaving it implicit here
        # would silently fold the trailing stop into "baseline" too.
        cfg_baseline = dict(config.STRATEGY)
        cfg_baseline["trailing_stop_enabled"] = False
        cfg_sector = dict(config.STRATEGY)
        cfg_sector["trailing_stop_enabled"] = False
        cfg_sector["sector_bonus_weight"] = sector_weight
        with st.spinner("Simulating baseline..."):
            res_baseline = bt.run_backtest(candles_ab, bench_ab, cfg_baseline,
                                           initial_capital=bt_capital)
        with st.spinner("Simulating sector-aware..."):
            res_sector = bt.run_backtest(
                candles_ab, bench_ab, cfg_sector, initial_capital=bt_capital,
                sector_candles=sec_data["sector_candles"],
                sector_membership=sec_data["sector_membership"])
        st.session_state["bt_ab_baseline"] = res_baseline
        st.session_state["bt_ab_sector"] = res_sector
        st.session_state["bt_ab_bench"] = bench_ab

    if "bt_ab_baseline" in st.session_state:
        ab_base = st.session_state["bt_ab_baseline"]
        ab_sector = st.session_state["bt_ab_sector"]
        ab_bench = st.session_state["bt_ab_bench"]
        eq_base = ab_base["equity_curve"]
        eq_sector = ab_sector["equity_curve"]
        nifty_ab = ab_bench["close"].reindex(eq_base.index).ffill()
        ab_fig = go.Figure()
        ab_fig.add_trace(go.Scatter(
            x=eq_base.index, y=eq_base / eq_base.iloc[0] * 100,
            name="Baseline", mode="lines", line=dict(color="#6b7280", width=1.5),
            hovertemplate="%{y:.1f}<extra>Baseline</extra>"))
        ab_fig.add_trace(go.Scatter(
            x=eq_base.index,
            y=(eq_sector / eq_sector.iloc[0] * 100).reindex(eq_base.index).ffill(),
            name="Sector-aware", mode="lines", line=dict(color="#16a34a", width=2),
            hovertemplate="%{y:.1f}<extra>Sector-aware</extra>"))
        ab_fig.add_trace(go.Scatter(
            x=nifty_ab.index, y=nifty_ab / nifty_ab.iloc[0] * 100,
            name="NIFTY 50", mode="lines", line=dict(color="#9ca3af", width=1, dash="dot"),
            hovertemplate="%{y:.1f}<extra>NIFTY 50</extra>"))
        ab_fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10),
                            hovermode="x unified", yaxis=dict(title="Growth of 100"),
                            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
        st.plotly_chart(ab_fig, width="stretch")
        st.dataframe(pd.DataFrame({"Baseline": ab_base["metrics"],
                                   "Sector-aware": ab_sector["metrics"]}),
                    width="stretch")

    st.divider()
    st.subheader("Trailing stop A/B comparison")
    st.caption("Runs the backtest twice with identical settings — once with "
              "a fixed entry stop (baseline), once with the stop trailing "
              "up at the chosen ATR multiple above — so you can see the "
              "actual effect instead of assuming one.")
    if st.button("Run A/B: baseline vs trailing-stop", disabled=run_disabled):
        with st.spinner("Loading candles (cached daily, first run is slow)..."):
            if range_mode == "Custom dates":
                ts_days = (dt.date.today() - start_date).days + 400
                candles_ts, bench_ts = bt.load_candles_cached(
                    config.UNIVERSE, ts_days, end_date=end_date)
            else:
                candles_ts, bench_ts = bt.load_candles_cached(
                    config.UNIVERSE, int(years * 365) + 400)
        cfg_ts_baseline = dict(config.STRATEGY)
        cfg_ts_baseline["trailing_stop_enabled"] = False
        cfg_trailing = dict(config.STRATEGY)
        cfg_trailing["trailing_stop_enabled"] = True
        cfg_trailing["trailing_atr_multiple"] = trailing_mult
        with st.spinner("Simulating baseline..."):
            res_ts_baseline = bt.run_backtest(candles_ts, bench_ts, cfg_ts_baseline,
                                              initial_capital=bt_capital)
        with st.spinner("Simulating trailing-stop..."):
            res_ts_trailing = bt.run_backtest(candles_ts, bench_ts, cfg_trailing,
                                              initial_capital=bt_capital)
        st.session_state["bt_ts_baseline"] = res_ts_baseline
        st.session_state["bt_ts_trailing"] = res_ts_trailing
        st.session_state["bt_ts_bench"] = bench_ts

    if "bt_ts_baseline" in st.session_state:
        ts_base = st.session_state["bt_ts_baseline"]
        ts_trailing = st.session_state["bt_ts_trailing"]
        ts_bench = st.session_state["bt_ts_bench"]
        eq_ts_base = ts_base["equity_curve"]
        eq_ts_trailing = ts_trailing["equity_curve"]
        nifty_ts = ts_bench["close"].reindex(eq_ts_base.index).ffill()
        ts_fig = go.Figure()
        ts_fig.add_trace(go.Scatter(
            x=eq_ts_base.index, y=eq_ts_base / eq_ts_base.iloc[0] * 100,
            name="Baseline", mode="lines", line=dict(color="#6b7280", width=1.5),
            hovertemplate="%{y:.1f}<extra>Baseline</extra>"))
        ts_fig.add_trace(go.Scatter(
            x=eq_ts_base.index,
            y=(eq_ts_trailing / eq_ts_trailing.iloc[0] * 100).reindex(eq_ts_base.index).ffill(),
            name="Trailing-stop", mode="lines", line=dict(color="#16a34a", width=2),
            hovertemplate="%{y:.1f}<extra>Trailing-stop</extra>"))
        ts_fig.add_trace(go.Scatter(
            x=nifty_ts.index, y=nifty_ts / nifty_ts.iloc[0] * 100,
            name="NIFTY 50", mode="lines", line=dict(color="#9ca3af", width=1, dash="dot"),
            hovertemplate="%{y:.1f}<extra>NIFTY 50</extra>"))
        ts_fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10),
                            hovermode="x unified", yaxis=dict(title="Growth of 100"),
                            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
        st.plotly_chart(ts_fig, width="stretch")
        st.dataframe(pd.DataFrame({"Baseline": ts_base["metrics"],
                                   "Trailing-stop": ts_trailing["metrics"]}),
                    width="stretch")

        yp_ts_base = bt.yearly_performance(eq_ts_base, ts_bench, ts_base["trades"])
        yp_ts_trailing = bt.yearly_performance(eq_ts_trailing, ts_bench, ts_trailing["trades"])
        st.caption("Year-by-year Strategy % — baseline vs trailing-stop")
        st.dataframe(pd.DataFrame({
            "Baseline %": yp_ts_base["Strategy %"],
            "Trailing-stop %": yp_ts_trailing["Strategy %"],
            "NIFTY %": yp_ts_base["NIFTY %"],
            "Difference": yp_ts_trailing["Strategy %"] - yp_ts_base["Strategy %"],
        }), width="stretch")

    if "bt_result" not in st.session_state:
        st.info("Click **Run backtest** to simulate on real Kite data.")
        return

    run_time = st.session_state.get("bt_run_time")
    cached_note = " 📁 (from cache — click 'Run backtest' to refresh)" \
        if st.session_state.get("bt_is_cached") else ""
    if run_time is not None:
        st.caption(f"Last run: {run_time:%d %b %Y %H:%M}{cached_note}")

    res = st.session_state["bt_result"]
    eq = res["equity_curve"]
    bench_bt = st.session_state["bt_bench"]

    nifty = bench_bt["close"].reindex(eq.index).ffill()

    eq_fig = go.Figure()
    eq_fig.add_trace(go.Scatter(
        x=eq.index, y=eq / eq.iloc[0] * 100, name="Strategy", mode="lines",
        line=dict(color="#16a34a", width=2),
        hovertemplate="%{y:.1f}<extra>Strategy</extra>"))
    eq_fig.add_trace(go.Scatter(
        x=nifty.index, y=nifty / nifty.iloc[0] * 100, name="NIFTY 50", mode="lines",
        line=dict(color="#6b7280", width=1.5, dash="dot"),
        hovertemplate="%{y:.1f}<extra>NIFTY 50</extra>"))
    eq_fig.update_layout(
        height=380, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified",
        xaxis=dict(rangeslider=dict(visible=True), rangeselector=dict(buttons=[
            dict(count=1, label="1y", step="year", stepmode="backward"),
            dict(count=3, label="3y", step="year", stepmode="backward"),
            dict(step="all", label="All"),
        ])),
        yaxis=dict(title="Growth of 100"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
    st.plotly_chart(eq_fig, width="stretch")

    bc1, bc2, bc3, bc4 = st.columns(4)
    bc1.metric("Final capital", f"₹{res['final_capital']:,.0f}",
              f"{(res['final_capital'] / eq.iloc[0] - 1) * 100:+.1f}%")
    bc2.metric("CAGR", f"{res['metrics']['CAGR %']}%")
    bc3.metric("Sharpe", res["metrics"]["Sharpe"])
    bc4.metric("Open positions", len(res["open_positions"]))

    st.dataframe(pd.DataFrame({"Value": res["metrics"]}), width="stretch")

    dd = (eq / eq.cummax() - 1) * 100
    dd_fig = go.Figure()
    dd_fig.add_trace(go.Scatter(
        x=dd.index, y=dd, name="Drawdown %", mode="lines",
        line=dict(color="#dc2626", width=1.5), fill="tozeroy",
        fillcolor="rgba(220,38,38,0.15)",
        hovertemplate="%{y:.1f}%<extra>Drawdown</extra>"))
    dd_fig.update_layout(height=220, margin=dict(l=10, r=10, t=10, b=10),
                         yaxis=dict(title="Drawdown %"), showlegend=False)
    st.plotly_chart(dd_fig, width="stretch")

    st.subheader("Year-by-year performance")
    yp = bt.yearly_performance(eq, bench_bt, res["trades"])
    yc1, yc2 = st.columns([2, 3])
    with yc1:
        st.dataframe(
            pnl_style(yp, ["Strategy %", "Alpha %"],
                     {"Strategy %": "{:.2f}", "NIFTY %": "{:.2f}",
                      "Alpha %": "{:.2f}", "Win rate %": "{:.1f}"}),
            width="stretch")
    with yc2:
        bar_fig = go.Figure()
        bar_colors = ["#16a34a" if v >= 0 else "#dc2626" for v in yp["Strategy %"]]
        bar_fig.add_trace(go.Bar(
            x=yp.index.astype(str), y=yp["Strategy %"], name="Strategy %",
            marker_color=bar_colors, hovertemplate="%{y:.1f}%<extra>Strategy</extra>"))
        bar_fig.add_trace(go.Bar(
            x=yp.index.astype(str), y=yp["NIFTY %"], name="NIFTY %",
            marker_color="#9ca3af", hovertemplate="%{y:.1f}%<extra>NIFTY 50</extra>"))
        bar_fig.update_layout(
            height=320, margin=dict(l=10, r=10, t=10, b=10), barmode="group",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
        st.plotly_chart(bar_fig, width="stretch")

    if not res["open_positions"].empty:
        with st.expander(f"Open positions at period end ({len(res['open_positions'])})",
                         expanded=True):
            st.caption("Still held when the backtest's date range ran out — "
                      "not force-sold. Unrealized P&L is marked to the last "
                      "available close, not an actual exit.")
            op = res["open_positions"].copy()
            op["entry_date"] = pd.to_datetime(op["entry_date"]).dt.date
            st.dataframe(
                pnl_style(
                    op.sort_values("unrealized_pnl", ascending=False),
                    ["unrealized_pnl", "unrealized_ret_pct"],
                    {"entry_price": "{:.2f}", "current_price": "{:.2f}",
                     "stop": "{:.2f}", "unrealized_pnl": "{:,.0f}",
                     "unrealized_ret_pct": "{:.2f}"}),
                width="stretch", hide_index=True)

    with st.expander("All closed trades", expanded=True):
        tr = res["trades"].copy()
        if not tr.empty:
            tr["entry_date"] = pd.to_datetime(tr["entry_date"]).dt.date
            tr["exit_date"] = pd.to_datetime(tr["exit_date"]).dt.date

            f1, f2, f3 = st.columns(3)
            with f1:
                sym_filter = st.multiselect("Symbol", sorted(tr["symbol"].unique()),
                                            key="tr_sym_filter")
            with f2:
                reason_filter = st.multiselect("Exit reason", sorted(tr["reason"].unique()),
                                               key="tr_reason_filter")
            with f3:
                outcome_filter = st.selectbox("Outcome", ["All", "Wins only", "Losses only"],
                                              key="tr_outcome_filter")

            filtered = tr
            if sym_filter:
                filtered = filtered[filtered["symbol"].isin(sym_filter)]
            if reason_filter:
                filtered = filtered[filtered["reason"].isin(reason_filter)]
            if outcome_filter == "Wins only":
                filtered = filtered[filtered["pnl"] > 0]
            elif outcome_filter == "Losses only":
                filtered = filtered[filtered["pnl"] <= 0]

            st.caption(f"Showing {len(filtered)} of {len(tr)} trades")
            st.dataframe(
                pnl_style(filtered.sort_values("entry_date", ascending=False),
                         ["pnl", "ret_pct"],
                         {"entry_price": "{:.2f}", "exit_price": "{:.2f}",
                          "pnl": "{:,.0f}", "ret_pct": "{:.2f}"}),
                width="stretch", hide_index=True)
            st.download_button("Download trades CSV (filtered view)",
                              filtered.to_csv(index=False),
                              "backtest_trades.csv")


# ---------------------------------------------------------------------------
# Page: Fundamentals (Value Score)
# ---------------------------------------------------------------------------

def page_fundamentals():
    st.subheader("📊 Fundamentals — primary-source value score")
    st.caption(
        "0-100 score from three pillars, computed entirely in Python from "
        "the company's own audited XBRL filings — no scraping, no LLM. "
        "Deterministic and free, so it runs across the full F&O universe."
    )
    st.info(
        "**Sector-aware scoring.** Banks and NBFCs file under structurally "
        "different XBRL taxonomies — banks don't tag Revenue/Equity/Current "
        "Assets at all, and general-company thresholds would flag every "
        "healthy NBFC as over-levered (NBFCs run 3-6x leverage by design). "
        "Each symbol is routed to the rubric matching what its filings "
        "actually contain: **general** (ROE, D/E, Current Ratio, FCF, "
        "Revenue CAGR, PEG), **banking** (ROE, ROA, NIM proxy, Gross/Net "
        "NPA, Advances growth), or **nbfc** (ROE, ROA, D/E, Loan growth — "
        "also covers AMCs per NSE's own filing classification). Insurers "
        "aren't covered yet — their key metrics (persistency, embedded "
        "value, solvency ratio) aren't reliably XBRL-tagged. Balance-sheet "
        "ratios only refresh once a year (audited annual filing). Missing "
        "sub-metrics are dropped, not faked — check a row's missing "
        "pillars before trusting a high total score."
    )

    if "value_scores" not in st.session_state and os.path.exists(VALUE_SCORE_CACHE):
        st.session_state["value_scores"] = pd.read_pickle(VALUE_SCORE_CACHE)
        st.session_state["value_scores_is_cached"] = True

    if st.session_state.get("value_scores_is_cached"):
        age = dt.datetime.now() - dt.datetime.fromtimestamp(
            os.path.getmtime(VALUE_SCORE_CACHE))
        st.caption(f"📁 Showing results from the last scan "
                  f"({age.total_seconds() / 3600:.1f}h ago). Click below to "
                  f"re-run against live NSE data.")

    v1, v2, v3 = st.columns(3)
    with v1:
        max_syms_v = st.slider("Symbols to scan", 10, len(config.UNIVERSE),
                               len(config.UNIVERSE), step=10,
                               key="value_scan_n",
                               help="~0.3s/symbol + XBRL download time")
    with v2:
        use_price = st.checkbox("Use live price (for PEG)", value=True,
                                key="value_scan_price")
    with v3:
        n_years_v = st.slider("Years of annual history", 2, 5, 3,
                              key="value_scan_years")

    if st.button("Run value score scan", type="primary"):
        bar = st.progress(0.0, text="Starting...")
        result = fa.fno_value_scan(
            config.UNIVERSE[:max_syms_v], n_years=n_years_v,
            use_live_price=use_price,
            progress_cb=lambda s, f: bar.progress(f, text=s))
        bar.empty()
        st.session_state["value_scores"] = result
        st.session_state["value_scores_is_cached"] = False
        os.makedirs("cache", exist_ok=True)
        result.to_pickle(VALUE_SCORE_CACHE)

    # Column labels for this page's tables live in the module-level
    # COLUMN_LABELS dict (near pnl_style/readable_df) alongside every other
    # page's, rather than a local copy here.

    # DECISION-relevant headline metrics only — excludes the 0-5 pillar
    # averages and sub-scores, which explain HOW a score was computed, not
    # WHAT to decide on (see the "Score breakdown" expander for that).
    RUBRIC_HEADLINE_COLS = {
        "general": ["roe", "debt_to_equity", "current_ratio",
                   "revenue_cagr_pct", "fcf_yoy_pct", "peg"],
        "banking": ["roe", "roa", "nim_proxy_pct", "gross_npa_pct",
                   "net_npa_pct", "advances_yoy_pct", "pat_yoy_pct"],
        "nbfc": ["roe", "roa", "debt_to_equity", "loan_yoy_pct", "pat_yoy_pct"],
        "general_insurance": ["roe", "roa", "combined_ratio_pct",
                              "incurred_claim_ratio_pct", "premium_yoy_pct",
                              "pat_yoy_pct"],
        "life_insurance": ["roe", "premium_yoy_pct", "pat_yoy_pct"],
    }

    if "value_scores" not in st.session_state:
        return
    vdf = st.session_state["value_scores"].copy()

    rubrics_present = sorted(vdf["rubric"].dropna().unique())
    sector = st.selectbox(
        "Filter by sector", ["All"] + rubrics_present,
        help="Each sector uses a different rubric with different metrics — "
             "filtering keeps the table to the columns that actually apply.")

    if sector == "All":
        shown = vdf
        seen_cols, numeric_cols = set(), []
        for rubric_cols in RUBRIC_HEADLINE_COLS.values():
            for c in rubric_cols:
                if c not in seen_cols and c in vdf.columns:
                    seen_cols.add(c)
                    numeric_cols.append(c)
    else:
        shown = vdf[vdf["rubric"] == sector]
        numeric_cols = [c for c in RUBRIC_HEADLINE_COLS.get(sector, [])
                       if c in vdf.columns]
    show_cols = ["total_score", "rubric"] + numeric_cols + ["fiscal_year_end"]
    show_cols = [c for c in show_cols if c in shown.columns]

    st.subheader(f"Ranked ({shown['total_score'].notna().sum()}/{len(shown)} scored"
                f"{'' if sector == 'All' else f', {sector}'})")
    fmt = {c: "{:.2f}" for c in ["total_score"] + numeric_cols}
    st.dataframe(pnl_style(shown[show_cols], fmt=fmt), width="stretch")

    with st.expander("🔍 Score breakdown for one symbol"):
        st.caption("The 0-5 pillar scores and individual sub-metric buckets "
                  "behind the headline total.")
        sym_choice = st.selectbox("Symbol", list(shown.index),
                                  key="value_score_detail_sym")
        if sym_choice:
            row = shown.loc[sym_choice]
            st.write(f"**{sym_choice}** — {row.get('rubric')} rubric, "
                    f"score {row.get('total_score')}, "
                    f"as of {row.get('fiscal_year_end', '—')}")
            pillar_scores = row.get("pillar_scores") or {}
            sub_scores = row.get("sub_scores") or {}
            pc1, pc2 = st.columns(2)
            with pc1:
                st.markdown("**Pillar scores (0-5)**")
                st.dataframe(pd.DataFrame(
                    [{"Pillar": k.replace("_", " ").title(), "Score": round(v, 2)}
                     for k, v in pillar_scores.items()]),
                    hide_index=True, width="stretch")
            with pc2:
                st.markdown("**Sub-metric buckets (0-5)**")
                st.dataframe(pd.DataFrame(
                    [{"Metric": k.replace("_", " ").title(), "Bucket": v}
                     for k, v in sub_scores.items()]),
                    hide_index=True, width="stretch")
            if row.get("missing_pillars"):
                st.warning(f"Excluded from total (no data): "
                          f"{', '.join(row['missing_pillars'])}")

    incomplete = shown[shown["missing_pillars"].apply(bool)]
    with st.expander(f"Rows with incomplete data ({len(incomplete)})"):
        st.caption("A pillar is excluded from the total (not defaulted) when "
                  "none of its sub-metrics are available — usually means "
                  "fewer than 2 years of annual filings are retrievable via "
                  "NSE's endpoint for this name, or (for "
                  "'unsupported_taxonomy') the sector isn't covered by any "
                  "rubric yet.")
        inc_cols = show_cols + ["missing_pillars"]
        st.dataframe(readable_df(incomplete[inc_cols]), width="stretch")


# ---------------------------------------------------------------------------
# Page: Job Log
# ---------------------------------------------------------------------------

JOB_TYPES = ["rebalance_scan", "gap_check", "fundamentals_refresh", "screen_run"]


def page_job_log():
    st.subheader("🗂️ Job Log")
    st.caption("Every scheduled job (systemd timers on the VPS) and manual "
              "background-job button writes a row here -- answers \"did "
              "today's jobs actually run\" without SSH-ing in to read raw "
              "logs.")

    st.markdown("**Last run of each job**")
    cols = st.columns(len(JOB_TYPES))
    for col, jt in zip(cols, JOB_TYPES):
        last = state_db.get_last_job_run(jt)
        with col:
            if last is None:
                st.metric(COLUMN_LABELS.get(jt, jt), "never run")
                continue
            started = dt.datetime.fromisoformat(last["started_at"])
            age_hr = (dt.datetime.now() - started).total_seconds() / 3600
            badge = {"success": "✅", "failed": "❌", "running": "⏳"}.get(last["status"], "❓")
            st.metric(jt, f"{badge} {age_hr:.1f}h ago",
                     help=last.get("summary") or last.get("error_message") or "")

    st.divider()
    st.markdown("**History**")
    f1, f2, f3 = st.columns(3)
    with f1:
        type_filter = st.multiselect("Job type", JOB_TYPES, key="jl_type_filter")
    with f2:
        status_filter = st.multiselect("Status", ["success", "failed", "running"],
                                       key="jl_status_filter")
    with f3:
        since_date = st.date_input("Since", value=dt.date.today() - dt.timedelta(days=30),
                                   key="jl_since")

    runs = state_db.get_job_runs(since=since_date.isoformat(), limit=1000)
    if type_filter:
        runs = runs[runs["job_type"].isin(type_filter)]
    if status_filter:
        runs = runs[runs["status"].isin(status_filter)]

    st.caption(f"Showing {len(runs)} run(s)")
    if runs.empty:
        st.info("No job runs match these filters.")
        return

    st.dataframe(
        pnl_style(runs.drop(columns=["id"]),
                 fmt={"duration_sec": "{:.1f}"}),
        width="stretch", hide_index=True)

    failed = runs[runs["status"] == "failed"]
    if not failed.empty:
        with st.expander(f"Failed runs -- full error detail ({len(failed)})"):
            for _, r in failed.iterrows():
                st.markdown(f"**{r['job_type']}** at {r['started_at']}")
                st.code(r["error_message"] or "(no error message captured)")


# ---------------------------------------------------------------------------
# Page: Tradebook
# ---------------------------------------------------------------------------

def page_tradebook():
    st.subheader("📒 Tradebook")
    st.caption("Every trade this app has opened, with its entry-time "
              "technical/fundamental snapshot and a real exit reason -- "
              "separate from the Positions & Trade page's live view, meant "
              "for historical/analytics use.")

    trades = state_db.get_trades()
    if trades.empty:
        st.info("No trades recorded yet.")
        return

    closed = trades[trades["status"] == "closed"]
    if not closed.empty:
        s1, s2, s3, s4 = st.columns(4)
        wins = closed[closed["realized_pnl"] > 0]
        win_rate = 100 * len(wins) / len(closed[closed["realized_pnl"].notna()]) \
            if closed["realized_pnl"].notna().any() else float("nan")
        s1.metric("Win rate", f"{win_rate:.1f}%" if win_rate == win_rate else "—")
        s2.metric("Avg holding days", f"{closed['holding_days'].mean():.0f}"
                 if closed["holding_days"].notna().any() else "—")
        s3.metric("Total realized P&L", f"₹{closed['realized_pnl'].sum():,.0f}")
        best = closed["realized_ret_pct"].max()
        worst = closed["realized_ret_pct"].min()
        s4.metric("Best / worst trade",
                 f"{best:+.1f}% / {worst:+.1f}%" if pd.notna(best) else "—")

    st.divider()
    f1, f2, f3, f4 = st.columns(4)
    with f1:
        sym_filter = st.multiselect("Symbol", sorted(trades["symbol"].unique()),
                                    key="tb_sym_filter")
    with f2:
        status_filter = st.multiselect("Status", ["open", "closed"],
                                       key="tb_status_filter")
    with f3:
        reasons = sorted(trades["exit_reason"].dropna().unique())
        reason_filter = st.multiselect("Exit reason", reasons, key="tb_reason_filter")
    with f4:
        since_date = st.date_input("Entered since",
                                   value=dt.date.today() - dt.timedelta(days=365),
                                   key="tb_since")

    filtered = trades[trades["entry_date"] >= since_date.isoformat()]
    if sym_filter:
        filtered = filtered[filtered["symbol"].isin(sym_filter)]
    if status_filter:
        filtered = filtered[filtered["status"].isin(status_filter)]
    if reason_filter:
        filtered = filtered[filtered["exit_reason"].isin(reason_filter)]

    st.caption(f"Showing {len(filtered)} of {len(trades)} trades")
    rest_cols = [c for c in filtered.columns
                if c not in ("id", "position_id", "status", "latest_recommended_stop")]
    stop_idx = rest_cols.index("initial_stop") + 1
    display_cols = (["status"] + rest_cols[:stop_idx] + ["latest_recommended_stop"]
                   + rest_cols[stop_idx:])
    styled = pnl_style(
        filtered[display_cols], ["realized_pnl", "realized_ret_pct"],
        {"entry_price": "{:.2f}", "exit_price": "{:.2f}",
         "initial_stop": "{:.2f}", "latest_recommended_stop": "{:.2f}",
         "entry_score": "{:.2f}", "entry_rsi": "{:.1f}",
         "entry_pct_52w_high": "{:.2f}", "entry_vol_expansion": "{:.2f}",
         "entry_fundamental_score": "{:.1f}", "realized_pnl": "{:,.0f}",
         "realized_ret_pct": "{:.2f}"})
    styled = styled.map(_status_color, subset=["Status"])
    st.dataframe(styled, width="stretch", hide_index=True)
    st.download_button("Download tradebook CSV (filtered view)",
                       filtered.to_csv(index=False), "tradebook.csv")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

page_cockpit_p = st.Page(page_cockpit, title="Overview", icon="🏠", default=True)
page_screener_p = st.Page(page_screener, title="Screener", icon="🔍")
page_live_rebalance_p = st.Page(page_live_rebalance, title="Live Rebalance", icon="📡")
page_positions_trade_p = st.Page(page_positions_trade, title="Positions & Trade", icon="💼")
page_backtest_p = st.Page(page_backtest, title="Backtest", icon="🧪")
page_fundamentals_p = st.Page(page_fundamentals, title="Fundamentals", icon="📊")
page_tradebook_p = st.Page(page_tradebook, title="Tradebook", icon="📒")
page_job_log_p = st.Page(page_job_log, title="Job Log", icon="🗂️")
page_admin_p = st.Page(page_admin, title="Admin", icon="⚙️")

with st.sidebar:
    st.metric("Available cash", f"₹{available_cash:,.0f}")
    st.caption(f"F&O universe: {len(config.UNIVERSE)} stocks · "
              f"{dt.date.today():%d %b %Y}")

nav = st.navigation([page_cockpit_p, page_screener_p, page_live_rebalance_p,
                    page_positions_trade_p, page_backtest_p, page_fundamentals_p,
                    page_tradebook_p, page_job_log_p, page_admin_p])
nav.run()
