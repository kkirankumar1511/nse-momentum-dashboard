"""
NSE Calendar-Entry Momentum Cockpit (Streamlit)

Run:  streamlit run dashboard.py

Pages (sidebar navigation):
  Cockpit           - everything that matters at a glance: cash, portfolio
                      value, P&L, open positions, today's pending actions
  Screener          - full ranked universe (all gate-passers, not just what
                      fits your open slots), plus a symbol chart
  Live Rebalance    - run the daily scan, review proposed sells/buys, execute
  Positions & Trade - live holdings/positions, square-off, manual order entry
  Backtest          - calendar-entry engine on real Kite data, 1-5 years
  Fundamentals      - primary-source XBRL value score, all F&O stocks

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

st.set_page_config(page_title="NSE Momentum Cockpit", layout="wide", page_icon="📈")

# ---------------------------------------------------------------------------
# Connection check (runs once per script execution, before any page)
# ---------------------------------------------------------------------------
if not config.KITE_ACCESS_TOKEN:
    st.error(
        "No Kite access token found. Run `python kite_client.py login`, "
        "complete login, then `python kite_client.py token <request_token>` "
        "and restart this app."
    )
    st.stop()

try:
    margins = kite_client.get_margins()
    available_cash = margins["equity"]["available"]["live_balance"]
except Exception as e:
    st.error(f"Kite connection failed (token may have expired): {e}")
    st.stop()

EQUITY_LOG = os.path.join("cache", "equity_log.csv")
SCREEN_CACHE = os.path.join("cache", "screen.pkl")
VALUE_SCORE_CACHE = os.path.join("cache", "fno_value_scores.pkl")
BACKTEST_CACHE = os.path.join("cache", "backtest_result.pkl")
FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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


def log_equity_snapshot(value: float) -> pd.DataFrame:
    """Upserts today's portfolio value into a local CSV log, so the Cockpit
    can chart account growth over time -- Kite has no such history endpoint
    for a specific strategy's slice of the account."""
    os.makedirs("cache", exist_ok=True)
    today = dt.date.today().isoformat()
    if os.path.exists(EQUITY_LOG):
        log = pd.read_csv(EQUITY_LOG)
    else:
        log = pd.DataFrame(columns=["date", "value"])
    log = log[log["date"] != today]
    log = pd.concat([log, pd.DataFrame([{"date": today, "value": value}])],
                    ignore_index=True)
    log.to_csv(EQUITY_LOG, index=False)
    return log


# ---------------------------------------------------------------------------
# Page: Cockpit
# ---------------------------------------------------------------------------

def page_cockpit():
    st.subheader("🏠 Cockpit")
    st.caption("Calendar-entry momentum system — everything that matters, at a glance.")

    merged = merged_holdings()
    holdings_value = float((merged["qty"] * merged["ltp"]).sum()) if not merged.empty else 0.0
    total_pnl = float(merged["pnl"].sum()) if not merged.empty else 0.0
    portfolio_value = available_cash + holdings_value

    log = log_equity_snapshot(portfolio_value)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Available cash", f"₹{available_cash:,.0f}")
    k2.metric("Portfolio value", f"₹{portfolio_value:,.0f}")
    k3.metric("Total P&L (unrealized)", f"₹{total_pnl:,.0f}")
    k4.metric("Open positions", f"{len(merged)} / {config.STRATEGY['max_positions']}")

    if len(log) > 1:
        st.line_chart(log.set_index("date")["value"].rename("Portfolio value (₹)"))
    else:
        st.caption("Portfolio value is logged once a day when you open this page — "
                  "the chart builds up over time as you keep using the dashboard.")

    st.divider()
    st.subheader("Action needed today")
    if os.path.exists(lr.PROPOSAL_CACHE):
        prop = pd.read_pickle(lr.PROPOSAL_CACHE)
        age_hr = (dt.datetime.now() - prop["run_time"]).total_seconds() / 3600
        n_sell, n_buy = len(prop["sells"]), len(prop["buys"])
        if n_sell or n_buy:
            st.warning(f"**{n_sell} sell(s), {n_buy} buy(s) proposed** "
                      f"(scan run {age_hr:.1f}h ago).")
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
        st.dataframe(
            merged.sort_values("pnl", ascending=False)
                .style.format({"avg_price": "{:.2f}", "ltp": "{:.2f}", "pnl": "{:,.0f}"}),
            width="stretch", hide_index=True)


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

    colA, colB = st.columns([1, 3])
    with colA:
        with_fund = st.checkbox(
            "Include fundamental quality gate", value=True,
            help="Uses the Fundamentals page's primary-XBRL score. Reuses "
                 "those results if already run/loaded this session, or the "
                 "on-disk cache, rather than re-scanning NSE.")
        if st.button("Run screen", type="primary"):
            bar = st.progress(0.0, text="Starting...")
            def cb(stage, frac):
                bar.progress(frac, text=stage)
            result = screener.run_screen(
                with_fund, fundamentals=st.session_state.get("value_scores"),
                progress_cb=cb)
            bar.empty()
            st.session_state["screen"] = result
            st.session_state["screen_time"] = dt.datetime.now()
            st.session_state["screen_is_cached"] = False
            os.makedirs("cache", exist_ok=True)
            result.to_pickle(SCREEN_CACHE)

    if "screen" not in st.session_state:
        st.info("Click **Run screen** to fetch Kite data and rank the universe.")
        return

    t: pd.DataFrame = st.session_state["screen"]
    cached_note = " 📁 (from cache — click Run screen to refresh)" \
        if st.session_state.get("screen_is_cached") else ""
    st.caption(f"Last run: {st.session_state['screen_time']:%d %b %Y %H:%M}{cached_note}")

    candidates = t[t["all_gates"]]
    show_cols = ["score", "price", "rs_3m", "rs_6m", "pct_52w_high", "rsi",
                "vol_expansion", "atr_pct", "suggested_stop",
                "fundamental_score", "fundamental_rubric"]
    show_cols = [c for c in show_cols if c in candidates.columns]
    # fundamental_rubric is a string column ("general"/"nbfc"/...) — a single
    # global format spec would crash trying to apply "{:.2f}" to it.
    num_fmt = {c: "{:.2f}" for c in show_cols if c != "fundamental_rubric"}

    st.subheader(f"✅ Candidates passing all gates ({len(candidates)})")
    st.dataframe(candidates[show_cols].style.format(num_fmt, na_rep="—"), width="stretch")

    with st.expander("Full universe (including gate failures)"):
        all_cols = show_cols + ["trend_ok", "near_high_ok", "rsi_ok",
                                "quality_ok", "quality_fails"]
        all_cols = [c for c in all_cols if c in t.columns]
        st.dataframe(t[all_cols], width="stretch")

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


# ---------------------------------------------------------------------------
# Page: Live Rebalance
# ---------------------------------------------------------------------------

def page_live_rebalance():
    st.subheader("📡 Live Rebalance — review, then execute")
    st.warning(
        "**Running the scan never places an order by itself.** Execution "
        "below requires an explicit confirmation checkbox per batch."
    )
    st.caption(
        "Runs the exact same screener pipeline as Screener/Backtest, diffs "
        "it against your actual Kite holdings, and proposes sells (closed "
        "below 200 EMA, or dropped out of the top-ranked zone) and buys "
        "(open slots, sized off your real available cash). Stop-losses "
        "aren't covered here — if you placed a GTT at entry, your broker "
        "already enforces it intraday without this needing to run. Can "
        "also be scheduled externally via `python live_rebalance.py`."
    )

    if "rebalance_proposal" not in st.session_state and os.path.exists(lr.PROPOSAL_CACHE):
        st.session_state["rebalance_proposal"] = pd.read_pickle(lr.PROPOSAL_CACHE)

    if st.button("Run today's scan", type="primary"):
        bar = st.progress(0.0, text="Starting...")
        def cb(stage, frac):
            bar.progress(frac, text=stage)
        fundamentals = st.session_state.get("value_scores")
        result = lr.propose_rebalance(available_cash, fundamentals=fundamentals,
                                      progress_cb=cb)
        bar.empty()
        st.session_state["rebalance_proposal"] = result

    if "rebalance_proposal" not in st.session_state:
        st.info("Click **Run today's scan** to generate a proposal.")
        return

    result = st.session_state["rebalance_proposal"]
    st.caption(f"Last run: {result['run_time']:%d %b %Y %H:%M}")

    rc1, rc2, rc3 = st.columns(3)
    rc1.metric("Current holdings", len(result["holdings"]))
    rc2.metric("Proposed sells", len(result["sells"]))
    rc3.metric("Open slots after sells", result["open_slots"])

    st.subheader(f"🔴 Proposed sells ({len(result['sells'])})")
    if not result["sells"].empty:
        st.dataframe(result["sells"].style.format({"avg_price": "{:.2f}"}),
                    width="stretch", hide_index=True)
        confirm_sell = st.checkbox(
            "I confirm I want to execute ALL proposed sells at market",
            key="confirm_sell_all")
        if st.button("Execute all sells", disabled=not confirm_sell):
            log = []
            for _, r in result["sells"].iterrows():
                try:
                    oid = kite_client.square_off_position(r["symbol"])
                    log.append(f"✅ {r['symbol']}: order {oid}")
                except Exception as e:
                    log.append(f"❌ {r['symbol']}: FAILED — {e}")
            for line in log:
                st.write(line)
    else:
        st.caption("No current holdings fail the rebalance rule today.")

    st.subheader(f"🟢 Proposed buys ({len(result['buys'])})")
    if not result["buys"].empty:
        st.dataframe(
            result["buys"].style.format(
                {"price": "{:.2f}", "stop": "{:.2f}", "score": "{:.2f}"}),
            width="stretch", hide_index=True)
        place_gtt = st.checkbox("Also place a GTT stop-loss for each buy",
                                value=True, key="rebal_gtt")
        confirm_buy = st.checkbox(
            "I confirm I want to execute ALL proposed buys at market",
            key="confirm_buy_all")
        if st.button("Execute all buys", disabled=not confirm_buy):
            log = []
            for _, r in result["buys"].iterrows():
                try:
                    oid = kite_client.place_order(r["symbol"], int(r["qty"]), "BUY")
                    msg = f"✅ {r['symbol']}: order {oid}"
                    if place_gtt:
                        gtt_id = kite_client.place_gtt_stoploss(
                            r["symbol"], int(r["qty"]), r["stop"], r["price"])
                        msg += f", GTT {gtt_id} @ ₹{r['stop']:.1f}"
                    log.append(msg)
                except Exception as e:
                    log.append(f"❌ {r['symbol']}: FAILED — {e}")
            for line in log:
                st.write(line)
    else:
        st.caption("No open slots, or no candidates today.")

    with st.expander("Current holdings snapshot used for this proposal"):
        st.dataframe(result["holdings"].style.format({"average_price": "{:.2f}"}),
                    width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# Page: Positions & Trade
# ---------------------------------------------------------------------------

def page_positions_trade():
    st.subheader("💼 Positions, Holdings & Trade")

    left, right = st.columns(2)
    with left:
        st.markdown("**Open positions**")
        pos = kite_client.get_positions()
        if pos.empty or (pos.get("quantity") == 0).all():
            st.caption("No open positions.")
        else:
            live = pos[pos["quantity"] != 0]
            st.dataframe(
                live[["tradingsymbol", "quantity", "average_price",
                      "last_price", "pnl", "product"]],
                width="stretch", hide_index=True)
            st.metric("Total position P&L", f"₹{live['pnl'].sum():,.0f}")

    with right:
        st.markdown("**Holdings (CNC)**")
        hold = kite_client.get_holdings()
        if hold.empty:
            st.caption("No holdings.")
        else:
            hold = hold.copy()
            hold["pnl_pct"] = ((hold["last_price"] / hold["average_price"]) - 1) * 100
            st.dataframe(
                hold[["tradingsymbol", "quantity", "average_price",
                      "last_price", "pnl", "pnl_pct"]],
                width="stretch", hide_index=True)
            st.metric("Total holdings P&L", f"₹{hold['pnl'].sum():,.0f}")

    st.divider()
    st.subheader("Square off a position")
    all_syms = []
    if not pos.empty:
        all_syms += list(pos[pos["quantity"] != 0]["tradingsymbol"])
    if not hold.empty:
        all_syms += list(hold[hold["quantity"] > 0]["tradingsymbol"])
    all_syms = sorted(set(all_syms))

    if all_syms:
        sq_sym = st.selectbox("Symbol to square off", all_syms, key="sq")
        confirm_sq = st.checkbox(
            f"I confirm I want to close my entire {sq_sym} position at market")
        if st.button("Square off", type="primary", disabled=not confirm_sq):
            try:
                order_id = kite_client.square_off_position(sq_sym)
                st.success(f"Square-off order placed: {order_id}")
            except Exception as e:
                st.error(f"Order failed: {e}")
    else:
        st.caption("Nothing to square off.")

    st.divider()
    st.subheader("Today's orders")
    orders = kite_client.get_orders()
    if not orders.empty:
        st.dataframe(
            orders[["order_timestamp", "tradingsymbol", "transaction_type",
                    "quantity", "average_price", "status"]],
            width="stretch", hide_index=True)
    else:
        st.caption("No orders today.")

    st.divider()
    st.subheader("Place a manual order")
    st.caption("Sizing uses your ATR stop so every position risks the same % of capital.")

    cfg = config.STRATEGY
    col1, col2, col3 = st.columns(3)
    with col1:
        symbol = st.selectbox("Symbol", config.UNIVERSE, key="trade_symbol")
        side = st.radio("Side", ["BUY", "SELL"], horizontal=True, key="trade_side")
    with col2:
        capital = st.number_input("Capital for sizing (₹)",
                                  value=float(available_cash), step=10000.0,
                                  key="trade_capital")
        order_type = st.radio("Order type", ["MARKET", "LIMIT"], horizontal=True,
                              key="trade_order_type")
        limit_price = st.number_input("Limit price", value=0.0, step=0.05,
                                      key="trade_limit") \
            if order_type == "LIMIT" else None

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
        qty = st.number_input("Quantity", value=int(suggested_qty), min_value=0,
                              help=f"Suggested for {cfg['risk_per_trade_pct']}% risk",
                              key="trade_qty")

    place_gtt = st.checkbox("Also place GTT stop-loss at the ATR stop", value=True,
                            key="trade_place_gtt")

    est_value = qty * ltp
    st.info(f"Order preview: **{side} {qty} × {symbol}** ≈ ₹{est_value:,.0f} "
            f"({order_type}{f' @ ₹{limit_price}' if limit_price else ''})"
            + (f" + GTT SL at ₹{stop:,.1f}" if place_gtt and side == 'BUY' else ""))

    confirm = st.checkbox("I confirm this order", key="trade_confirm")
    if st.button("Execute order", type="primary", disabled=not confirm or qty == 0,
                key="trade_execute"):
        try:
            oid = kite_client.place_order(symbol, qty, side,
                                          order_type=order_type, price=limit_price)
            st.success(f"Order placed: {oid}")
            if place_gtt and side == "BUY":
                gtt_id = kite_client.place_gtt_stoploss(symbol, qty, stop, ltp)
                st.success(f"GTT stop-loss placed: trigger {gtt_id} at ₹{stop:,.1f}")
        except Exception as e:
            st.error(f"Order failed: {e}")


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
                candles_bt, bench_bt = bt.load_candles_cached(
                    config.UNIVERSE, int(years * 365) + 400)
        fundamentals_history = None
        if use_fundamentals and os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
            fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]
        with st.spinner("Simulating..."):
            res = bt.run_backtest(candles_bt, bench_bt, initial_capital=bt_capital,
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

    run_time = st.session_state.get("bt_run_time")
    cached_note = " 📁 (from cache — click 'Run backtest' to refresh)" \
        if st.session_state.get("bt_is_cached") else ""
    if run_time is not None:
        st.caption(f"Last run: {run_time:%d %b %Y %H:%M}{cached_note}")

    res = st.session_state["bt_result"]
    eq = res["equity_curve"]
    bench_bt = st.session_state["bt_bench"]

    nifty = bench_bt["close"].reindex(eq.index).ffill()
    plot_df = pd.DataFrame({
        "Strategy": eq / eq.iloc[0] * 100,
        "NIFTY 50": nifty / nifty.iloc[0] * 100,
    })
    st.line_chart(plot_df)

    bc1, bc2, bc3, bc4 = st.columns(4)
    bc1.metric("Final capital", f"₹{res['final_capital']:,.0f}",
              f"{(res['final_capital'] / eq.iloc[0] - 1) * 100:+.1f}%")
    bc2.metric("CAGR", f"{res['metrics']['CAGR %']}%")
    bc3.metric("Sharpe", res["metrics"]["Sharpe"])
    bc4.metric("Open positions", len(res["open_positions"]))

    st.dataframe(pd.DataFrame({"Metric": res["metrics"]}), width="stretch")

    dd = (eq / eq.cummax() - 1) * 100
    st.area_chart(dd.rename("Drawdown %"))

    if not res["open_positions"].empty:
        with st.expander(f"Open positions at period end ({len(res['open_positions'])})",
                         expanded=True):
            st.caption("Still held when the backtest's date range ran out — "
                      "not force-sold. Unrealized P&L is marked to the last "
                      "available close, not an actual exit.")
            op = res["open_positions"].copy()
            op["entry_date"] = pd.to_datetime(op["entry_date"]).dt.date
            st.dataframe(
                op.sort_values("unrealized_pnl", ascending=False)
                  .style.format({
                      "entry_price": "{:.2f}", "current_price": "{:.2f}",
                      "stop": "{:.2f}", "unrealized_pnl": "{:,.0f}",
                      "unrealized_ret_pct": "{:.2f}",
                  }),
                width="stretch", hide_index=True)

    with st.expander("All closed trades"):
        tr = res["trades"].copy()
        if not tr.empty:
            tr["entry_date"] = pd.to_datetime(tr["entry_date"]).dt.date
            tr["exit_date"] = pd.to_datetime(tr["exit_date"]).dt.date
            st.dataframe(tr.sort_values("entry_date", ascending=False),
                        width="stretch", hide_index=True)
            st.download_button("Download trades CSV", tr.to_csv(index=False),
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

    COLUMN_LABELS = {
        "total_score": "Score (0-100)", "rubric": "Sector", "roe": "ROE %",
        "roa": "ROA %", "debt_to_equity": "Debt / Equity",
        "current_ratio": "Current Ratio", "revenue_cagr_pct": "Revenue CAGR %",
        "fcf_yoy_pct": "FCF Growth %", "peg": "PEG Ratio",
        "gross_npa_pct": "Gross NPA %", "net_npa_pct": "Net NPA %",
        "nim_proxy_pct": "NIM (approx.) %", "advances_yoy_pct": "Advances Growth %",
        "pat_yoy_pct": "Profit Growth %", "combined_ratio_pct": "Combined Ratio %",
        "incurred_claim_ratio_pct": "Claims Ratio %",
        "premium_yoy_pct": "Premium Growth %", "loan_yoy_pct": "Loan Book Growth %",
        "fiscal_year_end": "As of", "missing_pillars": "Data Gaps",
    }

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
    display_df = shown[show_cols].rename(columns=COLUMN_LABELS)
    fmt = {COLUMN_LABELS.get(c, c): "{:.2f}" for c in ["total_score"] + numeric_cols}
    st.dataframe(display_df.style.format(fmt, na_rep="—"), width="stretch")

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
        st.dataframe(incomplete[inc_cols].rename(columns=COLUMN_LABELS), width="stretch")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

page_cockpit_p = st.Page(page_cockpit, title="Cockpit", icon="🏠", default=True)
page_screener_p = st.Page(page_screener, title="Screener", icon="🔍")
page_live_rebalance_p = st.Page(page_live_rebalance, title="Live Rebalance", icon="📡")
page_positions_trade_p = st.Page(page_positions_trade, title="Positions & Trade", icon="💼")
page_backtest_p = st.Page(page_backtest, title="Backtest", icon="🧪")
page_fundamentals_p = st.Page(page_fundamentals, title="Fundamentals", icon="📊")

with st.sidebar:
    st.metric("Available cash", f"₹{available_cash:,.0f}")
    st.caption(f"F&O universe: {len(config.UNIVERSE)} stocks · "
              f"{dt.date.today():%d %b %Y}")

nav = st.navigation([page_cockpit_p, page_screener_p, page_live_rebalance_p,
                    page_positions_trade_p, page_backtest_p, page_fundamentals_p])
nav.run()
