"""
Final shortlist: the last stage of the funnel, combining technical momentum
with fundamentals to pick a robust, diversified 10-15 name shortlist.

Pipeline:
  210 F&O stocks
    -> technical gates (screener.run_screen)         -> ~15-25 pass all_gates
    -> ranked by fundamental score (fno_value_scan)   -> top 20 candidates
    -> composite score: technical + fundamental       -> percentile-ranked
       + earnings consistency + promoter buying          and blended
    -> sector-diversification cap                     -> final 10-15
    -> AI deep-read (filing_analyst.analyze_many)      -> ENRICHMENT ONLY

The AI deep-read does NOT gate or rank the final selection — it used to,
but that made the whole shortlist hostage to whatever Groq's daily free-tier
quota happened to be doing that hour (verified: a real run where every
single stock showed WATCH purely because the LLM was rate-limited, despite
some scoring 90-100 on fundamentals). The composite score is a fully
deterministic, LLM-independent decision; the AI's verdict/summary/catalysts/
risks are displayed alongside the final picks as bonus color when available,
never as a requirement.

Technical gates ARE still a hard filter (not blended into the composite):
this is a 3-6 month momentum system, so a fundamentally excellent stock with
no current uptrend isn't tradeable here regardless of quality — see
config.STRATEGY / screener.apply_gates for what "passing gates" means.

Note on the two fundamental scores in play: this module's ranking uses
xbrl_parser.value_score() (via fundamentals_agent.fno_value_scan) — the
sector-aware, primary-XBRL rubric. filing_analyst's own deterministic_verdict()
produces a SEPARATE score (surfaced here as 'ai_fund_score') from a different,
older methodology. Both are shown rather than silently picking one.
"""

from __future__ import annotations

import pandas as pd

import filing_analyst as fan
import nse_api
import xbrl_parser

DEFAULT_WEIGHTS = {
    "technical": 0.30,
    "fundamental": 0.40,
    "earnings_consistency": 0.20,
    "promoter": 0.10,
}

_PROMOTER_TREND_SCORE = {"increasing": 100.0, "stable": 50.0, "decreasing": 0.0}


def select_top20(tech: pd.DataFrame, fund: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Stage 1+2: technical-gate-passers, ranked by fundamental score.

    tech: screener.run_screen() output (indexed by symbol; needs 'all_gates', 'score').
    fund: fundamentals_agent.fno_value_scan() output (indexed by symbol; needs
          'total_score', 'rubric').
    """
    gate_passers = tech.index[tech["all_gates"].astype(bool)]
    combined = fund.loc[fund.index.intersection(gate_passers)].copy()
    combined = combined.join(
        tech[["score", "ly_breakout", "rs_6m", "pct_52w_high"]],
        how="left")
    combined = combined.rename(columns={
        "score": "technical_score", "total_score": "fundamental_score"})
    return combined.sort_values("fundamental_score", ascending=False).head(n)


def compute_composite_scores(candidates: pd.DataFrame,
                             weights: dict | None = None,
                             progress_cb=None) -> pd.DataFrame:
    """Blend technical + fundamental + earnings-consistency + promoter-buying
    into one composite score (0-100). Technical/fundamental are percentile-
    ranked WITHIN this candidate set first (they're on different scales —
    z-score composite vs. 0-100 rubric); earnings consistency and promoter
    score are already 0-100. A factor missing for a given stock is dropped
    from ITS blend (re-normalizing the remaining weights), not defaulted —
    same "don't fake missing data" policy as the fundamental rubrics
    (xbrl_parser.value_score et al).
    """
    weights = weights or DEFAULT_WEIGHTS
    df = candidates.copy()

    consistency, accel, promoter_score = {}, {}, {}
    for i, sym in enumerate(df.index):
        if progress_cb:
            progress_cb(f"{sym}: earnings consistency + promoter trend...",
                        i / max(len(df), 1))
        try:
            qdf = xbrl_parser.quarterly_financials(sym, max_quarters=12)
            eq = xbrl_parser.earnings_quality(qdf)
            consistency[sym] = eq.get("yoy_win_rate_pct")
            accel[sym] = eq.get("pat_growth_accel")
        except Exception:
            pass
        try:
            pr = nse_api.promoter_trend(sym)
            promoter_score[sym] = _PROMOTER_TREND_SCORE.get(pr.get("promoter_trend"))
        except Exception:
            pass

    df["earnings_consistency_pct"] = pd.Series(consistency)
    df["pat_growth_accel"] = pd.Series(accel)
    df["promoter_score"] = pd.Series(promoter_score)

    def pct_rank(s: pd.Series) -> pd.Series:
        return s.rank(pct=True, na_option="keep") * 100

    rank_cols = {
        "technical": pct_rank(df["technical_score"]),
        "fundamental": pct_rank(df["fundamental_score"]),
        "earnings_consistency": df["earnings_consistency_pct"],  # already 0-100
        "promoter": df["promoter_score"],  # already 0-100
    }

    def blend(idx) -> float | None:
        total_w, total = 0.0, 0.0
        for factor, series in rank_cols.items():
            v = series.get(idx)
            if pd.notna(v):
                total += weights[factor] * v
                total_w += weights[factor]
        return round(total / total_w, 1) if total_w > 0 else None

    df["composite_score"] = [blend(idx) for idx in df.index]
    return df.sort_values("composite_score", ascending=False)


def apply_sector_cap(df: pd.DataFrame, max_per_sector: int = 4,
                     n: int = 15, sector_col: str = "rubric") -> pd.DataFrame:
    """Greedy pick by composite_score descending, skipping a candidate once
    its sector already has max_per_sector picks — keeps the final list from
    being dominated by one or two sectors (a real risk without this: nothing
    stops the raw top-N from being e.g. 8 NBFCs)."""
    ordered = df.sort_values("composite_score", ascending=False)
    counts: dict[str, int] = {}
    picked = []
    for sym, row in ordered.iterrows():
        sec = row.get(sector_col, "unknown")
        if counts.get(sec, 0) >= max_per_sector:
            continue
        picked.append(sym)
        counts[sec] = counts.get(sec, 0) + 1
        if len(picked) >= n:
            break
    return ordered.loc[picked]


def run_final_shortlist(tech: pd.DataFrame, fund: pd.DataFrame,
                        n_candidates: int = 20, final_min: int = 10,
                        final_max: int = 15, max_per_sector: int = 4,
                        weights: dict | None = None,
                        run_ai_enrichment: bool = True,
                        read_annual_reports: bool = True,
                        progress_cb=None) -> dict:
    """Full pipeline: candidates -> composite score -> sector cap -> (optional)
    AI enrichment. The composite score and sector cap fully determine the
    final list; AI results (if run) are joined on for display only.

    Returns {'candidates': df, 'scored': df, 'final': df}:
      candidates - technical-gate-passers ranked by fundamental score (stage 1+2)
      scored     - candidates + composite_score (stage 3, pre-cap)
      final      - after the sector-diversification cap, the actual shortlist,
                   joined with AI verdict/summary/etc if run_ai_enrichment
    """
    def note(msg, frac):
        if progress_cb:
            progress_cb(msg, frac)

    candidates = select_top20(tech, fund, n=n_candidates)
    if candidates.empty:
        return {"candidates": candidates, "scored": candidates, "final": candidates}

    note("Computing composite score (technical + fundamental + earnings "
        "consistency + promoter buying)...", 0.05)
    scored = compute_composite_scores(
        candidates, weights=weights,
        progress_cb=lambda s, f: note(s, 0.05 + f * 0.35))

    final = apply_sector_cap(scored, max_per_sector=max_per_sector, n=final_max)
    if len(final) < final_min:
        # Too few sectors represented to hit the minimum at this cap —
        # relax it rather than hand back a short list.
        final = apply_sector_cap(scored, max_per_sector=max_per_sector + 2,
                                 n=final_max)

    if run_ai_enrichment:
        symbols = list(final.index)
        note(f"AI deep-read on {len(symbols)} picks (enrichment only — does "
            f"NOT affect ranking or selection)...", 0.45)
        ai_df = fan.analyze_many(
            symbols, read_annual_reports=read_annual_reports,
            progress_cb=lambda s, f: note(s, 0.45 + f * 0.5))
        ai_cols = [c for c in ["verdict", "fund_score", "earnings_real",
                               "durability", "confidence", "summary",
                               "red_flags", "risks", "catalysts"]
                  if c in ai_df.columns]
        final = final.join(ai_df[ai_cols], how="left")
        final = final.rename(columns={"fund_score": "ai_fund_score"})

    note("Done.", 1.0)
    return {"candidates": candidates, "scored": scored, "final": final}
