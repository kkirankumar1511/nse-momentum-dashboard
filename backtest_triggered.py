"""
Experimental trigger-based entry backtest engine.

Replaces the production engine's (backtest.py) "rank top-N, buy all via
equal weight at the same close" entry with: filter/rank the universe
exactly as today (SAME gates/config, reused unchanged), then wait for a
concrete technical trigger (breakout-and-hold or EMA pullback-bounce, each
with volume confirmation -- see trigger_strategy.py) before entering each
name. Sizing/stops are ATR-risk based instead of pure equal weight (see
trigger_strategy.trigger_position_size).

Universe ranking (rank_universe_asof) and sector-diversification cap
(_apply_sector_cap) are reused UNCHANGED from backtest.py, by import, so
the WATCHLIST is built off the same filter pipeline as production. Exits
are NOT shared with production, by design: this engine only exits via the
daily ATR stop-loss + chandelier trailing-stop mechanism (steps 1/1b below,
same logic as backtest.run_backtest's own, just mirrored here since it
isn't factored into a standalone function) -- production's rebalance-day
200-EMA/rank exit (screener.sell_check) is deliberately NOT run here, since
that check belongs to production's rotation strategy (drop whatever's no
longer top-ranked) and is inconsistent with a strategy where every position
already has its own defined risk via a real stop. compute_metrics is
reused unchanged for the equity-curve stats.

Local-only, experimental: NOT wired into dashboard.py, NOT deployed to the
VPS. See scripts/run_triggered_backtest_local.py for a runnable comparison
against the production engine.

Known simplification vs. the production engine (acceptable for a local
research tool, called out explicitly rather than silently differing): the
market regime filter (cfg["regime_filter_enabled"]) is NOT wired in here --
cfg["max_positions"] is used directly, not an effective_max_positions. If a
caller's cfg has it enabled, it is silently ignored by this engine.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import pandas as pd

import indicators
import resistance_zones
import trigger_indicators as ti
from backtest import (Position, Trade, _apply_sector_cap, compute_metrics,
                      rank_universe_asof)

import trigger_strategy as ts


def _rebalance_dates(dates: pd.DatetimeIndex, rebalance: str) -> set:
    """Identical logic to backtest.run_backtest's inline rb_dates block."""
    if rebalance == "D":
        return set(dates)
    if rebalance == "W":
        weekday_dates = dates[dates.dayofweek < 5]
        iso = weekday_dates.isocalendar()
        return set(pd.Series(weekday_dates).groupby(
            [iso["year"].values, iso["week"].values]).max())
    # "MS": first trading day of each month
    return set(pd.Series(dates).groupby([dates.year, dates.month]).min())


def run_triggered_backtest(candles: dict, bench: pd.DataFrame,
                           cfg: dict | None = None,
                           initial_capital: float = 1_000_000,
                           cost_bps: float = 0.0,
                           rebalance: str = "D",
                           warmup_days: int = 780,
                           verbose: bool = False,
                           fundamentals_history: dict | None = None,
                           sector_candles: dict | None = None,
                           sector_membership: dict | None = None,
                           long_candles: dict | None = None,
                           start_date: dt.date | None = None,
                           progress_cb=None,
                           precomputed_pivots: dict | None = None) -> dict:
    """See module docstring. cfg is layered on top of
    trigger_strategy.TRIGGERED_DEFAULTS (itself a full copy of
    config.STRATEGY plus the new trigger/sizing/stop keys), so passing
    None reproduces TRIGGERED_DEFAULTS exactly.

    warmup_days defaults to 780 (vs. production's 260) so the ~3-year
    multi-year-breakout window (TRIGGERED_DEFAULTS["multiyear_lookback_
    days"] = 756) has enough history before the first tradeable day.
    """
    cfg = {**ts.TRIGGERED_DEFAULTS, **(cfg or {})}
    cost = cost_bps / 10_000
    score_cache: dict = {}

    n_syms = len(candles)
    precomputed: dict = {}
    for i, (sym, df) in enumerate(candles.items()):
        if progress_cb and (i % max(1, n_syms // 20) == 0 or i == n_syms - 1):
            progress_cb(f"Precomputing indicators ({i + 1}/{n_syms})...",
                       (i + 1) / n_syms * 0.1)
        if not df.empty and len(df) >= cfg["ema_slow"]:
            precomputed[sym] = indicators.precompute_daily_series(df, cfg)

    if precomputed_pivots is None and long_candles is not None \
            and cfg.get("resistance_zone_weight", 0.0):
        precomputed_pivots = {}
        window = cfg.get("resistance_zone_pivot_window", 10)
        for sym, df in long_candles.items():
            if not df.empty:
                precomputed_pivots[sym] = resistance_zones.precompute_pivots(df, window=window)

    # One-time precompute of the weekly/monthly confirmation gate's
    # resampled bar history -- identical to backtest.run_backtest's own
    # block. Without this, rank_universe_asof() re-resamples the full
    # long_candles history for every symbol on every single rebalance day,
    # profiled elsewhere as ~45% of a gate-enabled backtest's runtime --
    # with rebalance="D" (the default here) that's O(days x symbols) full
    # 16-year resamples, not O(symbols) once.
    precomputed_weekly_monthly: dict = {}
    if long_candles is not None and cfg.get("weekly_monthly_gate_enabled", False):
        for sym, df in long_candles.items():
            if not df.empty:
                precomputed_weekly_monthly[sym] = indicators.precompute_weekly_monthly_bars(df["close"])

    # One-time precompute of each symbol's FULL-history Heikin-Ashi series
    # -- required (not just an optimization) for the Heikin-Ashi trigger,
    # since HA_open's recursive definition means a truncated window
    # computes a genuinely different value, not an approximation (see
    # trigger_indicators.precompute_heikin_ashi's docstring).
    precomputed_ha: dict = {}
    if cfg.get("heikin_ashi_enabled", False) or cfg.get("ha_ema21_bounce_enabled", False):
        for sym, df in candles.items():
            if not df.empty:
                precomputed_ha[sym] = ti.precompute_heikin_ashi(df)

    dates = bench.index.sort_values()
    dates = dates[warmup_days:]
    if start_date is not None:
        dates = dates[dates >= pd.Timestamp(start_date)]
    rb_dates = _rebalance_dates(dates, rebalance)

    cash = initial_capital
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    curve = []
    trigger_type_counts: dict[str, int] = {}
    # (symbol, entry_date) -> trigger type, so the final trades table can
    # show WHY each position was opened, not just why it closed (Trade's
    # own `reason` field is exit-only, shared with backtest.py's Trade).
    entry_trigger_log: dict[tuple[str, pd.Timestamp], str] = {}
    # symbol -> the ORIGINAL stop set at entry, never modified by the
    # trailing ratchet -- lets close_position() tell whether an exit hit
    # the initial stop untouched or a ratcheted (trailing) stop, without
    # touching backtest.py's shared Position dataclass (which only tracks
    # the CURRENT stop, overwritten as it trails).
    initial_stops: dict[str, float] = {}
    # (symbol, exit_date) -> {"exit_stop_price", "stop_type"}, merged onto
    # the final trades table the same way entry_trigger_log is -- 2026-08-
    # 15, explicit request to see whether each stop-out was the initial
    # stop or a trailed one, and at what price.
    exit_stop_log: dict[tuple[str, pd.Timestamp], dict] = {}
    # symbols that have crossed ha_breakeven_trigger_r and had their stop
    # jumped to breakeven -- 2026-08-16, tracks which positions are in the
    # "wide trail" phase of the breakeven-then-wide-trail stop mechanism
    # (see TRIGGERED_DEFAULTS' ha_breakeven_trail_enabled docstring).
    breakeven_locked: set[str] = set()
    # symbol -> profit-target price, only set for trigger types that
    # compute one (currently just the fast/21 EMA swing-pullback pattern
    # -- see trigger_strategy.detect_trigger's "target" key). Positions
    # without an entry here never get a target-based exit, only stop/
    # trailing-stop, same as before this feature existed.
    position_targets: dict[str, float] = {}
    # Symbols currently held via the institutional pullback trigger
    # (identified by trig["shape"], only that trigger sets it) -- these
    # get the strategy's own extra exit rules (close below EMA21, EMA21
    # crosses below EMA50, bearish engulfing near highs) on top of the
    # shared stop/trailing-stop/target checks; no other trigger type does.
    institutional_positions: set[str] = set()
    # Gate-passers not yet held, refreshed at each rebalance and scanned
    # daily for a trigger (see step 2b) -- unlike the production engine,
    # a slot free here does NOT mean an immediate buy, only eligibility to
    # be checked for a trigger.
    watchlist: dict[str, pd.Series] = {}
    ranked = pd.DataFrame()

    def close_position(sym, price, date, reason):
        nonlocal cash
        pos = positions.pop(sym)
        proceeds = pos.qty * price * (1 - cost)
        cash += proceeds
        trades.append(Trade(sym, pos.entry_date, date, pos.entry_price,
                            price * (1 - cost), pos.qty, reason, sector=pos.sector))
        # pos.stop is the CURRENT stop level at the moment of exit (already
        # ratcheted by step 1b if the trailing stop had moved it) --
        # comparing against the entry's untouched initial_stops[sym] value
        # tells whether this exit's stop level was ever actually trailed,
        # regardless of which check (stop/target) ultimately closed it.
        orig_stop = initial_stops.pop(sym, None)
        breakeven_locked.discard(sym)
        if reason == "stop":
            stop_type = "initial" if orig_stop is not None and abs(pos.stop - orig_stop) < 1e-6 else "trailing"
        else:
            stop_type = "n/a"
        exit_stop_log[(sym, date)] = {"exit_stop_price": round(pos.stop, 4), "stop_type": stop_type}
        position_targets.pop(sym, None)
        institutional_positions.discard(sym)

    def _price_asof(sym: str, date) -> float | None:
        sliced = candles[sym].loc[:date, "close"]
        return float(sliced.iloc[-1]) if not sliced.empty else None

    n_dates = len(dates)
    for i, date in enumerate(dates):
        if progress_cb and (i % max(1, n_dates // 100) == 0 or i == n_dates - 1):
            progress_cb(f"Simulating {date.date()}...", 0.1 + (i + 1) / n_dates * 0.9)

        # 1) stop checks on today's bar -- identical to
        # backtest.run_backtest's step 1.
        for sym in list(positions):
            df = candles[sym]
            if date not in df.index:
                continue
            bar = df.loc[date]
            pos = positions[sym]
            if bar["low"] <= pos.stop:
                fill = min(pos.stop, bar["high"])
                fill = min(fill, bar["open"]) if bar["open"] < pos.stop else fill
                close_position(sym, fill, date, "stop")

        # 1b) trailing stop ratchet -- generic ATR-chandelier trail for
        # most positions (identical to run_backtest's step 1b, with
        # atr_stop_multiple/trailing_atr_multiple overridden to 2.0/2.0
        # by TRIGGERED_DEFAULTS). Institutional positions use their OWN
        # spec's trailing rule instead -- "trail remaining using EMA21 or
        # previous 3-day low" -- candidate new stop is the HIGHER
        # (tighter) of the two, same monotonic-ratchet-up-only convention.
        if cfg.get("trailing_stop_enabled", False):
            for sym, pos in positions.items():
                df = candles[sym]
                if date not in df.index:
                    continue
                pos.highest_close = max(pos.highest_close, float(df.loc[date, "close"]))
                df_upto = df.loc[:date]
                if sym in institutional_positions:
                    ema21_now = float(indicators.ema(
                        df_upto["close"], cfg["institutional_ema21_period"]).iloc[-1])
                    three_day_low = float(df_upto["low"].tail(3).min())
                    new_stop = max(ema21_now, three_day_low)
                elif cfg.get("ha_breakeven_trail_enabled", False) and sym in initial_stops:
                    # Breakeven-then-wide-trail: stop stays FIXED at the
                    # initial ATR stop (no ratchet at all) until price
                    # reaches ha_breakeven_trigger_r * initial risk in
                    # unrealized profit -- then jumps straight to
                    # breakeven and switches to the wider ha_breakeven_
                    # trail_atr_multiple chandelier trail from then on.
                    risk = pos.entry_price - initial_stops[sym]
                    if sym not in breakeven_locked and risk > 0:
                        trigger_price = pos.entry_price + cfg["ha_breakeven_trigger_r"] * risk
                        if pos.highest_close >= trigger_price:
                            breakeven_locked.add(sym)
                            new_stop = pos.entry_price
                        else:
                            new_stop = pos.stop
                    elif sym in breakeven_locked:
                        atr_now = float(indicators.atr(df_upto, cfg["atr_period"]).iloc[-1])
                        new_stop = pos.highest_close - cfg["ha_breakeven_trail_atr_multiple"] * atr_now
                    else:
                        new_stop = pos.stop
                else:
                    atr_now = float(indicators.atr(df_upto, cfg["atr_period"]).iloc[-1])
                    new_stop = pos.highest_close - cfg["trailing_atr_multiple"] * atr_now
                if new_stop > pos.stop:
                    pos.stop = new_stop

        # 1c) profit-target check -- only for positions whose entry
        # trigger computed one (currently the fast/21 EMA swing-pullback
        # pattern's "prior swing high or fixed risk-reward" target).
        # Closes at the target price if today's HIGH reached it. Checked
        # after the stop-check above (a position that stopped out today
        # is already gone from `positions`, so it can't also "hit its
        # target" the same day) -- conservative when both could plausibly
        # have happened intraday, same convention as everywhere else in
        # this engine that resolves same-day ambiguity toward the worse
        # outcome.
        for sym in list(position_targets):
            if sym not in positions:
                continue
            df = candles[sym]
            if date not in df.index:
                continue
            bar = df.loc[date]
            target = position_targets[sym]
            if bar["high"] >= target:
                close_position(sym, target, date, "target")

        # 1d) institutional-strategy-specific exits -- "Exit if any
        # occurs: daily close below EMA21 / bearish engulfing near highs
        # / EMA21 crosses below EMA50" (volume climax not implemented,
        # see trigger_strategy.TRIGGERED_DEFAULTS' comment on why). Only
        # applied to positions opened via that trigger -- checked in this
        # priority order, first match closes at today's close.
        for sym in list(institutional_positions):
            if sym not in positions:
                continue
            df = candles[sym]
            if date not in df.index:
                continue
            df_upto = df.loc[:date]
            close_s, open_s, high_s = df_upto["close"], df_upto["open"], df_upto["high"]
            ema21_now = float(indicators.ema(
                close_s, cfg["institutional_ema21_period"]).iloc[-1])
            today_close = float(close_s.iloc[-1])

            if today_close < ema21_now:
                close_position(sym, today_close, date, "close_below_ema21")
            elif ti.bearish_engulfing_near_highs(open_s, high_s, close_s):
                close_position(sym, today_close, date, "bearish_engulfing")
            elif ti.ema_cross_below(close_s, cfg["institutional_ema21_period"], cfg["ema_fast"]):
                close_position(sym, today_close, date, "ema21_cross_below_ema50")

        # 2) rebalance day: recompute the universe/ranking with the SAME
        # gates/config as production, and refresh the standing watchlist
        # to the top watchlist_size gate-passers not already held.
        #
        # Deliberately does NOT run the 200-EMA/rank-based sell_check exit
        # production uses -- that check belongs to production's rotation
        # strategy (drop whatever's no longer top-ranked), which is a
        # different risk model than this one. Every position here already
        # has its own defined risk (initial + trailing ATR stop, sized via
        # trigger_position_size's max-loss cap), so exiting it early
        # because it slipped in the rank is inconsistent with having given
        # it a real stop in the first place -- confirmed via a real traced
        # trade (ASIANPAINT 2026-07-29/30): it only got a watchlist slot
        # on a modest raw rank, then failed a rank-based keep_zone check
        # the very next day regardless of price action, a near-mechanical
        # 1-day round trip unrelated to the trade's own stop. Positions
        # here exit ONLY via step 1/1b's stop-loss or trailing stop.
        if date in rb_dates:
            ranked = rank_universe_asof(candles, bench, date, cfg,
                                       fundamentals_history, score_cache,
                                       sector_candles, sector_membership,
                                       long_candles, precomputed,
                                       precomputed_pivots, precomputed_weekly_monthly)
            if not ranked.empty:
                candidates = ranked[ranked["all_gates"]]
                watch_syms = [s for s in candidates.index if s not in positions]
                watch_syms = _apply_sector_cap(watch_syms, positions, ranked, cfg)
                watchlist = {sym: candidates.loc[sym]
                            for sym in watch_syms[:cfg["watchlist_size"]]}

        # 2b) daily trigger scan over the standing watchlist -- every day,
        # not just at rebalance. Highest-ranked stock first when multiple
        # names trigger the same day and slots are scarce (same
        # convention as every fill loop in run_backtest).
        if watchlist and len(positions) < cfg["max_positions"]:
            ordered = sorted(watchlist.items(),
                            key=lambda kv: kv[1].get("score", 0), reverse=True)
            for sym, row in ordered:
                if len(positions) >= cfg["max_positions"]:
                    break
                if sym in positions or date not in candles[sym].index:
                    continue
                df_upto = candles[sym].loc[:date]
                sector_df_upto = None
                if sector_candles is not None:
                    sym_sector = row.get("top_sector") if hasattr(row, "get") else None
                    sector_df = sector_candles.get(sym_sector) if sym_sector else None
                    if sector_df is not None and not sector_df.empty:
                        sector_df_upto = sector_df.loc[:date]
                ha_upto = None
                if sym in precomputed_ha:
                    ha_upto = precomputed_ha[sym].loc[:date]
                trig = ts.detect_trigger(df_upto, cfg, sector_df_upto, ha_upto)
                if trig is None:
                    continue

                price = trig["price"]
                if "stop" in trig:
                    # Swing-pullback trigger already computed a swing-
                    # low/EMA-anchored stop -- use it instead of the
                    # generic ATR-based one.
                    initial_stop = trig["stop"]
                else:
                    atr_now = float(indicators.atr(df_upto, cfg["atr_period"]).iloc[-1])
                    initial_stop = price - cfg["atr_stop_multiple"] * atr_now

                equity_now = cash + sum(
                    p.qty * (_price_asof(s, date) or 0.0)
                    for s, p in positions.items())
                qty = ts.trigger_position_size(
                    equity_now, price, initial_stop,
                    cfg["max_positions"], cfg["max_loss_pct_per_trade"])
                qty = min(qty, int(cash / (price * (1 + cost))))
                if qty <= 0:
                    continue

                cash -= qty * price * (1 + cost)
                entry_price = price * (1 + cost)
                sector = row.get("top_sector") if hasattr(row, "get") else None
                positions[sym] = Position(sym, qty, entry_price, initial_stop, date,
                                          highest_close=entry_price, sector=sector)
                initial_stops[sym] = initial_stop
                if "target" in trig:
                    position_targets[sym] = trig["target"]
                if "shape" in trig:
                    institutional_positions.add(sym)
                trigger_type_counts[trig["type"]] = trigger_type_counts.get(trig["type"], 0) + 1
                entry_trigger_log[(sym, date)] = trig["type"]
                watchlist.pop(sym, None)
                if verbose:
                    target_str = f" target {trig['target']:.1f}" if "target" in trig else ""
                    print(f"{date.date()} BUY  {sym:8s} x{qty} @ {price:.1f} "
                         f"stop {initial_stop:.1f}{target_str} trigger={trig['type']}")

        # 3) mark to market
        mtm = cash + sum(
            p.qty * (_price_asof(s, date) or 0.0)
            for s, p in positions.items())
        curve.append((date, mtm))

    last = dates[-1]
    open_positions = []
    for sym, pos in positions.items():
        if candles[sym].loc[:last].empty:
            continue
        last_price = float(candles[sym].loc[:last, "close"].iloc[-1])
        unrealized_pnl = (last_price - pos.entry_price) * pos.qty
        open_positions.append({
            "symbol": sym, "entry_date": pos.entry_date, "entry_price": pos.entry_price,
            "current_price": last_price, "qty": pos.qty, "stop": pos.stop,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_ret_pct": (last_price / pos.entry_price - 1) * 100,
            "holding_days": (last - pos.entry_date).days,
            "sector": pos.sector,
            "entry_reason": entry_trigger_log.get((sym, pos.entry_date), ""),
        })
    open_positions_df = pd.DataFrame(open_positions)

    equity = pd.Series(dict(curve)).sort_index()
    metrics = compute_metrics(equity, trades, bench.loc[equity.index[0]:])
    metrics["Final Capital"] = round(float(equity.iloc[-1]), 2)
    metrics["Open positions"] = len(open_positions_df)
    trades_df = pd.DataFrame([dataclasses.asdict(t) | {
        "pnl": t.pnl, "ret_pct": t.ret_pct, "holding_days": t.holding_days,
        "entry_reason": entry_trigger_log.get((t.symbol, t.entry_date), ""),
        **exit_stop_log.get((t.symbol, t.exit_date), {"exit_stop_price": None, "stop_type": None}),
    } for t in trades])
    return {
        "equity_curve": equity,
        "trades": trades_df,
        "open_positions": open_positions_df,
        "final_capital": float(equity.iloc[-1]),
        "metrics": metrics,
        "trigger_type_counts": trigger_type_counts,
    }
