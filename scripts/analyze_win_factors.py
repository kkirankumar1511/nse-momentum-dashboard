"""
Ad-hoc, read-only analysis: for every trade in a saved EMA21-touch
backtest run, reconstruct the signal candle (HA RSI, volume vs 50/100-day
SMA, run length, confirmation lag, day-of-week, month, sector, holding
days) and cross-tabulate each factor against win/loss. Not a strategy
change -- pure post-hoc analysis to find what characterizes winning vs
losing trades.

Run with: python scripts/analyze_win_factors.py <trades.csv> [--real-green] [--reversal-wick]
(pass the flags matching whichever ha_ema21_touch_require_real_green/
ha_ema21_touch_allow_reversal_wick_shapes settings actually produced
<trades.csv>, or signal-date reconstruction silently diverges from the
real run -- see the reconstruction call's own comment below.)
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime as dt

import pandas as pd

import config
import indicators
import trigger_indicators as ti
from backtest import LONG_CACHE_DIR, _tz_naive, load_long_history_cached

CFG_RSI_PERIOD = 14
CFG_EMA13 = 13
CFG_EMA21 = 21
RECON_REQUIRE_REAL_GREEN = False
RECON_ALLOW_REVERSAL_WICK_SHAPES = False


def find_signal_date(ha: pd.DataFrame, entry_date: pd.Timestamp, signal_high: float,
                     lookback: int = 15) -> pd.Timestamp | None:
    """The signal candle is the LAST qualifying candle before/at entry whose
    HA high matches the stored signal_high (state machine locks this in at
    commit time -- see precompute_ema21_touch_signals)."""
    idx = ha.index
    if entry_date not in idx:
        return None
    pos = idx.get_loc(entry_date)
    window = idx[max(0, pos - lookback):pos + 1]
    matches = [d for d in window if abs(float(ha.loc[d, "ha_high"]) - signal_high) < 0.01]
    return matches[-1] if matches else None


def main():
    global RECON_REQUIRE_REAL_GREEN, RECON_ALLOW_REVERSAL_WICK_SHAPES
    ap = argparse.ArgumentParser()
    ap.add_argument("trades_path")
    ap.add_argument("--real-green", action="store_true",
                    help="set if the trades file was produced with "
                        "ha_ema21_touch_require_real_green=True (default since 2026-08-24)")
    ap.add_argument("--reversal-wick", action="store_true",
                    help="set if the trades file was produced with "
                        "ha_ema21_touch_allow_reversal_wick_shapes=True")
    args = ap.parse_args()
    RECON_REQUIRE_REAL_GREEN = args.real_green
    RECON_ALLOW_REVERSAL_WICK_SHAPES = args.reversal_wick

    trades_path = args.trades_path
    trades = pd.read_csv(trades_path, parse_dates=["entry_date", "exit_date"])
    trades["win"] = trades["pnl"] > 0

    bench = _tz_naive(pd.read_csv(os.path.join(LONG_CACHE_DIR, "_NIFTY.csv"),
                                  index_col=0, parse_dates=True))
    cache_end_date = bench.index.max().date()
    symbols = sorted(trades["symbol"].unique())
    long_candles = load_long_history_cached(symbols, end_date=cache_end_date)

    rows = []
    for _, t in trades.iterrows():
        sym = t["symbol"]
        if sym not in long_candles or long_candles[sym].empty:
            continue
        df = long_candles[sym]
        ha = ti.precompute_heikin_ashi(df)
        # Need signal_high -- recompute JUST the touch series for this
        # symbol with default (unfiltered) settings to locate signal_high/
        # signal_low at entry_date (watchlist/prior-RSI/volume gating
        # doesn't change WHICH candle is the signal, only whether it's
        # allowed to commit -- BUT signal_close_above_ema13 DOES change
        # the shape condition itself, so it must match the actual run or
        # the state machine's own run-tracking diverges, producing a
        # different signal_high that then fails to match any real candle
        # in find_signal_date -- a real bug found via low reconstruction
        # rates (63-94%) on files that actually used close-above-EMA21.
        # False here matches every current best-validated 5yr run.
        # 2026-08-24: must match the ACTUAL run's config or the state
        # machine's own run-tracking diverges, producing a different
        # signal_high that fails to match a real candle in
        # find_signal_date -- same bug class fixed earlier for
        # signal_close_above_ema13 (63-94% -> 85% reconstruction).
        # require_real_green defaults True in TRIGGERED_DEFAULTS (baked
        # in 2026-08-24) even though the bare function default is False,
        # so it must be passed explicitly here to match any run made
        # after that change.
        touch = ti.precompute_ema21_touch_signals(
            df, ha, signal_close_above_ema13=False, require_real_green=RECON_REQUIRE_REAL_GREEN,
            allow_reversal_wick_shapes=RECON_ALLOW_REVERSAL_WICK_SHAPES)
        if t["entry_date"] not in touch.index:
            continue
        row = touch.loc[t["entry_date"]]
        sig_high, sig_low = row["signal_high"], row["signal_low"]
        if pd.isna(sig_high):
            continue
        sig_date = find_signal_date(ha, t["entry_date"], sig_high)
        if sig_date is None:
            continue

        ema13 = indicators.ema(ha["ha_close"], CFG_EMA13)
        ema21 = indicators.ema(ha["ha_close"], CFG_EMA21)
        # Real-price (not HA) EMA50/EMA200 -- matches how the rest of the
        # codebase treats these two periods (FILTER_CFG_OVERRIDE's
        # ema_fast/ema_slow, sector_above_ema, trend_template_ok), unlike
        # the HA-based EMA13/21 the state machine itself runs on. The
        # point is to test a SLOWER, structural-trend slope, distinct
        # from EMA21's own (noisy, short-horizon) slope tested above.
        ema50_real = indicators.ema(df["close"], 50)
        ema200_real = indicators.ema(df["close"], 200)
        # HA-based EMA50/EMA200, per explicit request -- matches the
        # "HA EMA13 > HA EMA21 > HA EMA50 > HA EMA200, all computed on
        # HA_close" stacked-trend condition that heikin_ashi_trend_entry
        # and heikin_ashi_ema21_bounce_entry both require (trigger_
        # indicators.py lines 76/200) but precompute_ema21_touch_signals
        # (this pattern) never inherited -- testing whether it should.
        ema50_ha = indicators.ema(ha["ha_close"], 50)
        ema200_ha = indicators.ema(ha["ha_close"], 200)
        rsi = indicators.rsi(ha["ha_close"], CFG_RSI_PERIOD)
        i = ha.index.get_loc(sig_date)
        sig_rsi = rsi.iloc[i]
        vol_sma50 = df["volume"].rolling(50).mean().iloc[i]
        vol_sma100 = df["volume"].rolling(100).mean().iloc[i]
        vol_today = float(df["volume"].iloc[i])
        prior_rsi_max = rsi.iloc[max(0, i - 10):i].max()

        # Run length: count consecutive prior HA candles (ending at sig_date)
        # that also independently qualify as signal candles. Matches the
        # CURRENT shape (any color, close above EMA21) -- was hardcoded to
        # the old red-only/close-above-EMA13 shape, same class of bug as
        # the signal_close_above_ema13 fix above.
        run_len = 1
        j = i - 1
        while j >= 0:
            e13, e21, r = ema13.iloc[j], ema21.iloc[j], rsi.iloc[j]
            if pd.isna(e13) or pd.isna(e21) or pd.isna(r):
                break
            hc, ho, hh, hl = (ha["ha_close"].iloc[j], ha["ha_open"].iloc[j],
                             ha["ha_high"].iloc[j], ha["ha_low"].iloc[j])
            if hl <= e21 and hh > e13 and hc > e21 and r > 50.0:
                run_len += 1
                j -= 1
            else:
                break

        # Real (non-HA) candle color on the signal day -- the strategy no
        # longer requires red, but check whether it correlates with
        # outcome anyway.
        real_open, real_close = float(df["open"].iloc[i]), float(df["close"].iloc[i])
        real_candle_color = "green" if real_close > real_open else (
            "red" if real_close < real_open else "doji")

        # 2026-08-24 addition, per explicit request: classify the signal
        # candle's REAL shape (hammer / dragonfly-doji / plain), not just
        # its color -- a red candle with a long lower wick (rejection of
        # lower prices) is the textbook reversal-at-support pattern this
        # strategy is built around (pullback to EMA21), and may carry a
        # real edge that the plain green/red split misses or dilutes.
        real_high, real_low = float(df["high"].iloc[i]), float(df["low"].iloc[i])
        body = abs(real_close - real_open)
        upper_wick = real_high - max(real_open, real_close)
        lower_wick = min(real_open, real_close) - real_low
        candle_range = real_high - real_low
        if candle_range <= 0:
            real_candle_shape = "flat"
        elif body <= 0.1 * candle_range and lower_wick / candle_range >= 0.6 \
                and upper_wick / candle_range <= 0.15:
            real_candle_shape = "dragonfly_doji"
        elif body > 0 and lower_wick >= 2 * body and upper_wick <= body:
            real_candle_shape = "hammer"
        else:
            real_candle_shape = "plain"
        # Combine shape with color -- the user's specific hypothesis is
        # about RED hammers/dragonflies specifically (a rejection candle
        # that still closed red), as distinct from green ones.
        real_shape_color = f"{real_candle_shape}_{real_candle_color}" \
            if real_candle_shape in ("hammer", "dragonfly_doji") else real_candle_shape

        # 2026-08-23 addition: HA EMA21's own slope over the prior 10
        # trading days, as a % change -- checks whether the LINE is rising
        # vs flat/rolling-over at the signal candle (distinct from the
        # existing shape/RSI checks, which only look at price relative to
        # the EMA, not the EMA's own trajectory). Not yet a strategy gate
        # -- pure post-hoc analysis to see if it's worth adding one.
        ema21_lookback = 10
        j10 = i - ema21_lookback
        ema21_slope_pct = (
            (ema21.iloc[i] / ema21.iloc[j10] - 1) * 100
            if j10 >= 0 and not pd.isna(ema21.iloc[j10]) and ema21.iloc[j10] != 0
            else None)
        # Same slope idea, but on the slower structural EMAs, each over
        # ITS OWN natural lookback rather than reusing EMA21's 10 days --
        # per explicit user request ("same formula will not help for EMA
        # 50 and EMA 200"): a 50-period EMA needs longer than 10 days to
        # show a meaningful move, so EMA50 uses 20 days and EMA200 uses
        # 50 days.
        j20, j50 = i - 20, i - 50
        ema50_slope_pct = (
            (ema50_real.iloc[i] / ema50_real.iloc[j20] - 1) * 100
            if j20 >= 0 and not pd.isna(ema50_real.iloc[j20]) and ema50_real.iloc[j20] != 0
            else None)
        ema200_slope_pct = (
            (ema200_real.iloc[i] / ema200_real.iloc[j50] - 1) * 100
            if j50 >= 0 and not pd.isna(ema200_real.iloc[j50]) and ema200_real.iloc[j50] != 0
            else None)

        # Re-derive the strategy's own prior_above_ema13_ok gate (default
        # lookback=5) to sanity-check that every reconstructed trade
        # actually satisfies it, matching trigger_indicators.
        # precompute_ema21_touch_signals' exact formula: shift(1) excludes
        # the signal candle itself, rolling(5).sum() over the shifted
        # 0/1 series is >0 iff at least one of the prior 5 candles had
        # BOTH its HA open and close above HA EMA13.
        above_ema13_series = ((ha["ha_open"] > ema13) & (ha["ha_close"] > ema13)).astype(int)
        prior_above_ema13_check = bool(
            above_ema13_series.shift(1).rolling(5).sum().iloc[i] > 0)

        e13_i, e21_i = ema13.iloc[i], ema21.iloc[i]
        e50ha_i, e200ha_i = ema50_ha.iloc[i], ema200_ha.iloc[i]
        if any(pd.isna(v) for v in (e13_i, e21_i, e50ha_i, e200ha_i)):
            ha_stack_ok, ha_stack_count = None, None
        else:
            checks = [e13_i > e21_i, e21_i > e50ha_i, e50ha_i > e200ha_i]
            ha_stack_ok = all(checks)
            ha_stack_count = sum(checks)

        confirm_lag = (t["entry_date"] - sig_date).days
        sig_adx = ti.adx(df["high"].iloc[:i + 1], df["low"].iloc[:i + 1],
                        df["close"].iloc[:i + 1], period=14)

        rows.append({
            "signal_adx": round(sig_adx, 2),
            "symbol": sym, "entry_date": t["entry_date"], "win": t["win"],
            "ret_pct": t["ret_pct"], "holding_days": t["holding_days"],
            "sector": t["sector"], "exit_reason": t["reason"],
            "signal_ha_rsi": round(float(sig_rsi), 2) if not pd.isna(sig_rsi) else None,
            "prior_10d_max_rsi": round(float(prior_rsi_max), 2) if not pd.isna(prior_rsi_max) else None,
            "vol_above_50sma": (not pd.isna(vol_sma50)) and vol_today > vol_sma50,
            "vol_above_100sma": (not pd.isna(vol_sma100)) and vol_today > vol_sma100,
            "run_length": run_len,
            "real_candle_color": real_candle_color,
            "real_candle_shape": real_candle_shape,
            "real_shape_color": real_shape_color,
            "sig_date": sig_date,
            "real_open": round(real_open, 2), "real_high": round(real_high, 2),
            "real_low": round(real_low, 2), "real_close": round(real_close, 2),
            "body": round(body, 2), "upper_wick": round(upper_wick, 2),
            "lower_wick": round(lower_wick, 2),
            "ha_stack_ok": ha_stack_ok,
            "ha_stack_count": ha_stack_count,
            "prior_above_ema13_check": prior_above_ema13_check,
            "ema13": round(float(e13_i), 2) if not pd.isna(e13_i) else None,
            "ema21": round(float(e21_i), 2) if not pd.isna(e21_i) else None,
            "ema50_ha": round(float(e50ha_i), 2) if not pd.isna(e50ha_i) else None,
            "ema200_ha": round(float(e200ha_i), 2) if not pd.isna(e200ha_i) else None,
            "ema21_slope_pct": round(float(ema21_slope_pct), 3) if ema21_slope_pct is not None else None,
            "ema50_slope_pct": round(float(ema50_slope_pct), 3) if ema50_slope_pct is not None else None,
            "ema200_slope_pct": round(float(ema200_slope_pct), 3) if ema200_slope_pct is not None else None,
            "confirm_lag_days": confirm_lag,
            "entry_dow": t["entry_date"].day_name(),
            "entry_month": t["entry_date"].month,
        })

    feat = pd.DataFrame(rows)
    print(f"Reconstructed features for {len(feat)} of {len(trades)} trades "
         f"({len(feat)/len(trades)*100:.0f}%).\n")

    print("--- Per-symbol signal-candle HA RSI + volume vs 50/100-SMA, sorted by RSI ---")
    raw = feat[["symbol", "entry_date", "win", "signal_ha_rsi",
               "vol_above_50sma", "vol_above_100sma", "ret_pct"]].copy()
    raw = raw.sort_values("signal_ha_rsi")
    with pd.option_context("display.max_rows", None, "display.width", 200):
        print(raw.to_string(index=False))
    print()

    def bucket_report(col, bins=None, labels=None):
        s = feat[col]
        if bins is not None:
            grp = pd.cut(s, bins=bins, labels=labels)
        else:
            grp = s
        tbl = feat.groupby(grp, observed=True).agg(
            n=("win", "size"), win_rate=("win", "mean"), avg_ret=("ret_pct", "mean"))
        tbl["win_rate"] = (tbl["win_rate"] * 100).round(1)
        tbl["avg_ret"] = tbl["avg_ret"].round(2)
        print(f"--- {col} ---")
        print(tbl)
        print()

    print(f"Overall win rate: {feat['win'].mean()*100:.1f}% ({feat['win'].sum()}/{len(feat)}), "
         f"avg ret {feat['ret_pct'].mean():.2f}%\n")

    bucket_report("signal_adx", bins=[0, 15, 20, 25, 30, 40, 100],
                  labels=["<15", "15-20", "20-25", "25-30", "30-40", ">40"])
    n_win_below25 = ((feat["win"]) & (feat["signal_adx"] < 25)).sum()
    n_win_total = feat["win"].sum()
    n_loss_above25 = ((~feat["win"]) & (feat["signal_adx"] >= 25)).sum()
    n_loss_total = (~feat["win"]).sum()
    print(f"Strict hypothesis check (winners ADX>=25, losers ADX<25):")
    print(f"  Winners with ADX < 25 (violates hypothesis): {n_win_below25}/{n_win_total} "
         f"({n_win_below25/n_win_total*100:.1f}%)")
    print(f"  Losers with ADX >= 25 (violates hypothesis): {n_loss_above25}/{n_loss_total} "
         f"({n_loss_above25/n_loss_total*100:.1f}%)")
    print(f"  Mean ADX -- winners: {feat.loc[feat['win'], 'signal_adx'].mean():.2f}, "
         f"losers: {feat.loc[~feat['win'], 'signal_adx'].mean():.2f}\n")

    bucket_report("signal_ha_rsi", bins=[0, 50, 55, 60, 65, 100],
                  labels=["<=50", "50-55", "55-60", "60-65", ">65"])
    bucket_report("prior_10d_max_rsi", bins=[0, 50, 60, 70, 100],
                  labels=["<=50", "50-60", "60-70", ">70"])
    print("--- signal_ha_rsi / prior_10d_max_rsi: winners vs losers (mean/median) ---")
    for col in ("signal_ha_rsi", "prior_10d_max_rsi"):
        w = feat.loc[feat["win"], col]
        l = feat.loc[~feat["win"], col]
        print(f"  {col}: winners mean={w.mean():.2f} median={w.median():.2f} (n={len(w)})  |  "
             f"losers mean={l.mean():.2f} median={l.median():.2f} (n={len(l)})")
    print()

    th = 58.0
    below = feat[feat["signal_ha_rsi"] < th]
    above = feat[feat["signal_ha_rsi"] >= th]
    n_loss_below = (~below["win"]).sum()
    n_loss_above = (~above["win"]).sum()
    n_loss_total = (~feat["win"]).sum()
    print(f"--- signal_ha_rsi < {th} breakdown ---")
    print(f"  Trades with RSI < {th}: n={len(below)}, {(~below['win']).sum()} losers "
         f"({n_loss_below/n_loss_total*100:.1f}% of all {n_loss_total} losers), "
         f"win_rate={below['win'].mean()*100:.1f}%, avg_ret={below['ret_pct'].mean():.2f}%")
    print(f"  Trades with RSI >= {th}: n={len(above)}, {(~above['win']).sum()} losers "
         f"({n_loss_above/n_loss_total*100:.1f}% of all {n_loss_total} losers), "
         f"win_rate={above['win'].mean()*100:.1f}%, avg_ret={above['ret_pct'].mean():.2f}%")
    print()
    bucket_report("vol_above_50sma")
    bucket_report("vol_above_100sma")
    bucket_report("run_length", bins=[0, 1, 2, 3, 20], labels=["1", "2", "3", "4+"])
    bucket_report("real_candle_color")
    bucket_report("real_candle_shape")
    bucket_report("real_shape_color")
    bucket_report("ha_stack_ok")
    bucket_report("ha_stack_count")

    n_pass = feat["prior_above_ema13_check"].sum()
    n_total = len(feat)
    print(f"--- prior_above_ema13_check (1 of prior 5 candles open&close > EMA13) ---")
    print(f"  Pass: {n_pass}/{n_total} ({n_pass/n_total*100:.1f}%)")
    fail = feat[~feat["prior_above_ema13_check"]]
    if len(fail):
        print(f"  FAILING trades (should be 0 if the gate was active in this run's config):")
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(fail[["symbol", "sig_date", "win", "ret_pct"]].to_string(index=False))
    print()

    stack_fail = feat[feat["ha_stack_ok"] == False]  # noqa: E712
    if len(stack_fail):
        print(f"--- ha_stack_ok == False example trades ({len(stack_fail)} total) ---")
        cols = ["symbol", "sig_date", "win", "ret_pct", "ema13", "ema21",
               "ema50_ha", "ema200_ha"]
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(stack_fail[cols].to_string(index=False))
        print()

    hammer_red = feat[feat["real_shape_color"] == "hammer_red"]
    if len(hammer_red):
        print(f"--- hammer_red example trades ({len(hammer_red)} total) ---")
        cols = ["symbol", "sig_date", "win", "ret_pct", "real_open", "real_high",
               "real_low", "real_close", "body", "upper_wick", "lower_wick"]
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(hammer_red[cols].to_string(index=False))
        print()
    bucket_report("ema21_slope_pct", bins=[-100, 0, 2, 5, 100],
                  labels=["falling(<=0%)", "flat(0-2%)", "rising(2-5%)", "strong(>5%)"])

    def bucket_report_qcut(col, q=4):
        # Slower EMAs move far less per 10 days than EMA21 in % terms, so
        # fixed thresholds tuned for EMA21 don't transfer -- quartile
        # bucketing lets each EMA's own distribution set its bin edges.
        s = feat[col].dropna()
        grp = pd.qcut(s, q=q, duplicates="drop")
        tbl = feat.loc[s.index].groupby(grp, observed=True).agg(
            n=("win", "size"), win_rate=("win", "mean"), avg_ret=("ret_pct", "mean"))
        tbl["win_rate"] = (tbl["win_rate"] * 100).round(1)
        tbl["avg_ret"] = tbl["avg_ret"].round(2)
        print(f"--- {col} (quartile-binned) ---")
        print(tbl)
        print()

    bucket_report_qcut("ema50_slope_pct")
    bucket_report_qcut("ema200_slope_pct")

    def threshold_scan(col, thresholds):
        # "How much slope %% is valid to take the trade" -- for each
        # candidate cutoff, splits into >= threshold vs < threshold and
        # reports both sides, so a real breakpoint (if one exists) shows
        # up as a jump in win_rate/avg_ret between the two rows.
        print(f"--- {col}: threshold scan ---")
        for th in thresholds:
            above = feat[feat[col] >= th]
            below = feat[feat[col] < th]
            if len(above) == 0 or len(below) == 0:
                continue
            print(f"  >= {th:>5.1f}%: n={len(above):3d} win_rate={above['win'].mean()*100:5.1f}% "
                 f"avg_ret={above['ret_pct'].mean():6.2f}%   |  "
                 f"< {th:>5.1f}%: n={len(below):3d} win_rate={below['win'].mean()*100:5.1f}% "
                 f"avg_ret={below['ret_pct'].mean():6.2f}%")
        print()

    threshold_scan("ema50_slope_pct", [0, 1, 2, 3, 4, 5, 6])
    threshold_scan("ema200_slope_pct", [0, 1, 2, 3, 4, 5])
    bucket_report("confirm_lag_days", bins=[-1, 0, 2, 5, 10, 20],
                  labels=["0d(sameday)", "1-2d", "3-5d", "6-10d", ">10d"])
    bucket_report("holding_days", bins=[0, 1, 3, 7, 14, 100],
                  labels=["1d", "2-3d", "4-7d", "8-14d", ">14d"])
    bucket_report("exit_reason")
    bucket_report("entry_dow")
    bucket_report("sector")


if __name__ == "__main__":
    main()
