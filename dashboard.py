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

import base64
import datetime as dt
import html as html_lib
import math
import os
import re

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from kiteconnect.exceptions import TokenException

import backtest as bt
import config
import fundamentals_agent as fa
import indicators
import kite_client
import live_rebalance as lr
import notify
import nse_holidays
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

st.set_page_config(page_title="KK Trading System", layout="wide",
                   page_icon="assets/logo.png" if os.path.exists("assets/logo.png") else "📈")

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
#
# "Remember me" bridges the OTHER case a session reset happens: this
# server process itself restarting (every deploy does this) or the
# browser's WebSocket dropping (network blip, laptop sleep) -- neither of
# those is a fresh external redirect, so nothing above re-authenticates
# them. A valid remember_token cookie (see state_db.create_remember_token)
# skips the form entirely; it deliberately expires at the next 6 AM
# (state_db._next_daily_cutoff) rather than after a fixed duration, so a
# fresh sign-in is still required every morning, same cadence as Kite's
# own daily token expiry.
# ---------------------------------------------------------------------------
state_db.ensure_dashboard_auth_seeded(config.DASHBOARD_USERNAME, config.DASHBOARD_PASSWORD)

if not st.session_state.get("dashboard_authenticated", False):
    remembered_user = state_db.verify_remember_token(st.context.cookies.get("remember_token", ""))
    if remembered_user:
        st.session_state["dashboard_authenticated"] = True
        st.session_state["dashboard_username"] = remembered_user
    else:
        st.title("🔒 KK Trading System — sign in")
        with st.form("login_form", clear_on_submit=True):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            remember = st.checkbox("Remember me on this device until tomorrow morning",
                                   value=True)
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            if state_db.verify_dashboard_login(u, p):
                st.session_state["dashboard_authenticated"] = True
                st.session_state["dashboard_username"] = u
                if remember:
                    token, max_age = state_db.create_remember_token(u)
                    st.session_state["_pending_remember_cookie"] = (token, max_age)
                st.rerun()
            else:
                st.error("Incorrect username or password.")
        st.stop()

# Sets the remember-me cookie via a one-off injected script -- can't be done
# in the same run as the st.rerun() above (the rerun cuts execution off
# before a component would ever reach the browser), so the token is stashed
# in session_state and the cookie gets set here, on the very next run, once.
_pending_cookie = st.session_state.pop("_pending_remember_cookie", None)
if _pending_cookie:
    _token, _max_age = _pending_cookie
    st.html(
        f'<script>document.cookie = "remember_token={_token}; max-age={_max_age}; '
        f'path=/; SameSite=Lax; Secure";</script>',
        unsafe_allow_javascript=True)

# ---------------------------------------------------------------------------
# Kite connection health check -- only reached after the dashboard login
# above succeeds. Shows the Kite login redirect ONLY when the token is
# actually missing/expired; a still-valid token falls straight through to
# the normal dashboard pages below.
#
# Only TokenException triggers the login redirect -- previously a bare
# `except Exception` treated ANY margins() failure (a transient network
# blip, Kite API hiccup, timeout) identically to a genuinely expired
# token, so a real login prompt couldn't be trusted to mean "your session
# actually expired." Anything else surfaces as an explicit retryable
# error instead, since re-doing Zerodha's login+2FA wouldn't fix it
# anyway.
# ---------------------------------------------------------------------------
if not config.KITE_ACCESS_TOKEN:
    _redirect_to_kite_login()

try:
    margins = kite_client.get_margins()
    available_cash = margins["equity"]["available"]["live_balance"]
except TokenException:
    _redirect_to_kite_login()
except Exception as e:
    st.error(f"Couldn't reach Kite to check your account: {e}")
    st.caption("This looks like a transient network/API issue, not an "
              "expired session -- your Kite login should still be fine. "
              "Try again in a moment.")
    if st.button("Retry"):
        st.rerun()
    st.stop()

SCREEN_CACHE = os.path.join("cache", "screen.pkl")
VALUE_SCORE_CACHE = os.path.join("cache", "fno_value_scores.pkl")
BACKTEST_CACHE = os.path.join("cache", "backtest_result.pkl")
FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_OVERVIEW_CSS = """
<style>
:root {
    --ov-surface-2: #ffffff; --ov-text-primary: #1a1a18;
    --ov-text-secondary: #5f5e5a; --ov-text-muted: #8a8983;
    --ov-green-d: #0f6e56; --ov-green: #1d9e75; --ov-green-l: #e1f5ee;
    --ov-red-d: #a32d2d; --ov-red: #e24b4a; --ov-red-l: #fcebeb;
    --ov-amber-d: #854f0b; --ov-amber: #ef9f27; --ov-amber-l: #faeeda;
    --ov-blue-d: #185fa5; --ov-blue: #378add; --ov-blue-l: #e6f1fb;
    --ov-purple-d: #534ab7; --ov-purple: #7f77dd; --ov-purple-l: #eeedfe;
    --ov-teal: #5dcaa5; --ov-coral: #d85a30;
    --ov-pink-d: #993556; --ov-pink: #d4537e; --ov-pink-l: #fbeaf0;
    --ov-surface-1: #eeede6; --ov-border: #e0ded5; --ov-border-strong: #a8a69c;
}
@media (prefers-color-scheme: dark) {
    :root {
        --ov-surface-2: #2a2a2e; --ov-text-primary: #ececea;
        --ov-text-secondary: #a8a7a2; --ov-text-muted: #7c7b76;
        --ov-green-l: #08402f; --ov-red-l: #471414; --ov-amber-l: #3d2404;
        --ov-blue-l: #0b3a66; --ov-purple-l: #292361; --ov-pink-l: #451527;
        --ov-green-d: #5dcaa5; --ov-red-d: #f09595; --ov-amber-d: #fac775;
        --ov-blue-d: #85b7eb; --ov-purple: #afa9ec; --ov-purple-d: #afa9ec;
        --ov-teal: #5dcaa5; --ov-coral: #f0997b; --ov-pink-d: #ed93b1;
        --ov-surface-1: #222225; --ov-border: #38383c; --ov-border-strong: #5c5c60;
    }
}
/* ---- global compaction: match the mockup's density ----
   Streamlit defaults measured on 1.59.2: main padding 96/80/160px, block
   gap 16px, buttons 40px min-height, metric values 36px, alert padding
   16px, divider margin 32px -- all far looser than the mockup (page
   padding 14-20px, gaps 6-10px, 12px buttons, 16px metric values). */
[data-testid="stMainBlockContainer"] {
    padding: 1.2rem 1.4rem 3rem !important;
    max-width: 1120px !important; margin: 0 auto;
}
/* [data-testid="stAppToolbar"] never matched anything -- the actual
   testid Streamlit renders is "stHeader" (the outer bar) / "stToolbar"
   (the Deploy/menu strip inside it); "stAppToolbar" is only a CSS class
   name, not the testid. toolbarMode="minimal" already suppresses Deploy/
   hamburger content on its own. This bar only renders (non-null, with an
   OPAQUE background) once the sidebar is collapsed, specifically to host
   the expand-sidebar arrow -- that's what was painting over the brandbar
   chips right after collapsing. Keep the bar (needed for that arrow) but
   force it transparent so it never covers content underneath. */
[data-testid="stHeader"] { background: transparent !important; box-shadow: none !important; }
[data-testid="stMainBlockContainer"] [data-testid="stVerticalBlock"] { gap: 0.45rem; }
[data-testid="stMainBlockContainer"] hr { margin: 10px 0 !important; }
[data-testid="stCaptionContainer"] p { font-size: 11px !important; }

.stButton button, [data-testid="stFormSubmitButton"] button,
[data-testid="stDownloadButton"] button {
    min-height: 1.9rem !important; padding: 3px 12px !important;
}
.stButton button p, [data-testid="stFormSubmitButton"] button p,
[data-testid="stDownloadButton"] button p { font-size: 12px !important; font-weight: 500 !important; }

[data-testid="stWidgetLabel"] { min-height: 0 !important; margin-bottom: 1px !important; }
[data-testid="stWidgetLabel"] p {
    font-size: 11px !important; color: var(--ov-text-muted) !important; font-weight: 500 !important;
}
.stCheckbox { min-height: 0 !important; }
.stCheckbox p, .stRadio p { font-size: 12px !important; }
.stNumberInput input, .stTextInput input, .stDateInput input {
    padding: 5px 10px !important; font-size: 12.5px !important;
}
.stNumberInput button { min-height: 0 !important; }
[data-baseweb="select"] > div { min-height: 2rem !important; font-size: 12.5px !important; }
/* 1.59's selectbox is a custom input+button combo, no data-baseweb hook */
[data-testid="stSelectbox"] > div > div { height: 2.1rem !important; min-height: 2.1rem !important; }
[data-baseweb="menu"] li { font-size: 12.5px !important; }
.stMultiSelect [data-baseweb="tag"] { font-size: 11px !important; }

[data-testid="stAlertContainer"] { padding: 6px 10px !important; border-radius: 6px !important; }
[data-testid="stAlertContainer"] p { font-size: 11.5px !important; margin-bottom: 0 !important; }

[data-testid="stMetric"] {
    background: var(--ov-surface-2); border-top: 2px solid var(--ov-blue);
    border-radius: 8px; padding: 8px 10px;
}
[data-testid="stMetricValue"] { font-size: 16px !important; font-weight: 700; padding-bottom: 0 !important; }
[data-testid="stMetricLabel"] p {
    font-size: 11px !important; color: var(--ov-text-muted) !important; font-weight: 500 !important;
}
[data-testid="stMetricDelta"] { font-size: 11px !important; }

[data-testid="stExpander"] details {
    background: var(--ov-surface-2); border: 1px solid var(--ov-border) !important;
    border-radius: 10px !important;
}
[data-testid="stExpander"] summary { min-height: 0 !important; padding: 8px 13px !important; }
[data-testid="stExpander"] summary p, [data-testid="stExpander"] summary span {
    font-size: 12.5px !important; font-weight: 600 !important;
}
[data-testid="stExpander"] details > div { padding: 2px 13px 10px !important; }

[data-testid="stForm"] {
    background: var(--ov-surface-2); border: 1px solid var(--ov-border-strong) !important;
    border-radius: 10px !important; padding: 10px 13px !important;
}
.st-key-ov_sync button {
    font-size:12px !important; padding:5px 11px !important; min-height:0 !important;
    height:auto !important; border-radius:8px !important; width:auto !important;
}
/* Sync button is taken out of the chips' flex flow entirely (see the
   .st-key-ov-topbar rule below) -- pinned to this container's top-right
   corner instead of sharing a row/column with the chips, so it can't
   overlap the logo or squeeze the chips into wrapping no matter the
   window width. That's what repeated column/flex-ratio attempts here
   kept fighting. */
/* z-index above Streamlit's own [data-testid="stAppToolbar"] (999990) --
   that native toolbar spans the full viewport width at the very top
   (0-52.5px tall) on EVERY screen size, invisible/empty past its sidebar-
   expand button but still pointer-events:auto, so it silently swallows
   taps meant for anything under it. On desktop this container's own
   "top:2px" position happens to land below that 52.5px strip so it never
   came up; on a narrow phone viewport the topbar sits higher up (logo
   stacks above the chips there -- see the @media rule above) and the
   button's position overlaps the strip, so taps hit the toolbar's empty
   space instead of Sync and nothing happens. */
.st-key-ov_sync { position:absolute !important; top:2px !important; right:0 !important; z-index:1000000 !important; }
/* Divider line under the whole brandbar row, and the positioning
   context for the absolutely-placed Sync button above. */
.st-key-ov-topbar {
    position:relative !important;
    border-bottom:1px solid var(--ov-border) !important;
    padding-bottom:12px !important; margin-bottom:6px !important;
    padding-right:40px !important;
}
.st-key-ov_logout button {
    background:var(--ov-red) !important; color:#fff !important; border:none !important;
    border-radius:999px !important; font-weight:700 !important; text-transform:uppercase !important;
    letter-spacing:.03em !important; font-size:11px !important; padding:5px 13px !important;
    min-height:0 !important; height:auto !important; width:auto !important;
}
/* "Run today's scan" header button (Live Rebalance, manual mode) -- push
   it to the right edge of its column. align-items:flex-end on the outer
   stColumn still left a gap before the true page edge; margin-left:auto
   on the button's own element container is the same technique that
   reliably worked for the Sync icon button earlier -- it pushes flush
   right regardless of the parent's flex-direction. */
[data-testid="stColumn"]:has(.st-key-lr_run_scan_hdr) { display:flex !important; }
.st-key-lr_run_scan_hdr { margin-left:auto !important; width:fit-content !important; }
/* Segmented control's real root is [data-testid="stButtonGroup"] (found
   by reading Streamlit's own source -- button_group.py/ButtonGroup.*.js
   -- after several guesses at the wrong element failed). It has exactly
   two children: the label (with an optional help/tooltip icon) and the
   pill row; by default these stack vertically, which is why "Auto-
   refresh" kept landing above the pills instead of beside them. */
.st-key-pt_refresh_interval [data-testid="stButtonGroup"] {
    display:flex !important; flex-direction:row !important;
    align-items:center !important; gap:8px !important; flex-wrap:nowrap !important;
    justify-content:flex-end !important;
}
.st-key-pt_refresh_interval label {
    padding:3px 8px !important; font-size:11px !important; white-space:nowrap !important;
}
/* The "(?)" help icon next to the label -- shrink it to match the
   compact pills instead of its default (larger) size. */
.st-key-pt_refresh_interval [data-testid="stTooltipIcon"] {
    width:14px !important; height:14px !important; font-size:11px !important;
}
/* Verified via a real headless-browser DOM inspection (Playwright) that
   stButtonGroup was ALREADY stretched to full column width thanks to
   this width cascade -- so margin-left:auto had nothing to push into
   (no slack space existed anywhere in the chain). The actual fix is the
   justify-content:flex-end added above, on stButtonGroup's OWN flex
   layout, pushing its label+pills to ITS OWN right edge. */
.st-key-pt_refresh_row,
.st-key-pt_refresh_row [data-testid="stVerticalBlock"],
.st-key-pt_refresh_row [data-testid="stElementContainer"] {
    width:100% !important;
}
.st-key-ov_logout button:hover { filter:brightness(0.9) !important; color:#fff !important; }
.st-key-ov_logout button p { color:#fff !important; }
/* Screener header: "Fetch fundamental score" checkbox + "Run screen"
   button, side by side and right-aligned instead of stacked. Confirmed
   via DOM inspection that st.container(key=...)'s class lands directly
   on the stVerticalBlock that arranges its own children -- no need to
   reach into a nested selector here. */
.st-key-screen_run_row {
    display:flex !important; flex-direction:row !important;
    align-items:center !important; justify-content:flex-end !important; gap:12px !important;
}
/* Fundamentals header: the info popover + "Run value score scan" button,
   side by side and right-aligned, same technique. */
.st-key-fund_scan_row {
    display:flex !important; flex-direction:row !important;
    align-items:center !important; justify-content:flex-end !important; gap:12px !important;
}
/* Every text/number/date/select/multiselect input's actual bordered
   wrapper has border:1px solid #fff by default (found via DOM inspection)
   -- an invisible white border against the light background, which is why
   "no border appears" on ANY form field app-wide. Selectboxes render as
   react-aria-ComboBox; multiselect renders classic BaseWeb instead (its
   bordered box is the DIRECT child of [data-baseweb="select"]); text/
   number/date inputs use stable testids. All safe to select on directly
   and applied app-wide rather than per-key. */
.react-aria-ComboBox > div,
[data-testid="stTextInputRootElement"],
[data-testid="stNumberInputContainer"],
[data-testid="stDateInput"] div[data-baseweb="input"],
[data-baseweb="select"] > div {
    border:1px solid var(--ov-border-strong) !important;
}
.st-key-value_score_detail_sym .react-aria-ComboBox > div {
    justify-content:flex-start !important;
}
.st-key-value_score_detail_sym input { text-align:left !important; }
/* number_input's +/- stepper buttons default to 38px tall against a 27.5px
   text field, ballooning the WHOLE bordered box to 40px -- taller than
   every other input type's ~29.5px (text/date/select all share that
   height since their content is just the 27.5px field + 1px border each
   side). Shrinking the steppers to match is what actually fixes the
   input row's overall height, not the outer container. */
[data-testid="stNumberInputStepUp"], [data-testid="stNumberInputStepDown"] {
    height:27.5px !important; min-height:0 !important; padding:0 6px !important;
}
/* The steppers' own flex-row wrapper (unnamed testid, second child of
   stNumberInputContainer) still centers them in a taller box than the
   buttons themselves need -- align-items:center collapses that extra
   space instead of stretching to fit some invisible minimum. */
[data-testid="stNumberInputContainer"] > div:last-child {
    align-items:center !important; height:27.5px !important;
}
/* stNumberInputContainer itself also carries an explicit height:35px
   (Streamlit's own emotion-cache rule, sized for the ORIGINAL 38px-tall
   steppers) -- shrinking just the children above doesn't override an
   explicit height on the parent, so the box itself needs the same
   29.5px every other input type's wrapper measures (27.5px content +
   1px border each side). */
[data-testid="stNumberInputContainer"] {
    height:29.5px !important; align-items:center !important;
}
/* Download buttons sit flush against their card's bottom edge when
   they're the last element -- a bit of breathing room matches the
   padding every other end-of-card element gets. */
[data-testid="stDownloadButton"] { margin-bottom:8px; }
/* Requested directly via an emotion-hash class found in dev tools --
   unlike the testid/library-class selectors elsewhere in this file,
   emotion hashes like this can change on a Streamlit version bump or
   even a rebuild, so if this stops matching later that's why. */
.st-emotion-cache-eqh6wq { margin-bottom:0rem !important; }
.st-key-fund_sector_filter { max-width:140px !important; }
/* Slider track thickness -- found via DOM inspection (rail is normally
   only 3.5px tall). The component-identity suffix classes (e23vpic5 = rail,
   e23vpic3 = thumb/fill) are shared by EVERY slider instance, unlike the
   emotion-cache-XXXXXX prefix which is regenerated per-instance based on
   the rail's inline width % -- so targeting the prefix only thickened
   whichever slider happened to hash-collide with the rule. Scoped under
   the stable [data-testid="stSlider"] testid as a fallback safety net. */
[data-testid="stSlider"] [class*="e23vpic5"],
[data-testid="stSlider"] [class*="e23vpic3"] {
    height:8px !important;
}
/* Live Rebalance's "Execute all ..." action buttons -- solid color,
   full-width, matching the mockup's rose/green execute bars. Disabled
   state keeps the color but dims it so it doesn't read as just another
   plain gray Streamlit button once the confirm checkbox is ticked. */
.st-key-lr_execute_sells button {
    background:var(--ov-red) !important; color:#fff !important; border:none !important;
}
.st-key-lr_execute_sells button:hover:not(:disabled) { filter:brightness(0.92) !important; color:#fff !important; }
.st-key-lr_execute_sells button p { color:#fff !important; }
.st-key-lr_execute_buys button {
    background:var(--ov-green) !important; color:#fff !important; border:none !important;
}
.st-key-lr_execute_buys button:hover:not(:disabled) { filter:brightness(0.92) !important; color:#fff !important; }
.st-key-lr_execute_buys button p { color:#fff !important; }
.st-key-lr_execute_sells button:disabled, .st-key-lr_execute_buys button:disabled {
    opacity:0.45 !important; color:#fff !important;
}
.st-key-lr_execute_sells button:disabled p, .st-key-lr_execute_buys button:disabled p { color:#fff !important; }
/* Admin's "Delete entry" button (Ledger) -- outlined red, distinct from
   the blue primary "Save changes" button next to it. */
.st-key-admin_ledger_delete_btn button { color:var(--ov-red) !important; border-color:var(--ov-red) !important; }
.st-key-admin_ledger_delete_btn button p { color:var(--ov-red) !important; }
.st-key-admin_ledger_delete_btn button:hover { background:var(--ov-red-l) !important; }
/* Admin Ledger's manually-built table (st.columns per row instead of
   _ov_table_html) -- needed so each row's radio-select button can be a
   real Streamlit widget lined up with plain-text cells. .ov-manual-th/
   .ov-manual-cell mirror .ov-table's th/td font sizing so it still reads
   as "the same table style" as every other page. */
.ov-manual-th { font-weight:600; font-size:12px; color:var(--ov-text-muted); }
.ov-manual-th.r, .ov-manual-cell.r { display:block; text-align:right; }
.ov-manual-cell { font-size:12px; color:var(--ov-text-primary); }
/* No border-bottom here -- _ov_table_html's header row has none either;
   the single separator line under it comes from the first data row's own
   border-top below, same as every other table. A border here too was
   drawing a doubled/thicker line under the header than every other table. */
.st-key-admin_ledger_head { padding-bottom:2px; }
/* Markdown wraps a lone <span> in a <p>, which carries the browser's
   default ~1em paragraph margin -- that's what was ballooning each row to
   ~47px tall instead of the ~30px every other table uses. */
.st-key-admin_ledger_rows [data-testid="stMarkdownContainer"] p,
.st-key-admin_ledger_head [data-testid="stMarkdownContainer"] p {
    margin:0 !important;
}
.st-key-admin_ledger_rows { gap:0 !important; }
.st-key-admin_ledger_rows [data-testid="stHorizontalBlock"] {
    border-top:1px solid var(--ov-border); padding:2px 0; align-items:center;
}
.st-key-admin_ledger_rows [data-testid="stButton"] button {
    background:transparent !important; border:none !important; padding:0 !important;
    min-height:0 !important; height:auto !important; font-size:15px !important;
    color:var(--ov-text-secondary) !important; line-height:1 !important;
}
.st-key-admin_ledger_rows [data-testid="stButton"] button:hover {
    color:var(--ov-blue) !important; background:transparent !important;
}
/* Positions & Trade's "Place stop-loss" and "Square off"/"Execute order"
   buttons -- solid blue, full-width, matching the mockup. */
.st-key-manual_sl_place button, .st-key-trade_execute button {
    background:var(--ov-blue) !important; color:#fff !important; border:none !important;
}
.st-key-manual_sl_place button:hover:not(:disabled),
.st-key-trade_execute button:hover:not(:disabled) { filter:brightness(0.92) !important; color:#fff !important; }
.st-key-manual_sl_place button p, .st-key-trade_execute button p { color:#fff !important; }
.st-key-manual_sl_place button:disabled, .st-key-trade_execute button:disabled {
    opacity:0.45 !important; color:#fff !important;
}
.st-key-manual_sl_place button:disabled p, .st-key-trade_execute button:disabled p { color:#fff !important; }
/* No extra left offset here -- the sidebar's own default content padding
   already lines this up with the page-link tabs/section labels above it. */
.st-key-ov_logout { margin:0 4px !important; }
.ov-header { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:10px; }
.ov-h1 { font-size:19px; font-weight:700; margin:0; color:var(--ov-text-primary); }
.ov-sub { font-size:12px; color:var(--ov-text-muted); font-weight:400; }
.ov-chips { display:flex; gap:6px; align-items:center; flex-wrap:wrap; flex-shrink:0; }
.ov-chip { font-size:11.5px; padding:4px 10px; border-radius:11px; font-weight:500; white-space:nowrap; }
/* flex-shrink:0 on .ov-chips (above) keeps it at its natural full width on
   desktop -- deliberately, so it never squeezes/wraps mid-fight with the
   logo (see the Sync-button comment further down for the history there).
   But on a narrow phone viewport that same natural width is wider than
   the screen, so the chips silently overflow off the right edge instead
   of wrapping, even though flex-wrap:wrap is already set -- wrapping only
   ever kicks in once the container is forced narrower than its content.
   Below this breakpoint the topbar has no logo/chips fight to referee
   (the sidebar is already collapsed to icons-only), so it's safe to let
   the chips container actually shrink to the viewport and wrap for real. */
@media (max-width: 600px) {
    .ov-header { flex-direction:column; align-items:flex-start; }
    .ov-chips { flex-shrink:1; width:100%; }
}
.ov-info-icon { cursor:help; font-size:13px; margin-left:4px; }
.ov-chip-accent { background:var(--ov-blue-l); color:var(--ov-blue-d); }
.ov-chip-success { background:var(--ov-green-l); color:var(--ov-green-d); }
.ov-chip-amber { background:var(--ov-amber-l); color:var(--ov-amber-d); }
.ov-chip-danger { background:var(--ov-red-l); color:var(--ov-red-d); }
.ov-chip-muted { background:var(--ov-surface-1); color:var(--ov-text-secondary); }
.ov-grid-metrics { display:grid; grid-template-columns:repeat(auto-fit, minmax(100px,1fr)); gap:8px; margin-bottom:10px; }
.ov-metric { background:var(--ov-surface-2); border-radius:8px; padding:8px 10px; border-top:2px solid var(--ov-border); }
.ov-metric.t-blue { border-top-color:var(--ov-blue); }
.ov-metric.t-purple { border-top-color:var(--ov-purple); }
.ov-metric.t-teal { border-top-color:var(--ov-teal); }
.ov-metric.t-amber { border-top-color:var(--ov-amber); }
.ov-metric.t-green { border-top-color:var(--ov-green); }
.ov-metric.t-coral { border-top-color:var(--ov-coral); }
.ov-metric.t-red { border-top-color:var(--ov-red); }
.ov-metric.t-pink { border-top-color:var(--ov-pink); }
.ov-metric .ov-label { font-size:11px; color:var(--ov-text-muted); margin:0; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.ov-metric .ov-value { font-size:16px; font-weight:700; margin:1px 0 0; color:var(--ov-text-primary); }
.ov-metric .ov-note { font-size:11px; color:var(--ov-text-secondary); margin:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.ov-pos { color:var(--ov-green-d) !important; }
.ov-neg { color:var(--ov-red-d) !important; }
[class*="st-key-ov-card-"] {
    border-radius:10px !important; background:var(--ov-surface-2) !important;
    border:1px solid var(--ov-border) !important;
    padding:10px 13px !important; gap:0.35rem !important;
}
.ov-card { background:var(--ov-surface-2); border:1px solid var(--ov-border); border-radius:10px; padding:10px 13px; margin-bottom:10px; }
.ov-two-col { display:grid; grid-template-columns:repeat(auto-fit, minmax(290px,1fr)); gap:10px; margin-bottom:10px; }
.ov-two-col .ov-card { margin-bottom:0; }
.ov-muted { color:var(--ov-text-muted); font-size:11.5px; margin:0 0 4px; font-weight:600; text-transform:uppercase; letter-spacing:.03em; }
.ov-card-title {
    font-size:13px; font-weight:700; margin:0 0 10px; display:flex; align-items:center;
    gap:7px; color:var(--ov-text-primary); padding-bottom:8px;
    border-bottom:1px solid var(--ov-border);
}
.ov-dot { width:7px; height:7px; border-radius:50%; display:inline-block; }
.ov-card-meta { font-size:11px; color:var(--ov-text-secondary); }
.ov-order-preview {
    font-size:11.5px; font-weight:700; color:var(--ov-blue-d); margin-left:auto;
}
.ov-table { width:100%; font-size:12px; border-collapse:collapse; }
/* Only horizontal (row) separators, matching the mockup -- no vertical
   lines between columns, ever, regardless of any inherited default. */
.ov-table th, .ov-table td { border-left:none !important; border-right:none !important; }
.ov-table th { font-weight:600; padding:4px 8px; text-align:left; color:var(--ov-text-muted); white-space:nowrap; }
/* white-space:nowrap is the actual fix for tables blowing up to ~68px
   row height on wide tables (e.g. Fundamentals' "Ranked" with ~19
   columns) -- a cell like "31-MAR-2026" was wrapping to 3 lines when its
   column got squeezed too narrow, and since a table row's height is the
   MAX of all its cells, that one wrapped cell stretched the entire row.
   Forcing single-line cells means the table just scrolls horizontally
   (.ov-tbl-scroll already supports that) instead of wrapping vertically. */
.ov-table td { padding:5px 8px; border-top:1px solid var(--ov-border); color:var(--ov-text-primary); white-space:nowrap; }
.ov-table th.r, .ov-table td.r { text-align:right; }
/* Wide tables (e.g. Fundamentals' "Ranked" with ~19 columns) get cut off
   hard at the container's right edge when they need to scroll, while
   narrower tables (e.g. Holdings) end cleanly with nothing to scroll --
   that abrupt cutoff on the wide ones was reading as an inconsistent
   "extra border" rather than an obviously-scrollable table. A visible,
   styled scrollbar (instead of the browser's default, easy to miss one)
   makes the scrollability clear so it looks intentional either way. */
.ov-tbl-scroll {
    overflow-x:auto; margin-bottom:6px;
    scrollbar-width:thin; scrollbar-color:var(--ov-border-strong) transparent;
}
.ov-tbl-scroll::-webkit-scrollbar { height:6px; }
.ov-tbl-scroll::-webkit-scrollbar-track { background:transparent; }
.ov-tbl-scroll::-webkit-scrollbar-thumb { background:var(--ov-border-strong); border-radius:3px; }
/* "Review rebalance orders" sits directly under the proposal rows with
   no breathing room, making the last row read as if the button were
   cutting it off -- give the button's own element-container a top gap. */
[class*="st-key-ov_review_rebal"] { margin-top:10px !important; }
.ov-sym { font-weight:700; }
.ov-badge { padding:1px 8px; border-radius:9px; font-size:11px; font-weight:600; white-space:nowrap; }
.ov-badge-green { background:var(--ov-green-l); color:var(--ov-green-d); }
.ov-badge-red { background:var(--ov-red-l); color:var(--ov-red-d); }
.ov-badge-amber { background:var(--ov-amber-l); color:var(--ov-amber-d); }
.ov-badge-blue { background:var(--ov-blue-l); color:var(--ov-blue-d); }
.ov-badge-purple { background:var(--ov-purple-l); color:var(--ov-purple-d); }
.ov-badge-pink { background:var(--ov-pink-l); color:var(--ov-pink-d); }
.ov-badge-gray { background:var(--ov-surface-1); color:var(--ov-text-secondary); }
.ov-row { display:flex; justify-content:space-between; align-items:center; padding:5px 0; border-bottom:1px solid var(--ov-border); font-size:12px; color:var(--ov-text-primary); gap:8px; }
.ov-row:last-of-type { border-bottom:none; }
.ov-minibar { position:relative; height:7px; background:var(--ov-surface-1); border-radius:4px; }
.ov-minibar .ov-fill { position:absolute; left:0; top:0; height:7px; border-radius:4px; }
.ov-minibar .ov-tick { position:absolute; left:80%; top:-2px; width:2px; height:11px; background:var(--ov-border-strong); }
.ov-allocbar { display:flex; height:16px; border-radius:6px; overflow:hidden; margin-bottom:6px; }
.ov-sector-row { margin-bottom:7px; }
.ov-sector-row:last-child { margin-bottom:0; }
.ov-sector-head { display:flex; justify-content:space-between; font-size:11.5px; margin-bottom:2px; color:var(--ov-text-primary); gap:6px; }
.ov-sector-bar { height:7px; background:var(--ov-surface-1); border-radius:4px; }
.ov-sector-fill { height:7px; border-radius:4px; }
/* padding-left is 5px, not 10px, so the 3px left border + padding lands
   at ~8px total inset -- matching the table cells' own 8px padding
   above it, instead of sitting ~5px further right/misaligned. */
.ov-alert { margin-top:0; padding:4px 8px 4px 5px; border-radius:6px; background:var(--ov-amber-l); color:var(--ov-amber-d); font-size:11px; border-left:3px solid var(--ov-amber); }
.ov-alert-success { background:var(--ov-green-l); color:var(--ov-green-d); border-left-color:var(--ov-green); }
.ov-alert-info { background:var(--ov-blue-l); color:var(--ov-blue-d); border-left-color:var(--ov-blue); }
.ov-donut-wrap { display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
.ov-donut-legend { flex:1; min-width:170px; }
.ov-sw { display:inline-block; width:9px; height:9px; border-radius:2px; vertical-align:-1px; margin-right:4px; }

/* ---- sidebar / side menu, matching the mockup's .side/.side-label/.tab ---- */
[data-testid="stSidebar"] { background:var(--ov-surface-1) !important; }
[data-testid="stSidebar"] [data-testid="stElementContainer"] {
    margin-bottom:0 !important;
}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap:0.1rem !important; }
/* The pill is the ANCHOR (stPageLink-NavLink), not the outer stPageLink
   container -- padding/hover/active must live on the anchor or the
   highlight renders as a thin strip inside a padded box. */
section[data-testid="stSidebar"][aria-expanded="true"] {
    width:205px !important; max-width:205px !important; min-width:205px !important;
    box-sizing:border-box !important; overflow-x:hidden !important;
}
[data-testid="stSidebarContent"], [data-testid="stSidebarUserContent"] {
    overflow-x:hidden !important; max-width:100% !important; box-sizing:border-box !important;
    padding-top:12px !important;
}
[data-testid="stSidebarHeader"] { height:auto !important; min-height:0 !important; }
[data-testid="stSidebar"] [data-testid="stPageLink"] {
    padding:0 !important; margin:0 !important; min-height:0 !important;
}
[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"] {
    border-radius:8px !important; padding:5px 10px !important; width:100%;
    background:transparent;
}
[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"]:hover { background:var(--ov-surface-2) !important; }
[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"] span {
    font-size:12.5px !important; font-weight:600 !important; color:var(--ov-text-secondary) !important;
}
/* Active page: blue pill, exactly like the mockup's checked tab. The
   aria-current attribute is stamped by the small script in the sidebar
   block (Streamlit itself gives the current link no stable marker). */
[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"][aria-current="page"] {
    background:var(--ov-blue-l) !important;
}
[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"][aria-current="page"] span {
    color:var(--ov-blue-d) !important;
}
[data-testid="stSidebar"] hr { margin:8px 0 !important; }
[data-testid="stSidebarUserContent"] { padding-bottom:1rem !important; }
.ov-side-label {
    font-size:10.5px; font-weight:700; letter-spacing:.06em; text-transform:uppercase;
    color:var(--ov-text-muted); margin:0 4px !important;
}
/* Space around a section label lives on ITS OWN element-container (via
   margin-top/padding-bottom), not on the <p> itself -- a margin on the
   <p> sits inside a container whose margin-bottom is already forced to 0
   above, which was silently eating the intended gap. margin-top pushes
   the label away from the PREVIOUS section's last tab; padding-bottom
   (never collapses) pushes the FIRST tab of this section away from the
   label text, on top of the small 0.1rem inter-tab gap. */
[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.ov-side-label) {
    margin-top:12px !important;
    padding-bottom:12px !important;
}
/* "Trading" is now the sidebar's first element (brand moved to the top
   page brandbar) -- a smaller top gap for the very first label than the
   shared 12px the rule above gives Audit Trail/Testing, since there's no
   preceding tab to separate it from, just the sidebar's own edge. */
[data-testid="stSidebar"] [data-testid="stElementContainer"]:first-child:has(.ov-side-label) {
    margin-top:4px !important;
}
.ov-brand { font-size:15px; font-weight:700; color:var(--ov-text-primary); line-height:1.3; }
.ov-brand .ov-sub {
    display:block; font-weight:400; font-size:12px; color:var(--ov-text-muted); margin-top:1px;
}
.ov-topbar-logo { display:block; height:50px; width:auto; }
</style>
"""

_OV_TONE_CYCLE = ["blue", "purple", "teal", "amber", "green", "coral"]
_OV_HEX_CYCLE = ["#534ab7", "#7f77dd", "#1d9e75", "#5dcaa5", "#ef9f27",
                "#378add", "#d85a30", "#d4537e"]


def _ov_arrow(better: bool | None) -> str:
    """A small ▲/▼ span, colored green/red -- better=True renders the "this
    improved" arrow (▲), False the "this worsened" arrow (▼), None (the
    reference value is missing, e.g. no entry-rank on record) renders
    nothing. Shared by every arrow_cols comparison in _ov_table_html so
    "up/down vs a reference column" always looks the same everywhere."""
    if better is None:
        return ""
    cls = "ov-pos" if better else "ov-neg"
    arrow = "▲" if better else "▼"
    return f'<span class="{cls}">{arrow}</span> '


def _ov_table_html(df: pd.DataFrame, columns: list[str] | None = None,
                   badges: dict | None = None, pnl_cols: list[str] | None = None,
                   num_fmt: dict | None = None, sym_cols: list[str] | None = None,
                   arrow_cols: dict | None = None, na_rep: str = "—") -> str:
    """Renders a DataFrame as the mockup's compact .ov-table -- plain HTML,
    no Streamlit dataframe chrome (sort/resize/selection). Used for every
    purely-DISPLAY table site-wide: none of these ever used row-selection
    or in-place editing, only the Overview holdings table (click-through to
    Tradebook) and the equity chart do, and those stay real Streamlit
    widgets -- this never trades away functionality, just chrome.

    columns: ordered column list to show (default: every column in df).
    badges: {col: {value: css_class}} or {col: callable(value) -> css_class}
        -- renders that cell as a colored pill instead of plain text.
    pnl_cols: columns whose sign colors the cell green/red (also bolded).
    num_fmt: {col: format_spec} for numeric columns (right-aligned).
    sym_cols: columns rendered bold (e.g. the symbol column).
    arrow_cols: {displayed_col: (value_col, reference_col, higher_is_better)}
        -- prepends a ▲/▼ to displayed_col's cell comparing df[value_col]
        against df[reference_col] for the same row (value_col is usually
        displayed_col itself -- e.g. current_capital vs invested_capital
        -- but can differ, e.g. displaying the badge-formatted "rank_fmt"
        string while comparing the underlying numeric "rank" against
        "entry_rank"; lower is better for rank, so higher_is_better=
        False there). Missing/NaN/equal values render no arrow rather
        than a misleading one. Composes with badges/pnl_cols/num_fmt --
        the arrow is just prepended to whatever that cell would
        otherwise show.
    """
    columns = columns or list(df.columns)
    num_fmt = num_fmt or {}
    badges = badges or {}
    pnl_cols = set(pnl_cols or [])
    sym_cols = set(sym_cols or [])
    arrow_cols = arrow_cols or {}
    right_cols = set(num_fmt) | pnl_cols

    def _label(c):
        return COLUMN_LABELS.get(c, c.replace("_", " ").title())

    def _arrow_prefix(c, row) -> str:
        if c not in arrow_cols:
            return ""
        value_col, ref_col, higher_is_better = arrow_cols[c]
        v, ref = row.get(value_col), row.get(ref_col)
        if pd.isna(v) or pd.isna(ref) or v == ref:
            return _ov_arrow(None)
        better = (v > ref) == higher_is_better
        return _ov_arrow(better)

    header = "".join(
        f'<th{" class=\"r\"" if c in right_cols else ""}>{html_lib.escape(_label(c))}</th>'
        for c in columns)

    rows_html = []
    for _, row in df.iterrows():
        cells = []
        for c in columns:
            v = row[c]
            r_attr = ' class="r"' if c in right_cols else ""
            sym_cls = "ov-sym" if c in sym_cols else ""
            if pd.isna(v):
                cells.append(f'<td{r_attr}><span class="{sym_cls}">{na_rep}</span></td>')
            elif c in badges:
                mapping = badges[c]
                css = mapping(v) if callable(mapping) else mapping.get(v, "ov-badge-gray")
                cells.append(f'<td{r_attr}>{_arrow_prefix(c, row)}<span class="ov-badge {css}">'
                            f'{html_lib.escape(str(v))}</span></td>')
            elif c in pnl_cols:
                fv = float(v)
                cls = "ov-pos" if fv >= 0 else "ov-neg"
                fmt = num_fmt.get(c, "{:+,.2f}")
                cells.append(f'<td{r_attr}>{_arrow_prefix(c, row)}<span class="{cls} ov-sym">'
                            f'{fmt.format(fv)}</span></td>')
            elif c in num_fmt:
                cells.append(f'<td{r_attr}>{_arrow_prefix(c, row)}<span class="{sym_cls}">'
                            f'{num_fmt[c].format(v)}</span></td>')
            else:
                cells.append(f'<td>{_arrow_prefix(c, row)}<span class="{sym_cls}">'
                            f'{html_lib.escape(str(v))}</span></td>')
        rows_html.append(f"<tr>{''.join(cells)}</tr>")

    return (f'<div class="ov-tbl-scroll"><table class="ov-table">'
           f'<tr>{header}</tr>{"".join(rows_html)}</table></div>')


def _ov_page_slice(df: pd.DataFrame, key: str, page_size: int = 10) -> pd.DataFrame:
    """Returns just the current page's slice of `df` -- tables render
    everything via raw HTML (_ov_table_html), so there's no native
    st.dataframe pagination to lean on; this keeps long tables (e.g. the
    full screener universe) from dumping hundreds of rows at once. Pairs
    with _ov_pagination_controls() using the SAME (df, key, page_size) --
    call that AFTER the table so the Prev/Next strip sits below it, not
    above. Page position is kept in st.session_state under a name derived
    from `key`, so multiple tables on the same page paginate independently."""
    n = len(df)
    n_pages = max(1, math.ceil(n / page_size))
    state_key = f"_ov_page_{key}"
    page = min(st.session_state.get(state_key, 0), n_pages - 1)
    start = page * page_size
    return df.iloc[start:start + page_size]


def _ov_pagination_controls(df: pd.DataFrame, key: str, page_size: int = 10) -> None:
    """Prev/Next control strip for the table already sliced by
    _ov_page_slice() with this SAME (df, key, page_size) -- render this
    right after the table so it appears below it."""
    n = len(df)
    n_pages = max(1, math.ceil(n / page_size))
    if n_pages <= 1:
        return
    state_key = f"_ov_page_{key}"
    page = min(st.session_state.get(state_key, 0), n_pages - 1)
    pc1, pc2, pc3 = st.columns([1, 3, 1])
    with pc1:
        if st.button("← Prev", key=f"{key}_pg_prev", disabled=page <= 0,
                    use_container_width=True):
            st.session_state[state_key] = page - 1
            st.rerun()
    with pc2:
        st.markdown(
            f'<p class="ov-card-meta" style="text-align:center;margin:6px 0;">'
            f'Page {page + 1} of {n_pages} ({n} rows)</p>', unsafe_allow_html=True)
    with pc3:
        if st.button("Next →", key=f"{key}_pg_next", disabled=page >= n_pages - 1,
                    use_container_width=True):
            st.session_state[state_key] = page + 1
            st.rerun()


def _ov_order_status_cls(v: str) -> str:
    """Badge color for a raw Kite order status string."""
    v = str(v).upper()
    if v in ("COMPLETE", "COMPLETED"):
        return "ov-badge-green"
    if v in ("OPEN", "TRIGGER PENDING", "PENDING", "PUT ORDER REQ RECEIVED"):
        return "ov-badge-amber"
    if v in ("REJECTED", "CANCELLED"):
        return "ov-badge-red"
    return "ov-badge-gray"


def _ov_donut_svg(values: list[float], center_label: str) -> str:
    """A hand-drawn ring-arc donut (stacked stroke-dasharray circles) --
    matches the compact hand-styled look used elsewhere on this page
    instead of a full Plotly figure (no hover/click needed here, so the
    lighter-weight static SVG costs nothing functionally)."""
    total = sum(values) or 1.0
    r = 44
    circumference = 2 * math.pi * r
    offset = 0.0
    circles = []
    for i, value in enumerate(values):
        color = _OV_HEX_CYCLE[i % len(_OV_HEX_CYCLE)]
        dash = value / total * circumference
        gap = circumference - dash
        circles.append(
            f'<circle cx="60" cy="60" r="{r}" fill="none" stroke="{color}" '
            f'stroke-width="18" stroke-dasharray="{dash:.1f} {gap:.1f}" '
            f'stroke-dashoffset="{-offset:.1f}" transform="rotate(-90 60 60)"/>')
        offset += dash
    return (f'<svg viewBox="0 0 120 120" width="104" height="104" role="img">'
           + "".join(circles) +
           f'<text x="60" y="57" text-anchor="middle" style="font-size:14px;font-weight:700;" '
           f'fill="currentColor">{center_label}</text></svg>')


def _ov_metric_html(label: str, value: str, note: str | None = None,
                    note_cls: str = "", tone: str = "blue",
                    value_cls: str = "") -> str:
    """One metric-card's HTML -- callers join several of these into one
    `<div class="ov-grid-metrics">...</div>` and render with a SINGLE
    st.markdown call, so the whole dense metrics strip is one lightweight
    DOM write instead of 9 separate bordered st.container widgets."""
    note_html = f'<p class="ov-note {note_cls}">{note}</p>' if note else ""
    return (f'<div class="ov-metric t-{tone}"><p class="ov-label">{label}</p>'
           f'<p class="ov-value {value_cls}">{value}</p>{note_html}</div>')


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
    "source": "Source", "reason": "Reason", "skip": "Skip?",
    "product": "Product", "status": "Status",
    "transaction_type": "Type", "order_timestamp": "Time",
    "unrealized_pnl": "Unrealized P&L", "unrealized_ret_pct": "Unrealized return %",
    "ret_pct": "Return %",
    "date": "Date", "amount": "Amount (₹)", "note": "Note",
    "value": "Current value (₹)", "allocation_pct": "Allocation %",
    "run_id": "Run ID", "run_time": "Run time", "action_type": "Action",
    "detail": "Reason/Detail", "resolved_at": "Resolved at",
    "current_qty": "Current qty", "rank": "Momentum rank", "rank_fmt": "Rank",

    # Screener / momentum
    "score": "Score", "price": "Price", "rs_3m": "RS 3M", "rs_6m": "RS 6M",
    "pct_52w_high": "% of 52W high", "rsi": "RSI",
    "vol_expansion": "Vol expansion", "atr_pct": "ATR %",
    "fundamental_score": "Fundamental score", "fundamental_rubric": "Sector rubric",
    "trend_ok": "Trend", "near_high_ok": "Near-high", "rsi_ok": "RSI",
    "quality_ok": "Quality", "quality_fails": "Quality fails",

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
    "latest_recommended_stop": "Latest stop (scan-ratcheted, else initial GTT)",
    "extra_qty": "Extra qty",
    "trigger_price": "GTT trigger price", "updated_at": "Last updated",
    "apply_error": "Why it needs attention",
    "realized_pnl": "Realized P&L",
    "realized_ret_pct": "Realized return %",
}


def merged_holdings() -> pd.DataFrame:
    """Positions + holdings merged into one live table with current P&L,
    GROUPED BY SYMBOL. A same-day CNC buy or top-up transiently appears in
    BOTH Kite endpoints until it settles into holdings overnight -- a naive
    concat (the previous behavior here) double-lists that symbol as two
    partial rows instead of one true combined position. Real example hit
    live: after executing top-ups, BHEL/LODHA/KALYANKJIL/SONACOMS each
    briefly split across a position-row and a holding-row the same day,
    silently understating each row's own qty/value even though the
    aggregate .sum() totals downstream happened to still be correct.
    Grouping here means every reader -- the Holdings table, invested/
    holdings-value totals, and the per-stock allocation view -- always
    sees one accurate row per symbol."""
    pos = kite_client.get_positions()
    hold = kite_client.get_holdings()
    rows = []
    if not pos.empty and "quantity" in pos.columns:
        # > 0, not != 0 -- a same-day SELL of an existing holding shows up
        # here as a NEGATIVE "day" quantity (the settlement-lag leg, nets
        # to 0 against the holding once it clears), not a real short
        # position (this app is long-only CNC swing trading). Including
        # it double-counted a fully-closed position as still "held" with
        # a negative qty, which also poisoned avg_price = cost/qty here
        # (dividing by a negative number). Confirmed live 2026-08-04: a
        # same-day SONACOMS sell left qty=-13 in positions(), 0 in
        # holdings() -- summed to a phantom -13 "holding" instead of
        # correctly disappearing.
        for _, r in pos[pos["quantity"] > 0].iterrows():
            rows.append({"symbol": r["tradingsymbol"], "qty": r["quantity"],
                        "avg_price": r["average_price"], "ltp": r["last_price"],
                        "pnl": r["pnl"]})
    if not hold.empty and "quantity" in hold.columns:
        for _, r in hold[hold["quantity"] > 0].iterrows():
            rows.append({"symbol": r["tradingsymbol"], "qty": r["quantity"],
                        "avg_price": r["average_price"], "ltp": r["last_price"],
                        "pnl": r["pnl"]})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["cost"] = df["qty"] * df["avg_price"]
    merged = df.groupby("symbol", as_index=False).agg(
        qty=("qty", "sum"), cost=("cost", "sum"),
        ltp=("ltp", "last"), pnl=("pnl", "sum"))
    merged["avg_price"] = merged["cost"] / merged["qty"]
    return merged.drop(columns="cost")


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

def _is_market_hours(now: dt.datetime | None = None) -> bool:
    """NSE cash-market hours, weekday-only approximation (no holiday
    calendar) -- good enough for gating a display-only auto-refresh so it
    doesn't keep polling Kite every 30s all night for numbers that can't
    have changed."""
    now = now or dt.datetime.now()
    return now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 30)


@st.cache_data(ttl="6h", show_spinner=False)
def _benchmark_cagr_since(start_date_iso: str) -> float | None:
    """NIFTY 50's own annualized return (XIRR of a single buy-and-hold from
    `start_date_iso` to today) -- the correct apples-to-apples comparison
    for 'Alpha vs NIFTY50' against the portfolio's own overall XIRR (both
    annualized, so a young account and an old one are compared fairly).
    Cached 6h: this is a real Kite historical-data network call and index
    closes don't meaningfully change within a session."""
    try:
        start_date = dt.date.fromisoformat(start_date_iso)
        days = max((dt.date.today() - start_date).days + 5, 30)
        bench = kite_client.benchmark_candles(days)
        if bench.empty:
            return None
        if bench.index.tz is not None:
            bench = bench.set_axis(bench.index.tz_localize(None))
        bench = bench[bench.index >= pd.Timestamp(start_date)]
        if len(bench) < 2:
            return None
        start_price = float(bench["close"].iloc[0])
        end_price = float(bench["close"].iloc[-1])
        return indicators.xirr([(start_date, -1.0), (dt.date.today(), end_price / start_price)])
    except Exception:
        return None


@st.cache_data
def _asset_data_uri(filename: str) -> str | None:
    """Any assets/ image as an inline data URI -- embedded rather than
    referenced by path since Streamlit doesn't serve arbitrary project
    directories by default. Cached so each file is only read/base64-
    encoded once per process, not on every script rerun."""
    path = os.path.join("assets", filename)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


def _sidebar_logo_data_uri() -> str | None:
    return _asset_data_uri("logo.png")


def _max_drawdown_pct(equity_log: pd.DataFrame) -> float | None:
    """Largest peak-to-trough decline in the logged total-capital series so
    far, as a negative percentage -- pure arithmetic on data the equity
    chart already reads, no new storage or network call."""
    if equity_log.empty or len(equity_log) < 2:
        return None
    vals = equity_log["value"].astype(float)
    running_peak = vals.cummax()
    drawdown = (vals - running_peak) / running_peak.replace(0, pd.NA)
    dd = drawdown.min()
    return float(dd) * 100 if pd.notna(dd) else None


def _load_screen_cache() -> pd.DataFrame | None:
    """The last screener run's ranked table (written by both the Screener
    page's 'Run screen' button and every rebalance scan -- see
    screener.run_screen()'s caching note). Used for a live 'rank' column
    on Holdings and the Watchlist card; gracefully returns None if no scan
    has ever run yet rather than erroring the whole Overview page."""
    try:
        if os.path.exists(SCREEN_CACHE):
            return pd.read_pickle(SCREEN_CACHE)
    except Exception:
        pass
    return None


@st.fragment(run_every="30s" if _is_market_hours() else None)
def _live_kpi_row():
    """The Overview page's top KPI strip, isolated into its own fragment so
    it can tick on a timer without rerunning (and re-fetching Kite data
    for) the rest of the page -- equity chart, holdings table, funds
    breakdown all stay exactly as they render today. Every value here is
    independently re-fetched fresh each tick via the SAME functions the
    page already used (merged_holdings(), kite_client.get_margins(),
    state_db.get_realized_pnl()/get_equity_log(), _annualized_returns()) --
    no calculation logic duplicated or changed, just called again on a
    schedule. The 30s timer itself is only armed during market hours (see
    the decorator above) so an open tab doesn't keep re-rendering/polling
    Kite all night for numbers that can't have moved; the still-present
    _is_market_hours() branch below covers the same tab staying open
    across the market open/close transition within one session."""
    if _is_market_hours():
        live_merged = merged_holdings()
        try:
            live_cash = kite_client.get_margins()["equity"]["available"]["live_balance"]
        except Exception:
            live_cash = available_cash
    else:
        live_merged = merged_holdings()
        live_cash = available_cash

    live_invested = float((live_merged["qty"] * live_merged["avg_price"]).sum()) if not live_merged.empty else 0.0
    live_holdings_value = float((live_merged["qty"] * live_merged["ltp"]).sum()) if not live_merged.empty else 0.0
    live_unrealized_pnl = float(live_merged["pnl"].sum()) if not live_merged.empty else 0.0
    live_portfolio_value = live_cash + live_holdings_value
    live_realized_pnl = state_db.get_realized_pnl()
    live_total_pnl = live_realized_pnl + live_unrealized_pnl
    live_current_xirr, live_overall_xirr = _annualized_returns(
        live_portfolio_value, state_db.get_equity_log())

    unrealized_pct = (live_unrealized_pnl / live_invested * 100) if live_invested else None
    pct_deployed = (live_invested / live_portfolio_value * 100) if live_portfolio_value else None

    # Day's gain/loss: today's live total capital vs the most recent
    # logged snapshot BEFORE today (i.e. last night's close) -- the same
    # equity_log the performance chart already reads, no new storage.
    _day_log = state_db.get_equity_log()
    _prior_day_log = _day_log[_day_log["date"] < dt.date.today().isoformat()]
    _day_start_value = float(_prior_day_log.iloc[-1]["value"]) if not _prior_day_log.empty else None
    day_change = (live_portfolio_value - _day_start_value) if _day_start_value else None
    day_change_pct = (day_change / _day_start_value * 100) if _day_start_value else None

    max_dd = _max_drawdown_pct(_day_log)
    alpha = None
    if live_overall_xirr is not None and not _day_log.empty:
        bench_cagr = _benchmark_cagr_since(_day_log.iloc[0]["date"])
        if bench_cagr is not None:
            alpha = (live_overall_xirr - bench_cagr) * 100

    _refresh_note = ("🟢 live" if _is_market_hours() else "⚪ market closed")
    st.markdown(
        '<div class="ov-header" style="margin-bottom:14px;">'
        '<div><span class="ov-h1">Overview</span> '
        '<span class="ov-sub">· everything at a glance</span></div>'
        f'<span class="ov-card-meta">{_refresh_note} · '
        f'last updated {dt.datetime.now():%H:%M:%S}</span>'
        '</div>', unsafe_allow_html=True)

    metrics_html = "".join([
        _ov_metric_html(
            "Total capital", f"₹{live_portfolio_value:,.0f}",
            (f"{day_change:+,.0f} today" if day_change is not None else "no prior snapshot yet"),
            "ov-pos" if (day_change or 0) >= 0 else "ov-neg", "blue"),
        _ov_metric_html("Invested", f"₹{live_invested:,.0f}", "Cost basis", "", "purple"),
        _ov_metric_html(
            "Holdings", f"₹{live_holdings_value:,.0f}",
            (f"{unrealized_pct:+.1f}% MTM" if unrealized_pct is not None else None),
            "ov-pos" if (unrealized_pct or 0) >= 0 else "ov-neg", "teal"),
        _ov_metric_html(
            "Cash", f"₹{live_cash:,.0f}",
            (f"{pct_deployed:.0f}% deployed" if pct_deployed is not None else None),
            "", "amber"),
        _ov_metric_html(
            "Total P&L", f"₹{live_total_pnl:+,.0f}",
            f"₹{live_unrealized_pnl:+,.0f} unrealized",
            "ov-pos" if live_unrealized_pnl >= 0 else "ov-neg", "green",
            "ov-pos" if live_total_pnl >= 0 else "ov-neg"),
        _ov_metric_html(
            "XIRR — year",
            f"{live_current_xirr * 100:+.1f}%" if live_current_xirr is not None else "—",
            "Annualized", "", "green",
            "ov-pos" if (live_current_xirr or 0) >= 0 else "ov-neg"),
        _ov_metric_html(
            "XIRR — overall",
            f"{live_overall_xirr * 100:+.1f}%" if live_overall_xirr is not None else "—",
            "Inception", "", "green",
            "ov-pos" if (live_overall_xirr or 0) >= 0 else "ov-neg"),
        _ov_metric_html(
            "Alpha vs NIFTY50", f"{alpha:+.1f}%" if alpha is not None else "—",
            "vs index CAGR, same period", "", "blue",
            "ov-pos" if (alpha or 0) >= 0 else "ov-neg"),
        _ov_metric_html(
            "Max drawdown", f"{max_dd:.1f}%" if max_dd is not None else "—",
            "Peak to trough", "", "coral"),
    ])
    st.markdown(f'<div class="ov-grid-metrics">{metrics_html}</div>', unsafe_allow_html=True)

    if live_overall_xirr is None:
        st.caption("XIRR needs at least one logged deposit and one portfolio "
                  "value snapshot — log a deposit on the Admin page (or fund "
                  "the account) to start tracking annualized return.")


def page_cockpit():
    merged = merged_holdings()
    if not merged.empty:
        merged["value"] = merged["qty"] * merged["ltp"]
    invested_amount = float((merged["qty"] * merged["avg_price"]).sum()) if not merged.empty else 0.0
    holdings_value = float(merged["value"].sum()) if not merged.empty else 0.0
    portfolio_value = available_cash + holdings_value

    if portfolio_value > 0:
        log = log_equity_snapshot(portfolio_value, invested_amount)
    else:
        # A Kite auth failure or transient fetch error can silently leave
        # available_cash/holdings_value at 0 -- logging that as a real
        # snapshot would put a fake drop-to-zero in the equity curve (this
        # happened for real on a live install; see get_equity_log()'s
        # docstring). Skip the write, just show the log as it already is.
        log = state_db.get_equity_log()
        st.warning("⚠️ Computed portfolio value is ₹0 — not logging today's "
                  "snapshot (likely a Kite connection issue, not a real "
                  "zero balance). Check the Kite login if this persists.")
    state_db.ensure_first_cash_flow_captured(available_cash)

    _live_kpi_row()

    col_chart, col_positions = st.columns(2)

    with col_chart:
      with st.container(border=True, key="ov-card-chart"):
        if len(log) > 1:
            plot_log_full = log.copy()
            plot_log_full["date"] = pd.to_datetime(plot_log_full["date"])

            _chart_title_slot = st.empty()
            _range_days = {"1W": 7, "1M": 30, "3M": 90, "6M": 182, "1Y": 365, "All": None}
            _range_choice = st.segmented_control(
                "Range", list(_range_days), default="All", required=True,
                key="perf_range", label_visibility="collapsed")
            _chart_title_slot.markdown(
                '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
                '<span class="ov-dot" style="background:var(--ov-green);"></span>'
                'Portfolio value over time'
                f'<span class="ov-card-meta" style="font-weight:400;margin-left:auto;">'
                f'{html_lib.escape(_range_choice)}</span></p>',
                unsafe_allow_html=True)
            _cutoff_days = _range_days[_range_choice]
            if _cutoff_days:
                _cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=_cutoff_days)
                plot_log = plot_log_full[plot_log_full["date"] >= _cutoff].reset_index(drop=True)
                if len(plot_log) < 2:
                    # Not enough history in the selected window yet -- fall
                    # back to the full series rather than show a near-empty
                    # chart for a brand-new account.
                    plot_log = plot_log_full
            else:
                plot_log = plot_log_full

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=plot_log["date"], y=plot_log["value"], name="Total capital (₹)",
                mode="lines+markers", line=dict(color="#16a34a", width=2),
                marker=dict(size=5),
                hovertemplate="₹%{y:,.0f}<extra>Total capital</extra>"))
            if plot_log["invested_amount"].notna().any():
                fig.add_trace(go.Scatter(
                    x=plot_log["date"], y=plot_log["invested_amount"],
                    name="Invested amount (₹)", mode="lines+markers",
                    line=dict(color="#6b7280", width=1.5, dash="dot"),
                    marker=dict(size=5),
                    hovertemplate="₹%{y:,.0f}<extra>Invested amount</extra>"))
            # No fill-to-zero and an explicit, padded y-range -- with a small
            # account and only a few days of history, the day-to-day move is
            # tiny relative to the absolute total (e.g. ~2.5% over 4 days), so
            # an axis forced to include ₹0 (which "fill: tozeroy" does) squashes
            # that real movement into an invisible sliver at the top: the chart
            # LOOKS flat/broken even though the underlying data is fine. Padding
            # off the actual min/max instead makes real day-to-day change
            # visible regardless of how large the total balance is.
            all_vals = pd.concat([plot_log["value"], plot_log["invested_amount"]]).dropna()
            y_lo, y_hi = float(all_vals.min()), float(all_vals.max())
            pad = (y_hi - y_lo) * 0.15 or max(y_hi * 0.02, 100.0)
            fig.update_layout(
                height=310, margin=dict(l=10, r=10, t=20, b=10),
                hovermode="x unified",
                yaxis=dict(tickprefix="₹", separatethousands=True,
                          range=[y_lo - pad, y_hi + pad]),
                legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0))
            chart_selection = st.plotly_chart(
                fig, width="stretch", on_select="rerun", selection_mode="points",
                key="equity_chart_select")
            if plot_log["invested_amount"].isna().any():
                st.caption("Invested amount only started being logged recently — "
                          "earlier days show a gap until enough history builds up.")
            st.caption("Click a point on the chart above for that day's detail.")

            _clicked_points = chart_selection.selection.points if chart_selection else []
            if _clicked_points:
                _idx = _clicked_points[0]["point_index"]
                _day = plot_log.iloc[_idx]
                _prev = plot_log.iloc[_idx - 1] if _idx > 0 else None
                with st.expander(f"📅 {_day['date']:%d %b %Y} detail", expanded=True):
                    dc1, dc2, dc3 = st.columns(3)
                    dc1.metric("Total capital", f"₹{_day['value']:,.0f}",
                              delta=(f"₹{_day['value'] - _prev['value']:+,.0f} vs prev. day"
                                    if _prev is not None else None))
                    _day_invested = _day["invested_amount"]
                    _prev_invested = _prev["invested_amount"] if _prev is not None else None
                    dc2.metric("Invested amount",
                              f"₹{_day_invested:,.0f}" if pd.notna(_day_invested) else "—",
                              delta=(f"₹{_day_invested - _prev_invested:+,.0f} vs prev. day"
                                    if pd.notna(_day_invested) and pd.notna(_prev_invested)
                                    else None))
                    _pct_deployed_day = (_day_invested / _day["value"] * 100
                                         if pd.notna(_day_invested) and _day["value"] else None)
                    dc3.metric("% deployed",
                              f"{_pct_deployed_day:.0f}%" if _pct_deployed_day is not None else "—")
        else:
            st.caption("Portfolio value is logged once a day when you open this page — "
                      "the chart builds up over time as you keep using the dashboard.")

    with col_positions:
        with st.container(border=True, key="ov-card-positions"):
            st.markdown(
                '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
                '<span class="ov-dot" '
                'style="background:var(--ov-purple);"></span>Positions '
                '<span class="ov-card-meta" style="font-weight:400;margin-left:auto;">'
                'By momentum rank</span></p>', unsafe_allow_html=True)
            if merged.empty:
                st.caption("No open positions or holdings.")
            else:
                _screen = _load_screen_cache()
                _rank_map = {}
                if _screen is not None and "score" in _screen.columns:
                    _rank_map = {sym: i + 1 for i, sym in enumerate(_screen.index)}
                _keep_zone = (config.STRATEGY.get("max_positions") or 0) * 2

                pos_desc = merged.sort_values("value", ascending=False).reset_index(drop=True)
                pos_desc["rank"] = pos_desc["symbol"].map(_rank_map)

                # Entry rank -- the "Ranked #N of M momentum candidates"
                # this symbol's FIRST open trade recorded (live_rebalance.
                # propose_rebalance()'s buy reason string, see state_db.
                # record_trade_entry()) -- so the arrow below reflects
                # this specific holding's own rank drift since it was
                # bought, not just today's snapshot. Absent for a position
                # opened outside this app (backfilled trades, no rank in
                # their reason text), which renders as no arrow rather
                # than a misleading one.
                _open_trades = state_db.get_trades(status="open")
                _entry_rank_map = {}
                if not _open_trades.empty:
                    _first_entries = (_open_trades.sort_values("entry_date")
                                     .groupby("symbol").first())
                    for _sym, _t in _first_entries.iterrows():
                        _m = re.search(r"Ranked #(\d+)", str(_t.get("entry_reason") or ""))
                        if _m:
                            _entry_rank_map[_sym] = int(_m.group(1))
                pos_desc["entry_rank"] = pos_desc["symbol"].map(_entry_rank_map)

                def _rank_badge_cls(v: str) -> str:
                    if v == "—":
                        return "ov-badge-gray"
                    return "ov-badge-green" if int(v) <= _keep_zone else "ov-badge-red"

                pos_desc["rank_fmt"] = pos_desc["rank"].apply(
                    lambda v: str(int(v)) if pd.notna(v) else "—")
                st.markdown(
                    _ov_table_html(
                        pos_desc, columns=["symbol", "value", "rank_fmt", "pnl"],
                        sym_cols=["symbol"], pnl_cols=["pnl"],
                        num_fmt={"value": "₹{:,.0f}"},
                        badges={"rank_fmt": _rank_badge_cls},
                        arrow_cols={"rank_fmt": ("rank", "entry_rank", False)}),
                    unsafe_allow_html=True)

                # Real, data-driven equivalent of the mockup's "likely
                # exit" alert -- flags currently-held symbols the LAST
                # rebalance scan actually proposed selling, not a guess.
                _last_run_pos = state_db.get_last_rebalance_run()
                if _last_run_pos is not None:
                    _sells_pos = _last_run_pos.get("sells", pd.DataFrame())
                    _at_risk = [s for s in pos_desc["symbol"] if not _sells_pos.empty
                               and s in set(_sells_pos["symbol"])]
                    if _at_risk:
                        st.markdown(
                            f'<div class="ov-alert">⚠ Likely exit next rebalance: '
                            f'{", ".join(_at_risk)}</div>', unsafe_allow_html=True)

    if not merged.empty:
        # ---- Capital allocation per stock (weight vs equal-weight target,
        # as a mini-bar drift gauge) + Asset allocation by sector (hand-
        # drawn donut + legend) -- both pure static HTML/SVG (neither had a
        # click-handler even as Plotly figures), rendered as ONE markdown
        # call each so the two-column CSS grid actually lays them out
        # side by side.
        max_positions = config.STRATEGY.get("max_positions") or 0
        target_pct = 100.0 / max_positions if max_positions else None
        total_value = float(merged["value"].sum())
        # Denominator for weight/drift is TOTAL portfolio value (cash +
        # holdings), matching target_pct's own basis -- screener.
        # allocate_equal_weight_buys() defines its target as total_equity
        # (cash+holdings) / max_positions, not holdings-only. Using
        # holdings-only here (the previous behavior) systematically
        # understated drift whenever real cash was sitting uninvested --
        # e.g. 10 positions each truly at 7.5% of a $200k account with
        # $50k idle cash would each read as exactly 10.0%/on-target
        # instead of correctly showing -2.5% drift, since 15k/150k
        # (holdings-only) = 10% even though 15k/200k (true) = 7.5%.
        total_equity = available_cash + total_value
        alloc_desc = merged.sort_values("value", ascending=False).reset_index(drop=True)

        allocbar_segments, alloc_rows = [], []
        for i, row in alloc_desc.iterrows():
            color = _OV_HEX_CYCLE[i % len(_OV_HEX_CYCLE)]
            weight_pct = (row["value"] / total_equity * 100) if total_equity else 0.0
            allocbar_segments.append(f'<div style="width:{weight_pct:.2f}%;background:{color};"></div>')
            if target_pct:
                drift = weight_pct - target_pct
                fill_pct = min(100.0, (weight_pct / target_pct) * 80)
                drift_cls = "ov-pos" if drift >= 0 else "ov-neg"
                alloc_rows.append(
                    f'<tr><td class="ov-sym">{row["symbol"]}</td><td>{weight_pct:.1f}%</td>'
                    f'<td><div class="ov-minibar"><div class="ov-fill" '
                    f'style="width:{fill_pct:.1f}%;background:{color};"></div>'
                    f'<div class="ov-tick"></div></div></td>'
                    f'<td class="r {drift_cls}">{drift:+.1f}%</td></tr>')
            else:
                alloc_rows.append(
                    f'<tr><td class="ov-sym">{row["symbol"]}</td>'
                    f'<td>{weight_pct:.1f}%</td><td></td><td class="r">—</td></tr>')
        target_meta = f"Target {target_pct:.0f}% ±" if target_pct else ""
        alloc_html = (
            '<div class="ov-card"><p class="ov-card-title">'
            '<span class="ov-dot" style="background:var(--ov-blue);"></span>'
            f'Capital allocation per stock <span class="ov-card-meta" '
            f'style="font-weight:400;margin-left:auto;">{target_meta}</span></p>'
            f'<div class="ov-allocbar">{"".join(allocbar_segments)}</div>'
            '<table class="ov-table"><tr><th>Symbol</th><th>Weight</th>'
            '<th style="width:38%;">vs target</th><th class="r">Drift</th></tr>'
            + "".join(alloc_rows) + '</table></div>')

        donut_html = ""
        try:
            membership = su.get_sector_membership(verbose=False)
            sector_of = merged["symbol"].map(lambda s: (membership.get(s) or ["Unclassified"])[0])
            symbols_by_sector = merged.assign(sector=sector_of).groupby("sector")["symbol"] \
                .apply(lambda s: ", ".join(s))
            sector_group = (merged.assign(sector=sector_of).groupby("sector")["value"]
                            .sum().sort_values(ascending=False))
            donut_total = float(sector_group.sum()) or 1.0
            legend_rows = []
            for i, (sector, value) in enumerate(sector_group.items()):
                color = _OV_HEX_CYCLE[i % len(_OV_HEX_CYCLE)]
                pct = value / donut_total * 100
                legend_rows.append(
                    '<div class="ov-sector-row"><div class="ov-sector-head">'
                    f'<span><span class="ov-sw" style="background:{color};"></span>'
                    f'{sector} · {symbols_by_sector[sector]}</span>'
                    f'<span class="ov-sym">{pct:.1f}%</span></div>'
                    '<div class="ov-sector-bar"><div class="ov-sector-fill" '
                    f'style="width:{pct:.1f}%;background:{color};"></div></div></div>')
            n_sectors = len(sector_group)
            svg = _ov_donut_svg(list(sector_group.values), str(n_sectors))
            max_sector_pct = float((sector_group / donut_total * 100).max())
            # margin-top here (unlike the Positions card's own "Likely
            # exit" ov-alert) -- that one sits inside a real st.container,
            # where Streamlit's own inter-block gap already separates it
            # from the table above; this one is glued into one raw HTML
            # string right after the donut/legend markup with no such
            # gap, so it needs its own spacing to not look flush/cramped.
            if max_sector_pct <= 25:
                alert_html = ('<div class="ov-alert ov-alert-success" style="margin-top:8px;">'
                             '✓ Diversified — no sector above 25%</div>')
            else:
                top_sector = (sector_group / donut_total * 100).idxmax()
                alert_html = (f'<div class="ov-alert" style="margin-top:8px;">'
                             f'⚠ Concentrated — {top_sector} is '
                             f'{max_sector_pct:.0f}% of the portfolio</div>')
            donut_html = (
                '<div class="ov-card"><p class="ov-card-title">'
                '<span class="ov-dot" style="background:var(--ov-purple);"></span>'
                'Asset allocation by sector <span class="ov-card-meta" '
                'style="font-weight:400;margin-left:auto;">Concentration check</span></p>'
                f'<div class="ov-donut-wrap">{svg}'
                f'<div class="ov-donut-legend">{"".join(legend_rows)}</div></div>'
                f'{alert_html}</div>')
        except Exception as e:
            donut_html = (
                '<div class="ov-card"><p class="ov-card-title">Asset allocation by sector</p>'
                f'<p class="ov-card-meta">Sector data unavailable right now: {e}</p></div>')

        st.markdown(f'<div class="ov-two-col">{alloc_html}{donut_html}</div>',
                   unsafe_allow_html=True)

        # ---- Rebalance preview (real data from the last scan) + Watchlist
        # (next-in-queue candidates from the cached screener ranking), laid
        # out side by side like the allocation/donut row above -- these two
        # have real Streamlit widgets inside (a button, cached data calls),
        # so they need actual st.columns rather than the flattened
        # single-markdown ov-two-col trick used for the pure-HTML cards.
        _last_run = state_db.get_last_rebalance_run()
        col_rebal, col_watch = st.columns(2)
        with col_rebal:
            with st.container(border=True, key="ov-card-rebal"):
                _scan_meta = (f"as of {_last_run['run_time']:%d %b %H:%M}"
                             if _last_run is not None else "")
                st.markdown(
                    '<p class="ov-card-title"><span class="ov-dot" '
                    'style="background:var(--ov-amber);"></span>Rebalance preview '
                    f'<span class="ov-card-meta" style="font-weight:400;margin-left:auto;">'
                    f'{_scan_meta}</span></p>', unsafe_allow_html=True)
                rebal_rows = []
                if _last_run is not None:
                    sells_df = _last_run.get("sells", pd.DataFrame())
                    buys_df = _last_run.get("buys", pd.DataFrame())
                    sold_syms = set(sells_df["symbol"]) if not sells_df.empty else set()
                    for _, r in sells_df.iterrows():
                        rebal_rows.append(
                            '<div class="ov-row"><span><span class="ov-badge ov-badge-red">'
                            f'Exit</span>&nbsp; {r["symbol"]}</span>'
                            f'<span class="ov-card-meta">{r.get("reason", "")}</span></div>')
                    for _, r in buys_df.iterrows():
                        price = r.get("price")
                        price_str = f"~₹{price:,.0f}" if pd.notna(price) else ""
                        rebal_rows.append(
                            '<div class="ov-row"><span><span class="ov-badge ov-badge-green">'
                            f'Enter</span>&nbsp; {r["symbol"]}</span>'
                            f'<span class="ov-card-meta">{price_str}</span></div>')
                    hold_syms = ([s for s in merged["symbol"] if s not in sold_syms]
                                if not merged.empty else [])
                    if hold_syms:
                        rebal_rows.append(
                            '<div class="ov-row"><span><span class="ov-badge ov-badge-gray">'
                            f'Hold</span>&nbsp; {", ".join(hold_syms)}</span>'
                            '<span class="ov-card-meta">Within cutoff</span></div>')
                if rebal_rows:
                    st.markdown("".join(rebal_rows), unsafe_allow_html=True)
                else:
                    st.markdown('<p class="ov-card-meta">'
                               + ("No changes proposed in the last scan." if _last_run is not None
                                  else "No scan has run yet.") + "</p>", unsafe_allow_html=True)
                if st.button("Review rebalance orders →", key="ov_review_rebal",
                            type="primary", use_container_width=True):
                    st.switch_page(page_live_rebalance_p)

        with col_watch:
            with st.container(border=True, key="ov-card-watchlist"):
                st.markdown('<p class="ov-card-title"><span class="ov-dot" '
                           'style="background:var(--ov-teal);"></span>'
                           'Watchlist — next in queue</p>', unsafe_allow_html=True)
                _screen = _load_screen_cache()
                watch_rows = []
                if _screen is not None and "all_gates" in _screen.columns:
                    held_syms = set(merged["symbol"])
                    buy_syms = (set(_last_run["buys"]["symbol"])
                               if _last_run is not None and not _last_run.get(
                                   "buys", pd.DataFrame()).empty else set())
                    candidates = _screen[_screen["all_gates"]]
                    for i, sym in enumerate(candidates.index):
                        if sym in held_syms:
                            continue
                        rank = i + 1
                        if sym in buy_syms:
                            watch_rows.append(
                                f'<div class="ov-row"><span class="ov-sym">{sym}</span>'
                                f'<span class="ov-badge ov-badge-green">Rank {rank} · entering</span></div>')
                        else:
                            watch_rows.append(
                                f'<div class="ov-row"><span class="ov-sym">{sym}</span>'
                                f'<span class="ov-card-meta">Rank {rank} · reserve</span></div>')
                        if len(watch_rows) >= 4:
                            break
                if watch_rows:
                    st.markdown("".join(watch_rows), unsafe_allow_html=True)
                else:
                    st.markdown('<p class="ov-card-meta">Run a scan (Live Rebalance or '
                               'Screener) to populate the watchlist.</p>', unsafe_allow_html=True)

    st.divider()
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
                with st.container(border=True, key="ov-card-funds-available"):
                    st.markdown('<p class="ov-card-title">Available</p>', unsafe_allow_html=True)
                    st.markdown(_ov_table_html(_breakdown_table(m["available"])),
                               unsafe_allow_html=True)
            with fc2:
                with st.container(border=True, key="ov-card-funds-utilised"):
                    st.markdown('<p class="ov-card-title">Utilised</p>', unsafe_allow_html=True)
                    st.markdown(_ov_table_html(_breakdown_table(m["utilised"])),
                               unsafe_allow_html=True)
        except Exception as e:
            st.warning(f"Could not fetch funds breakdown: {e}")

# ---------------------------------------------------------------------------
# Page: Ledger
# ---------------------------------------------------------------------------

def page_ledger():
    st.markdown(
        '<div class="ov-header"><div><span class="ov-h1">💰 Ledger</span> '
        '<span class="ov-sub">· The deposit/withdrawal ledger used for XIRR</span></div></div>',
        unsafe_allow_html=True)

    ledger = state_db.get_cash_flows()
    _ledger_sel_id = st.session_state.get("_admin_ledger_sel_id")
    _editing = (_ledger_sel_id is not None and not ledger.empty
               and _ledger_sel_id in ledger["id"].values)

    _cashflow_tip = html_lib.escape(
        "Kite's API can't see bank transfers — this ledger is what keeps "
        "XIRR accurate as you add money over time.")
    with st.container(border=True, key="ov-card-admin-cashflow"):
        st.markdown(
            '<p class="ov-card-title"><span class="ov-dot" '
            'style="background:var(--ov-green);"></span>💰 Log a deposit / withdrawal'
            f'<span class="ov-info-icon" title="{_cashflow_tip}">ℹ️</span>'
            + ('<span class="ov-card-meta" style="font-weight:400;margin-left:auto;">'
               'Editing the entry selected below</span>' if _editing else '')
            + '</p>', unsafe_allow_html=True)
        if _editing:
            _edit_row = ledger.set_index("id").loc[_ledger_sel_id]
            _def_date = pd.to_datetime(_edit_row["date"]).date()
            _def_amount = float(_edit_row["amount"])
            _def_note = _edit_row["note"] or ""
        else:
            _def_date, _def_amount, _def_note = dt.date.today(), 0.0, ""
        with st.form(f"cash_flow_form_{_ledger_sel_id if _editing else 'new'}",
                     clear_on_submit=not _editing):
            cff1, cff2 = st.columns(2)
            cf_date = cff1.date_input("Date", value=_def_date)
            cf_amount = cff2.number_input(
                "Amount (₹) — + deposit / − withdrawal",
                value=_def_amount, step=1000.0, format="%.2f")
            cf_note = st.text_input("Note (optional)", value=_def_note)
            if _editing:
                fb1, fb2 = st.columns(2)
                cf_submitted = fb1.form_submit_button(
                    "Save changes", type="primary", use_container_width=True)
                cf_delete_clicked = fb2.form_submit_button(
                    "Delete entry", key="admin_ledger_delete_btn", use_container_width=True)
            else:
                cf_submitted = st.form_submit_button("Log cash flow", type="primary")
                cf_delete_clicked = False
        if cf_submitted:
            if cf_amount == 0:
                st.error("Amount can't be zero.")
            elif _editing:
                state_db.update_cash_flow(_ledger_sel_id, cf_date.isoformat(),
                                          float(cf_amount), cf_note)
                st.session_state["_admin_ledger_sel_id"] = None
                st.success("Updated.")
                st.rerun()
            else:
                state_db.record_cash_flow(cf_date.isoformat(), float(cf_amount), cf_note)
                st.success("Logged.")
                st.rerun()
        if cf_delete_clicked:
            state_db.delete_cash_flow(_ledger_sel_id)
            st.session_state["_admin_ledger_sel_id"] = None
            st.success("Deleted.")
            st.rerun()
        if _editing:
            if st.button("+ Log a new entry instead", key="admin_ledger_new_entry_btn"):
                st.session_state["_admin_ledger_sel_id"] = None
                st.rerun()

    with st.container(border=True, key="ov-card-admin-ledger"):
        st.markdown(
            '<p class="ov-card-title"><span class="ov-dot" '
            'style="background:var(--ov-green);"></span>Ledger'
            '<span class="ov-card-meta" style="font-weight:400;margin-left:auto;">'
            'Select a row to edit or delete it above</span></p>',
            unsafe_allow_html=True)
        if ledger.empty:
            st.caption("No cash flows logged yet.")
        else:
            display_ledger = ledger.sort_values("date", ascending=False).reset_index(drop=True)
            display_ledger["date"] = pd.to_datetime(display_ledger["date"]).dt.date
            ledger_page = _ov_page_slice(display_ledger, key="admin_ledger")

            with st.container(key="admin_ledger_head"):
                hh1, hh2, hh3, hh4 = st.columns([0.4, 1.3, 1.3, 2.4])
                hh2.markdown('<span class="ov-manual-th">Date</span>', unsafe_allow_html=True)
                hh3.markdown('<span class="ov-manual-th r">Amount (₹)</span>', unsafe_allow_html=True)
                hh4.markdown('<span class="ov-manual-th">Note</span>', unsafe_allow_html=True)
            with st.container(key="admin_ledger_rows"):
                for _, r in ledger_page.iterrows():
                    rid = int(r["id"])
                    rc1, rc2, rc3, rc4 = st.columns([0.4, 1.3, 1.3, 2.4])
                    with rc1:
                        if st.button("●" if rid == _ledger_sel_id else "○",
                                    key=f"admin_ledger_radio_{rid}",
                                    help="Select to edit/delete"):
                            st.session_state["_admin_ledger_sel_id"] = (
                                None if rid == _ledger_sel_id else rid)
                            st.rerun()
                    amt = float(r["amount"])
                    amt_cls = "ov-pos" if amt >= 0 else "ov-neg"
                    rc2.markdown(f'<span class="ov-manual-cell">{r["date"]}</span>',
                                unsafe_allow_html=True)
                    rc3.markdown(
                        f'<span class="ov-manual-cell r {amt_cls} ov-sym">{amt:+,.2f}</span>',
                        unsafe_allow_html=True)
                    rc4.markdown(
                        f'<span class="ov-manual-cell">{html_lib.escape(r["note"] or "—")}</span>',
                        unsafe_allow_html=True)
            _ov_pagination_controls(display_ledger, key="admin_ledger")


# ---------------------------------------------------------------------------
# Page: Admin
# ---------------------------------------------------------------------------

def page_admin():
    st.markdown(
        '<div class="ov-header"><div><span class="ov-h1">⚙️ Admin</span> '
        '<span class="ov-sub">Kite settings and strategy configuration</span></div></div>',
        unsafe_allow_html=True)

    if state_db.is_using_default_dashboard_password(config.DASHBOARD_USERNAME, config.DASHBOARD_PASSWORD):
        st.markdown(
            '<div class="ov-alert">⚠️ Using the default Admin/Admin login — '
            'this app places real orders. Change it in "Change dashboard '
            'password" below.</div>', unsafe_allow_html=True)

    _strategy_tip = html_lib.escape(
        "Stored in state.db (strategy_config table) — takes effect "
        "immediately for this dashboard process (no restart needed), and "
        "for the next scheduled/manual rebalance scan. See the README for "
        "the research behind each default.")
    st.markdown(
        '<p class="ov-card-title" style="margin-top:14px;"><span class="ov-dot" '
        'style="background:var(--ov-purple);"></span>🎯 Strategy configuration'
        f'<span class="ov-info-icon" title="{_strategy_tip}">ℹ️</span>'
        '<span class="ov-card-meta" style="font-weight:400;margin-left:auto;">'
        'Takes effect immediately — no restart needed</span></p>',
        unsafe_allow_html=True)
    cfg = config.STRATEGY
    with st.container(border=True, key="ov-card-admin-strategy"):
        with st.form("strategy_config_form"):
            st.markdown('<p class="ov-muted">Portfolio &amp; risk</p>', unsafe_allow_html=True)
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

            st.markdown('<p class="ov-muted">Trailing stop</p>', unsafe_allow_html=True)
            c4, c5 = st.columns(2)
            trailing_stop_enabled = c4.checkbox(
                "Enabled", value=bool(cfg["trailing_stop_enabled"]))
            trailing_atr_multiple = c5.number_input(
                "Trailing stop (× ATR)", min_value=0.5, max_value=10.0,
                value=float(cfg["trailing_atr_multiple"]), step=0.1)

            st.markdown('<p class="ov-muted">Automation</p>', unsafe_allow_html=True)
            c4b, c4c, c4d = st.columns(3)
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
            rebalance_cadence = c4d.segmented_control(
                "Rebalance cadence", ["daily", "monthly"],
                default=cfg.get("rebalance_cadence", "daily"), key="admin_rebalance_cadence",
                help="How often the SELL/keep-zone decision is re-evaluated. "
                     "'daily' checks every scheduled run; 'monthly' only on "
                     "the first trading day of each month (matching the "
                     "backtest's own monthly rb_dates). Either way, new buys "
                     "still fill any already-open slot the same day it opens "
                     "-- only the sell decision is gated. Real 2016-2026 data "
                     "+ a 5-seed synthetic test both found daily meaningfully "
                     "reduces max drawdown (~-50% to ~-35% over 10 years) at "
                     "a real but smaller cost to CAGR -- a priced trade-off, "
                     "not a free win.")

            st.markdown('<p class="ov-muted">Momentum &amp; trend</p>', unsafe_allow_html=True)
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

            st.markdown('<p class="ov-muted">Momentum score weights (fixed, not configurable)</p>',
                       unsafe_allow_html=True)
            st.caption(
                "Score = **0.40** × 6-month relative strength + **0.25** × "
                "3-month relative strength + **0.20** × 52-week-high "
                "proximity + **0.15** × volume expansion (20d avg volume ÷ "
                "60d avg volume) — each Z-scored across that day's universe "
                "before weighting. These four weights are hardcoded in "
                "`screener.score()`, the same function backtest.py and live "
                "both call — there's no slider for them here because there's "
                "nothing to save. The two tilts below (sector/fundamental "
                "bonus) are the only adjustable additions on top of this "
                "base score.")

            st.markdown('<p class="ov-muted">RSI</p>', unsafe_allow_html=True)
            c12, c13 = st.columns(2)
            rsi_min = c12.number_input(
                "RSI min", min_value=0, max_value=100, value=int(cfg["rsi_min"]), step=1)
            rsi_max = c13.number_input(
                "RSI max", min_value=0, max_value=100, value=int(cfg["rsi_max"]), step=1)

            st.markdown('<p class="ov-muted">Fundamental gate &amp; sector bonus (opt-in features)</p>',
                       unsafe_allow_html=True)
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
                help="0 = off (recommended). Re-tested with the equal-weight "
                     "allocator specifically (0.5/1.0/2.0): loses on CAGR and "
                     "Sharpe at every weight, AND drawdown gets worse too "
                     "(-19.58%->-22 to -27%), so there's no risk/reward "
                     "trade-off to make here, unlike the fundamental gate. "
                     "See README's Sector relative-strength section.")
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
                "rebalance_cadence": rebalance_cadence,
                "mom_lookback_days_short": int(mom_lookback_days_short),
                "mom_lookback_days_long": int(mom_lookback_days_long),
                "skip_recent_days": int(skip_recent_days),
                "near_high_threshold": float(near_high_threshold) / 100,
                "ema_fast": int(ema_fast),
                "ema_slow": int(ema_slow),
                "rsi_min": int(rsi_min),
                "rsi_max": int(rsi_max),
                "fundamental_gate_enabled": bool(fundamental_gate_enabled),
                "min_fundamental_score": float(min_fundamental_score),
                "fundamental_bonus_weight": float(fundamental_bonus_weight),
                "sector_bonus_weight": float(sector_bonus_weight),
                "history_days": int(history_days),
            }
            state_db.update_strategy_config(updates)
            config.STRATEGY.update(updates)  # live for this process -- no restart needed
            st.success("Strategy settings saved — in effect immediately.")

    st.markdown(
        '<p class="ov-card-title" style="margin-top:14px;"><span class="ov-dot" '
        'style="background:var(--ov-blue);"></span>🔔 Push notifications</p>',
        unsafe_allow_html=True)
    with st.container(border=True, key="ov-card-admin-push"):
        _n_subs = len(state_db.get_push_subscriptions())
        if not notify.VAPID_PUBLIC_KEY:
            st.caption("Not configured -- set VAPID_PRIVATE_KEY/VAPID_PUBLIC_KEY "
                      "in .env to enable this (see notify.py's docstring for "
                      "how to generate a keypair).")
        else:
            st.caption(f"{_n_subs} device(s) currently subscribed. Alerts fire "
                      "when today's Kite session expires and needs a fresh "
                      "login (the daily ~07:00 check, or any scheduled job "
                      "that hits it mid-run), and after every scheduled "
                      "rebalance completes. Click below on every phone or "
                      "laptop browser you want alerted.")
            pb1, pb2 = st.columns(2)
            if pb1.button("Enable notifications on this device"):
                st.html(f"""
<script>
(async () => {{
  try {{
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {{
      alert('Push notifications are not supported in this browser.');
      return;
    }}
    function urlBase64ToUint8Array(base64String) {{
      const padding = '='.repeat((4 - base64String.length % 4) % 4);
      const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
      const rawData = window.atob(base64);
      const arr = new Uint8Array(rawData.length);
      for (let i = 0; i < rawData.length; ++i) arr[i] = rawData.charCodeAt(i);
      return arr;
    }}
    const reg = await navigator.serviceWorker.register('/app/static/sw.js');
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') {{ alert('Notification permission was not granted.'); return; }}
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {{
      sub = await reg.pushManager.subscribe({{
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array('{notify.VAPID_PUBLIC_KEY}')
      }});
    }}
    const pushServerUrl = 'https://' + window.location.hostname + ':{notify.PUSH_SERVER_PORT}';
    const resp = await fetch(pushServerUrl + '/subscribe', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(sub.toJSON())
    }});
    alert(resp.ok ? 'Notifications enabled on this device.'
                  : 'Could not save the subscription (server error). Try again in a moment.');
  }} catch (e) {{
    alert('Failed to enable notifications: ' + e.message);
  }}
}})();
</script>
""", unsafe_allow_javascript=True)
            if pb2.button("Send test notification", disabled=_n_subs == 0):
                _dead = notify.send_webpush_all(
                    state_db.get_push_subscriptions(),
                    "KK Trading -- test notification",
                    "If you see this, browser push is wired up correctly.",
                    notify.DASHBOARD_URL)
                for _d in _dead:
                    state_db.delete_push_subscription(_d)
                st.success(f"Sent to {_n_subs - len(_dead)} device(s)."
                          + (f" Removed {len(_dead)} dead subscription(s)." if _dead else ""))

    _skip_tip = html_lib.escape(
        "Manually excluded symbols are removed from config.UNIVERSE — the "
        "shared candidate list the Screener, Live Rebalance, and "
        "backtest.py all fetch candles for. Tick/untick Skip, optionally "
        "edit the reason, then click Update — nothing changes until you do.")
    st.markdown(
        '<p class="ov-card-title" style="margin-top:14px;"><span class="ov-dot" '
        'style="background:var(--ov-coral);"></span>🚫 Skip stocks from scanner'
        f'<span class="ov-info-icon" title="{_skip_tip}">ℹ️</span>'
        '<span class="ov-card-meta" style="font-weight:400;margin-left:auto;">'
        'Removed from the shared universe everywhere at once</span></p>',
        unsafe_allow_html=True)
    with st.container(border=True, key="ov-card-admin-skip"):
        _skipped_df = state_db.get_skipped_symbols_df()
        skipped_reasons = (_skipped_df.set_index("symbol")["reason"] if not _skipped_df.empty
                          else pd.Series(dtype=str))
        all_syms_for_skip = sorted(set(config.UNIVERSE_RAW) | set(skipped_reasons.index))

        st.markdown('<p class="ov-muted">Skip / un-skip a symbol</p>', unsafe_allow_html=True)
        skip_sym = st.selectbox("Symbol", all_syms_for_skip, key="admin_skip_sym_sel")
        skip_checked = skip_sym in skipped_reasons.index
        with st.form(f"admin_skip_form_{skip_sym}"):
            skip_toggle = st.checkbox("Skip this symbol", value=skip_checked)
            skip_reason = st.text_input(
                "Reason (optional)", value=skipped_reasons.get(skip_sym, ""))
            skip_save_clicked = st.form_submit_button("Save", type="primary")
        if skip_save_clicked:
            if skip_toggle:
                state_db.add_skipped_symbol(skip_sym, skip_reason)
            elif skip_checked:
                state_db.remove_skipped_symbol(skip_sym)
            config.refresh_universe()
            st.success(f"{skip_sym} updated — in effect immediately.")
            st.rerun()

        st.divider()
        skip_filter = st.radio(
            "Show", ["All", "Skipped only", "Not skipped"], horizontal=True,
            key="admin_skip_filter", label_visibility="collapsed")

        try:
            skip_prices = kite_client.get_ltp(all_syms_for_skip)
        except Exception as e:
            st.warning(f"Couldn't fetch live prices: {e}")
            skip_prices = {}

        skip_table = pd.DataFrame({
            "symbol": all_syms_for_skip,
            "price": [skip_prices.get(s) for s in all_syms_for_skip],
            "skip": ["✓" if s in skipped_reasons.index else "✗" for s in all_syms_for_skip],
            "reason": [skipped_reasons.get(s, "") or "—" for s in all_syms_for_skip],
        })
        if skip_filter == "Skipped only":
            skip_table = skip_table[skip_table["skip"] == "✓"]
        elif skip_filter == "Not skipped":
            skip_table = skip_table[skip_table["skip"] == "✗"]
        skip_page = _ov_page_slice(skip_table, key="admin_skip")
        st.markdown(
            _ov_table_html(
                skip_page, columns=["symbol", "price", "skip", "reason"],
                sym_cols=["symbol"], num_fmt={"price": "₹{:,.2f}"},
                badges={"skip": {"✓": "ov-badge-red", "✗": "ov-badge-gray"}}),
            unsafe_allow_html=True)
        _ov_pagination_controls(skip_table, key="admin_skip")

    col_pw, col_api = st.columns(2)
    with col_pw:
      with st.container(border=True, key="ov-card-admin-password"):
        st.markdown(
            '<p class="ov-card-title"><span class="ov-dot" '
            'style="background:var(--ov-amber);"></span>🔑 Change dashboard password</p>',
            unsafe_allow_html=True)
        st.caption("Stored as a salted hash in state.db — the password "
                  "itself is never saved anywhere, not even here.")
        with st.form("change_password_form", clear_on_submit=True):
            new_user = st.text_input("Username", value=config.DASHBOARD_USERNAME)
            new_pw = st.text_input("New password", type="password")
            confirm_pw = st.text_input("Confirm new password", type="password")
            change_submitted = st.form_submit_button("Update credentials", type="primary")
        if change_submitted:
            if not new_user or not new_pw:
                st.error("Username and password can't be empty.")
            elif new_pw != confirm_pw:
                st.error("Passwords don't match.")
            else:
                state_db.update_dashboard_password(new_user, new_pw)
                st.success("Credentials updated — use the new username/password "
                          "next time you sign in.")

    with col_api:
      with st.container(border=True, key="ov-card-admin-kite"):
        _kite_tip = html_lib.escape(
            "Stored in state.db, not .env — only needed if you regenerate "
            "keys in the Kite developer console. Unlike the dashboard "
            "password, these are kept plaintext (Kite's own login flow "
            "needs the real api_secret value back), so this is a "
            "convenience move, not a security upgrade.")
        st.markdown(
            '<p class="ov-card-title"><span class="ov-dot" '
            'style="background:var(--ov-blue);"></span>🔑 Kite API settings'
            f'<span class="ov-info-icon" title="{_kite_tip}">ℹ️</span></p>',
            unsafe_allow_html=True)
        masked_key = (config.KITE_API_KEY[:4] + "…" + config.KITE_API_KEY[-4:]
                     if len(config.KITE_API_KEY) > 8 else "(not set)")
        st.caption(f"Current API key: `{masked_key}` · stored in state.db, "
                  "only needed after regenerating keys.")
        with st.form("kite_api_settings_form", clear_on_submit=True):
            new_api_key = st.text_input("New API key (blank = keep current)")
            new_api_secret = st.text_input("New API secret (blank = keep current)",
                                           type="password")
            api_submitted = st.form_submit_button("Update Kite API credentials", type="primary")
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
    _screener_tip = html_lib.escape(
        "Every F&O stock passing the technical gates (trend structure, "
        "52-week-high proximity, RSI regime) and the fundamental quality "
        "gate, ranked by momentum score. This is the broader browse/chart "
        "view; Live Rebalance shows only what actually fits your open "
        "position slots.")
    hdr_l, hdr_r = st.columns([2, 2])
    with hdr_l:
        st.markdown(
            '<div class="ov-header" style="margin-bottom:0;">'
            '<div><span class="ov-h1">🔍 Screener</span> '
            '<span class="ov-sub">· full ranked universe</span>'
            f'<span class="ov-info-icon" title="{_screener_tip}">ℹ️</span></div></div>',
            unsafe_allow_html=True)

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

    with hdr_r:
        with st.container(key="screen_run_row"):
            with_fund = st.checkbox(
                "Fetch fundamental score", value=True,
                help="Shows each candidate's fundamental score/sector rubric for "
                     "reference (uses the Fundamentals page's primary-XBRL score, "
                     "reusing the on-disk cache if present rather than re-scanning "
                     "NSE). Whether it also FILTERS candidates is a separate "
                     "toggle — see Admin → Strategy configuration → 'Filter "
                     "candidates on fundamental score' (off by default).")
            if st.button("Run screen", type="primary", disabled=screen_running):
                start_background_job(
                    "screen_run", _run_and_cache_screen, with_fund,
                    st.session_state.get("value_scores"), job_type="screen_run",
                    summarize_fn=lambda r: f"{len(r)} candidates")
                st.rerun()
    if screen_running:
        st.info(f"⏳ Scan running since {screen_job['started_at']:%H:%M:%S} — safe to switch tabs.")

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
    _candidates_tip = html_lib.escape(
        f"Sorted by score, highest first — Live Rebalance keeps a held "
        f"position only while it's ranked in the top {keep_zone_size} "
        f"here (max_positions × 2); dropping below that rank is what "
        f"triggers a proposed sell.")
    with st.container(border=True, key="ov-card-screen-candidates"):
        st.markdown(
            '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
            '<span class="ov-dot" style="background:var(--ov-green);">'
            f'</span>Candidates passing all gates <span class="ov-badge ov-badge-green">'
            f'{len(candidates)}</span>'
            f'<span class="ov-info-icon" title="{_candidates_tip}">ℹ️</span></p>',
            unsafe_allow_html=True)
        cand_display = candidates[show_cols].copy()
        cand_display.insert(0, "symbol", cand_display.index)
        cand_display["rank"] = cand_display["rank"].astype(int)
        cand_page = _ov_page_slice(cand_display, key="screen_candidates")
        st.markdown(
            _ov_table_html(
                cand_page, columns=["rank", "symbol"] + [c for c in show_cols if c != "rank"],
                sym_cols=["symbol"], num_fmt={**num_fmt, "rank": "{:.0f}"},
                badges={"rank": lambda v: ("ov-badge-green" if v <= keep_zone_size else "ov-badge-red")}),
            unsafe_allow_html=True)
        _ov_pagination_controls(cand_display, key="screen_candidates")

    with st.expander("Full universe (including gate failures)"):
        gate_cols = ["trend_ok", "near_high_ok", "rsi_ok", "quality_ok", "quality_fails"]
        all_cols = gate_cols + show_cols
        all_cols = [c for c in all_cols if c in t.columns and c != "rank"]
        full_display = t[all_cols].copy()
        full_display.insert(0, "symbol", full_display.index)
        # ✓/✗ pill badges instead of literal "True"/"False" text, matching
        # the mockup.
        for c in ("trend_ok", "near_high_ok", "rsi_ok", "quality_ok"):
            if c in full_display.columns:
                full_display[c] = full_display[c].map({True: "✓", False: "✗"})
        _bool_badges = {c: {"✓": "ov-badge-green", "✗": "ov-badge-red"}
                       for c in ("trend_ok", "near_high_ok", "rsi_ok", "quality_ok")
                       if c in all_cols}
        full_page = _ov_page_slice(full_display, key="screen_full_universe")
        st.markdown(
            _ov_table_html(full_page, sym_cols=["symbol"], num_fmt=num_fmt,
                          badges=_bool_badges),
            unsafe_allow_html=True)
        _ov_pagination_controls(full_display, key="screen_full_universe")

    with st.container(border=True, key="ov-card-screen-chart"):
        st.markdown(
            '<p class="ov-card-title"><span class="ov-dot" '
            'style="background:var(--ov-purple);"></span>Chart a symbol</p>',
            unsafe_allow_html=True)
        sym = st.selectbox("Chart a symbol", list(t.index), label_visibility="collapsed")
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
            _rank_series = st.session_state["sector_rank"].sort_values(ascending=False)
            _max_abs = float(_rank_series.abs().max()) or 1.0
            _sector_rows = []
            for _sector, _val in _rank_series.items():
                _pct_width = min(100.0, abs(_val) / _max_abs * 100)
                _color = "#1d9e75" if _val >= 0 else "#e24b4a"
                _cls = "ov-pos" if _val >= 0 else "ov-neg"
                _sector_rows.append(
                    f'<div class="ov-sector-row"><div class="ov-sector-head">'
                    f'<span>{html_lib.escape(str(_sector))}</span>'
                    f'<span class="ov-sym {_cls}">{_val:+.1f}</span></div>'
                    f'<div class="ov-sector-bar"><div class="ov-sector-fill" '
                    f'style="width:{_pct_width:.1f}%;background:{_color};"></div></div></div>')
            st.markdown("".join(_sector_rows), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page: Live Rebalance
# ---------------------------------------------------------------------------

def page_live_rebalance():
    auto_exec = bool(config.STRATEGY.get("auto_execute_trades", False))
    rebalance_job = get_background_job("rebalance_run")
    rebalance_running = rebalance_job is not None and not rebalance_job["done"]

    def _run_scan_now():
        fundamentals = st.session_state.get("value_scores")
        start_background_job(
            "rebalance_run", lr.propose_rebalance, available_cash,
            fundamentals=fundamentals, job_type="rebalance_scan",
            summarize_fn=lambda r: (f"{len(r['buys'])} buys, {len(r['sells'])} sells, "
                                    f"{len(r['stop_updates'])} stop updates"))
        st.rerun()

    # Auto-execute mode still shows its status chip in the header (the
    # scan button stays further down as the manual-override path). Manual
    # mode drops the "Manual mode — review-first" label entirely and puts
    # the actual "Run today's scan" button in the header instead, since
    # that's the one thing this page's whole title bar exists to trigger.
    if auto_exec:
        _auto_exec_tip = html_lib.escape(
            "auto_execute_trades is ON — the scheduled daily scan places "
            "these sells/buys/top-ups as real orders automatically, with no "
            "confirmation step. The buttons below still work as a manual "
            "override for whatever's left (e.g. a manual 'Run today's scan'). "
            "Turn this off in Admin → Strategy configuration to go back to "
            "manual-only.")
        st.markdown(
            '<div class="ov-header"><div><span class="ov-h1">📡 Live Rebalance</span> '
            '<span class="ov-sub">· review, then execute</span></div>'
            '<div class="ov-chips">'
            f'<span class="ov-info-icon" title="{_auto_exec_tip}">ℹ️</span>'
            '<span class="ov-chip ov-chip-amber">'
            '⚠ Auto-execute ON</span></div></div>', unsafe_allow_html=True)
    else:
        _hdr_l, _hdr_r = st.columns([5, 2])
        with _hdr_l:
            st.markdown(
                '<div class="ov-header" style="margin-bottom:0;">'
                '<div><span class="ov-h1">📡 Live Rebalance</span> '
                '<span class="ov-sub">· review, then execute</span></div>'
                '</div>', unsafe_allow_html=True)
        with _hdr_r:
            if st.button("Run today's scan", type="primary",
                        disabled=rebalance_running, key="lr_run_scan_hdr"):
                _run_scan_now()

    if "rebalance_proposal" not in st.session_state:
        last_run = state_db.get_last_rebalance_run()
        if last_run is not None:
            # holdings is never persisted (see state_db.get_last_rebalance_run
            # -- it's a live snapshot, not historical), so always re-fetch it
            # fresh here regardless of when the underlying proposal ran.
            last_run["holdings"] = lr.get_live_holdings().reset_index().rename(
                columns={"tradingsymbol": "symbol"})
            st.session_state["rebalance_proposal"] = last_run

    if rebalance_running:
        st.info(f"⏳ Scan running since {rebalance_job['started_at']:%H:%M:%S} — safe to switch tabs.")
    if auto_exec:
        _, _scan_col = st.columns([5, 2])
        with _scan_col:
            if st.button("Run today's scan", type="primary",
                        disabled=rebalance_running, key="lr_run_scan_autoexec"):
                _run_scan_now()

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
    if rebalance_running:
        st.warning("⏳ A new scan is running — actions below are locked until "
                  "it finishes, so you can't execute against this now-stale "
                  "proposal while a fresh one is being computed.")

    _real_max_positions = config.STRATEGY["max_positions"]
    _real_target = result.get("target_per_slot") or 0
    _cash_pool = result.get("cash_pool") or 0
    st.markdown(
        '<div class="ov-grid-metrics">'
        + _ov_metric_html("Current holdings", str(len(result["holdings"])), "CNC positions", "", "blue")
        + _ov_metric_html("Proposed sells", str(len(result["sells"])),
                         (result["sells"].iloc[0]["symbol"] if not result["sells"].empty else None),
                         "", "red")
        + _ov_metric_html("Proposed buys", str(len(result["buys"])),
                         (result["buys"].iloc[0]["symbol"] if not result["buys"].empty else None),
                         "", "green")
        + _ov_metric_html("Open slots after sells", str(result["open_slots"]),
                         f"of {_real_max_positions} max", "", "purple")
        + _ov_metric_html("Target / slot", f"₹{_real_target:,.0f}", "Equal weight", "", "teal")
        + _ov_metric_html("Cash pool", f"₹{_cash_pool:,.0f}", "incl. sell proceeds", "", "amber")
        + '</div>', unsafe_allow_html=True)

    # cash_shortfall/target_per_slot/cash_pool are snapshotted once at
    # proposal time and never recomputed -- once every buy/top-up from
    # this proposal has actually executed (or there were none to begin
    # with), this message is describing a cash situation that's no
    # longer relevant to anything still actionable, so skip it entirely
    # rather than show a stale "buys below may be partial" next to an
    # empty buys table.
    still_actionable = (not result["buys"].empty
                        or not result.get("top_ups", pd.DataFrame()).empty)
    if still_actionable and result.get("cash_shortfall") is not None:
        target = result.get("target_per_slot") or 0
        pool = result.get("cash_pool") or 0
        shortfall = result.get("cash_shortfall") or 0
        if shortfall > 0:
            st.markdown(
                f'<div class="ov-alert">💰 <b>₹{shortfall:,.0f} more needed</b> to '
                f"fully equal-weight every open slot and under-target holding "
                f"(target ₹{target:,.0f}/slot, ₹{pool:,.0f} available including "
                "proposed sell proceeds) — buys below may be partial or fewer "
                "than ideal until more cash is added.</div>", unsafe_allow_html=True)
        else:
            st.markdown(
                '<div class="ov-alert ov-alert-success">✅ Enough cash '
                f"(₹{pool:,.0f} available including proposed sell proceeds) to "
                f"fully equal-weight every open slot and under-target holding at "
                f"₹{target:,.0f}/slot.</div>", unsafe_allow_html=True)
        unsettled = result.get("unsettled_proceeds") or 0
        if unsettled:
            st.markdown(
                f'<div class="ov-alert ov-alert-info">ℹ️ ₹{unsettled:,.0f} of '
                "today's sell proceeds is from a same-day position or T1 (BTST) "
                "holding — already excluded from the available figure above, "
                "since Zerodha won't treat it as usable cash until that "
                "settlement cycle completes (next trading day for T1, the day "
                "after for a same-day sale).</div>", unsafe_allow_html=True)

    with st.container(border=True, key="ov-card-lr-sells"):
        _keep_zone_size = (config.STRATEGY.get("max_positions") or 0) * 2
        st.markdown(
            '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
            '<span class="ov-dot" style="background:var(--ov-red);">'
            f'</span>Proposed sells <span class="ov-badge ov-badge-red">{len(result["sells"])}</span>'
            f'<span class="ov-card-meta" style="font-weight:400;margin-left:auto;">'
            f'Dropped out of the top-{_keep_zone_size} keep zone</span></p>',
            unsafe_allow_html=True)
        if st.session_state.get("sell_exec_log"):
            st.success("Sell(s) executed and removed from the list below.")
            for line in st.session_state["sell_exec_log"]:
                st.write(line)
            del st.session_state["sell_exec_log"]
        if not result["sells"].empty:
            _sells_display = result["sells"].copy()
            try:
                _ltp_map = kite_client.get_ltp(list(_sells_display["symbol"]))
                _sells_display["ltp"] = _sells_display["symbol"].map(_ltp_map)
            except Exception:
                _sells_display["ltp"] = pd.NA
            _sells_display["pnl"] = (
                (_sells_display["ltp"] - _sells_display["avg_price"]) * _sells_display["qty"])
            st.markdown(
                _ov_table_html(
                    _sells_display,
                    columns=["symbol", "qty", "avg_price", "ltp", "pnl", "reason"],
                    sym_cols=["symbol"], pnl_cols=["pnl"],
                    num_fmt={"qty": "{:.0f}", "avg_price": "₹{:.2f}", "ltp": "₹{:.2f}",
                             "pnl": "₹{:+,.0f}"}),
                unsafe_allow_html=True)
            confirm_sell = st.checkbox(
                "I confirm I want to execute ALL proposed sells at market",
                key="confirm_sell_all")
            if st.button("Execute all sells", disabled=not confirm_sell or rebalance_running,
                        use_container_width=True, key="lr_execute_sells"):
                log, succeeded, failed = lr.execute_sells(result["sells"])
                st.session_state["sell_exec_log"] = log
                resolved = succeeded + list(failed)
                if resolved:
                    result["sells"] = result["sells"][
                        ~result["sells"]["symbol"].isin(resolved)].reset_index(drop=True)
                    result["open_slots"] = result.get("open_slots", 0) + len(succeeded)
                    st.session_state["rebalance_proposal"] = result
                    state_db.mark_rebalance_sells_executed(result.get("run_id"), succeeded)
                    state_db.mark_rebalance_sells_failed(result.get("run_id"), failed)
                    state_db.set_rebalance_open_slots(result.get("run_id"), result["open_slots"])
                st.rerun()
        elif not result.get("is_rebalance_day", True):
            st.caption(f"Sell/keep-zone rule not evaluated today -- "
                      f"'{result.get('rebalance_cadence', 'daily')}' cadence selected in "
                      f"Admin, only checked on the first trading day of the month. "
                      f"New buys still fill any already-open slot as usual.")
        else:
            st.caption("No current holdings fail the rebalance rule today.")

    with st.container(border=True, key="ov-card-lr-buys"):
        st.markdown(
            '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
            '<span class="ov-dot" style="background:var(--ov-green);">'
            f'</span>Proposed buys <span class="ov-badge ov-badge-green">{len(result["buys"])}</span>'
            '<span class="ov-card-meta" style="font-weight:400;margin-left:auto;">'
            'Sized off real available cash</span></p>',
            unsafe_allow_html=True)
        if st.session_state.get("buy_exec_log"):
            st.success("Buy(s) executed and removed from the list below.")
            for line in st.session_state["buy_exec_log"]:
                st.write(line)
            del st.session_state["buy_exec_log"]
        if not result["buys"].empty:
            # "rank" was added to the buys dict alongside this restyle -- a
            # proposal already sitting in session state (or loaded from a
            # run stored before this change) won't have it, so build the
            # column list from what's actually present rather than assume.
            _buys_display = result["buys"].copy()
            _buys_display["amount"] = _buys_display["qty"] * _buys_display["price"]
            _buys_cols = [c for c in
                         ["symbol", "rank", "score", "price", "qty", "amount", "stop",
                          "fundamental_score"] if c in _buys_display.columns]
            st.markdown(
                _ov_table_html(
                    _buys_display, columns=_buys_cols, sym_cols=["symbol"],
                    num_fmt={"rank": "{:.0f}", "qty": "{:.0f}", "price": "₹{:.2f}",
                             "amount": "₹{:,.0f}", "stop": "₹{:.2f}", "score": "{:.2f}",
                             "fundamental_score": "{:.1f}"}),
                unsafe_allow_html=True)
            st.markdown(
                f'<div class="ov-row"><span class="ov-card-meta">Total amount</span>'
                f'<span class="ov-sym">₹{_buys_display["amount"].sum():,.0f}</span></div>',
                unsafe_allow_html=True)
            place_gtt = st.checkbox("Also place a GTT stop-loss for each buy",
                                    value=True, key="rebal_gtt")
            confirm_buy = st.checkbox(
                "I confirm I want to execute ALL proposed buys at market",
                key="confirm_buy_all")
            if st.button("Execute all buys", disabled=not confirm_buy or rebalance_running,
                        use_container_width=True, key="lr_execute_buys"):
                log, succeeded, failed = lr.execute_buys(result["buys"], place_gtt=place_gtt)
                st.session_state["buy_exec_log"] = log
                resolved = succeeded + list(failed)
                if resolved:
                    result["buys"] = result["buys"][
                        ~result["buys"]["symbol"].isin(resolved)].reset_index(drop=True)
                    result["open_slots"] = max(result.get("open_slots", 0) - len(succeeded), 0)
                    st.session_state["rebalance_proposal"] = result
                    state_db.mark_rebalance_buys_executed(result.get("run_id"), succeeded)
                    state_db.mark_rebalance_buys_failed(result.get("run_id"), failed)
                    state_db.set_rebalance_open_slots(result.get("run_id"), result["open_slots"])
                st.rerun()
        else:
            st.caption("No open slots, or no candidates today.")

    top_ups = result.get("top_ups", pd.DataFrame())
    stop_updates = result.get("stop_updates", pd.DataFrame())
    col_topups, col_stops = st.columns(2)
    with col_topups:
        with st.container(border=True, key="ov-card-lr-topups"):
            st.markdown(
                '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
                '<span class="ov-dot" style="background:var(--ov-blue);">'
                f'</span>Proposed top-ups <span class="ov-badge ov-badge-gray">{len(top_ups)}</span></p>',
                unsafe_allow_html=True)
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
                _topups_display = top_ups.copy()
                _topups_display["amount"] = _topups_display["extra_qty"] * _topups_display["price"]
                _topups_cols = [c for c in
                               ["symbol", "extra_qty", "price", "amount", "gtt_trigger_id"]
                               if c in _topups_display.columns]
                st.markdown(
                    _ov_table_html(
                        _topups_display, columns=_topups_cols, sym_cols=["symbol"],
                        num_fmt={"extra_qty": "{:.0f}", "price": "₹{:.2f}",
                                "amount": "₹{:,.0f}"}),
                    unsafe_allow_html=True)
                st.markdown(
                    f'<div class="ov-row"><span class="ov-card-meta">Total amount</span>'
                    f'<span class="ov-sym">₹{_topups_display["amount"].sum():,.0f}</span></div>',
                    unsafe_allow_html=True)
                confirm_topup = st.checkbox(
                    "I confirm I want to execute ALL proposed top-ups at market",
                    key="confirm_topup_all")
                if st.button("Execute all top-ups", disabled=not confirm_topup or rebalance_running,
                            use_container_width=True, key="lr_execute_topups"):
                    log, succeeded, failed = lr.execute_top_ups(top_ups)
                    st.session_state["topup_exec_log"] = log
                    resolved = succeeded + list(failed)
                    if resolved:
                        result["top_ups"] = top_ups[
                            ~top_ups["symbol"].isin(resolved)].reset_index(drop=True)
                        st.session_state["rebalance_proposal"] = result
                        state_db.mark_rebalance_top_ups_executed(result.get("run_id"), succeeded)
                        state_db.mark_rebalance_top_ups_failed(result.get("run_id"), failed)
                    st.rerun()
            else:
                st.caption("No under-target holdings, or no cash left over to top up with.")

    with col_stops:
        with st.container(border=True, key="ov-card-lr-stops"):
            st.markdown(
                '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
                '<span class="ov-dot" style="background:var(--ov-amber);">'
                f'</span>Stop updates needing attention '
                f'<span class="ov-badge ov-badge-gray">{len(stop_updates)}</span></p>',
                unsafe_allow_html=True)
            if st.session_state.get("stopupdate_exec_log"):
                st.success("Stop update(s) applied and removed from the list below.")
                for line in st.session_state["stopupdate_exec_log"]:
                    st.write(line)
                del st.session_state["stopupdate_exec_log"]
            if not stop_updates.empty:
                st.caption("Ratchets auto-apply; only ones that couldn't (no active "
                          "GTT / Kite error) land here.")
                _stops_display = stop_updates.copy()
                _stops_display["gtt_status"] = _stops_display["gtt_trigger_id"].apply(
                    lambda v: "none active" if pd.isna(v) else "active")
                st.markdown(
                    _ov_table_html(
                        _stops_display,
                        columns=["symbol", "current_stop", "recommended_stop", "gtt_status"],
                        sym_cols=["symbol"],
                        num_fmt={"current_stop": "₹{:.2f}", "recommended_stop": "₹{:.2f}"},
                        badges={"gtt_status": {"active": "ov-badge-green",
                                               "none active": "ov-badge-red"}}),
                    unsafe_allow_html=True)
                confirm_stops = st.checkbox(
                    "I confirm I want to raise ALL these GTT stop-losses",
                    key="confirm_stop_updates")
                if st.button("Apply stop updates", disabled=not confirm_stops or rebalance_running,
                            use_container_width=True, key="lr_apply_stops"):
                    log = []
                    succeeded = []
                    failed = {}
                    for _, r in stop_updates.iterrows():
                        if pd.isna(r["gtt_trigger_id"]):
                            log.append(f"⚠️ {r['symbol']}: no active GTT to update — "
                                      "place one manually first (Trade tab).")
                            failed[r["symbol"]] = "No active GTT to update"
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
                            failed[r["symbol"]] = str(e)
                    st.session_state["stopupdate_exec_log"] = log
                    resolved = succeeded + list(failed)
                    if resolved:
                        result["stop_updates"] = stop_updates[
                            ~stop_updates["symbol"].isin(resolved)].reset_index(drop=True)
                        st.session_state["rebalance_proposal"] = result
                        state_db.mark_rebalance_stop_updates_executed(result.get("run_id"), succeeded)
                        state_db.mark_rebalance_stop_updates_failed(result.get("run_id"), failed)
                    st.rerun()
            else:
                st.caption("No trailing-stop increases needed attention today — "
                          "either nothing ratcheted, or it all auto-applied cleanly.")

    with st.expander("🔮 What-if: preview equal-weight sizing"):
        st.caption("Instant preview using the SAME sizing formula as the "
                  "real proposal below (target/slot = total equity ÷ max "
                  "positions, shortfall = slots needed × target − cash "
                  "pool) — just with hypothetical inputs instead of the "
                  "live config/balance. Doesn't re-run the screener, so it "
                  "can't tell you which symbols would actually be picked "
                  "at a different slot count, only how much cash each slot "
                  "would need.")
        _total_equity = _real_target * _real_max_positions
        _held_value = _total_equity - available_cash
        _still_held_count = len(result["holdings"]) - len(result["sells"])

        wc1, wc2 = st.columns(2)
        with wc1:
            what_if_slots = st.slider("Max positions", min_value=1, max_value=25,
                                      value=_real_max_positions, key="whatif_slots")
        with wc2:
            what_if_cash = st.number_input(
                "Available cash (₹)", min_value=0.0, value=float(available_cash),
                step=1000.0, key="whatif_cash")

        what_if_total_equity = what_if_cash + _held_value
        what_if_target = what_if_total_equity / what_if_slots if what_if_slots else 0.0
        what_if_open_slots = max(what_if_slots - _still_held_count, 0)
        what_if_cash_needed = what_if_open_slots * what_if_target
        what_if_shortfall = max(0.0, what_if_cash_needed - what_if_cash)

        st.markdown(
            '<div class="ov-grid-metrics">'
            + _ov_metric_html(
                "Target per slot", f"₹{what_if_target:,.0f}",
                (f"{what_if_target - _real_target:+,.0f} vs current" if _real_target else None),
                "ov-pos" if what_if_target >= _real_target else "ov-neg", "teal")
            + _ov_metric_html("Open slots", str(what_if_open_slots), "after sells", "", "purple")
            + _ov_metric_html(
                "Cash shortfall",
                f"₹{what_if_shortfall:,.0f}" if what_if_shortfall else "₹0",
                "fully funded" if not what_if_shortfall else None, "", "green")
            + '</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page: Positions & Trade
# ---------------------------------------------------------------------------

def page_positions_trade():
    cfg = config.STRATEGY

    hdr_l, hdr_r = st.columns([2, 3])
    with hdr_l:
        st.markdown(
            '<div class="ov-header" style="margin-bottom:0;">'
            '<div><span class="ov-h1">💼 Positions, Holdings &amp; Trade</span></div></div>',
            unsafe_allow_html=True)
    with hdr_r:
        with st.container(key="pt_refresh_row"):
            # A separate "Auto-refresh" label div next to the control kept
            # landing in the wrong visual position however the flex CSS
            # was tuned -- the widget's OWN native label (rendered above
            # it) is unambiguous and needs no CSS guesswork to place
            # correctly, at the cost of sitting above instead of beside.
            refresh_choice = st.segmented_control(
                "Auto-refresh", ["Off", "10s", "30s", "1m", "5m"],
                default="30s", key="pt_refresh_interval", required=True,
                help="Only the positions/holdings tables below refresh on this timer -- "
                     "the rest of this page (square-off, orders, trade forms) isn't "
                     "affected, so nothing you're typing gets reset by it.")
    run_every = None if refresh_choice == "Off" else refresh_choice

    # Own small fragment (not the big orders/holdings one below) so this
    # timestamp still ticks with the same run_every, but can render
    # directly under the segmented control in the header instead of at
    # the bottom of the orders/holdings cards.
    @st.fragment(run_every=run_every)
    def _refresh_status():
        st.markdown(
            '<p class="ov-card-meta" style="text-align:right;margin:2px 0 14px;font-size:10px;">'
            f'Last refreshed {dt.datetime.now():%H:%M:%S}'
            + (f" · auto-refreshing every {refresh_choice}" if run_every else "")
            + '</p>', unsafe_allow_html=True)

    with hdr_r:
        _refresh_status()

    @st.fragment(run_every=run_every)
    def _live_holdings():
        with st.container(border=True, key="ov-card-pt-holdings"):
            st.markdown(
                '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
                '<span class="ov-dot" '
                'style="background:var(--ov-purple);"></span>Holdings (CNC)</p>',
                unsafe_allow_html=True)
            live_hold = kite_client.get_holdings()
            if live_hold.empty:
                st.caption("No holdings.")
            else:
                live_hold = live_hold.copy()
                live_hold["pnl_pct"] = ((live_hold["last_price"] / live_hold["average_price"]) - 1) * 100
                live_hold["invested_capital"] = live_hold["quantity"] * live_hold["average_price"]
                live_hold["current_capital"] = live_hold["quantity"] * live_hold["last_price"]
                st.markdown(
                    _ov_table_html(
                        live_hold[["tradingsymbol", "quantity", "average_price",
                                  "last_price", "invested_capital", "current_capital",
                                  "pnl", "pnl_pct"]],
                        sym_cols=["tradingsymbol"], pnl_cols=["pnl", "pnl_pct"],
                        num_fmt={"quantity": "{:.0f}", "average_price": "₹{:.2f}",
                                "last_price": "₹{:.2f}", "invested_capital": "₹{:,.0f}",
                                "current_capital": "₹{:,.0f}", "pnl": "{:+,.0f}",
                                "pnl_pct": "{:+.2f}%"},
                        arrow_cols={"current_capital":
                                   ("current_capital", "invested_capital", True)}),
                    unsafe_allow_html=True)
                _hold_pnl = float(live_hold["pnl"].sum())
                _hold_pnl_cls = "ov-pos" if _hold_pnl >= 0 else "ov-neg"
                st.markdown(
                    f'<div class="ov-row"><span class="ov-card-meta">Total holdings P&amp;L</span>'
                    f'<span class="ov-sym {_hold_pnl_cls}">₹{_hold_pnl:+,.0f}</span></div>',
                    unsafe_allow_html=True)

    @st.fragment(run_every=run_every)
    def _live_orders():
        with st.container(border=True, key="ov-card-pt-orders"):
            _orders_tip = html_lib.escape(
                "Every order placed today, enriched with the resulting "
                "position's live LTP/P&L where it's still open -- open "
                "positions and today's orders showed almost entirely "
                "overlapping symbols/qty as separate tables, so this is "
                "one combined view instead of two near-duplicates.")
            st.markdown(
                '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
                '<span class="ov-dot" style="background:var(--ov-blue);">'
                '</span>Today\'s orders &amp; positions'
                f'<span class="ov-info-icon" title="{_orders_tip}">ℹ️</span></p>',
                unsafe_allow_html=True)
            orders = kite_client.get_orders()
            live_pos = kite_client.get_positions()
            if orders.empty:
                st.caption("No orders today.")
            else:
                pos_by_symbol = {}
                if not live_pos.empty:
                    for _, r in live_pos[live_pos["quantity"] != 0].iterrows():
                        pos_by_symbol[r["tradingsymbol"]] = {
                            "current_qty": r["quantity"], "ltp": r["last_price"],
                            "pnl": r["pnl"], "product": r["product"]}
                display = orders[["order_timestamp", "tradingsymbol", "transaction_type",
                                 "quantity", "average_price", "status"]].copy()
                display["current_qty"] = display["tradingsymbol"].map(
                    lambda s: pos_by_symbol.get(s, {}).get("current_qty"))
                display["ltp"] = display["tradingsymbol"].map(
                    lambda s: pos_by_symbol.get(s, {}).get("ltp"))
                display["pnl"] = display["tradingsymbol"].map(
                    lambda s: pos_by_symbol.get(s, {}).get("pnl"))
                display["product"] = display["tradingsymbol"].map(
                    lambda s: pos_by_symbol.get(s, {}).get("product"))
                st.markdown(
                    _ov_table_html(
                        display, sym_cols=["tradingsymbol"], pnl_cols=["pnl"],
                        num_fmt={"quantity": "{:.0f}", "average_price": "₹{:.2f}",
                                "current_qty": "{:.0f}", "ltp": "₹{:.2f}"},
                        badges={
                            "transaction_type": lambda v: "ov-badge-green" if v == "BUY" else "ov-badge-red",
                            "status": _ov_order_status_cls}),
                    unsafe_allow_html=True)
                total_pnl = live_pos[live_pos["quantity"] != 0]["pnl"].sum() \
                    if not live_pos.empty else 0.0
                _pnl_cls = "ov-pos" if total_pnl >= 0 else "ov-neg"
                st.markdown(
                    f'<div class="ov-row"><span class="ov-card-meta">Total position P&amp;L</span>'
                    f'<span class="ov-sym {_pnl_cls}">₹{total_pnl:+,.0f}</span></div>',
                    unsafe_allow_html=True)

    _live_holdings()

    # Separate fetch for the sections below (square-off, stop-loss, orders) --
    # these don't live inside the auto-refreshing fragment above, since their
    # widgets/forms shouldn't get reset every refresh tick; a second cheap
    # positions/holdings call here keeps them independent of that cadence.
    pos = kite_client.get_positions()
    hold = kite_client.get_holdings()

    all_syms = []
    if not pos.empty:
        # > 0, not != 0 -- a same-day SELL leaves a NEGATIVE "day"
        # quantity here (settlement-lag artifact, nets to 0 against the
        # holding overnight), not a real short (long-only CNC swing
        # trading) -- see merged_holdings()'s identical fix for the full
        # story. Without this, a symbol sold entirely TODAY still showed
        # up as "unprotected, needs a stop-loss" even though it's not
        # actually held anymore and there was never a GTT to place or
        # delete for it.
        all_syms += list(pos[pos["quantity"] > 0]["tradingsymbol"])
    if not hold.empty:
        all_syms += list(hold[hold["quantity"] > 0]["tradingsymbol"])
    all_syms = sorted(set(all_syms))

    col_orders, col_gtt = st.columns(2)
    with col_orders:
        _live_orders()

    with col_gtt:
        with st.container(border=True, key="ov-card-pt-gtt"):
            st.markdown(
                '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
                '<span class="ov-dot" '
                'style="background:var(--ov-red);"></span> GTT / Stop-Loss Management'
                '<span class="ov-card-meta" style="font-weight:400;margin-left:auto;">'
                'Straight from Kite — the source of truth</span></p>',
                unsafe_allow_html=True,
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
                _updated_raw = g["updated_at"] if g else None
                _updated_fmt = None
                if _updated_raw:
                    try:
                        _updated_fmt = pd.to_datetime(_updated_raw).strftime("%d %b")
                    except Exception:
                        _updated_fmt = str(_updated_raw)
                gtt_rows.append({
                    "symbol": sym, "gtt_active": "active" if g else "none",
                    "trigger_price": g["trigger_price"] if g else None,
                    "updated_at": _updated_fmt,
                })
            gtt_table = pd.DataFrame(gtt_rows)
            if not gtt_table.empty:
                st.markdown(
                    _ov_table_html(
                        gtt_table.sort_values("symbol"), sym_cols=["symbol"],
                        num_fmt={"trigger_price": "₹{:.2f}"},
                        badges={"gtt_active": {"active": "ov-badge-green", "none": "ov-badge-red"}}),
                    unsafe_allow_html=True)

            unprotected_syms = [s for s in all_syms if s not in gtt_symbols]

            if not unprotected_syms:
                st.markdown(
                    '<div class="ov-alert ov-alert-success">✓ Every current '
                    'position/holding has an active GTT.</div>', unsafe_allow_html=True)
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

                st.markdown(
                    '<div class="ov-grid-metrics">'
                    + _ov_metric_html("Quantity", str(sl_qty), None, "", "blue")
                    + _ov_metric_html("LTP", f"₹{sl_ltp:,.2f}", None, "", "red")
                    + _ov_metric_html("ATR stop", f"₹{sl_stop:,.2f}",
                                     f"{cfg['atr_stop_multiple']}× ATR({cfg['atr_period']})",
                                     "", "purple")
                    + '</div>', unsafe_allow_html=True)

                sl_confirm = st.checkbox("I confirm this GTT stop-loss", key="manual_sl_confirm")
                if st.button("Place stop-loss", type="primary",
                            disabled=not sl_confirm or sl_qty == 0 or sl_stop <= 0,
                            key="manual_sl_place", use_container_width=True):
                    try:
                        gtt_id = kite_client.place_gtt_stoploss(sl_symbol, sl_qty, sl_stop, sl_ltp)
                    except Exception as e:
                        st.error(f"GTT placement failed: {e}")
                    else:
                        state_db.upsert_manual_position(sl_symbol, sl_avg_price, sl_qty,
                                                        sl_stop, gtt_id)
                        st.success(f"GTT stop-loss placed: trigger {gtt_id} at ₹{sl_stop:,.1f}")
                        st.rerun()

    with st.container(border=True, key="ov-card-pt-order"):
        # The right-hand subtitle needs qty/ltp/side, which aren't known
        # until after the form widgets below run -- render into this
        # placeholder later instead of a static line up front.
        _order_title_ph = st.empty()
        _order_tip = html_lib.escape(
            "Sizing uses your ATR stop so every BUY risks the same % of "
            "capital. Pick SELL on something you currently hold to square "
            "off the whole position at market in one click.")

        symbol_choices = sorted(set(all_syms) | set(config.UNIVERSE))
        col1, col2, col3 = st.columns(3)
        with col1:
            symbol = st.selectbox("Symbol", symbol_choices, key="trade_symbol")
            side = st.segmented_control("Side", ["BUY", "SELL"], default="BUY",
                                        key="trade_side", required=True)

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
            order_type = st.segmented_control(
                "Order type", ["MARKET", "LIMIT"], default="MARKET",
                key="trade_order_type", disabled=square_off_mode, required=True)
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
            st.markdown(
                '<div class="ov-grid-metrics">'
                + _ov_metric_html("LTP", f"₹{ltp:,.2f}", None, "", "blue")
                + _ov_metric_html("ATR stop", f"₹{stop:,.2f}", None, "", "red")
                + '</div>', unsafe_allow_html=True)
            if square_off_mode:
                qty = held_qty
                st.markdown(
                    '<div class="ov-grid-metrics">'
                    + _ov_metric_html("Quantity", str(qty), None, "", "purple")
                    + '</div>', unsafe_allow_html=True)
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

        confirm = st.checkbox(
            f"I confirm I want to close my entire {symbol} position at market"
            if square_off_mode else "I confirm this order",
            key="trade_confirm")

        if square_off_mode:
            _preview_bold = f"SELL {qty} × {symbol} at market (square-off)"
            _preview_rest = f" ≈ ₹{qty * ltp:,.0f}"
        else:
            est_value = qty * ltp
            _preview_bold = f"{side} {qty} × {symbol}"
            _preview_rest = (f" ≈ ₹{est_value:,.0f} "
                            f"({order_type}{f' @ ₹{limit_price}' if limit_price else ''})"
                            + (f" + GTT SL at ₹{stop:,.1f}" if place_gtt else ""))
        _order_title_ph.markdown(
            '<p class="ov-card-title"><span class="ov-dot" '
            'style="background:var(--ov-green);"></span>Place an order'
            f'<span class="ov-info-icon" title="{_order_tip}">ℹ️</span></p>',
            unsafe_allow_html=True)

        if st.button("Square off" if square_off_mode else "Execute order", type="primary",
                    disabled=not confirm or qty == 0, key="trade_execute",
                    use_container_width=True):
            if square_off_mode:
                try:
                    order_id = kite_client.square_off_position(symbol)
                    st.success(f"Square-off order placed: {order_id}")
                    try:
                        exit_ltp = kite_client.get_ltp([symbol])[symbol]
                    except Exception:
                        exit_ltp = None
                    state_db.close_trade(symbol, exit_ltp, "manual_square_off")
                    state_db.close_position(symbol, exit_ltp)
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

        st.markdown(
            '<div class="ov-alert ov-alert-info">ℹ️ Order preview: '
            f'<b>{_preview_bold}</b>{_preview_rest}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page: Backtest
# ---------------------------------------------------------------------------

def page_backtest():
    _backtest_tip = html_lib.escape(
        "Replays the exact screener logic point-in-time with monthly "
        "rebalancing (any slot freed by a stop gets redeployed immediately, "
        "not just at the next rebalance), daily ATR-stop checks, and "
        "transaction costs. Defaults below match the current LIVE strategy "
        "(config.STRATEGY) exactly: fundamental quality gate on, trailing "
        "stop on, the equal-weight allocator with cross-slot borrowing. "
        "(The sector relative-strength bonus was tested here too -- "
        "including together with the equal-weight allocator -- and "
        "removed: it lost on CAGR/Sharpe at every weight tried, with worse "
        "drawdown too, so there's no free lunch even in trade for safety.) "
        "Today's universe implies some survivorship bias regardless -- "
        "treat parameter-sensitivity comparisons as more reliable than "
        "absolute returns.")
    _bt_hdr_l, _bt_hdr_r = st.columns([4, 3])
    with _bt_hdr_l:
        st.markdown(
            '<div class="ov-header" style="margin-bottom:0;">'
            '<div><span class="ov-h1">🧪 Backtest</span> '
            '<span class="ov-sub">calendar-entry momentum system</span>'
            f'<span class="ov-info-icon" title="{_backtest_tip}">ℹ️</span></div></div>',
            unsafe_allow_html=True)
    with _bt_hdr_r:
        _bt_hdr_r1, _bt_hdr_r2 = st.columns(2)
        _build_fh_clicked = _bt_hdr_r1.button(
            "Build/Refresh fundamentals history", key="bt_build_fh_hdr")
        _run_backtest_clicked = _bt_hdr_r2.button(
            "Run backtest", type="primary", key="bt_run_hdr")

    if "bt_result" not in st.session_state and os.path.exists(BACKTEST_CACHE):
        _cached_bt = pd.read_pickle(BACKTEST_CACHE)
        st.session_state["bt_result"] = _cached_bt["result"]
        st.session_state["bt_bench"] = _cached_bt["bench"]
        st.session_state["bt_run_time"] = _cached_bt["run_time"]
        st.session_state["bt_is_cached"] = True

    _bt_run_time_hdr = st.session_state.get("bt_run_time")
    if _bt_run_time_hdr is not None:
        _bt_cached_note_hdr = (" 📁 (from cache — click 'Run backtest' to refresh)"
                               if st.session_state.get("bt_is_cached") else "")
        _bt_run_meta = f"Last run: {_bt_run_time_hdr:%d %b %Y %H:%M}{_bt_cached_note_hdr}"
    else:
        _bt_run_meta = "Not run yet"

    with st.container(border=True, key="ov-card-bt-config"):
        st.markdown(
            '<p class="ov-card-title"><span class="ov-dot" '
            'style="background:var(--ov-blue);"></span>Run configuration'
            f'<span class="ov-card-meta" style="font-weight:400;margin-left:auto;">'
            f'{_bt_run_meta}</span></p>',
            unsafe_allow_html=True)

        rc1, rc2, rc3, rc4 = st.columns([1.3, 1.6, 1.1, 1.1])
        with rc1:
            range_mode = st.segmented_control(
                "Date range", ["Trailing years", "Custom dates"],
                default="Trailing years", key="bt_range_mode", required=True)
        if range_mode == "Trailing years":
            with rc2:
                years = st.slider("Years of history", 1.0, 5.0, 3.0, 0.5,
                                  help="Up to 5 years supported via chunked Kite "
                                       "fetches (Kite's historical API caps a single "
                                       "request at ~2000 days).")
            start_date, end_date = None, None
        else:
            with rc2:
                rc2a, rc2b = st.columns(2)
                default_start = dt.date.today() - dt.timedelta(days=3 * 365)
                start_date = rc2a.date_input("Start date", value=default_start,
                                             max_value=dt.date.today())
                end_date = rc2b.date_input("End date", value=dt.date.today(),
                                           max_value=dt.date.today())
            years = None
        with rc3:
            bt_capital = st.number_input("Starting capital (₹)", value=1_000_000.0,
                                        step=100000.0)
        with rc4:
            bt_max_positions = st.number_input(
                "Max open positions", min_value=1, max_value=30,
                value=int(config.STRATEGY["max_positions"]), step=1,
                help="Backtest-only override (config.STRATEGY['max_positions'] "
                     "is 10 live) -- more slots means more diversification but "
                     "smaller equal-weight targets per slot; fewer slots "
                     "concentrates capital in higher-conviction picks. Not "
                     "itself an A/B-tuned edge parameter the way the ones below "
                     "are, just a portfolio-construction choice to experiment "
                     "with.")

        st.divider()
        _hist_available = os.path.exists(FUNDAMENTALS_HISTORY_CACHE)
        _hist_badge_color = "var(--ov-green-d)" if _hist_available else "var(--ov-red-d)"
        _hist_badge_text = "AVAILABLE" if _hist_available else "NOT AVAILABLE"
        st.markdown(
            '<p class="ov-card-title"><span class="ov-dot" '
            'style="background:var(--ov-purple);"></span>🎯 Strategy parameters — '
            'tested &amp; approved'
            f'<span class="ov-card-meta" style="font-weight:700;margin-left:auto;'
            f'color:{_hist_badge_color};">'
            f'Fundamentals Build History - {_hist_badge_text}</span></p>',
            unsafe_allow_html=True)

        def _ov_muted(text):
            st.markdown(
                f'<p class="ov-muted" style="margin:10px 0 2px;text-transform:none;">{text}</p>',
                unsafe_allow_html=True)

        _ov_muted("Trade management")
        tm1, tm2, tm3, tm4 = st.columns(4)
        with tm1:
            atr_stop_multiple_v = st.number_input(
                "Initial stop (× ATR)", min_value=0.5, max_value=10.0,
                value=float(config.STRATEGY["atr_stop_multiple"]), step=0.1)
        with tm2:
            ts1, ts2 = st.columns([1, 1])
            with ts1:
                use_trailing = st.checkbox(
                    "Trailing stop", value=bool(config.STRATEGY["trailing_stop_enabled"]),
                    key="bt_use_trailing",
                    help="LIVE default is ON. Ratchets each position's stop up to "
                         "highest_close_since_entry - multiple*ATR as it gains, "
                         "never back down.")
            with ts2:
                trailing_mult_v = st.number_input(
                    "ATR multiple", min_value=0.5, max_value=10.0,
                    value=float(config.STRATEGY["trailing_atr_multiple"]), step=0.25,
                    disabled=not use_trailing,
                    help="A 5-year sweep found an inverted-U peaking at 4.0x (the "
                         "live default): CAGR 24.30% vs baseline 22.51%, Sharpe 1.73 "
                         "vs 1.50, max drawdown -14.37% vs -18.06%.")
        with tm3:
            risk_per_trade_pct_v = st.number_input(
                "Risk per trade (% of capital)", min_value=0.1, max_value=10.0,
                value=float(config.STRATEGY["risk_per_trade_pct"]), step=0.1)
        with tm4:
            rebalance_cadence_v = st.segmented_control(
                "Rebalance cadence", ["daily", "monthly"],
                default=config.STRATEGY.get("rebalance_cadence", "daily"),
                key="bt_rebalance_cadence",
                help="Mirrors the LIVE Admin setting. 'daily' re-checks the "
                     "sell/keep-zone rule every trading day (matches the live "
                     "default); 'monthly' only re-checks it on the first "
                     "trading day of each month. Buys/top-ups always fill open "
                     "slots daily either way, in both live and this backtest.")

        _ov_muted("Technical indicator")
        ti1, ti2, ti3 = st.columns(3)
        with ti1:
            rsi_min_v = st.number_input(
                "RSI min", min_value=0.0, max_value=100.0,
                value=float(config.STRATEGY["rsi_min"]), step=0.01, format="%.2f",
                help="Momentum names trade 45-80 in this system's regime; below "
                     "this is not yet in an uptrend.")
        with ti2:
            rsi_max_v = st.number_input(
                "RSI max", min_value=0.0, max_value=100.0,
                value=float(config.STRATEGY["rsi_max"]), step=0.01, format="%.2f",
                help="A 5-year A/B (max_positions=4) found raising this 78->80 "
                     "improved CAGR 7.36->8.15%, Sharpe 1.14->1.25 -- re-verify "
                     "any further move against that same baseline, not just "
                     "eyeballing one run.")
        with ti3:
            ema_fast_v = st.number_input(
                "EMA (fast)", min_value=5, max_value=100,
                value=int(config.STRATEGY["ema_fast"]), step=1)
        ti4, ti5, ti6 = st.columns(3)
        with ti4:
            ema_slow_v = st.number_input(
                "EMA (slow)", min_value=50, max_value=400,
                value=int(config.STRATEGY["ema_slow"]), step=1)
        with ti5:
            mom_lookback_short_v = st.number_input(
                "Momentum lookback — short (days)", min_value=5, max_value=252,
                value=int(config.STRATEGY["mom_lookback_days_short"]), step=1)
        with ti6:
            mom_lookback_long_v = st.number_input(
                "Momentum lookback — long (days)", min_value=5, max_value=504,
                value=int(config.STRATEGY["mom_lookback_days_long"]), step=1)
        ti7, ti8 = st.columns(2)
        with ti7:
            skip_recent_days_v = st.number_input(
                "Skip most recent (days)", min_value=0, max_value=30,
                value=int(config.STRATEGY["skip_recent_days"]), step=1)
        with ti8:
            history_days_v = st.number_input(
                "Candle history fetched (days)", min_value=300, max_value=3000,
                value=int(config.STRATEGY["history_days"]), step=100)

        _ov_muted("Scanner param")
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            ew1, ew2 = st.columns([1, 1])
            with ew1:
                use_equal_weight = st.checkbox(
                    "Equal-weight allocator",
                    value=bool(config.STRATEGY["advanced_equal_weight_sizing"]),
                    key="bt_use_equal_weight",
                    help="LIVE default is ON. Sizes the whole day's buys in one "
                         "pass -- cross-slot borrowing within tolerance, partial "
                         "fill on shortfall, hard-stop-not-substitute, top-up of "
                         "under-target holdings -- instead of one-symbol-at-a-time "
                         "greedy sizing.")
            with ew2:
                equal_weight_tolerance_v = st.number_input(
                    "Tolerance", min_value=0.0, max_value=1.0,
                    value=float(config.STRATEGY["equal_weight_tolerance_pct"]), step=0.01,
                    format="%.2f", disabled=not use_equal_weight,
                    help="A 5-year A/B found 0.20 (the live default) beats the "
                         "original one-at-a-time fill on every metric at once: CAGR "
                         "43.06->44.39%, Sharpe 1.64->1.67, max drawdown "
                         "-20.30->-19.58%, profit factor 2.10->2.12.")
        with sc2:
            use_fundamentals = st.checkbox(
                "Fundamental gate",
                value=bool(config.STRATEGY["fundamental_gate_enabled"]),
                key="bt_use_fundamentals",
                help="This is the LIVE default (config.STRATEGY['fundamental_gate_"
                     "enabled']=True) -- uncheck only to see the pure-technical "
                     "baseline it was A/B'd against. Uses each filing's real "
                     "broadcast timestamp to only count what was actually public "
                     "knowledge as of each rebalance date -- not today's "
                     "fundamentals applied retroactively. Needs a fundamentals "
                     "history built first (top-right button) -- without one, "
                     "this checkbox has no effect regardless of its state.")
        with sc3:
            fundamental_bonus_weight_v = st.number_input(
                "Fundamental bonus weight", min_value=0.0, max_value=3.0,
                value=float(config.STRATEGY["fundamental_bonus_weight"]), step=0.1,
                disabled=not use_fundamentals,
                help="Tilts ranking toward higher-quality gate-passers (on top "
                     "of the gate itself, which only excludes/includes). A "
                     "5-year sweep found an inverted-U peaking at 0.5 (the live "
                     "default): CAGR 43.70->43.03% at 0.5, Sharpe 1.62->1.64, "
                     "max drawdown improves -24.61->-20.30% -- anything above "
                     "0.5 is a clear net negative.")
        sc4, sc5, sc6 = st.columns(3)
        with sc4:
            min_fundamental_score_v = st.number_input(
                "Min fundamental score", min_value=0.0, max_value=100.0,
                value=float(config.STRATEGY["min_fundamental_score"]), step=1.0,
                disabled=not use_fundamentals,
                help="NOT independently A/B-tuned -- config.py calls 50 (the "
                     "live default) 'a rough average-or-better bar, tune to "
                     "taste.' Only the gate's on/off status and the bonus "
                     "weight above have documented A/B history; this threshold "
                     "itself has never been swept, so treat any result here as "
                     "a first look, not a verified finding.")
        with sc5:
            near_high_threshold_v = st.number_input(
                "52-week-high proximity (%)", min_value=50.0, max_value=100.0,
                value=float(config.STRATEGY["near_high_threshold"]) * 100, step=1.0,
                help="Price must be at least this % of its 52-week high to qualify.")
        with sc6:
            sector_bonus_weight_v = st.number_input(
                "Sector bonus weight", min_value=0.0, max_value=1.0,
                value=float(config.STRATEGY["sector_bonus_weight"]), step=0.05,
                help="0 = off (recommended) -- re-tested with the equal-weight "
                     "allocator specifically: loses on CAGR and Sharpe at every "
                     "weight, and drawdown gets worse too, so there's no "
                     "risk/reward trade-off to make here.")

        st.caption("No per-trade cost is modeled — Zerodha charges no brokerage "
                  "on equity delivery (CNC). Statutory costs (STT, stamp duty, "
                  "exchange/SEBI charges) still apply in reality (~5-7 bps round "
                  "trip) but aren't broker-specific; use `--cost-bps` on the CLI "
                  "if you want a more conservative run that includes them.")

        if _build_fh_clicked:
            bar = st.progress(0.0, text="Starting...")
            history = fa.build_fundamentals_history(
                config.UNIVERSE, n_years=5,
                progress_cb=lambda s, f: bar.progress(f, text=s))
            bar.empty()
            os.makedirs("cache", exist_ok=True)
            pd.to_pickle({"history": history, "run_time": dt.datetime.now()},
                        FUNDAMENTALS_HISTORY_CACHE)
            st.rerun()


    run_disabled = range_mode == "Custom dates" and start_date >= end_date
    if run_disabled:
        st.error("Start date must be before end date.")

    if _run_backtest_clicked and not run_disabled:
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
        # Always built fresh from the live config + every control above, so
        # the backtest that actually runs can never silently diverge from
        # what's shown on screen (previously run_cfg stayed None -- falling
        # back to config.STRATEGY untouched -- unless sector was checked).
        run_cfg = dict(config.STRATEGY)
        run_cfg["max_positions"] = int(bt_max_positions)
        run_cfg["rsi_min"] = rsi_min_v
        run_cfg["rsi_max"] = rsi_max_v
        run_cfg["trailing_stop_enabled"] = use_trailing
        run_cfg["trailing_atr_multiple"] = trailing_mult_v
        run_cfg["advanced_equal_weight_sizing"] = use_equal_weight
        run_cfg["equal_weight_tolerance_pct"] = equal_weight_tolerance_v
        # fundamental_gate_enabled is set explicitly too, even though the
        # gate is already a no-op without fundamentals_history (see
        # use_fundamentals above) -- this keeps run_cfg an honest mirror of
        # every control on screen rather than relying on that side channel.
        run_cfg["fundamental_gate_enabled"] = use_fundamentals
        run_cfg["fundamental_bonus_weight"] = fundamental_bonus_weight_v
        run_cfg["min_fundamental_score"] = min_fundamental_score_v
        run_cfg["mom_lookback_days_short"] = int(mom_lookback_short_v)
        run_cfg["mom_lookback_days_long"] = int(mom_lookback_long_v)
        run_cfg["skip_recent_days"] = int(skip_recent_days_v)
        run_cfg["near_high_threshold"] = float(near_high_threshold_v) / 100
        run_cfg["ema_fast"] = int(ema_fast_v)
        run_cfg["ema_slow"] = int(ema_slow_v)
        run_cfg["atr_stop_multiple"] = float(atr_stop_multiple_v)
        run_cfg["risk_per_trade_pct"] = float(risk_per_trade_pct_v)
        run_cfg["sector_bonus_weight"] = float(sector_bonus_weight_v)
        run_cfg["history_days"] = int(history_days_v)
        run_cfg["rebalance_cadence"] = rebalance_cadence_v
        with st.spinner("Simulating..."):
            res = bt.run_backtest(candles_bt, bench_bt, run_cfg,
                                  initial_capital=bt_capital,
                                  rebalance="D" if rebalance_cadence_v == "daily" else "MS",
                                  fundamentals_history=fundamentals_history)
            run_time = dt.datetime.now()
            st.session_state["bt_result"] = res
            st.session_state["bt_bench"] = bench_bt
            st.session_state["bt_run_time"] = run_time
            st.session_state["bt_is_cached"] = False
            os.makedirs("cache", exist_ok=True)
            pd.to_pickle({"result": res, "bench": bench_bt, "run_time": run_time},
                        BACKTEST_CACHE)

    if "bt_result" not in st.session_state:
        st.info("Click **Run backtest** to simulate on real Kite data.")
        return

    res = st.session_state["bt_result"]
    eq = res["equity_curve"]
    bench_bt = st.session_state["bt_bench"]

    nifty = bench_bt["close"].reindex(eq.index).ffill()

    with st.container(border=True, key="ov-card-bt-equity"):
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
            title=dict(text="Backtest equity curve — growth of ₹100", x=0, xanchor="left"),
            height=420, margin=dict(l=10, r=10, t=60, b=10), hovermode="x unified",
            yaxis=dict(title="Growth of 100"),
            legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0))
        st.plotly_chart(eq_fig, width="stretch")

    _m = res["metrics"]
    _total_ret = (res["final_capital"] / eq.iloc[0] - 1) * 100
    _cagr = _m.get("CAGR %")
    _maxdd = _m.get("Max drawdown %")
    st.markdown(
        '<div class="ov-grid-metrics">'
        + _ov_metric_html("Final capital", f"₹{res['final_capital']:,.0f}",
                         f"{_total_ret:+.1f}% total", "ov-pos" if _total_ret >= 0 else "ov-neg", "green")
        + _ov_metric_html("CAGR", f"{_cagr:.1f}%" if _cagr is not None else "—",
                         None, "ov-pos" if (_cagr or 0) >= 0 else "ov-neg", "green",
                         "ov-pos" if (_cagr or 0) >= 0 else "ov-neg")
        + _ov_metric_html("Sharpe", f"{_m.get('Sharpe', '—')}", "daily, annualized", "", "blue")
        + _ov_metric_html("Max drawdown", f"{_maxdd:.1f}%" if _maxdd is not None else "—",
                         f"NIFTY {_m.get('NIFTY CAGR %', '—')}% CAGR", "", "coral")
        + _ov_metric_html("Win rate", f"{_m.get('Win rate %', '—')}%",
                         f"{_m.get('Trades', '—')} trades", "", "purple")
        + _ov_metric_html("Profit factor", f"{_m.get('Profit factor', '—')}",
                         "gross win/loss", "", "teal")
        + _ov_metric_html("Open at end", str(len(res["open_positions"])),
                         "not force-sold", "", "amber")
        + '</div>', unsafe_allow_html=True)

    with st.expander("Full metrics table"):
        _metrics_df = pd.DataFrame({"Value": res["metrics"]})
        _metrics_df.insert(0, "Metric", _metrics_df.index)
        st.markdown(_ov_table_html(_metrics_df), unsafe_allow_html=True)

    dd = (eq / eq.cummax() - 1) * 100
    with st.container(border=True, key="ov-card-bt-drawdown"):
        dd_fig = go.Figure()
        dd_fig.add_trace(go.Scatter(
            x=dd.index, y=dd, name="Drawdown %", mode="lines",
            line=dict(color="#dc2626", width=1.5), fill="tozeroy",
            fillcolor="rgba(220,38,38,0.15)",
            hovertemplate="%{y:.1f}%<extra>Drawdown</extra>"))
        dd_fig.update_layout(
            title=dict(text="Drawdown from peak", x=0, xanchor="left"),
            height=260, margin=dict(l=10, r=10, t=50, b=10),
            yaxis=dict(title="Drawdown %"), showlegend=False)
        st.plotly_chart(dd_fig, width="stretch")

    yp = bt.yearly_performance(eq, bench_bt, res["trades"])
    yc1, yc2 = st.columns([2, 3])
    with yc1:
      with st.container(border=True, key="ov-card-bt-yearly"):
        st.markdown(
            '<p class="ov-card-title"><span class="ov-dot" '
            'style="background:var(--ov-teal);"></span>Year-by-year performance</p>',
            unsafe_allow_html=True)
        yp_display = yp.copy()
        yp_display.insert(0, "Year", yp_display.index.astype(str))
        st.markdown(
            _ov_table_html(
                yp_display, sym_cols=["Year"], pnl_cols=["Strategy %", "Alpha %"],
                num_fmt={"NIFTY %": "{:+.2f}", "Win rate %": "{:.1f}"}),
            unsafe_allow_html=True)
    with yc2:
      with st.container(border=True, key="ov-card-bt-yearly-bar"):
        bar_fig = go.Figure()
        bar_colors = ["#16a34a" if v >= 0 else "#dc2626" for v in yp["Strategy %"]]
        bar_fig.add_trace(go.Bar(
            x=yp.index.astype(str), y=yp["Strategy %"], name="Strategy %",
            marker_color=bar_colors, hovertemplate="%{y:.1f}%<extra>Strategy</extra>"))
        bar_fig.add_trace(go.Bar(
            x=yp.index.astype(str), y=yp["NIFTY %"], name="NIFTY %",
            marker_color="#9ca3af", hovertemplate="%{y:.1f}%<extra>NIFTY 50</extra>"))
        bar_fig.update_layout(
            title=dict(text="Strategy vs NIFTY by year", x=0, xanchor="left"),
            height=350, margin=dict(l=10, r=10, t=50, b=10), barmode="group",
            legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0))
        st.plotly_chart(bar_fig, width="stretch")

    if not res["open_positions"].empty:
        with st.expander(f"Open positions at period end ({len(res['open_positions'])})",
                         expanded=True):
            st.caption("Still held when the backtest's date range ran out — "
                      "not force-sold. Unrealized P&L is marked to the last "
                      "available close, not an actual exit.")
            op = res["open_positions"].copy()
            op["entry_date"] = pd.to_datetime(op["entry_date"]).dt.date.astype(str)
            st.markdown(
                _ov_table_html(
                    op.sort_values("unrealized_pnl", ascending=False),
                    sym_cols=["symbol"], pnl_cols=["unrealized_pnl", "unrealized_ret_pct"],
                    num_fmt={"entry_price": "₹{:.2f}", "current_price": "₹{:.2f}",
                            "stop": "₹{:.2f}", "qty": "{:.0f}"}),
                unsafe_allow_html=True)

    tr = res["trades"].copy()
    if not tr.empty:
        with st.container(border=True, key="ov-card-bt-closed"):
            st.markdown(
                '<p class="ov-card-title"><span class="ov-dot" '
                'style="background:var(--ov-coral);"></span>All closed trades</p>',
                unsafe_allow_html=True)
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
            filtered_sorted = filtered.sort_values("entry_date", ascending=False)
            filtered_page = _ov_page_slice(filtered_sorted, key="bt_closed", page_size=20)
            st.markdown(
                _ov_table_html(
                    filtered_page,
                    sym_cols=["symbol"], pnl_cols=["pnl", "ret_pct"],
                    num_fmt={"entry_price": "₹{:.2f}", "exit_price": "₹{:.2f}"},
                    badges={"reason": lambda v: ("ov-badge-red" if v == "stop_hit"
                                                 else "ov-badge-gray")}),
                unsafe_allow_html=True)
            _ov_pagination_controls(filtered_sorted, key="bt_closed", page_size=20)
            st.download_button("Download trades CSV (filtered view)",
                              filtered.to_csv(index=False),
                              "backtest_trades.csv")


# ---------------------------------------------------------------------------
# Page: Fundamentals (Value Score)
# ---------------------------------------------------------------------------

def page_fundamentals():
    if "value_scores" not in st.session_state and os.path.exists(VALUE_SCORE_CACHE):
        st.session_state["value_scores"] = pd.read_pickle(VALUE_SCORE_CACHE)
        st.session_state["value_scores_is_cached"] = True

    _last_scan_bit = ""
    if st.session_state.get("value_scores_is_cached"):
        age = dt.datetime.now() - dt.datetime.fromtimestamp(
            os.path.getmtime(VALUE_SCORE_CACHE))
        _last_scan_bit = f" · last scan {age.total_seconds() / 3600:.1f}h ago"

    hdr_l, hdr_r = st.columns([3, 2])
    with hdr_l:
        st.markdown(
            '<div class="ov-header" style="margin-bottom:0;">'
            '<div><span class="ov-h1">📊 Fundamentals</span> '
            '<span class="ov-sub">· primary-source value score</span></div></div>',
            unsafe_allow_html=True)
        st.caption(
            "0-100 from audited XBRL filings — no scraping, no LLM"
            + _last_scan_bit)
    with hdr_r:
        with st.container(key="fund_scan_row"):
            with st.popover("ℹ️"):
                st.markdown(
                    "**Sector-aware scoring.** Banks and NBFCs file under "
                    "structurally different XBRL taxonomies — banks don't tag "
                    "Revenue/Equity/Current Assets at all, and general-company "
                    "thresholds would flag every healthy NBFC as over-levered "
                    "(NBFCs run 3-6x leverage by design). Each symbol is routed "
                    "to the rubric matching what its filings actually contain: "
                    "**general** (ROE, D/E, Current Ratio, FCF, Revenue CAGR, "
                    "PEG), **banking** (ROE, ROA, NIM proxy, Gross/Net NPA, "
                    "Advances growth), or **nbfc** (ROE, ROA, D/E, Loan growth "
                    "— also covers AMCs per NSE's own filing classification). "
                    "Insurers aren't covered yet — their key metrics "
                    "(persistency, embedded value, solvency ratio) aren't "
                    "reliably XBRL-tagged. Balance-sheet ratios only refresh "
                    "once a year (audited annual filing). Missing sub-metrics "
                    "are dropped, not faked — check a row's missing pillars "
                    "before trusting a high total score.")
            _run_value_scan_clicked = st.button("Run value score scan", type="primary")

    _existing_scores = st.session_state.get("value_scores")
    _rubrics_present = (sorted(_existing_scores["rubric"].dropna().unique())
                       if _existing_scores is not None else [])

    with st.container(border=True, key="ov-card-fund-filters"):
        v1, v2, v3, v4 = st.columns(4)
        with v1:
            max_syms_v = st.slider("Symbols to scan", 10, len(config.UNIVERSE),
                                   len(config.UNIVERSE), step=1,
                                   key="value_scan_n",
                                   help="~0.3s/symbol + XBRL download time")
        with v2:
            n_years_v = st.slider("Years of annual history", 2, 5, 3,
                                  key="value_scan_years")
        with v3:
            st.markdown(
                '<p style="font-size:11px;font-weight:500;color:var(--ov-text-muted);'
                'margin:0 0 4px;">PEG input</p>',
                unsafe_allow_html=True)
            use_price = st.checkbox("Use live price", value=True,
                                    key="value_scan_price",
                                    help="Uses today's live price for PEG instead of "
                                         "the price as of the filing date.")
        with v4:
            sector = st.selectbox(
                "Filter by sector", ["All"] + _rubrics_present,
                key="fund_sector_filter",
                help="Each sector uses a different rubric with different metrics — "
                     "filtering keeps the table to the columns that actually apply.")

        if _run_value_scan_clicked:
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
            st.markdown(
                '<div class="ov-alert ov-alert-success">✓ Scan complete — '
                f'{int(result["total_score"].notna().sum())}/{len(result)} scored.'
                '</div>', unsafe_allow_html=True)

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
        st.info("Click **Run value score scan** to fetch XBRL filings and score the universe.")
        return
    vdf = st.session_state["value_scores"].copy()

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

    with st.container(border=True, key="ov-card-fund-ranked"):
        st.markdown(
            '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
            '<span class="ov-dot" style="background:var(--ov-teal);">'
            f'</span>Ranked <span class="ov-badge ov-badge-gray">'
            f'{shown["total_score"].notna().sum()} / {len(shown)} scored</span>'
            f'{f" · {sector}" if sector != "All" else ""}</p>', unsafe_allow_html=True)
        fmt = {c: "{:.2f}" for c in numeric_cols}
        rubric_badge = {"general": "ov-badge-purple", "banking": "ov-badge-blue",
                        "nbfc": "ov-badge-pink", "general_insurance": "ov-badge-gray",
                        "life_insurance": "ov-badge-gray"}

        def _score_badge_cls(v):
            return "ov-badge-green" if v >= 60 else "ov-badge-amber" if v >= 40 else "ov-badge-red"

        shown_display = shown[show_cols].copy()
        shown_display.insert(0, "symbol", shown_display.index)
        if "total_score" in shown_display.columns:
            shown_display["total_score"] = shown_display["total_score"].apply(
                lambda v: round(v, 1) if pd.notna(v) else v)
        shown_page = _ov_page_slice(shown_display, key="fund_ranked")
        st.markdown(
            _ov_table_html(
                shown_page, sym_cols=["symbol"], num_fmt=fmt,
                badges={"rubric": rubric_badge, "total_score": _score_badge_cls}),
            unsafe_allow_html=True)
        _ov_pagination_controls(shown_display, key="fund_ranked")

    def _bucket_badge(v):
        try:
            n = float(v)
        except (TypeError, ValueError):
            return "ov-badge-gray"
        return "ov-badge-green" if n >= 4 else "ov-badge-amber" if n >= 2 else "ov-badge-red"

    col_score, col_buckets = st.columns(2)
    with col_score:
        with st.container(border=True, key="ov-card-fund-breakdown"):
            bd1, bd2, bd3 = st.columns([1.9, 1.6, 2.5])
            with bd1:
                st.markdown(
                    '<p class="ov-card-title" style="margin-bottom:0;border-bottom:0px solid var(--ov-border);">'
                    '<span class="ov-dot" style="background:var(--ov-purple);"></span>'
                    'Score breakdown</p>', unsafe_allow_html=True)
            with bd2:
                sym_choice = st.selectbox(
                    "Symbol", list(shown.index), key="value_score_detail_sym",
                    label_visibility="collapsed")
            if sym_choice:
                row = shown.loc[sym_choice]
                with bd3:
                    st.markdown(
                        f'<p class="ov-card-meta" style="text-align:right;margin:6px 0 0;'
                        f'font-size:10px;font-weight:700;color:var(--ov-purple-d);">'
                        f'{row.get("rubric")} rubric · score {row.get("total_score")} · '
                        f'FY {row.get("fiscal_year_end", "—")}</p>', unsafe_allow_html=True)
                st.markdown(
                    '<p class="ov-muted" style="margin-top:10px;font-size:10px;">Pillar scores (0-5)</p>',
                    unsafe_allow_html=True)
                pillar_scores = row.get("pillar_scores") or {}
                _pillar_rows = []
                for k, v in pillar_scores.items():
                    _pct = min(100.0, max(0.0, float(v) / 5 * 100))
                    _color = ("#1d9e75" if v >= 4 else "#ef9f27" if v >= 2 else "#e24b4a")
                    _pillar_rows.append(
                        '<div class="ov-sector-row"><div class="ov-sector-head">'
                        f'<span>{html_lib.escape(k.replace("_", " ").title())}</span>'
                        f'<span class="ov-sym">{v:.1f}</span></div>'
                        f'<div class="ov-sector-bar"><div class="ov-sector-fill" '
                        f'style="width:{_pct:.1f}%;background:{_color};"></div></div></div>')
                st.markdown("".join(_pillar_rows), unsafe_allow_html=True)
                if row.get("missing_pillars"):
                    st.markdown(
                        '<div class="ov-alert">Excluded from total (no data): '
                        f'{", ".join(row["missing_pillars"])}</div>', unsafe_allow_html=True)

    with col_buckets:
        with st.container(border=True, key="ov-card-fund-buckets"):
            st.markdown(
                '<p class="ov-card-title" style="border-bottom:0px solid var(--ov-border);">'
                '<span class="ov-dot" '
                'style="background:var(--ov-teal);"></span>Sub-metric buckets (0-5)</p>',
                unsafe_allow_html=True)
            if sym_choice:
                sub_scores = row.get("sub_scores") or {}
                sub_df = pd.DataFrame(
                    [{"Metric": k.replace("_", " ").title(), "Bucket": v}
                     for k, v in sub_scores.items()])
                st.markdown(_ov_table_html(sub_df, badges={"Bucket": _bucket_badge}),
                           unsafe_allow_html=True)

    incomplete = shown[shown["missing_pillars"].apply(bool)]
    with st.expander(f"Rows with incomplete data ({len(incomplete)})"):
        _incomplete_tip = html_lib.escape(
            "A pillar is excluded from the total (not defaulted) when "
            "none of its sub-metrics are available — usually means "
            "fewer than 2 years of annual filings are retrievable via "
            "NSE's endpoint for this name, or (for "
            "'unsupported_taxonomy') the sector isn't covered by any "
            "rubric yet.")
        st.markdown(
            f'<span class="ov-info-icon" title="{_incomplete_tip}">'
            'ℹ️ Why rows land here</span>', unsafe_allow_html=True)
        inc_cols = show_cols + ["missing_pillars"]
        inc_display = incomplete[inc_cols].copy()
        inc_display.insert(0, "symbol", inc_display.index)
        inc_display["missing_pillars"] = inc_display["missing_pillars"].apply(
            lambda ps: ", ".join(ps) if isinstance(ps, (list, tuple)) else ps)
        st.markdown(_ov_table_html(inc_display, sym_cols=["symbol"]), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page: Job Log
# ---------------------------------------------------------------------------

JOB_TYPES = ["rebalance_scan", "gap_check", "fundamentals_refresh", "screen_run"]

# Mirrors deploy/vps/systemd/*.timer's OnCalendar schedules -- kept in sync
# by hand, not read from systemd itself (this dashboard process has no
# visibility into the VPS's timer state). weekdays: 0=Mon..6=Sun.
# screen_run has no entry -- it's the Screener page's manual "Run screen"
# button only, never scheduled.
_JOB_SCHEDULES = {
    "rebalance_scan": ([0, 1, 2, 3, 4], 14, 45),
    "gap_check": ([0, 1, 2, 3, 4], 9, 16),
    "fundamentals_refresh": ([0], 8, 0),
}


def _next_scheduled_run(job_type: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """Next occurrence of job_type's systemd timer schedule, skipping NSE
    trading holidays (nse_holidays.py) on top of the timer's own weekday
    filter -- the timer itself fires on every Mon-Fri regardless of NSE
    holidays (systemd has no notion of them), so this is a display-only
    projection of when the job would next do something meaningful, not a
    claim about exactly when systemd will next invoke the service. None
    for a manual-only job type (no entry in _JOB_SCHEDULES)."""
    now = now or dt.datetime.now()
    sched = _JOB_SCHEDULES.get(job_type)
    if sched is None:
        return None
    weekdays, hour, minute = sched
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    for _ in range(21):  # far enough to clear a holiday cluster + a weekly schedule
        if (candidate.weekday() in weekdays and candidate > now
                and not nse_holidays.is_trading_holiday(candidate.date())):
            return candidate
        candidate = (candidate + dt.timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
    return None


def _format_next_run(job_type: str, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    nxt = _next_scheduled_run(job_type, now)
    if nxt is None:
        return "Manual only -- no schedule"
    if nxt.date() == now.date():
        when = f"today {nxt:%H:%M}"
    elif nxt.date() == (now + dt.timedelta(days=1)).date():
        when = f"tomorrow {nxt:%H:%M}"
    else:
        when = f"{nxt:%a %d %b} {nxt:%H:%M}"
    return f"Next: {when}"


def page_job_log():
    _joblog_tip = html_lib.escape(
        "Every scheduled job (systemd timers on the VPS) and manual "
        "background-job button writes a row here -- answers \"did today's "
        "jobs actually run\" without SSH-ing in to read raw logs.")
    st.markdown(
        '<div class="ov-header"><div><span class="ov-h1">🗂️ Job Log</span>'
        f'<span class="ov-info-icon" title="{_joblog_tip}">ℹ️</span></div></div>',
        unsafe_allow_html=True)

    _job_tones = {"success": "green", "failed": "red", "running": "amber"}
    _job_cards = []
    for jt in JOB_TYPES:
        last = state_db.get_last_job_run(jt)
        label = COLUMN_LABELS.get(jt, jt)
        next_run_line = _format_next_run(jt)
        if last is None:
            _job_cards.append(_ov_metric_html(label, "never run", next_run_line, "", "coral"))
            continue
        started = dt.datetime.fromisoformat(last["started_at"])
        age_hr = (dt.datetime.now() - started).total_seconds() / 3600
        badge = {"success": "✅", "failed": "❌", "running": "⏳"}.get(last["status"], "❓")
        # error_message stores the FULL traceback (real debugging value in
        # the DB/expander below) -- but a raw multi-hundred-line dump in
        # this compact metric card is unreadable. A traceback's last
        # non-empty line is always "ExceptionType: message", so that alone
        # is what shows here; the full detail is one click away below.
        _err_msg = last.get("error_message") or ""
        _err_last_line = _err_msg.strip().splitlines()[-1] if _err_msg.strip() else ""
        note = last.get("summary") or _err_last_line
        note = f"{note}<br>{next_run_line}" if note else next_run_line
        _job_cards.append(_ov_metric_html(
            label, f"{badge} {age_hr:.1f}h ago", note,
            "ov-neg" if last["status"] == "failed" else "", _job_tones.get(last["status"], "coral")))
    st.markdown(f'<div class="ov-grid-metrics">{"".join(_job_cards)}</div>', unsafe_allow_html=True)

    st.divider()
    with st.container(border=True, key="ov-card-joblog-history"):
        st.markdown(
            '<p class="ov-card-title"><span class="ov-dot" style="background:var(--ov-blue);">'
            '</span>History</p>', unsafe_allow_html=True)
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

        # error_message holds the FULL traceback (real value in the
        # "full error detail" expander below) -- dumping that raw into a
        # table cell blew up that row's height/width for every failed
        # run. Same last-non-empty-line truncation as the quick-glance
        # cards above; summary already stays blank on a failed run
        # (finish_job_run() only ever sets one or the other), so this
        # only ever fills in where summary was empty.
        _display_runs = runs.drop(columns=["id", "error_message"]).copy()
        _display_runs["summary"] = runs.apply(
            lambda r: r["summary"] or (
                str(r["error_message"]).strip().splitlines()[-1]
                if pd.notna(r["error_message"]) and str(r["error_message"]).strip() else ""),
            axis=1)
        st.markdown(
            _ov_table_html(
                _display_runs, num_fmt={"duration_sec": "{:.1f}s"},
                badges={
                    "status": {"success": "ov-badge-green", "failed": "ov-badge-red",
                              "running": "ov-badge-amber"},
                    "trigger_type": {"scheduled": "ov-badge-blue", "manual": "ov-badge-purple"}}),
            unsafe_allow_html=True)

    failed = runs[runs["status"] == "failed"]
    if not failed.empty:
        with st.expander(f"Failed runs -- full error detail ({len(failed)})"):
            for _, r in failed.iterrows():
                st.markdown(f"**{r['job_type']}** at {r['started_at']}")
                st.code(r["error_message"] or "(no error message captured)")


# ---------------------------------------------------------------------------
# Page: Rebalance History
# ---------------------------------------------------------------------------

def page_rebalance_history():
    _rh_tip = html_lib.escape(
        "Every sell/buy/top-up/stop-update ever proposed, across every "
        "rebalance run. Lifecycle: proposed -> executed | error (attempted, "
        "failed -- reason recorded) | expired (superseded by a newer scan "
        "before ever being acted on). Nothing here is ever deleted -- this "
        "is the full audit trail, not just what's currently pending (see "
        "Live Rebalance for that).")
    st.markdown(
        '<div class="ov-header"><div><span class="ov-h1">📜 Rebalance History</span>'
        f'<span class="ov-info-icon" title="{_rh_tip}">ℹ️</span></div>'
        '<div class="ov-chips"><span class="ov-chip ov-chip-muted">proposed → '
        'executed | error | expired</span></div></div>', unsafe_allow_html=True)

    with st.container(border=True, key="ov-card-rh-history"):
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            action_filter = st.multiselect(
                "Action", ["sell", "buy", "top_up", "stop_update"], key="rh_action_filter")
        with f2:
            status_filter = st.multiselect(
                "Status", ["proposed", "executed", "error", "expired"], key="rh_status_filter")
        with f3:
            symbol_filter = st.text_input("Symbol (exact)", key="rh_symbol_filter")
        with f4:
            since_date = st.date_input(
                "Since", value=dt.date.today() - dt.timedelta(days=30), key="rh_since")

        hist = state_db.get_rebalance_history(
            status=status_filter or None, action_type=action_filter or None,
            symbol=symbol_filter.strip() or None, since=since_date.isoformat(), limit=1000)

        st.caption(f"Showing {len(hist)} item(s)")
        if hist.empty:
            st.info("No rebalance items match these filters.")
            return

        st.markdown(
            _ov_table_html(
                hist.drop(columns=["error_message"]), sym_cols=["symbol"],
                num_fmt={"qty": "{:.0f}", "price": "₹{:.2f}"},
                badges={
                    "action_type": {"sell": "ov-badge-red", "buy": "ov-badge-green",
                                    "top_up": "ov-badge-blue", "stop_update": "ov-badge-purple"},
                    "status": {"proposed": "ov-badge-amber", "executed": "ov-badge-green",
                              "error": "ov-badge-red", "expired": "ov-badge-gray"}}),
            unsafe_allow_html=True)

    errors = hist[hist["status"] == "error"]
    if not errors.empty:
        with st.expander(f"Errors — full detail ({len(errors)})"):
            for _, r in errors.iterrows():
                st.markdown(f"**{r['symbol']}** ({r['action_type']}) — run at {r['run_time']}")
                st.code(r["error_message"] or "(no error message captured)")


# ---------------------------------------------------------------------------
# Page: Tradebook
# ---------------------------------------------------------------------------

def page_tradebook():
    _tb_tip = html_lib.escape(
        "Every trade this app has opened, with its entry-time "
        "technical/fundamental snapshot and a real exit reason -- separate "
        "from the Positions & Trade page's live view, meant for "
        "historical/analytics use.")
    st.markdown(
        '<div class="ov-header"><div><span class="ov-h1">📒 Tradebook</span>'
        f'<span class="ov-info-icon" title="{_tb_tip}">ℹ️</span></div></div>',
        unsafe_allow_html=True)

    trades = state_db.get_trades()
    if trades.empty:
        st.info("No trades recorded yet.")
        return

    closed = trades[trades["status"] == "closed"]
    if not closed.empty:
        wins = closed[closed["realized_pnl"] > 0]
        win_rate = 100 * len(wins) / len(closed[closed["realized_pnl"].notna()]) \
            if closed["realized_pnl"].notna().any() else float("nan")
        total_realized = float(closed["realized_pnl"].sum())
        best = closed["realized_ret_pct"].max()
        worst = closed["realized_ret_pct"].min()
        open_count = len(trades[trades["status"] == "open"])
        st.markdown(
            '<div class="ov-grid-metrics">'
            + _ov_metric_html("Win rate", f"{win_rate:.1f}%" if win_rate == win_rate else "—",
                             f"of {closed['realized_pnl'].notna().sum()} closed", "", "green")
            + _ov_metric_html(
                "Avg holding days",
                f"{closed['holding_days'].mean():.0f}" if closed["holding_days"].notna().any() else "—",
                "closed trades", "", "blue")
            + _ov_metric_html("Total realized P&L", f"₹{total_realized:+,.0f}", "since inception",
                             "ov-pos" if total_realized >= 0 else "ov-neg", "green",
                             "ov-pos" if total_realized >= 0 else "ov-neg")
            + _ov_metric_html(
                "Best / worst", f"{best:+.1f}% / {worst:+.1f}%" if pd.notna(best) else "—",
                "per trade return", "", "purple")
            + _ov_metric_html("Open trades", str(open_count), "live now", "", "teal")
            + '</div>', unsafe_allow_html=True)

    st.divider()
    with st.container(border=True, key="ov-card-tb-history"):
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
        st.markdown(
            _ov_table_html(
                filtered[display_cols], sym_cols=["symbol"],
                pnl_cols=["realized_pnl", "realized_ret_pct"],
                num_fmt={"entry_price": "₹{:.2f}", "exit_price": "₹{:.2f}",
                        "initial_stop": "₹{:.2f}", "latest_recommended_stop": "₹{:.2f}",
                        "entry_score": "{:.2f}", "entry_rsi": "{:.1f}",
                        "entry_pct_52w_high": "{:.2f}", "entry_vol_expansion": "{:.2f}",
                        "entry_fundamental_score": "{:.1f}"},
                badges={"status": {"open": "ov-badge-green", "closed": "ov-badge-gray"}}),
            unsafe_allow_html=True)
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
page_rebalance_history_p = st.Page(page_rebalance_history, title="Rebalance History", icon="📜")
page_ledger_p = st.Page(page_ledger, title="Ledger", icon="💰")
page_admin_p = st.Page(page_admin, title="Admin", icon="⚙️")

# Injected before the sidebar (not per-page) so every page -- not just
# Overview, where this design system started -- gets the same compact
# card/badge/table look, and so the sidebar CSS below applies immediately.
st.markdown(_OVERVIEW_CSS, unsafe_allow_html=True)

with st.sidebar:
    # Flat, always-visible tabs grouped under a plain small-caps label --
    # matches the mockup's .side-label/.tab exactly (no collapse behavior
    # there at all), which an st.expander could never fully look like no
    # matter how much its border/background got stripped via CSS (it still
    # carries its own chevron/toggle chrome). st.navigation itself runs
    # with position="hidden" below so routing/query-params/current-page
    # tracking keep working exactly as before, just with no visible
    # built-in widget -- this whole block is just the visible menu.
    st.markdown('<p class="ov-side-label">Trading</p>', unsafe_allow_html=True)
    st.page_link(page_cockpit_p)
    st.page_link(page_live_rebalance_p)
    st.page_link(page_positions_trade_p)
    st.page_link(page_screener_p)
    st.page_link(page_fundamentals_p)
    st.page_link(page_admin_p)
    st.page_link(page_ledger_p)

    st.markdown('<p class="ov-side-label">Audit Trail</p>', unsafe_allow_html=True)
    st.page_link(page_tradebook_p)
    st.page_link(page_job_log_p)
    st.page_link(page_rebalance_history_p)

    st.markdown('<p class="ov-side-label">Testing</p>', unsafe_allow_html=True)
    st.page_link(page_backtest_p)

    # Streamlit gives the current page's link no stable DOM marker (just an
    # unstable emotion class with a faint default tint), so CSS alone can't
    # paint the mockup's active-tab pill. Stamp aria-current="page" on the
    # link whose href matches the URL; the CSS above keys off it. A one-shot
    # script is not enough -- React re-renders the sidebar links right after
    # this script runs and wipes the attribute -- so a MutationObserver
    # (installed once per browser session) re-stamps after every re-render.
    # Watching childList only, not attributes, so its own setAttribute calls
    # can't re-trigger it in a loop.
    st.html(
        """<script>
        (function(){
          function mark(){
            const links = document.querySelectorAll(
              '[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"]');
            const path = location.pathname.replace(/\\/+$/, "");
            links.forEach(a => {
              const href = (a.getAttribute("href") || "").split("?")[0];
              const active = href === "" ? (path === "" || path === "/")
                                         : path.endsWith("/" + href);
              if (active) a.setAttribute("aria-current", "page");
              else if (a.hasAttribute("aria-current")) a.removeAttribute("aria-current");
            });
          }
          mark();
          if (!window.__ovNavMarker) {
            // Body-wide childList observer: navigating pages re-renders the
            // MAIN area but often leaves the sidebar links untouched, so a
            // sidebar-scoped observer never fires and the pill stays on the
            // old page. Any rerun mutates the body somewhere; mark() is two
            // cheap queries over ~10 links. history hooks catch the URL
            // change itself (Streamlit navigates via pushState, no popstate).
            window.__ovNavMarker = new MutationObserver(mark);
            window.__ovNavMarker.observe(document.body, {childList: true, subtree: true});
            const push = history.pushState.bind(history);
            history.pushState = function(){ push.apply(null, arguments); setTimeout(mark, 0); };
            const repl = history.replaceState.bind(history);
            history.replaceState = function(){ repl.apply(null, arguments); setTimeout(mark, 0); };
            window.addEventListener("popstate", () => setTimeout(mark, 0));
          }
        })();
        </script>""",
        unsafe_allow_javascript=True)

    st.divider()
    if st.button("LOGOUT →", key="ov_logout"):
        state_db.delete_remember_token(st.context.cookies.get("remember_token", ""))
        st.session_state["dashboard_authenticated"] = False
        st.html(
            '<script>document.cookie = "remember_token=; max-age=0; path=/; Secure";</script>',
            unsafe_allow_javascript=True)
        st.rerun()

# Global brandbar -- shown above every page's own content, matching the
# mockup's .brandbar (which sits above the tabpanes, not inside any one of
# them). Available cash / pending actions / universe size used to live in
# the sidebar; moved here to match the reference design exactly.
n_skipped = len(config.UNIVERSE_RAW) - len(config.UNIVERSE)
skipped_note = f" ({n_skipped} skipped)" if n_skipped else ""
_last_run = state_db.get_last_rebalance_run()
_n_pending = 0
if _last_run is not None:
    _n_pending = (len(_last_run["sells"]) + len(_last_run["buys"])
                 + len(_last_run.get("stop_updates", pd.DataFrame())))
_pending_chip = (f'<span class="ov-chip ov-chip-danger">🔴 {_n_pending} action(s) pending</span>'
                if _n_pending else '<span class="ov-chip ov-chip-success">✅ No actions pending</span>')
_scan_chip_g = (f"📅 Last scan {_last_run['run_time']:%d %b %H:%M}"
               if _last_run is not None else "📅 No scan run yet")
_open_slots = len(state_db.get_open_positions())
_logo_uri = _sidebar_logo_data_uri()
_logo_html = (f'<img src="{_logo_uri}" class="ov-topbar-logo" alt="KK Trading System">'
             if _logo_uri else
             '<div class="ov-brand">🚀 KK Trading System '
             '<span class="ov-sub">Calendar-entry momentum</span></div>')
# Splitting logo/chips/sync across st.columns() kept fighting the flex
# ratios (logo overlapping chips, chips wrapping early depending on
# window width) no matter how the flex-basis/shrink was tuned. Back to
# ONE markdown call for logo+chips (the original, proven .ov-header
# space-between layout -- logo left, chips right, never had this problem
# before the sync icon was added). The Sync button is a real widget that
# can't be flattened into that HTML string, so instead of sharing a flex
# row with the chips at all, it's taken OUT of the normal flow entirely
# via absolute positioning against this container (position:relative on
# .st-key-ov-topbar) -- it can't overlap or squeeze anything else because
# it no longer participates in anyone else's layout math.
with st.container(key="ov-topbar"):
    st.markdown(
        '<div class="ov-header" style="margin-bottom:0;">'
        f'{_logo_html}'
        '<div class="ov-chips">'
        f'{_pending_chip}'
        f'<span class="ov-chip ov-chip-accent">{_scan_chip_g}</span>'
        f'<span class="ov-chip ov-chip-success">Slots {_open_slots}/'
        f'{config.STRATEGY["max_positions"]}</span>'
        f'<span class="ov-chip ov-chip-accent">F&amp;O universe: {len(config.UNIVERSE_RAW)} stocks'
        f'{skipped_note} · {dt.date.today():%d %b %Y}</span>'
        '</div></div>', unsafe_allow_html=True)
    _sync_icon_uri = _asset_data_uri("synch.png")
    if _sync_icon_uri:
        # _OVERVIEW_CSS (injected once, module-level, earlier in this
        # same script run) already styles .st-key-ov_sync button as a
        # small text button -- this second <style> tag lands later in
        # the DOM, so on equal specificity it wins without needing to
        # touch that shared block just for this one icon swap.
        st.markdown(
            "<style>.st-key-ov_sync button {"
            f"background-image:url('{_sync_icon_uri}'); background-size:18px 18px; "
            "background-repeat:no-repeat; background-position:center; "
            "color:transparent !important; font-size:0 !important; "
            "width:30px !important; height:30px !important; padding:0 !important; "
            "min-width:0 !important; border-radius:50% !important;"
            "}</style>", unsafe_allow_html=True)
    if st.button("Sync", key="ov_sync"):
        st.rerun()

nav = st.navigation([page_cockpit_p, page_live_rebalance_p, page_positions_trade_p,
                    page_screener_p, page_fundamentals_p, page_tradebook_p,
                    page_job_log_p, page_rebalance_history_p, page_backtest_p,
                    page_admin_p, page_ledger_p], position="hidden")
nav.run()
