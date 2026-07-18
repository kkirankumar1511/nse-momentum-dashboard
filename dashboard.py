"""
NSE 3-6 Month Momentum Dashboard (Streamlit)

Run:  streamlit run dashboard.py

Tabs:
  1. Screener   — ranked candidates, gates, suggested stop & position size
  2. Positions  — live positions + holdings from Kite, one-click square-off
  3. Trade      — place orders (with explicit confirmation step) + GTT stops
  4. AI Briefs  — Claude-generated qualitative brief per candidate
"""

from __future__ import annotations

import datetime as dt
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
import indicators
import kite_client
import screener
import fundamentals_agent as fa

st.set_page_config(page_title="NSE Momentum Dashboard", layout="wide",
                   page_icon="📈")

st.title("NSE 3–6 Month Momentum Dashboard")
st.caption(
    "Momentum + quality screen (Jegadeesh–Titman / George–Hwang / QMJ). "
    "Signals are decision support, not advice — you own the final call."
)

# ---------------------------------------------------------------------------
# Connection check
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

c1, c2, c3 = st.columns(3)
c1.metric("Available cash", f"₹{available_cash:,.0f}")
c2.metric("F&O universe", len(config.UNIVERSE),
          help="NSE derivatives-eligible stocks, refreshed weekly from NSE")
c3.metric("Date", dt.date.today().strftime("%d %b %Y"))

import llm as _llm_top
_llm_ok, _llm_msg = _llm_top.is_available()
st.caption(f"{'🟢' if _llm_ok else '🔴'} LLM: `{_llm_top.describe()}` — {_llm_msg}")

(tab_screen, tab_pos, tab_trade, tab_ai, tab_bt, tab_comp, tab_fil, tab_value,
 tab_final, tab_rebal) = st.tabs(
    ["🔍 Screener", "💼 Positions & Holdings", "🛒 Trade", "🤖 AI Briefs",
     "🧪 Backtest", "🌱 Compounders", "📑 Filings Analyst", "📊 Value Score",
     "🎯 Final Shortlist", "📋 Daily Rebalance"]
)

# ---------------------------------------------------------------------------
# Tab 1: Screener
# ---------------------------------------------------------------------------
with tab_screen:
    SCREEN_CACHE = os.path.join("cache", "screen.pkl")

    # Show the last completed run immediately on tab visit — including after
    # a full server restart, since this reads from disk, not session state —
    # rather than forcing a fresh ~3-4 min Kite candle fetch just to see
    # results that were already computed. "Run screen" is what actually
    # re-hits Kite for live data.
    if "screen" not in st.session_state and os.path.exists(SCREEN_CACHE):
        st.session_state["screen"] = pd.read_pickle(SCREEN_CACHE)
        st.session_state["screen_time"] = dt.datetime.fromtimestamp(
            os.path.getmtime(SCREEN_CACHE))
        st.session_state["screen_is_cached"] = True

    colA, colB = st.columns([1, 3])
    with colA:
        with_fund = st.checkbox(
            "Include fundamental quality gate", value=True,
            help="Uses the Value Score tab's primary-XBRL fundamental score "
                 "(sector-aware: value_score/bank_score/nbfc_score/etc). "
                 "Reuses those results if already run/loaded this session, "
                 "or the on-disk cache, rather than re-scanning NSE.")
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

    if "screen" in st.session_state:
        t: pd.DataFrame = st.session_state["screen"]
        cached_note = " 📁 (from cache — click Run screen to refresh)" \
            if st.session_state.get("screen_is_cached") else ""
        st.caption(f"Last run: {st.session_state['screen_time']:%d %b %Y %H:%M}"
                  f"{cached_note}")

        candidates = t[t["all_gates"]]
        show_cols = ["score", "breakout_bonus", "base_days", "price",
                     "rs_3m", "rs_6m", "pct_52w_high", "rsi",
                     "vol_expansion", "atr_pct", "suggested_stop",
                     "fundamental_score", "fundamental_rubric"]
        show_cols = [c for c in show_cols if c in candidates.columns]
        # fundamental_rubric is a string column ("general"/"nbfc"/...) — a
        # single global format spec would crash trying to apply "{:.2f}" to
        # it, so format only the actually-numeric columns.
        num_fmt = {c: "{:.2f}" for c in show_cols if c != "fundamental_rubric"}

        priority = candidates[candidates.get("priority", False) == True]  # noqa: E712
        if not priority.empty:
            st.subheader(f"🚀 Priority: long-year breakouts ({len(priority)})")
            st.caption("Recently broke above a multi-year high after a ≥6-month "
                       "base — no overhead supply above current price.")
            st.dataframe(
                priority[show_cols].style.format(num_fmt, na_rep="—"),
                width="stretch",
            )

        rest = candidates[~candidates.index.isin(priority.index)]
        st.subheader(f"✅ Other candidates passing all gates ({len(rest)})")
        st.dataframe(
            rest[show_cols].style.format(num_fmt, na_rep="—"),
            width="stretch",
        )

        if "near_breakout" not in t.columns:
            st.info("This result was cached before the pre-breakout watchlist was "
                    "added — click 'Run screen' again to see it.")
            watch = pd.DataFrame()
        else:
            watch = screener.pre_breakout_watchlist(t)
        st.subheader(f"🔭 Pre-breakout watchlist — do not trade yet ({len(watch)})")
        st.caption("Coiled under an unbroken multi-year high with a ≥6-month base. "
                  "Overhead supply (trapped sellers from the old high) hasn't cleared "
                  "yet — a long base can just as easily fail at this ceiling as break "
                  "through it. Wait for a confirmed close above the prior high before "
                  "entering; these are excluded from the candidate lists above until then.")
        if not watch.empty:
            watch_cols = ["pct_to_breakout", "pct_of_ly_high", "base_days", "price",
                         "rs_3m", "rs_6m", "rsi", "fundamental_score", "fundamental_rubric"]
            watch_cols = [c for c in watch_cols if c in watch.columns]
            watch_fmt = {c: "{:.2f}" for c in watch_cols if c != "fundamental_rubric"}
            st.dataframe(
                watch[watch_cols].style.format(watch_fmt, na_rep="—"),
                width="stretch",
            )
        else:
            st.caption("No stocks currently coiled under a multi-year high.")

        with st.expander("Full universe (including gate failures)"):
            all_cols = show_cols + ["ly_breakout", "near_breakout", "trend_ok",
                                    "near_high_ok", "rsi_ok", "not_pre_breakout",
                                    "quality_ok", "quality_fails"]
            all_cols = [c for c in all_cols if c in t.columns]
            st.dataframe(t[all_cols], width="stretch")

        # ---- Chart for a selected symbol ----
        st.divider()
        sym = st.selectbox("Chart a symbol", list(t.index))
        if sym:
            df = kite_client.fetch_daily_candles(
                sym, days=config.STRATEGY["history_days"])
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
    else:
        st.info("Click **Run screen** to fetch Kite data and rank the universe.")

# ---------------------------------------------------------------------------
# Tab 2: Positions & Holdings
# ---------------------------------------------------------------------------
with tab_pos:
    left, right = st.columns(2)

    with left:
        st.subheader("Open positions")
        pos = kite_client.get_positions()
        if pos.empty or (pos.get("quantity") == 0).all():
            st.caption("No open positions.")
        else:
            live = pos[pos["quantity"] != 0]
            st.dataframe(
                live[["tradingsymbol", "quantity", "average_price",
                      "last_price", "pnl", "product"]],
                width="stretch", hide_index=True,
            )
            st.metric("Total position P&L", f"₹{live['pnl'].sum():,.0f}")

    with right:
        st.subheader("Holdings (CNC)")
        hold = kite_client.get_holdings()
        if hold.empty:
            st.caption("No holdings.")
        else:
            hold["pnl_pct"] = ((hold["last_price"] / hold["average_price"]) - 1) * 100
            st.dataframe(
                hold[["tradingsymbol", "quantity", "average_price",
                      "last_price", "pnl", "pnl_pct"]],
                width="stretch", hide_index=True,
            )
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
            width="stretch", hide_index=True,
        )
    else:
        st.caption("No orders today.")

# ---------------------------------------------------------------------------
# Tab 3: Trade
# ---------------------------------------------------------------------------
with tab_trade:
    st.subheader("Place an order")
    st.caption("Sizing uses your ATR stop so every position risks the same % of capital.")

    cfg = config.STRATEGY
    col1, col2, col3 = st.columns(3)
    with col1:
        symbol = st.selectbox("Symbol", config.UNIVERSE)
        side = st.radio("Side", ["BUY", "SELL"], horizontal=True)
    with col2:
        capital = st.number_input("Capital for sizing (₹)",
                                  value=float(available_cash), step=10000.0)
        order_type = st.radio("Order type", ["MARKET", "LIMIT"], horizontal=True)
        limit_price = st.number_input("Limit price", value=0.0, step=0.05) \
            if order_type == "LIMIT" else None

    # Live quote + suggested sizing
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
                              help=f"Suggested for {cfg['risk_per_trade_pct']}% risk")

    place_gtt = st.checkbox("Also place GTT stop-loss at the ATR stop", value=True)

    est_value = qty * ltp
    st.info(f"Order preview: **{side} {qty} × {symbol}** ≈ ₹{est_value:,.0f} "
            f"({order_type}{f' @ ₹{limit_price}' if limit_price else ''})"
            + (f" + GTT SL at ₹{stop:,.1f}" if place_gtt and side == 'BUY' else ""))

    confirm = st.checkbox("I confirm this order")
    if st.button("Execute order", type="primary", disabled=not confirm or qty == 0):
        try:
            oid = kite_client.place_order(symbol, qty, side,
                                          order_type=order_type,
                                          price=limit_price)
            st.success(f"Order placed: {oid}")
            if place_gtt and side == "BUY":
                gtt_id = kite_client.place_gtt_stoploss(symbol, qty, stop, ltp)
                st.success(f"GTT stop-loss placed: trigger {gtt_id} at ₹{stop:,.1f}")
        except Exception as e:
            st.error(f"Order failed: {e}")

# ---------------------------------------------------------------------------
# Tab 4: AI Briefs
# ---------------------------------------------------------------------------
with tab_ai:
    st.subheader("AI qualitative brief")
    import llm as _llm
    _ok, _msg = _llm.is_available()
    if not _ok:
        st.info(f"LLM not ready — {_msg}\n\nConfigured: `{_llm.describe()}`. "
                "Set `LLM_PROVIDER` in .env (ollama for free/local).")
    else:
        st.caption(f"Model: `{_llm.describe()}`")
        default = (list(st.session_state["screen"][st.session_state["screen"]["all_gates"]].index)
                   if "screen" in st.session_state else config.UNIVERSE[:5])
        pick = st.multiselect("Symbols to brief", config.UNIVERSE, default=default[:5])
        if st.button("Generate briefs"):
            for s in pick:
                with st.spinner(f"Researching {s}..."):
                    brief = fa.ai_brief(s)
                if brief:
                    icon = {"positive": "🟢", "neutral": "🟡", "negative": "🔴"}.get(
                        brief.get("verdict", "neutral"), "🟡")
                    with st.container(border=True):
                        st.markdown(f"**{icon} {s}** — {brief.get('summary', '')}")
                        if brief.get("catalysts"):
                            st.markdown("Catalysts: " + "; ".join(brief["catalysts"]))
                        if brief.get("red_flags"):
                            st.markdown("⚠️ Red flags: " + "; ".join(brief["red_flags"]))

# ---------------------------------------------------------------------------
# Tab 5: Backtest
# ---------------------------------------------------------------------------
with tab_bt:
    st.subheader("Backtest the strategy on real Kite data")
    st.caption(
        "Replays the exact screener logic point-in-time with monthly "
        "rebalancing, daily ATR-stop checks, and transaction costs. "
        "Fundamental gates are off (historical ratios can't be reconstructed "
        "without lookahead bias) and today's universe implies some "
        "survivorship bias — treat A/B comparisons as more reliable than "
        "absolute returns."
    )

    import backtest as bt

    b1, b2, b3 = st.columns(3)
    years = b1.slider("Years of history", 1.0, 4.0, 3.0, 0.5)
    bt_capital = b2.number_input("Starting capital (₹)", value=1_000_000.0,
                                 step=100000.0)
    cost_bps = b3.number_input("Cost per side (bps)", value=12.0, step=1.0,
                               help="STT + charges + slippage estimate")
    st.caption("Breakout priority tier is OFF by default (config.STRATEGY) — the "
              "≥6-month-base breakout bonus showed no edge over plain momentum "
              "trades on a small sample (4 breakout trades), so it no longer "
              "boosts scoring or ranking; those setups also get hard-excluded "
              "from candidates until confirmed (see Screener tab watchlist).")
    run_ab = st.checkbox("Also run WITH breakout priority, for comparison (A/B)",
                         value=True)
    st.caption("Entry timing default is 'calendar' — buys immediately at the "
              "monthly rebalance close, an arbitrary day relative to the "
              "stock's own short-term swing. The pullback A/B instead waits "
              "for gate-passers to dip to/through the 20 EMA and close back "
              "up before buying, at the cost of missing stocks that run "
              "straight up without ever pulling back.")
    run_pullback_ab = st.checkbox(
        "Also run WITH 20 EMA pullback entries, for comparison (A/B)",
        value=True)

    if st.button("Run backtest", type="primary"):
        with st.spinner("Loading candles (cached daily, first run is slow)..."):
            candles_bt, bench_bt = bt.load_candles_cached(
                config.UNIVERSE, int(years * 365) + 400)
        with st.spinner("Simulating..."):
            res_off = bt.run_backtest(candles_bt, bench_bt,
                                      initial_capital=bt_capital,
                                      cost_bps=cost_bps)
            st.session_state["bt_off"] = res_off
            if run_ab:
                cfg_on = dict(config.STRATEGY)
                cfg_on["breakout_bonus"] = 0.75
                st.session_state["bt_on"] = bt.run_backtest(
                    candles_bt, bench_bt, cfg_on,
                    initial_capital=bt_capital, cost_bps=cost_bps)
            elif "bt_on" in st.session_state:
                del st.session_state["bt_on"]
            if run_pullback_ab:
                cfg_pullback = dict(config.STRATEGY)
                cfg_pullback["entry_mode"] = "ema_pullback"
                st.session_state["bt_pullback"] = bt.run_backtest(
                    candles_bt, bench_bt, cfg_pullback,
                    initial_capital=bt_capital, cost_bps=cost_bps)
            elif "bt_pullback" in st.session_state:
                del st.session_state["bt_pullback"]
            st.session_state["bt_bench"] = bench_bt

    if "bt_off" in st.session_state:
        res = st.session_state["bt_off"]
        eq = res["equity_curve"]
        bench_bt = st.session_state["bt_bench"]

        # Equity vs NIFTY (both rebased to 100)
        nifty = bench_bt["close"].reindex(eq.index).ffill()
        plot_df = pd.DataFrame({
            "Strategy (no breakout tier)": eq / eq.iloc[0] * 100,
            "NIFTY 50": nifty / nifty.iloc[0] * 100,
        })
        if "bt_on" in st.session_state:
            eq_on = st.session_state["bt_on"]["equity_curve"]
            plot_df["Strategy (breakout tier)"] = eq_on / eq_on.iloc[0] * 100
        if "bt_pullback" in st.session_state:
            eq_pb = st.session_state["bt_pullback"]["equity_curve"]
            plot_df["Strategy (EMA pullback entry)"] = eq_pb / eq_pb.iloc[0] * 100
        st.line_chart(plot_df)

        # Final capital, prominent — cash + still-open positions marked to
        # market (nothing is force-liquidated at period end anymore; see the
        # Open Positions expander below for what's still held).
        if "final_capital" not in res:
            st.info("This result was computed before Final Capital / Open "
                    "Positions were added — click 'Run backtest' again to "
                    "see them.")
        else:
            bc1, bc2, bc3 = st.columns(3)
            bc1.metric("Final capital (calendar entry)", f"₹{res['final_capital']:,.0f}",
                      f"{(res['final_capital'] / eq.iloc[0] - 1) * 100:+.1f}%")
            bc2.metric("Open positions", len(res["open_positions"]))
            if not res["open_positions"].empty:
                bc3.metric("Unrealized P&L",
                          f"₹{res['open_positions']['unrealized_pnl'].sum():,.0f}")
            if "bt_pullback" in st.session_state:
                res_pb = st.session_state["bt_pullback"]
                eq_pb = res_pb["equity_curve"]
                pc1, pc2, pc3 = st.columns(3)
                pc1.metric("Final capital (EMA pullback entry)",
                          f"₹{res_pb['final_capital']:,.0f}",
                          f"{(res_pb['final_capital'] / eq_pb.iloc[0] - 1) * 100:+.1f}%")
                pc2.metric("Open positions", len(res_pb["open_positions"]))
                if not res_pb["open_positions"].empty:
                    pc3.metric("Unrealized P&L",
                              f"₹{res_pb['open_positions']['unrealized_pnl'].sum():,.0f}")

        # Metrics table
        m = pd.DataFrame({"Live default (calendar entry, no breakout)": res["metrics"]})
        if "bt_on" in st.session_state:
            m["Breakout ON (comparison)"] = st.session_state["bt_on"]["metrics"]
        if "bt_pullback" in st.session_state:
            m["EMA pullback entry (comparison)"] = st.session_state["bt_pullback"]["metrics"]
        st.dataframe(m, width="stretch")

        # Drawdown
        dd = (eq / eq.cummax() - 1) * 100
        st.area_chart(dd.rename("Drawdown %"))

        if "open_positions" in res and not res["open_positions"].empty:
            with st.expander(f"Open positions at period end - calendar entry "
                             f"({len(res['open_positions'])})", expanded=True):
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

        if "bt_pullback" in st.session_state:
            op_pb_src = st.session_state["bt_pullback"].get("open_positions")
            if op_pb_src is not None and not op_pb_src.empty:
                with st.expander(f"Open positions at period end - EMA pullback "
                                 f"entry ({len(op_pb_src)})", expanded=True):
                    st.caption("Still held when the backtest's date range ran out — "
                              "not force-sold. Unrealized P&L is marked to the last "
                              "available close, not an actual exit.")
                    op_pb = op_pb_src.copy()
                    op_pb["entry_date"] = pd.to_datetime(op_pb["entry_date"]).dt.date
                    st.dataframe(
                        op_pb.sort_values("unrealized_pnl", ascending=False)
                            .style.format({
                                "entry_price": "{:.2f}", "current_price": "{:.2f}",
                                "stop": "{:.2f}", "unrealized_pnl": "{:,.0f}",
                                "unrealized_ret_pct": "{:.2f}",
                            }),
                        width="stretch", hide_index=True)

        with st.expander("All closed trades (calendar entry)"):
            tr = res["trades"].copy()
            if not tr.empty:
                tr["entry_date"] = pd.to_datetime(tr["entry_date"]).dt.date
                tr["exit_date"] = pd.to_datetime(tr["exit_date"]).dt.date
                st.dataframe(tr.sort_values("entry_date", ascending=False),
                             width="stretch", hide_index=True)
                st.download_button("Download trades CSV",
                                   tr.to_csv(index=False),
                                   "backtest_trades.csv")

        if "bt_pullback" in st.session_state:
            with st.expander("All closed trades (EMA pullback entry)", expanded=True):
                tr_pb = st.session_state["bt_pullback"]["trades"].copy()
                if not tr_pb.empty:
                    tr_pb["entry_date"] = pd.to_datetime(tr_pb["entry_date"]).dt.date
                    tr_pb["exit_date"] = pd.to_datetime(tr_pb["exit_date"]).dt.date
                    st.dataframe(tr_pb.sort_values("entry_date", ascending=False),
                                width="stretch", hide_index=True)
                    st.download_button("Download pullback trades CSV",
                                       tr_pb.to_csv(index=False),
                                       "backtest_trades_pullback.csv")
                else:
                    st.caption("No closed trades in the pullback-entry run.")

# ---------------------------------------------------------------------------
# Tab 6: Compounders (multibagger-trait watchlist)
# ---------------------------------------------------------------------------
with tab_comp:
    import compounder_scan as cs

    st.subheader("Compounder watchlist — multibagger characteristics")
    st.warning(
        "**This is not a multibagger predictor, and it is not part of your "
        "3–6 month book.** Multibaggers are 3–7 year outcomes. This scans for "
        "the *traits* multibaggers showed early (accelerating earnings, high "
        "ROCE, margin expansion, low debt, promoter skin-in-the-game, room to "
        "grow). Two honest limits: (1) the F&O universe is large-cap by "
        "construction — the worst place to hunt multibaggers, so scores here "
        "skew low; (2) thousands of stocks show these same traits and go "
        "nowhere. A high score means *read the annual report*, not *buy*."
    )

    cc1, cc2 = st.columns([1, 2])
    with cc1:
        max_syms = st.slider("Symbols to scan", 10, len(config.UNIVERSE),
                             min(50, len(config.UNIVERSE)), step=10,
                             help="~1.5s per symbol (polite rate limiting)")
        min_score = st.slider("Min compounder score", 0, 100, 60)
        if st.button("Run compounder scan", type="primary"):
            bar = st.progress(0.0, text="Starting...")
            df_c = cs.scan(config.UNIVERSE[:max_syms],
                           progress_cb=lambda s, f: bar.progress(f, text=s))
            st.session_state["comp"] = df_c
            bar.empty()

    if "comp" in st.session_state:
        df_c = st.session_state["comp"]
        short = cs.shortlist(df_c, min_score)

        st.subheader(f"🌱 Shortlist ({len(short)}) — traits worth researching")
        cols = ["compounder_score", "mcap_cr", "roce", "profit_growth_3y",
                "growth_accel", "sales_growth_3y", "margin_trend_pp",
                "debt_to_equity", "promoter_holding", "pe", "peg",
                "qtr_consistency_pct"]
        cols = [c for c in cols if c in short.columns]
        if short.empty:
            st.info("Nothing clears the bar right now. That's a legitimate "
                    "result — within F&O large-caps it's often the correct one.")
        else:
            st.dataframe(short[cols].style.format("{:.1f}", na_rep="—"),
                         width="stretch")

        with st.expander("Full scan (with red flags)"):
            st.dataframe(
                df_c[cols + ["red_flags"]] if not df_c.empty else df_c,
                width="stretch")

        # Overlap: names that are BOTH long-term compounders and in momentum now
        if "screen" in st.session_state:
            st.divider()
            mom = st.session_state["screen"]
            overlap = [s for s in short.index
                       if s in mom.index and bool(mom.loc[s, "all_gates"])]
            st.subheader("⭐ Overlap: compounder traits + momentum right now")
            st.caption(
                "Names on the compounder shortlist that ALSO pass today's "
                "momentum gates. These are the most interesting: a long-term "
                "quality story the market is currently repricing. Still trade "
                "them with your 3–6 month rules and stops — the compounder "
                "thesis is a separate, unstopped, multi-year sleeve."
            )
            if overlap:
                st.dataframe(
                    mom.loc[overlap, ["score", "ly_breakout", "rs_6m",
                                      "pct_52w_high", "suggested_stop"]]
                    .join(short.loc[overlap, ["compounder_score", "roce",
                                              "growth_accel"]]),
                    width="stretch")
            else:
                st.caption("No overlap currently.")
        else:
            st.caption("Run the Screener tab too — this tab will then show "
                       "which compounders are in momentum right now.")

# ---------------------------------------------------------------------------
# Tab 7: Filings Analyst (NSE filings + AI deep read)
# ---------------------------------------------------------------------------
with tab_fil:
    import filing_analyst as fan
    import nse_api
    import xbrl_parser

    st.subheader("AI filings analyst — NSE primary sources")
    st.caption(
        "Reads the company's own filings: quarterly XBRL (as-reported "
        "financials), annual report PDFs (auditor's report, MD&A, "
        "related-party, contingent liabilities), announcements, and the "
        "promoter shareholding time series. Deterministic code owns the "
        "red flags; the AI adds judgement and can never overrule them."
    )

    st.info(
        "**This is the last stage of a funnel, not a scanner.** Annual reports "
        "are 200–400 pages each — reading all 210 F&O stocks × 5 years would "
        "be ~300,000 pages. Run the Screener first, then deep-read the ~10 "
        "names that survive. Roughly 30–60s and a few cents of tokens per stock."
    )

    # Default to the momentum survivors — that's the funnel working
    if "screen" in st.session_state:
        _s = st.session_state["screen"]
        funnel_default = list(_s[_s["all_gates"]].index[:10])
    else:
        funnel_default = []

    f1, f2 = st.columns([2, 1])
    with f1:
        picks = st.multiselect(
            "Symbols to analyze", config.UNIVERSE, default=funnel_default,
            help="Defaults to whatever passed the momentum screen.")
    with f2:
        read_ar = st.checkbox("Read annual report PDFs", value=True,
                              help="Slower/costlier but catches auditor "
                                   "qualifications and related-party issues.")

    if st.button("Run filings analysis", type="primary", disabled=not picks):
        bar = st.progress(0.0, text="Starting...")
        st.session_state["fil"] = fan.analyze_many(
            picks, read_annual_reports=read_ar,
            progress_cb=lambda s, f: bar.progress(f, text=s))
        bar.empty()

    if "fil" in st.session_state and not st.session_state["fil"].empty:
        fdf = st.session_state["fil"]

        vc = {"strong": "🟢", "watch": "🟡", "avoid": "🔴"}
        for sym, row in fdf.iterrows():
            with st.container(border=True):
                head = f"{vc.get(row['verdict'],'🟡')} **{sym}** — "\
                       f"{row['verdict'].upper()} · score {row['fund_score']}"
                if row.get("earnings_real"):
                    head += f" · earnings: {row['earnings_real']}"
                if row.get("durability"):
                    head += f" · durability: {row['durability']}"
                if row.get("confidence"):
                    head += f" · confidence: {row['confidence']}"
                st.markdown(head)
                st.write(row["summary"])
                if row["red_flags"]:
                    st.error(f"🚩 Red flags: {row['red_flags']}")
                if row.get("catalysts"):
                    st.success(f"Catalysts: {row['catalysts']}")
                if row.get("risks"):
                    st.warning(f"Risks: {row['risks']}")

        st.divider()
        st.subheader("⭐ Momentum + fundamentals confluence")
        st.caption("Names that pass the technical gates AND come back 'strong' "
                   "on the filings. This is the only list that has cleared both.")
        if "screen" in st.session_state:
            mom = st.session_state["screen"]
            strong = [s for s in fdf[fdf["verdict"] == "strong"].index
                      if s in mom.index and bool(mom.loc[s, "all_gates"])]
            if strong:
                st.dataframe(
                    mom.loc[strong, ["score", "ly_breakout", "rs_6m",
                                     "pct_52w_high", "suggested_stop"]]
                    .join(fdf.loc[strong, ["fund_score", "earnings_real",
                                           "durability"]]),
                    width="stretch")
            else:
                st.caption("No confluence right now — an honest and common result.")

        st.divider()
        with st.expander("Per-symbol detail (financials, promoter, filings)"):
            sym_d = st.selectbox("Symbol", list(fdf.index))
            if sym_d:
                qdf = xbrl_parser.quarterly_financials(sym_d)
                if not qdf.empty:
                    st.markdown("**Quarterly financials (from company XBRL)**")
                    cols = [c for c in ["qe_date", "revenue", "ebitda_margin",
                                        "pat", "net_margin", "eps_basic",
                                        "audited"] if c in qdf.columns]
                    st.dataframe(qdf[cols], width="stretch",
                                 hide_index=True)
                pt = nse_api.promoter_trend(sym_d)
                if pt.get("promoter_series"):
                    st.markdown(f"**Promoter holding** — {pt['promoter_trend']} "
                                f"({pt['promoter_change_1y']:+.2f}pp in 1y)")
                    ps = pd.DataFrame(pt["promoter_series"],
                                      columns=["date", "promoter_%"]
                                      ).set_index("date")
                    st.line_chart(ps)
                ca_up = nse_api.upcoming_corporate_actions(sym_d)
                if ca_up:
                    st.markdown("**Upcoming corporate actions** — mind the "
                                "ex-date gap; it is not a trend break.")
                    st.dataframe(pd.DataFrame(ca_up), width="stretch",
                                 hide_index=True)

# ---------------------------------------------------------------------------
# Tab 8: Value Score (primary-source fundamentals, full F&O universe)
# ---------------------------------------------------------------------------
with tab_value:
    st.subheader("📊 Value score — primary-source fundamentals, all F&O stocks")
    st.caption(
        "0-100 score from three pillars, computed entirely in Python from "
        "the company's own audited XBRL filings — no scraping, no LLM. "
        "Deterministic and free, so unlike the AI deep-read in Filings "
        "Analyst (deliberately scoped to a ~10-name shortlist), this runs "
        "across the full universe."
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

    VALUE_SCORE_CACHE = os.path.join("cache", "fno_value_scores.pkl")

    # Show the last completed run immediately on tab visit, without forcing
    # a fresh NSE-backed scan — the button below is what actually re-runs
    # against live data. Only autoloads if this browser session hasn't
    # already got results (e.g. from a run earlier in the same session).
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

    # Human-readable labels — the raw column names (roe, debt_to_equity,
    # revenue_cagr_pct...) are internal field names, not meant for reading
    # at a glance in a table header.
    COLUMN_LABELS = {
        "total_score": "Score (0-100)",
        "rubric": "Sector",
        "roe": "ROE %",
        "roa": "ROA %",
        "debt_to_equity": "Debt / Equity",
        "current_ratio": "Current Ratio",
        "revenue_cagr_pct": "Revenue CAGR %",
        "fcf_yoy_pct": "FCF Growth %",
        "peg": "PEG Ratio",
        "gross_npa_pct": "Gross NPA %",
        "net_npa_pct": "Net NPA %",
        "nim_proxy_pct": "NIM (approx.) %",
        "advances_yoy_pct": "Advances Growth %",
        "pat_yoy_pct": "Profit Growth %",
        "combined_ratio_pct": "Combined Ratio %",
        "incurred_claim_ratio_pct": "Claims Ratio %",
        "premium_yoy_pct": "Premium Growth %",
        "loan_yoy_pct": "Loan Book Growth %",
        "fiscal_year_end": "As of",
        "missing_pillars": "Data Gaps",
    }

    # DECISION-relevant headline metrics only — deliberately excludes the
    # 0-5 pillar averages (profitability/leverage/growth...) and the 0-5
    # sub-scores behind them: those explain HOW a score was computed, not
    # WHAT to decide on. See the "Score breakdown" expander below for that.
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

    if "value_scores" in st.session_state:
        vdf = st.session_state["value_scores"].copy()

        rubrics_present = sorted(vdf["rubric"].dropna().unique())
        sector = st.selectbox(
            "Filter by sector", ["All"] + rubrics_present,
            help="Each sector uses a different rubric with different metrics "
                 "(a bank's Gross NPA% isn't meaningful for an IT company, and "
                 "vice versa for PEG) — filtering keeps the table to the "
                 "columns that actually apply, instead of one wide table "
                 "that's mostly blank for any given row.")

        if sector == "All":
            shown = vdf
            # Union of every rubric's headline columns, not a separately
            # maintained list — a hand-kept "All" list silently drops columns
            # whenever a rubric's own set changes and this one isn't updated
            # to match (a mistake made and fixed once already here).
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
        fmt = {COLUMN_LABELS.get(c, c): "{:.2f}"
              for c in ["total_score"] + numeric_cols}
        st.dataframe(display_df.style.format(fmt, na_rep="—"), width="stretch")

        with st.expander("🔍 Score breakdown for one symbol"):
            st.caption("The 0-5 pillar scores and individual sub-metric "
                      "buckets behind the headline total — use this to see "
                      "*why* a symbol scored the way it did.")
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
            st.caption("A pillar is excluded from the total (not defaulted) "
                       "when none of its sub-metrics are available — usually "
                       "means fewer than 2 years of annual filings are "
                       "retrievable via NSE's endpoint for this name, or "
                       "(for 'unsupported_taxonomy') the sector isn't covered "
                       "by any rubric yet (currently just MCX, which has no "
                       "filings via this NSE endpoint at all).")
            inc_cols = show_cols + ["missing_pillars"]
            st.dataframe(
                incomplete[inc_cols].rename(columns=COLUMN_LABELS),
                width="stretch")

# ---------------------------------------------------------------------------
# Tab 9: Final Shortlist — composite quant score (LLM-independent) + optional
# AI enrichment
# ---------------------------------------------------------------------------
with tab_final:
    import final_shortlist as fs

    st.subheader("🎯 Final shortlist — the whole funnel in one click")
    st.caption(
        "210 F&O stocks → technical gates (Screener tab) → ranked by "
        "fundamental score (Value Score tab) → top N candidates → composite "
        "score (technical + fundamental + earnings consistency + promoter "
        "buying) → sector-diversification cap → final 10-15."
    )
    st.info(
        "**The final ranking is fully deterministic — no LLM required.** "
        "The AI deep-read below is optional enrichment (summary, catalysts, "
        "risks) shown alongside the picks; it does NOT decide who makes the "
        "list. This changed after a real run where every single stock "
        "showed WATCH purely because Groq's daily free-tier quota was "
        "exhausted, regardless of how strong the underlying fundamentals "
        "were — the composite score below can't be blocked by a rate limit. "
        "**Two different fundamental scores may appear**: `fundamental_score` "
        "is this session's primary-XBRL rubric (Value Score tab); "
        "`ai_fund_score` (only shown if AI enrichment ran) is Filings "
        "Analyst's own separate, older deterministic check — shown side by "
        "side since they can disagree."
    )

    have_tech = "screen" in st.session_state
    have_fund = "value_scores" in st.session_state
    if not have_tech or not have_fund:
        missing = []
        if not have_tech:
            missing.append("**Screener** tab (run the technical scan)")
        if not have_fund:
            missing.append("**Value Score** tab (run or load the fundamental scan)")
        st.warning("Needs results from: " + " and ".join(missing) +
                  " before this can run.")
    else:
        FINAL_CACHE = os.path.join("cache", "final_shortlist.pkl")
        if "final_shortlist" not in st.session_state and os.path.exists(FINAL_CACHE):
            st.session_state["final_shortlist"] = pd.read_pickle(FINAL_CACHE)
            st.session_state["final_shortlist_is_cached"] = True

        if st.session_state.get("final_shortlist_is_cached"):
            age = dt.datetime.now() - dt.datetime.fromtimestamp(
                os.path.getmtime(FINAL_CACHE))
            st.caption(f"📁 Showing the last run ({age.total_seconds() / 3600:.1f}h "
                      f"ago). Click below to re-run.")

        f1, f2, f3, f4 = st.columns(4)
        with f1:
            n_cand = st.slider("Candidates to score", 10, 30, 20, key="final_n_cand")
        with f2:
            n_final = st.slider("Final shortlist size", 5, 15, 12, key="final_n_final",
                                help="Aims for this many after the sector cap; "
                                     "relaxes the cap once if too few sectors "
                                     "are represented to reach it.")
        with f3:
            max_sector = st.slider("Max picks per sector", 1, 8, 4,
                                   key="final_max_sector")
        with f4:
            run_ai = st.checkbox("Run AI enrichment", value=False, key="final_run_ai",
                                 help="Optional. Adds summary/catalysts/risks "
                                      "per pick but never changes the ranking "
                                      "or who's selected. Off by default since "
                                      "Groq's free-tier quota is unreliable — "
                                      "turn on if you want to try it anyway.")
        read_ar = st.checkbox("Read annual report PDFs (AI enrichment only)",
                              value=True, key="final_read_ar", disabled=not run_ai)

        with st.expander("Composite score weights (advanced)"):
            w1, w2, w3, w4 = st.columns(4)
            weights = {
                "technical": w1.slider("Technical", 0.0, 1.0, fs.DEFAULT_WEIGHTS["technical"], key="w_tech"),
                "fundamental": w2.slider("Fundamental", 0.0, 1.0, fs.DEFAULT_WEIGHTS["fundamental"], key="w_fund"),
                "earnings_consistency": w3.slider("Earnings consistency", 0.0, 1.0, fs.DEFAULT_WEIGHTS["earnings_consistency"], key="w_earn"),
                "promoter": w4.slider("Promoter buying", 0.0, 1.0, fs.DEFAULT_WEIGHTS["promoter"], key="w_promo"),
            }
            st.caption("A factor missing for a given stock is dropped from "
                      "its own blend (weights renormalized), not defaulted.")

        tech = st.session_state["screen"]
        fund = st.session_state["value_scores"]
        n_gate_pass = int(tech["all_gates"].astype(bool).sum())
        st.caption(f"{n_gate_pass} of {len(tech)} stocks currently pass all "
                  f"technical gates.")

        if st.button("Run final shortlist", type="primary", disabled=n_gate_pass == 0):
            bar = st.progress(0.0, text="Starting...")
            result = fs.run_final_shortlist(
                tech, fund, n_candidates=n_cand, final_min=min(5, n_final),
                final_max=n_final, max_per_sector=max_sector, weights=weights,
                run_ai_enrichment=run_ai, read_annual_reports=read_ar,
                progress_cb=lambda s, f: bar.progress(min(f, 1.0), text=s))
            bar.empty()
            st.session_state["final_shortlist"] = result
            st.session_state["final_shortlist_is_cached"] = False
            os.makedirs("cache", exist_ok=True)
            pd.to_pickle(result, FINAL_CACHE)

    if "final_shortlist" in st.session_state:
        result = st.session_state["final_shortlist"]
        final_df = result["final"]
        has_ai = "verdict" in final_df.columns

        FINAL_LABELS = {
            "composite_score": "Composite Score",
            "technical_score": "Technical",
            "fundamental_score": "Fundamental",
            "rubric": "Sector",
            "earnings_consistency_pct": "Earnings Consistency %",
            "promoter_score": "Promoter Trend",
            "verdict": "AI Verdict",
            "ai_fund_score": "AI Fund Score",
            "confidence": "AI Confidence",
        }

        st.subheader(f"Top {len(final_df)} — final picks (ranked by composite score)")
        table_cols = ["composite_score", "technical_score", "fundamental_score",
                     "rubric", "earnings_consistency_pct", "promoter_score"]
        if has_ai:
            table_cols += ["verdict", "ai_fund_score", "confidence"]
        table_cols = [c for c in table_cols if c in final_df.columns]
        display_df = final_df[table_cols].rename(columns=FINAL_LABELS)
        num_cols = [FINAL_LABELS.get(c, c) for c in table_cols
                   if c not in ("rubric", "verdict", "confidence")]
        st.dataframe(
            display_df.style.format({c: "{:.1f}" for c in num_cols}, na_rep="—"),
            width="stretch")

        if has_ai:
            with st.expander("🔍 AI commentary per pick (summary, catalysts, risks, red flags)"):
                vc = {"strong": "🟢", "watch": "🟡", "avoid": "🔴"}
                for sym, row in final_df.iterrows():
                    if not any(row.get(c) for c in ("summary", "red_flags", "catalysts", "risks")):
                        continue
                    with st.container(border=True):
                        icon = vc.get(row.get("verdict"), "⚪")
                        st.markdown(f"{icon} **{sym}**")
                        if row.get("summary"):
                            st.write(row["summary"])
                        if row.get("red_flags"):
                            st.error(f"🚩 Red flags: {row['red_flags']}")
                        if row.get("catalysts"):
                            st.success(f"Catalysts: {row['catalysts']}")
                        if row.get("risks"):
                            st.warning(f"Risks: {row['risks']}")

        with st.expander(f"Stage 1+2 candidates ({len(result['candidates'])}, "
                         f"pre-composite-score)"):
            st.caption("Technical-gate-passers ranked by fundamental score — "
                      "this is the list the composite score is computed over.")
            cand_cols = [c for c in ["fundamental_score", "rubric",
                                     "technical_score", "ly_breakout",
                                     "rs_6m", "pct_52w_high"]
                        if c in result["candidates"].columns]
            st.dataframe(result["candidates"][cand_cols], width="stretch")

        with st.expander("All scored candidates (before sector cap)"):
            st.caption("Every candidate with its composite score, before the "
                      "sector-diversification cap narrows it to the final list.")
            scored_cols = [c for c in ["composite_score", "fundamental_score",
                                       "technical_score", "earnings_consistency_pct",
                                       "promoter_score", "rubric"]
                          if c in result["scored"].columns]
            st.dataframe(result["scored"][scored_cols], width="stretch")

# ---------------------------------------------------------------------------
# Tab 10: Daily Rebalance (Stage 1 automation — propose only, never executes)
# ---------------------------------------------------------------------------
with tab_rebal:
    import live_rebalance as lr

    st.subheader("📋 Daily Rebalance — proposed orders, review only")
    st.warning(
        "**Nothing on this tab places an order.** It runs the exact same "
        "screener pipeline as the Screener tab, diffs it against your "
        "actual Kite holdings, and proposes sells (rebalance-rule failures: "
        "closed below 200 EMA, or dropped out of the top-ranked zone) and "
        "buys (open slots, sized off your real available cash). Execute "
        "manually in the **Trade** tab, or run "
        "`python live_rebalance.py` on a schedule (Windows Task Scheduler / "
        "cron) to regenerate this proposal once a day and review it here."
    )
    st.caption(
        "Stop-losses are not covered here: if you placed a GTT stop-loss "
        "at entry (Trade tab checkbox), your broker already enforces it "
        "intraday without this job needing to run. This tab only proposes "
        "the monthly-style rebalance rule and new entries, on demand."
    )

    if "rebalance_proposal" not in st.session_state and os.path.exists(lr.PROPOSAL_CACHE):
        st.session_state["rebalance_proposal"] = pd.read_pickle(lr.PROPOSAL_CACHE)

    if st.button("Run today's scan", type="primary"):
        bar = st.progress(0.0, text="Starting...")
        def cb(stage, frac):
            bar.progress(frac, text=stage)
        fundamentals = st.session_state.get("value_scores")
        result = lr.propose_rebalance(available_cash,
                                      fundamentals=fundamentals, progress_cb=cb)
        bar.empty()
        st.session_state["rebalance_proposal"] = result

    if "rebalance_proposal" in st.session_state:
        result = st.session_state["rebalance_proposal"]
        st.caption(f"Last run: {result['run_time']:%d %b %Y %H:%M}")

        rc1, rc2, rc3 = st.columns(3)
        rc1.metric("Current holdings", len(result["holdings"]))
        rc2.metric("Proposed sells", len(result["sells"]))
        rc3.metric("Open slots after sells", result["open_slots"])

        st.subheader(f"🔴 Proposed sells ({len(result['sells'])})")
        if not result["sells"].empty:
            st.dataframe(
                result["sells"].style.format({"avg_price": "{:.2f}"}),
                width="stretch", hide_index=True)
        else:
            st.caption("No current holdings fail the rebalance rule today.")

        st.subheader(f"🟢 Proposed buys ({len(result['buys'])})")
        if not result["buys"].empty:
            st.dataframe(
                result["buys"].style.format({
                    "price": "{:.2f}", "stop": "{:.2f}", "score": "{:.2f}",
                }),
                width="stretch", hide_index=True)
        else:
            st.caption("No open slots, or no candidates triggered an entry today.")

        with st.expander("Current holdings snapshot used for this proposal"):
            st.dataframe(
                result["holdings"].style.format({"average_price": "{:.2f}"}),
                width="stretch", hide_index=True)
    else:
        st.info("Click **Run today's scan** to generate a proposal.")
