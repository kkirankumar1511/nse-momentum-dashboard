"""
5-year staged parameter sweep over Backtest-UI strategy knobs.

Loads candles once, runs many config permutations through backtest.run_backtest,
writes cache/sweep_results_5y.csv + cache/sweep_report_5y.md.

Usage:
    python scripts/sweep_backtest.py                  # stages A+B+C
    python scripts/sweep_backtest.py --stages A        # 1D only
    python scripts/sweep_backtest.py --stages A,B      # skip consistency hunt
    python scripts/sweep_backtest.py --resume          # skip configs already in CSV
    python scripts/sweep_backtest.py --report-only     # rebuild report from CSV
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sys
import time
from copy import deepcopy
from datetime import datetime

import numpy as np
import pandas as pd

# Repo root on sys.path when launched as scripts/sweep_backtest.py
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config
import backtest as bt
import sector_universe

YEARS = 5.0
INITIAL_CAPITAL = 1_000_000.0
RESULTS_CSV = os.path.join(ROOT, "cache", "sweep_results_5y.csv")
REPORT_MD = os.path.join(ROOT, "cache", "sweep_report_5y.md")
FUND_CACHE = os.path.join(ROOT, "cache", "fundamentals_history.pkl")
CACHE_DIR = os.path.join(ROOT, "cache")


def _ensure_cache_fresh_today() -> None:
    """Touch candle CSV mtimes so load_candles_cached()'s same-day freshness
    check passes without a live Kite session (master has no offline= flag)."""
    now = time.time()
    n = 0
    if not os.path.isdir(CACHE_DIR):
        return
    for name in os.listdir(CACHE_DIR):
        if not name.endswith(".csv"):
            continue
        path = os.path.join(CACHE_DIR, name)
        try:
            os.utime(path, (now, now))
            n += 1
        except OSError:
            pass
    print(f"  touched {n} cache CSV mtimes for same-day freshness")


def _kite_ok() -> bool:
    try:
        import kite_client
        kite_client.get_kite().profile()
        return True
    except Exception:
        return False


def _load_candles_disk(symbols: list[str], days: int):
    """Disk-only candle load (no Kite). Same trim window as load_candles_cached."""
    import datetime as dt
    cutoff = pd.Timestamp(dt.date.today() - dt.timedelta(days=days))

    def _naive(frame: pd.DataFrame) -> pd.DataFrame:
        if not frame.empty and getattr(frame.index, "tz", None) is not None:
            frame = frame.copy()
            frame.index = frame.index.tz_localize(None)
        return frame

    out = {}
    for sym in symbols:
        path = os.path.join(CACHE_DIR, f"{sym}.csv")
        if not os.path.exists(path):
            out[sym] = pd.DataFrame()
            continue
        df = _naive(pd.read_csv(path, index_col=0, parse_dates=True))
        out[sym] = df[df.index >= cutoff] if not df.empty else df

    bpath = os.path.join(CACHE_DIR, "_NIFTY.csv")
    if os.path.exists(bpath):
        bench = _naive(pd.read_csv(bpath, index_col=0, parse_dates=True))
        bench = bench[bench.index >= cutoff] if not bench.empty else bench
    else:
        bench = pd.DataFrame()
    return out, bench


# Columns we treat as the "config identity" for resume / report
CFG_KEYS = [
    "stage", "config_id",
    "max_positions", "atr_stop_multiple", "trailing_stop_enabled",
    "trailing_atr_multiple", "risk_per_trade_pct", "rebalance_cadence",
    "rsi_min", "rsi_max", "ema_fast", "ema_slow",
    "mom_lookback_days_short", "mom_lookback_days_long", "skip_recent_days",
    "advanced_equal_weight_sizing", "equal_weight_tolerance_pct",
    "capital_equal_weight_sizing",
    "fundamental_gate_enabled", "fundamental_bonus_weight",
    "min_fundamental_score", "near_high_threshold", "sector_bonus_weight",
]


def _baseline_cfg() -> dict:
    return dict(config.STRATEGY)


def _config_id(overrides: dict) -> str:
    payload = json.dumps(overrides, sort_keys=True, default=str)
    return hashlib.md5(payload.encode()).hexdigest()[:10]


def _apply(overrides: dict) -> dict:
    cfg = _baseline_cfg()
    cfg.update(overrides)
    # Trailing multiple only meaningful when trailing is on
    if not cfg.get("trailing_stop_enabled", False):
        cfg["trailing_atr_multiple"] = cfg.get("trailing_atr_multiple", 4.0)
    return cfg


def stage_a_configs() -> list[tuple[str, dict]]:
    """1D sweeps around live defaults."""
    base = _baseline_cfg()
    out: list[tuple[str, dict]] = []

    def add(name: str, ov: dict):
        out.append((f"A:{name}", ov))

    add("baseline", {})

    for v in [5, 8, 10, 12, 15]:
        if v != base["max_positions"]:
            add(f"max_positions={v}", {"max_positions": v})

    for v in [2.0, 2.5, 3.0]:
        if v != base["atr_stop_multiple"]:
            add(f"atr_stop={v}", {"atr_stop_multiple": v})

    add("trailing=off", {"trailing_stop_enabled": False})
    for v in [3.0, 3.5, 4.0, 4.5, 5.0]:
        if not (base["trailing_stop_enabled"] and v == base["trailing_atr_multiple"]):
            add(f"trail={v}", {"trailing_stop_enabled": True, "trailing_atr_multiple": v})

    for v in ["daily", "monthly"]:
        if v != base.get("rebalance_cadence", "daily"):
            add(f"rebalance={v}", {"rebalance_cadence": v})

    for v in [40.0, 45.0, 50.0]:
        if v != base["rsi_min"]:
            add(f"rsi_min={v}", {"rsi_min": v})
    for v in [75.0, 78.0, 80.0, 85.0]:
        if v != base["rsi_max"]:
            add(f"rsi_max={v}", {"rsi_max": v})

    for pair in [(50, 200), (20, 50), (50, 100)]:
        if pair != (base["ema_fast"], base["ema_slow"]):
            add(f"ema={pair[0]}/{pair[1]}", {"ema_fast": pair[0], "ema_slow": pair[1]})

    for pair in [(42, 84), (63, 126), (84, 168)]:
        if pair != (base["mom_lookback_days_short"], base["mom_lookback_days_long"]):
            add(f"mom={pair[0]}/{pair[1]}", {
                "mom_lookback_days_short": pair[0],
                "mom_lookback_days_long": pair[1],
            })

    for v in [0, 5, 10]:
        if v != base["skip_recent_days"]:
            add(f"skip={v}", {"skip_recent_days": v})

    add("equal_weight=off", {
        "advanced_equal_weight_sizing": False,
        "capital_equal_weight_sizing": False,
    })
    for v in [0.10, 0.20, 0.30]:
        if v != base["equal_weight_tolerance_pct"]:
            add(f"ew_tol={v}", {
                "advanced_equal_weight_sizing": True,
                "equal_weight_tolerance_pct": v,
            })

    add("fund_gate=off", {
        "fundamental_gate_enabled": False,
        "fundamental_bonus_weight": 0.0,
    })
    for v in [0.0, 0.5, 1.0]:
        if not (base["fundamental_gate_enabled"] and v == base["fundamental_bonus_weight"]):
            add(f"fund_bonus={v}", {
                "fundamental_gate_enabled": True,
                "fundamental_bonus_weight": v,
            })
    for v in [40.0, 50.0, 60.0]:
        if v != base["min_fundamental_score"]:
            add(f"fund_min={v}", {
                "fundamental_gate_enabled": True,
                "min_fundamental_score": v,
            })

    for v in [0.80, 0.85, 0.90]:
        if v != base["near_high_threshold"]:
            add(f"near_high={v}", {"near_high_threshold": v})

    for v in [0.0, 0.5, 1.0]:
        if v != base["sector_bonus_weight"]:
            add(f"sector={v}", {"sector_bonus_weight": v})

    # risk sizing only matters when advanced EW is off
    for v in [0.25, 0.5, 1.0]:
        add(f"risk={v}_no_ew", {
            "advanced_equal_weight_sizing": False,
            "capital_equal_weight_sizing": False,
            "risk_per_trade_pct": v,
        })

    return out


def stage_b_configs() -> list[tuple[str, dict]]:
    """High-impact factorial around live defaults (lean ~25–40 runs).

    Full trail×cadence×fund×near×rsi×pos is hundreds of runs; at ~15–20 min
    per config that is multi-day. Keep the plan's high-impact axes but
    subsample: core trail×cadence×fund at live near_high, a small near_high
    1D on live core, and rsi_max×max_positions around live defaults.
    """
    out: list[tuple[str, dict]] = []
    trails = [(True, 3.5), (True, 4.0), (True, 4.5), (False, 4.0)]
    cadences = ["daily", "monthly"]
    fund = [(False, 0.0), (True, 0.5)]
    rsi_maxes = [78.0, 80.0, 85.0]
    max_pos = [8, 10, 12]
    base = _baseline_cfg()
    live_near = float(base.get("near_high_threshold", 0.85))

    # Core: trail × cadence × fund @ live near_high / rsi / max_pos
    for (trail_on, trail_m), cadence, (fg, fb) in itertools.product(
            trails, cadences, fund):
        out.append(("B:core", {
            "trailing_stop_enabled": trail_on,
            "trailing_atr_multiple": trail_m,
            "rebalance_cadence": cadence,
            "fundamental_gate_enabled": fg,
            "fundamental_bonus_weight": fb,
            "near_high_threshold": live_near,
            "advanced_equal_weight_sizing": True,
            "equal_weight_tolerance_pct": base["equal_weight_tolerance_pct"],
            "rsi_max": base["rsi_max"],
            "max_positions": base["max_positions"],
        }))

    # near_high slice on live trail/cadence/fund
    for nh in [0.80, 0.85, 0.90]:
        if nh == live_near:
            continue
        out.append(("B:near", {
            "trailing_stop_enabled": True,
            "trailing_atr_multiple": 4.0,
            "rebalance_cadence": base.get("rebalance_cadence", "daily"),
            "fundamental_gate_enabled": True,
            "fundamental_bonus_weight": 0.5,
            "near_high_threshold": nh,
            "advanced_equal_weight_sizing": True,
            "max_positions": base["max_positions"],
            "rsi_max": base["rsi_max"],
        }))

    # rsi_max × max_positions on live trail/cadence/fund/near
    for rm, mp in itertools.product(rsi_maxes, max_pos):
        out.append(("B:rsi_pos", {
            "rsi_max": rm,
            "max_positions": mp,
            "trailing_stop_enabled": True,
            "trailing_atr_multiple": 4.0,
            "rebalance_cadence": base.get("rebalance_cadence", "daily"),
            "fundamental_gate_enabled": True,
            "fundamental_bonus_weight": 0.5,
            "near_high_threshold": live_near,
            "advanced_equal_weight_sizing": True,
        }))

    return out


def stage_c_configs(df: pd.DataFrame) -> list[tuple[str, dict]]:
    """Cross top CAGR / win-rate configs with lookback / skip / fund_min / sector."""
    if df is None or df.empty:
        return []

    tops = []
    for col in ["Win rate %", "CAGR %"]:
        if col not in df.columns:
            continue
        tops.append(df.nlargest(10, col))
    seed = pd.concat(tops).drop_duplicates(subset=["config_id"])

    extras = []
    for mom in [(63, 126), (84, 168)]:
        for skip in [5, 10]:
            for fund_min in [40.0, 50.0]:
                for sector in [0.0, 0.5]:
                    extras.append({
                        "mom_lookback_days_short": mom[0],
                        "mom_lookback_days_long": mom[1],
                        "skip_recent_days": skip,
                        "min_fundamental_score": fund_min,
                        "sector_bonus_weight": sector,
                        "fundamental_gate_enabled": True,
                    })

    out: list[tuple[str, dict]] = []
    # Top 4 seeds × 8 extras ≈ 32 (resume-deduped against A/B)
    seed_rows = seed.head(4)
    for _, row in seed_rows.iterrows():
        base_ov = {}
        for k in CFG_KEYS:
            if k in ("stage", "config_id") or k not in row.index or pd.isna(row[k]):
                continue
            val = row[k]
            if k in ("trailing_stop_enabled", "advanced_equal_weight_sizing",
                     "capital_equal_weight_sizing", "fundamental_gate_enabled"):
                base_ov[k] = bool(val) if isinstance(val, (bool, np.bool_)) else (
                    str(val).strip().lower() in ("1", "true", "yes"))
            elif k in ("max_positions", "ema_fast", "ema_slow",
                       "mom_lookback_days_short", "mom_lookback_days_long",
                       "skip_recent_days"):
                base_ov[k] = int(val)
            elif k in ("atr_stop_multiple", "trailing_atr_multiple",
                       "risk_per_trade_pct", "rsi_min", "rsi_max",
                       "equal_weight_tolerance_pct", "fundamental_bonus_weight",
                       "min_fundamental_score", "near_high_threshold",
                       "sector_bonus_weight"):
                base_ov[k] = float(val)
            else:
                base_ov[k] = val
        for extra in extras[:8]:
            ov = dict(base_ov)
            ov.update(extra)
            if not ov.get("fundamental_gate_enabled", True):
                ov["min_fundamental_score"] = base_ov.get("min_fundamental_score", 50.0)
            out.append(("C:seed", ov))
    return out


def _dedupe(cfgs: list[tuple[str, dict]]) -> list[tuple[str, dict, str]]:
    seen = set()
    out = []
    for stage, ov in cfgs:
        # Normalize floats that are ints
        clean = {}
        for k, v in ov.items():
            if isinstance(v, (np.floating, float)) and float(v).is_integer() and k in (
                    "max_positions", "ema_fast", "ema_slow",
                    "mom_lookback_days_short", "mom_lookback_days_long",
                    "skip_recent_days"):
                clean[k] = int(v)
            else:
                clean[k] = v
        cid = _config_id(clean)
        if cid in seen:
            continue
        seen.add(cid)
        out.append((stage, clean, cid))
    return out


def _yearly_flags(yearly: pd.DataFrame) -> dict:
    if yearly is None or yearly.empty:
        return {
            "years_beaten_nifty": 0,
            "n_years": 0,
            "beats_nifty_every_year": False,
            "min_yearly_alpha": np.nan,
            "years_positive": 0,
            "years_beaten_nifty_full": 0,
            "n_full_years": 0,
            "beats_nifty_every_full_year": False,
            "yearly_json": "{}",
        }

    years = yearly.copy()
    n = len(years)
    beaten = int((years["Strategy %"] > years["NIFTY %"]).sum())
    positive = int((years["Strategy %"] > 0).sum())
    min_alpha = float(years["Alpha %"].min())

    # Full years: exclude current calendar year if it's still running
    this_year = datetime.now().year
    full = years[years.index < this_year] if this_year in years.index else years
    n_full = len(full)
    beaten_full = int((full["Strategy %"] > full["NIFTY %"]).sum()) if n_full else 0

    detail = {
        int(yr): {
            "Strategy %": float(r["Strategy %"]),
            "NIFTY %": float(r["NIFTY %"]),
            "Alpha %": float(r["Alpha %"]),
            "Win rate %": float(r["Win rate %"]) if pd.notna(r["Win rate %"]) else None,
            "Trades": int(r["Trades"]),
        }
        for yr, r in years.iterrows()
    }

    return {
        "years_beaten_nifty": beaten,
        "n_years": n,
        "beats_nifty_every_year": beaten == n and n > 0,
        "min_yearly_alpha": round(min_alpha, 2),
        "years_positive": positive,
        "years_beaten_nifty_full": beaten_full,
        "n_full_years": n_full,
        "beats_nifty_every_full_year": beaten_full == n_full and n_full > 0,
        "yearly_json": json.dumps(detail),
    }


def run_one(cfg: dict, candles, bench, fund_hist, sector_pack) -> dict:
    cadence = cfg.get("rebalance_cadence", "daily")
    rebalance = "D" if cadence == "daily" else "MS"
    need_sector = float(cfg.get("sector_bonus_weight", 0) or 0) > 0
    sector_candles = sector_pack[0] if need_sector and sector_pack else None
    sector_membership = sector_pack[1] if need_sector and sector_pack else None
    use_fund = bool(cfg.get("fundamental_gate_enabled", False)) and fund_hist is not None

    t0 = time.time()
    res = bt.run_backtest(
        candles, bench, cfg,
        initial_capital=INITIAL_CAPITAL,
        rebalance=rebalance,
        fundamentals_history=fund_hist if use_fund else None,
        sector_candles=sector_candles,
        sector_membership=sector_membership,
    )
    elapsed = time.time() - t0
    metrics = dict(res["metrics"])
    yearly = bt.yearly_performance(
        res["equity_curve"], bench.loc[res["equity_curve"].index[0]:],
        res["trades"])
    flags = _yearly_flags(yearly)
    metrics.update(flags)
    metrics["elapsed_sec"] = round(elapsed, 1)
    return metrics


def _row_from_run(stage: str, overrides: dict, cid: str, metrics: dict) -> dict:
    cfg = _apply(overrides)
    row = {
        "stage": stage,
        "config_id": cid,
        "run_time": datetime.now().isoformat(timespec="seconds"),
    }
    for k in CFG_KEYS:
        if k in ("stage", "config_id"):
            continue
        row[k] = cfg.get(k)
    row.update(metrics)
    return row


def load_existing() -> pd.DataFrame:
    frames = []
    if os.path.exists(RESULTS_CSV):
        frames.append(pd.read_csv(RESULTS_CSV))
    # Merge any shard files from parallel workers
    cache = os.path.dirname(RESULTS_CSV)
    if os.path.isdir(cache):
        for name in sorted(os.listdir(cache)):
            if name.startswith("sweep_results_5y_s") and name.endswith(".csv"):
                frames.append(pd.read_csv(os.path.join(cache, name)))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "config_id" in df.columns and "run_time" in df.columns:
        df = df.sort_values("run_time").drop_duplicates("config_id", keep="last")
    elif "config_id" in df.columns:
        df = df.drop_duplicates("config_id", keep="last")
    return df


def append_row(row: dict, results_path: str | None = None):
    path = results_path or RESULTS_CSV
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df = pd.DataFrame([row])
    lock_path = path + ".lock"
    # Simple cross-process lock (Windows-safe enough for low contention)
    for _ in range(200):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            time.sleep(0.05)
    else:
        # Proceed without lock rather than deadlocking forever
        pass
    try:
        header = not os.path.exists(path)
        df.to_csv(path, mode="a", header=header, index=False)
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass


def merge_shard_csvs() -> pd.DataFrame:
    """Collapse shard CSVs into the main results file, then rebuild report."""
    df = load_existing()
    if df.empty:
        return df
    os.makedirs(os.path.dirname(RESULTS_CSV), exist_ok=True)
    df.to_csv(RESULTS_CSV, index=False)
    cache = os.path.dirname(RESULTS_CSV)
    for name in os.listdir(cache):
        if name.startswith("sweep_results_5y_s") and name.endswith(".csv"):
            try:
                os.remove(os.path.join(cache, name))
            except OSError:
                pass
    return df


def write_report(df: pd.DataFrame):
    if df.empty:
        with open(REPORT_MD, "w", encoding="utf-8") as f:
            f.write("# 5-year sweep report\n\nNo results yet.\n")
        return

    d = df.copy()
    # Prefer latest row per config_id
    if "run_time" in d.columns:
        d = d.sort_values("run_time").drop_duplicates("config_id", keep="last")

    def _as_bool(series):
        if series.dtype == bool:
            return series
        return series.map(lambda x: str(x).strip().lower() in ("1", "true", "yes"))

    for bcol in ("trailing_stop_enabled", "advanced_equal_weight_sizing",
                 "capital_equal_weight_sizing", "fundamental_gate_enabled",
                 "beats_nifty_every_year", "beats_nifty_every_full_year"):
        if bcol in d.columns:
            d[bcol] = _as_bool(d[bcol])

    lines = []
    lines.append("# 5-Year Parameter Sweep Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Configs evaluated: **{len(d)}**")
    lines.append(f"Window: trailing **{YEARS:.0f} years** (candles = "
                 f"`int(5*365)+400` days, same as Backtest UI)")
    lines.append("")
    lines.append("## Executive summary")
    lines.append("")

    def _fmt_cfg(r) -> str:
        trail_on = bool(r["trailing_stop_enabled"])
        fund_on = bool(r["fundamental_gate_enabled"])
        ew_on = bool(r["advanced_equal_weight_sizing"])
        parts = [
            f"trail={'ON '+str(r['trailing_atr_multiple'])+'x' if trail_on else 'OFF'}",
            f"rebalance={r['rebalance_cadence']}",
            f"fund={'ON bonus='+str(r['fundamental_bonus_weight']) if fund_on else 'OFF'}",
            f"near_high={r['near_high_threshold']}",
            f"rsi={r['rsi_min']}-{r['rsi_max']}",
            f"max_pos={int(r['max_positions'])}",
            f"ew={'ON' if ew_on else 'OFF'}",
            f"sector={r['sector_bonus_weight']}",
        ]
        return ", ".join(parts)

    def _pick(col, ascending=False):
        s = d.dropna(subset=[col])
        if s.empty:
            return None
        return s.sort_values(col, ascending=ascending).iloc[-1 if not ascending else 0]

    best_wr = _pick("Win rate %")
    best_cagr = _pick("CAGR %")
    all_beat = d[d["beats_nifty_every_full_year"]] if "beats_nifty_every_full_year" in d.columns else d.iloc[0:0]
    if all_beat.empty:
        # Closest: most full years beaten, then min_yearly_alpha
        closest = d.sort_values(
            ["years_beaten_nifty_full", "min_yearly_alpha", "CAGR %"],
            ascending=[False, False, False]).iloc[0]
        beat_blurb = (f"No config beat NIFTY in every full year. Closest: "
                      f"`{closest['config_id']}` "
                      f"({int(closest['years_beaten_nifty_full'])}/"
                      f"{int(closest['n_full_years'])} full years, "
                      f"min yearly alpha {closest['min_yearly_alpha']}%).")
    else:
        closest = all_beat.sort_values("CAGR %", ascending=False).iloc[0]
        beat_blurb = (f"**{len(all_beat)}** config(s) beat NIFTY every full year. "
                      f"Best CAGR among them: `{closest['config_id']}` "
                      f"({closest['CAGR %']}% CAGR).")

    if best_wr is not None:
        lines.append(f"- **Highest win rate:** {best_wr['Win rate %']}% "
                     f"(`{best_wr['config_id']}`) — {_fmt_cfg(best_wr)} "
                     f"| CAGR {best_wr['CAGR %']}% | Sharpe {best_wr['Sharpe']}")
    if best_cagr is not None:
        lines.append(f"- **Highest CAGR:** {best_cagr['CAGR %']}% "
                     f"(`{best_cagr['config_id']}`) — {_fmt_cfg(best_cagr)} "
                     f"| Win rate {best_cagr['Win rate %']}% | "
                     f"years beat NIFTY {int(best_cagr['years_beaten_nifty_full'])}/"
                     f"{int(best_cagr['n_full_years'])}")
    lines.append(f"- **NIFTY consistency:** {beat_blurb}")
    lines.append("")

    # Balanced score
    for col in ["CAGR %", "Win rate %", "years_beaten_nifty_full"]:
        mu, sd = d[col].mean(), d[col].std(ddof=0)
        d[f"z_{col}"] = 0.0 if sd == 0 or pd.isna(sd) else (d[col] - mu) / sd
    d["balanced_score"] = (
        0.40 * d["z_CAGR %"]
        + 0.25 * d["z_Win rate %"]
        + 0.35 * d["z_years_beaten_nifty_full"]
        + d["beats_nifty_every_full_year"].astype(float) * 0.5
    )
    if "Max drawdown %" in d.columns and best_cagr is not None:
        base_dd = d.loc[d["stage"].astype(str).str.startswith("A:baseline") |
                        (d["config_id"] == d.iloc[0]["config_id"]),
                        "Max drawdown %"]
        # milder: penalize DD much worse than median
        med_dd = d["Max drawdown %"].median()
        d["balanced_score"] -= np.where(
            d["Max drawdown %"] < med_dd - 5, 0.3, 0.0)

    best_bal = d.sort_values("balanced_score", ascending=False).iloc[0]
    lines.append(f"- **Balanced pick:** `{best_bal['config_id']}` — {_fmt_cfg(best_bal)}")
    lines.append(f"  CAGR {best_bal['CAGR %']}%, win rate {best_bal['Win rate %']}%, "
                 f"Sharpe {best_bal['Sharpe']}, max DD {best_bal['Max drawdown %']}%, "
                 f"NIFTY years {int(best_bal['years_beaten_nifty_full'])}/"
                 f"{int(best_bal['n_full_years'])}")
    lines.append("")

    def _leaderboard(title, col, n=15):
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| Rank | ID | " + col + " | CAGR % | Win rate % | Sharpe | Max DD % | "
                     "NIFTY years | Config |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        top = d.nlargest(n, col)
        for i, (_, r) in enumerate(top.iterrows(), 1):
            lines.append(
                f"| {i} | `{r['config_id']}` | {r[col]} | {r['CAGR %']} | "
                f"{r['Win rate %']} | {r['Sharpe']} | {r['Max drawdown %']} | "
                f"{int(r['years_beaten_nifty_full'])}/{int(r['n_full_years'])} | "
                f"{_fmt_cfg(r)} |"
            )
        lines.append("")

    _leaderboard("Leaderboard — highest win rate", "Win rate %")
    _leaderboard("Leaderboard — highest CAGR", "CAGR %")

    lines.append("## Leaderboard — beat NIFTY every full year")
    lines.append("")
    if all_beat.empty:
        lines.append("None. Closest configs:")
        lines.append("")
        close = d.nlargest(15, "years_beaten_nifty_full")
        lines.append("| Rank | ID | Years beaten | Min yearly alpha | CAGR % | Win rate % | Config |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, (_, r) in enumerate(close.iterrows(), 1):
            lines.append(
                f"| {i} | `{r['config_id']}` | "
                f"{int(r['years_beaten_nifty_full'])}/{int(r['n_full_years'])} | "
                f"{r['min_yearly_alpha']} | {r['CAGR %']} | {r['Win rate %']} | "
                f"{_fmt_cfg(r)} |"
            )
    else:
        lines.append("| Rank | ID | CAGR % | Win rate % | Sharpe | Max DD % | Config |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, (_, r) in enumerate(all_beat.sort_values("CAGR %", ascending=False).iterrows(), 1):
            lines.append(
                f"| {i} | `{r['config_id']}` | {r['CAGR %']} | {r['Win rate %']} | "
                f"{r['Sharpe']} | {r['Max drawdown %']} | {_fmt_cfg(r)} |"
            )
    lines.append("")

    # Year-by-year for top 5 balanced
    lines.append("## Year-by-year — top 5 balanced picks")
    lines.append("")
    for _, r in d.nlargest(5, "balanced_score").iterrows():
        lines.append(f"### `{r['config_id']}` — {_fmt_cfg(r)}")
        lines.append("")
        try:
            detail = json.loads(r["yearly_json"])
        except Exception:
            detail = {}
        if detail:
            lines.append("| Year | Strategy % | NIFTY % | Alpha % | Win rate % | Trades |")
            lines.append("|---|---|---|---|---|---|")
            for yr in sorted(detail.keys(), key=lambda x: int(x)):
                y = detail[yr]
                wr = y.get("Win rate %")
                wr_s = f"{wr}" if wr is not None else "—"
                lines.append(
                    f"| {yr} | {y['Strategy %']} | {y['NIFTY %']} | {y['Alpha %']} | "
                    f"{wr_s} | {y['Trades']} |"
                )
        lines.append("")

    # 1D sensitivity from stage A
    lines.append("## Stage A — 1D sensitivity vs baseline")
    lines.append("")
    a = d[d["stage"].astype(str).str.startswith("A:")]
    if not a.empty:
        base_rows = a[a["stage"].astype(str).str.contains("baseline")]
        if not base_rows.empty:
            b = base_rows.iloc[0]
            lines.append(f"Baseline `{b['config_id']}`: CAGR {b['CAGR %']}%, "
                         f"win rate {b['Win rate %']}%, "
                         f"NIFTY years {int(b['years_beaten_nifty_full'])}/"
                         f"{int(b['n_full_years'])}, Sharpe {b['Sharpe']}")
            lines.append("")
        lines.append("| Stage label | CAGR % | Win rate % | Sharpe | Max DD % | NIFTY years |")
        lines.append("|---|---|---|---|---|---|")
        for _, r in a.sort_values("CAGR %", ascending=False).iterrows():
            lines.append(
                f"| {r['stage']} | {r['CAGR %']} | {r['Win rate %']} | "
                f"{r['Sharpe']} | {r['Max drawdown %']} | "
                f"{int(r['years_beaten_nifty_full'])}/{int(r['n_full_years'])} |"
            )
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("- Survivorship bias: today's F&O universe applied historically.")
    lines.append("- Default `cost_bps=0` (no statutory costs modeled).")
    lines.append("- Current calendar year may be a partial YTD stub; "
                 "`beats_nifty_every_full_year` excludes it.")
    lines.append("- Relative comparisons across configs are more reliable than "
                 "absolute return levels.")
    lines.append("- Candles loaded from warm disk cache (CSV mtimes touched so "
                 "master's freshness check skips live Kite). "
                 "If Kite was unavailable, `sector_bonus_weight>0` configs "
                 "may have run without sector tilt.")
    lines.append("- Stage B was lean-subsampled (trail×cadence×fund + near "
                 "slice + rsi×max_positions) because full factorial is multi-day "
                 "at ~15–20+ min/config.")
    lines.append("")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Wrote {REPORT_MD}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="A,B,C",
                    help="Comma list of stages to run: A,B,C")
    ap.add_argument("--resume", action="store_true",
                    help="Skip config_ids already present in results CSV")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap total new runs (0 = no cap)")
    ap.add_argument("--shard", default="0/1",
                    help="Zero-based shard i/n of the planned queue (parallel workers)")
    ap.add_argument("--results-path", default="",
                    help="Override CSV path (used by parallel shards)")
    args = ap.parse_args()

    results_path = args.results_path or RESULTS_CSV

    if args.report_only:
        write_report(merge_shard_csvs())
        return

    stages = {s.strip().upper() for s in args.stages.split(",") if s.strip()}
    try:
        shard_i, shard_n = [int(x) for x in args.shard.split("/", 1)]
    except Exception:
        shard_i, shard_n = 0, 1
    if shard_n < 1 or shard_i < 0 or shard_i >= shard_n:
        print(f"ERROR: invalid --shard {args.shard}")
        sys.exit(2)

    print(f"Stages: {sorted(stages)}  shard={shard_i}/{shard_n}")
    print("Loading 5y candles (prefer warm cache)...")
    days = int(YEARS * 365) + 400
    if _kite_ok():
        _ensure_cache_fresh_today()
        try:
            candles, bench = bt.load_candles_cached(config.UNIVERSE, days)
        except TypeError:
            candles, bench = bt.load_candles_cached(config.UNIVERSE, days, offline=True)
    else:
        print("  Kite unavailable — loading candles from disk cache only")
        candles, bench = _load_candles_disk(config.UNIVERSE, days)
    print(f"  symbols with data: {sum(1 for v in candles.values() if not v.empty)} / {len(candles)}")
    print(f"  bench rows: {len(bench)}")
    if bench.empty:
        print("ERROR: empty NIFTY benchmark cache — cannot run sweep")
        sys.exit(1)

    fund_hist = None
    if os.path.exists(FUND_CACHE):
        fund_hist = pd.read_pickle(FUND_CACHE)["history"]
        print(f"  fundamentals history: {len(fund_hist)} symbols")
    else:
        print("  WARNING: no fundamentals_history.pkl — fund-gate configs run without fundamentals")

    sector_pack = None
    print("Loading sector data (optional; needs live Kite for index candles)...")
    if not _kite_ok():
        print("  Kite unavailable; skipping sector candles — "
              "sector_bonus>0 configs skipped (identical to sector=0 without tilt)")
    else:
        try:
            membership = sector_universe.get_sector_membership(verbose=False)
        except Exception as e:
            membership = None
            print(f"  membership load failed ({e})")
        if membership is not None:
            try:
                sector_candles = sector_universe.fetch_sector_index_candles(days=days)
                n_ok = sum(1 for v in sector_candles.values()
                           if v is not None and not getattr(v, "empty", True))
                print(f"  sector indices with data: {n_ok}/{len(sector_candles)}")
                if n_ok > 0:
                    sector_pack = (sector_candles, membership)
            except Exception as e:
                print(f"  sector candle load failed ({e})")
    print(f"  sector_pack active: {sector_pack is not None}")

    existing = load_existing()
    done_ids = set(existing["config_id"].astype(str)) if not existing.empty and "config_id" in existing.columns else set()
    print(f"Already completed: {len(done_ids)}")

    planned: list[tuple[str, dict, str]] = []
    if "A" in stages:
        planned.extend(_dedupe(stage_a_configs()))
    if "B" in stages:
        planned.extend(_dedupe(stage_b_configs()))
    planned = _dedupe([(s, ov) for s, ov, _ in planned])

    # Without sector candles, sector_bonus>0 is a no-op — skip duplicates
    if sector_pack is None:
        before = len(planned)
        planned = [(s, ov, cid) for s, ov, cid in planned
                   if float(ov.get("sector_bonus_weight", 0) or 0) == 0
                   or "sector_bonus_weight" not in ov]
        # Also drop configs whose only override is sector_bonus>0
        planned2 = []
        for s, ov, cid in planned:
            if set(ov.keys()) <= {"sector_bonus_weight"} and float(ov.get("sector_bonus_weight", 0) or 0) > 0:
                continue
            planned2.append((s, ov, cid))
        planned = planned2
        if before != len(planned):
            print(f"  skipped {before - len(planned)} sector_bonus>0 configs (no sector data)")

    # Only shard 0 runs Stage C after A/B (needs pooled results)
    run_c_after = "C" in stages and shard_i == 0

    if args.resume or done_ids:
        before = len(planned)
        planned = [p for p in planned if p[2] not in done_ids]
        if before != len(planned):
            print(f"  skipped {before - len(planned)} already-completed configs")

    if shard_n > 1:
        planned = [p for j, p in enumerate(planned) if j % shard_n == shard_i]
        print(f"  shard keeps {len(planned)} configs")

    if args.limit and args.limit > 0:
        planned = planned[:args.limit]

    print(f"Queued (A/B): {len(planned)}")
    ran = 0
    for i, (stage, ov, cid) in enumerate(planned, 1):
        if cid in done_ids:
            continue
        cfg = _apply(ov)
        print(f"\n[{i}/{len(planned)}] {stage} id={cid}")
        print(f"  overrides: {ov or '(baseline)'}")
        try:
            metrics = run_one(cfg, candles, bench, fund_hist, sector_pack)
        except Exception as e:
            print(f"  FAILED: {e}")
            continue
        row = _row_from_run(stage, ov, cid, metrics)
        append_row(row, results_path)
        done_ids.add(cid)
        ran += 1
        print(f"  CAGR={metrics.get('CAGR %')}% win={metrics.get('Win rate %')}% "
              f"sharpe={metrics.get('Sharpe')} "
              f"nifty_yrs={metrics.get('years_beaten_nifty_full')}/"
              f"{metrics.get('n_full_years')} "
              f"({metrics.get('elapsed_sec')}s)")
        if ran % 2 == 0:
            write_report(load_existing())

    if run_c_after:
        # Wait briefly so sibling shards can flush final A/B rows
        time.sleep(2)
        existing = load_existing()
        c_cfgs = _dedupe(stage_c_configs(existing))
        c_cfgs = [p for p in c_cfgs if p[2] not in done_ids]
        if args.limit and args.limit > 0:
            remain = max(0, args.limit - ran)
            c_cfgs = c_cfgs[:remain]
        print(f"\nQueued (C): {len(c_cfgs)}")
        for i, (stage, ov, cid) in enumerate(c_cfgs, 1):
            if cid in done_ids:
                continue
            cfg = _apply(ov)
            print(f"\n[C {i}/{len(c_cfgs)}] {stage} id={cid}")
            print(f"  overrides: {ov}")
            try:
                metrics = run_one(cfg, candles, bench, fund_hist, sector_pack)
            except Exception as e:
                print(f"  FAILED: {e}")
                continue
            row = _row_from_run(stage, ov, cid, metrics)
            append_row(row, results_path)
            done_ids.add(cid)
            print(f"  CAGR={metrics.get('CAGR %')}% win={metrics.get('Win rate %')}% "
                  f"({metrics.get('elapsed_sec')}s)")

    write_report(load_existing())
    print("\nDone.")


if __name__ == "__main__":
    main()
