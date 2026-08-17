"""
Dashboard page for the experimental Heikin-Ashi paper-trading strategy --
a fully SIMULATED portfolio, isolated in cache/state_paper.db, never
touching real capital or placing a real Kite order (see paper_db.py /
paper_engine.py's own docstrings for the containment story).

Kept as a single render() function taking the two shared HTML-table
helpers as arguments (rather than importing them from dashboard.py, which
runs as __main__ and can't be imported back) -- the smallest, most
reversible way to add this page: dashboard.py's own diff is 6 lines.
"""
from __future__ import annotations

import datetime as dt
import html as html_lib
import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import background_jobs
import paper_db
import paper_engine


def _compute_stats(equity: pd.DataFrame) -> dict:
    if equity.empty or len(equity) < 2:
        return {}
    vals = equity["value"].astype(float)
    dates = pd.to_datetime(equity["date"])
    years = max((dates.iloc[-1] - dates.iloc[0]).days / 365.25, 1 / 365.25)
    total_ret = vals.iloc[-1] / vals.iloc[0] - 1
    cagr = (vals.iloc[-1] / vals.iloc[0]) ** (1 / years) - 1 if years > 0 else float("nan")
    dd = (vals / vals.cummax() - 1).min()
    daily_ret = vals.pct_change().dropna()
    sharpe = (daily_ret.mean() / daily_ret.std()) * (252 ** 0.5) if daily_ret.std() > 0 else float("nan")
    return {"total_ret": total_ret, "cagr": cagr, "maxdd": dd, "sharpe": sharpe,
           "current_equity": vals.iloc[-1]}


def render(ov_table_html, ov_metric_html) -> None:
    st.markdown(
        '<div class="ov-header"><div><span class="ov-h1">🧻 Paper Trading</span>'
        '<span class="ov-info-icon" title="'
        + html_lib.escape(
            "Simulated portfolio, ₹10,00,000 virtual capital, running the "
            "Heikin-Ashi trend strategy validated over 8+ years of local "
            "backtesting (CAGR ~20%, alpha ~+9%). No real orders are ever "
            "placed; this is a fully separate database (cache/state_paper.db) "
            "from the real trading account.")
        + '">ℹ️</span></div></div>', unsafe_allow_html=True)
    st.warning("🧻 **SIMULATED — no real capital, no real orders.** "
              "Separate from your real portfolio in every way.")

    paper_db.ensure_initialised()

    # --- actions row -------------------------------------------------
    ac1, ac2, ac3 = st.columns([2, 2, 4])
    with ac1:
        if st.button("▶️ Run paper scan now", use_container_width=True):
            started = background_jobs.start_background_job(
                "paper_scan", _run_scan_job, job_type="paper_scan",
                summarize_fn=lambda r: r or "done")
            if started:
                st.toast("Paper scan started in the background.")
            else:
                st.toast("A paper scan is already running.")
            st.rerun()
    with ac2:
        confirm = st.checkbox("Confirm reset", key="paper_reset_confirm")
        if st.button("🗑️ Reset paper account", disabled=not confirm,
                    use_container_width=True, type="secondary"):
            paper_db.reset()
            paper_db.ensure_initialised()
            st.success("Paper account reset.")
            st.rerun()

    job = background_jobs.get_background_job("paper_scan")
    if job is not None and not job["done"]:
        frac, stage = job["progress"]
        st.progress(frac, text=stage)
    elif job is not None and job["done"] and job.get("error"):
        st.error(f"Last paper scan failed: {job['error']}")

    st.divider()

    # --- metrics -------------------------------------------------------
    equity = paper_db.get_equity_log()
    stats = _compute_stats(equity)
    open_pos = paper_db.get_open_positions()
    trades = paper_db.get_trades()
    closed = trades[trades["status"] == "closed"] if not trades.empty else trades
    win_rate = (float((closed["realized_pnl"] > 0).mean()) * 100
               if not closed.empty and closed["realized_pnl"].notna().any() else float("nan"))
    realized_pnl = float(closed["realized_pnl"].sum()) if not closed.empty else 0.0

    if stats:
        st.markdown(
            '<div class="ov-grid-metrics">'
            + ov_metric_html("Paper equity", f"₹{stats['current_equity']:,.0f}",
                            f"started at ₹{paper_db.STARTING_CAPITAL:,.0f}", "", "blue")
            + ov_metric_html("Total return", f"{stats['total_ret']*100:+.2f}%", "since inception",
                            "ov-pos" if stats["total_ret"] >= 0 else "ov-neg", "green",
                            "ov-pos" if stats["total_ret"] >= 0 else "ov-neg")
            + ov_metric_html("CAGR", f"{stats['cagr']*100:.2f}%", "annualized", "", "purple")
            + ov_metric_html("Max drawdown", f"{stats['maxdd']*100:.2f}%", "peak to trough", "", "amber")
            + ov_metric_html("Sharpe", f"{stats['sharpe']:.2f}", "daily returns", "", "teal")
            + ov_metric_html("Open positions", f"{len(open_pos)}/{paper_engine.PAPER_CFG['max_positions']}",
                            "slots used", "", "blue")
            + ov_metric_html("Win rate", f"{win_rate:.1f}%" if win_rate == win_rate else "—",
                            f"of {len(closed)} closed", "", "green")
            + ov_metric_html("Realized P&L", f"₹{realized_pnl:+,.0f}", "since inception",
                            "ov-pos" if realized_pnl >= 0 else "ov-neg", "green",
                            "ov-pos" if realized_pnl >= 0 else "ov-neg")
            + '</div>', unsafe_allow_html=True)
    else:
        st.info("No paper equity history yet — run the first scan above.")

    st.divider()

    # --- equity curve ----------------------------------------------
    with st.container(border=True, key="paper-card-chart"):
        st.markdown('<p class="ov-card-title">Paper equity curve</p>', unsafe_allow_html=True)
        if len(equity) > 1:
            plot = equity.copy()
            plot["date"] = pd.to_datetime(plot["date"])
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=plot["date"], y=plot["value"], name="Paper equity (₹)",
                mode="lines+markers", line=dict(color="#16a34a", width=2), marker=dict(size=4),
                hovertemplate="₹%{y:,.0f}<extra>Paper equity</extra>"))
            y_lo, y_hi = float(plot["value"].min()), float(plot["value"].max())
            pad = (y_hi - y_lo) * 0.15 or max(y_hi * 0.02, 100.0)
            fig.update_layout(
                height=300, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified",
                yaxis=dict(tickprefix="₹", separatethousands=True, range=[y_lo - pad, y_hi + pad]))
            st.plotly_chart(fig, use_container_width=True, key="paper_equity_chart")
        else:
            st.caption("Not enough history yet for a chart.")

    st.divider()

    # --- open positions -------------------------------------------
    st.markdown('<p class="ov-card-title">Open paper positions</p>', unsafe_allow_html=True)
    if open_pos:
        rows = []
        for sym, pos in open_pos.items():
            rows.append({
                "symbol": sym, "entry_date": pos["entry_date"], "qty": pos["qty"],
                "entry_price": pos["entry_price"], "current_stop": pos["current_stop"],
                "initial_stop": pos["initial_stop"],
                "stop_distance_pct": (pos["current_stop"] / pos["entry_price"] - 1) * 100,
            })
        df = pd.DataFrame(rows)
        st.markdown(
            ov_table_html(df, sym_cols=["symbol"], pnl_cols=["stop_distance_pct"],
                         num_fmt={"entry_price": "₹{:.2f}", "current_stop": "₹{:.2f}",
                                 "initial_stop": "₹{:.2f}", "stop_distance_pct": "{:+.2f}%"}),
            unsafe_allow_html=True)
    else:
        st.caption("No open positions.")

    st.divider()

    # --- trade history ------------------------------------------------
    st.markdown('<p class="ov-card-title">Paper trade history</p>', unsafe_allow_html=True)
    if not trades.empty:
        display_cols = ["status", "symbol", "entry_date", "entry_price", "qty", "initial_stop",
                        "exit_date", "exit_price", "exit_reason", "realized_pnl",
                        "realized_ret_pct", "holding_days", "entry_reason"]
        display_cols = [c for c in display_cols if c in trades.columns]
        st.markdown(
            ov_table_html(
                trades[display_cols], sym_cols=["symbol"],
                pnl_cols=["realized_pnl", "realized_ret_pct"],
                num_fmt={"entry_price": "₹{:.2f}", "exit_price": "₹{:.2f}",
                        "initial_stop": "₹{:.2f}"},
                badges={"status": {"open": "ov-badge-green", "closed": "ov-badge-gray"}}),
            unsafe_allow_html=True)
        st.download_button("Download paper tradebook CSV", trades.to_csv(index=False),
                           "paper_tradebook.csv")
    else:
        st.caption("No trades yet.")

    st.divider()

    # --- today's watchlist (transparency / debugging) -----------------
    scans = paper_db.get_scans(limit=1)
    with st.expander("Latest scan's watchlist (why did/didn't a stock trigger?)", expanded=False):
        if scans.empty:
            st.caption("No scans recorded yet.")
        else:
            latest = scans.iloc[0]
            st.caption(f"Scan date: {latest['scan_date']}  •  status: {latest['status']}")
            if latest.get("watchlist_json"):
                wl = pd.DataFrame(json.loads(latest["watchlist_json"]))
                if not wl.empty:
                    st.markdown(ov_table_html(wl), unsafe_allow_html=True)
                else:
                    st.caption("Watchlist was empty that day.")
            if latest.get("error_message"):
                st.error(latest["error_message"])

    # --- scan history ---------------------------------------------
    with st.expander("Scan history", expanded=False):
        scans_full = paper_db.get_scans(limit=60)
        if not scans_full.empty:
            cols = ["scan_date", "status", "n_watchlist", "n_entries", "n_exits", "cash", "equity"]
            cols = [c for c in cols if c in scans_full.columns]
            st.markdown(
                ov_table_html(scans_full[cols],
                             num_fmt={"cash": "₹{:,.0f}", "equity": "₹{:,.0f}"},
                             badges={"status": {"ok": "ov-badge-green", "error": "ov-badge-red",
                                                "skipped_no_data": "ov-badge-gray"}}),
                unsafe_allow_html=True)
        else:
            st.caption("No scans recorded yet.")


def _run_scan_job(progress_cb=None) -> str:
    """Adapter for background_jobs.start_background_job, which always
    calls fn(*args, **kwargs, progress_cb=...) -- paper_engine.main()
    doesn't take one, so this just discards it and reports a one-line
    summary for the Job Log."""
    if progress_cb:
        progress_cb("Running paper scan...", 0.1)
    before = len(paper_db.get_trades())
    paper_engine.main([])
    after = len(paper_db.get_trades())
    if progress_cb:
        progress_cb("Done", 1.0)
    return f"Paper scan complete ({after - before} new trade row(s))."
