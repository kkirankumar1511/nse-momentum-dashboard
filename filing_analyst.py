"""
AI filing analyst.

Reads a stock's NSE filings — quarterly XBRL, annual report PDFs, corporate
announcements, shareholding history, corporate actions — and produces a
structured fundamental verdict that plugs into the momentum screener.

=== ARCHITECTURE: WHY IT'S A FUNNEL, NOT A FIREHOSE ===

An Indian annual report runs 200-400 pages. 210 F&O stocks x 5 years is
~300,000 pages. Sending that to any LLM would cost more than most people's
trading capital and take days. So this agent is designed as the LAST stage
of a funnel:

    210 F&O stocks
      -> technical + gate screen (free, seconds)      -> ~15-25 names
      -> XBRL + announcements + promoter scan (cheap) -> ~10 names
      -> AI deep-read of annual reports (expensive)   -> final shortlist

and within a PDF it does TARGETED SECTION EXTRACTION — auditor's report,
MD&A, related-party transactions, contingent liabilities — instead of
dumping the whole document. Those four sections are where the information
that changes a decision actually lives.

=== DIVISION OF LABOUR ===

Deterministic code owns the safety-critical checks (auditor resignations,
pledges, tax-rate anomalies, other-income share of PBT). The LLM owns
judgement and synthesis. An LLM that creatively misses an auditor
resignation is an unacceptable failure mode; a regex that flags one is not.
The LLM never overrides a deterministic red flag — it can only add context.
"""

from __future__ import annotations

import json
import os
import re

import pandas as pd

import config
import llm
import nse_api
import xbrl_parser

# Sections worth reading in an annual report, with the phrases that anchor them
SECTION_ANCHORS = {
    "auditor_report": [
        "independent auditor", "auditor's report", "basis for opinion",
        "qualified opinion", "emphasis of matter", "key audit matters",
    ],
    "mda": [
        "management discussion and analysis", "industry structure",
        "outlook", "risks and concerns", "opportunities and threats",
    ],
    "related_party": [
        "related party transaction", "related party disclosures",
    ],
    "contingent": [
        "contingent liabilities", "commitments and contingencies",
    ],
}


def extract_sections(pdf_path: str, max_pages_per_section: int = 6) -> dict:
    """Pull only the pages that matter from a large annual report.

    Cached alongside the PDF (same directory, content-addressed by the PDF's
    own filename hash + max_pages_per_section): a 200-400 page annual report
    costs ~60-140s to walk page-by-page with pdfplumber, and annual reports
    are immutable once published — re-running that walk on every re-analysis
    of a stock (which happens routinely: the same symbol showing up in
    repeated Final Shortlist / Filings Analyst runs) was pure waste. This was
    identified as a known inefficiency earlier and left unfixed until a
    20-stock run actually hit it (3+ hours — most of that was re-extracting
    PDFs already extracted in a prior run).
    """
    cache_path = f"{pdf_path}.sections_p{max_pages_per_section}.json"
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass  # corrupt cache — fall through and recompute

    try:
        import pdfplumber
    except ImportError:
        return {}

    found: dict[str, list[str]] = {k: [] for k in SECTION_ANCHORS}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            n = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                if all(len(v) >= max_pages_per_section for v in found.values()):
                    break
                try:
                    text = page.extract_text() or ""
                except Exception:
                    continue
                low = text.lower()
                for sec, anchors in SECTION_ANCHORS.items():
                    if len(found[sec]) >= max_pages_per_section:
                        continue
                    if any(a in low for a in anchors):
                        # capture this page and the next two (sections run on)
                        chunk = [text]
                        for j in (i + 1, i + 2):
                            if j < n and len(chunk) < 3:
                                try:
                                    chunk.append(pdf.pages[j].extract_text() or "")
                                except Exception:
                                    pass
                        found[sec].append("\n".join(chunk))
    except Exception as e:
        return {"_error": str(e)}

    result = {k: "\n---\n".join(v)[:12000] for k, v in found.items() if v}
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(result, f)
    except OSError:
        pass  # cache write failure shouldn't break the actual analysis
    return result


_ENTITY_LINE = re.compile(
    r"\b(Subsidiary|Associate|Joint Venture|Controlled Trust)\b", re.I)


def _is_entity_annexure_page(text: str) -> bool:
    """Quarterly review-report PDFs append a list of every subsidiary/
    associate covered by the consolidation (often 100+ lines) as an
    Annexure. It repeats the same 'Limited Review Report (Continued)'
    header as the actual report text, so a naive anchor match would pull
    in the whole entity list instead of the auditor's conclusion."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    hits = sum(1 for l in lines if _ENTITY_LINE.search(l))
    return hits >= 3 and hits / len(lines) > 0.3


def extract_quarterly_result_text(pdf_path: str, max_chars: int = 12000) -> str:
    """Pull the Notes and auditor's Limited Review Report narrative out of a
    quarterly results filing PDF (~20 pages, vs. 200-400 for an annual
    report). Skips subsidiary-list annexures (boilerplate) and the raw
    financial statement tables — those numbers already come from structured
    XBRL; this is for the qualitative parts XBRL doesn't carry.
    """
    try:
        import pdfplumber
    except ImportError:
        return ""
    keep = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if not text or _is_entity_annexure_page(text):
                    continue
                low = text.lower()
                if ("notes" in low[:200] or "limited review report" in low
                        or "independent auditor" in low
                        or "qualified opinion" in low
                        or "emphasis of matter" in low):
                    keep.append(text)
    except Exception:
        return ""
    return "\n---\n".join(keep)[:max_chars]


def gather_evidence(symbol: str, n_years: int = 5,
                    read_annual_reports: bool = True,
                    progress_cb=None) -> dict:
    """Collect everything the analyst needs for one symbol. Deterministic."""
    def note(msg):
        if progress_cb:
            progress_cb(msg)

    ev: dict = {"symbol": symbol}

    note(f"{symbol}: quarterly XBRL...")
    qdf = xbrl_parser.quarterly_financials(symbol, max_quarters=12)
    ev["quarterly"] = qdf
    ev["earnings_quality"] = xbrl_parser.earnings_quality(qdf)

    note(f"{symbol}: announcements...")
    ev["announcements"] = nse_api.classify_announcements(symbol, days=365)

    note(f"{symbol}: quarterly results filing (notes + auditor review)...")
    ev["quarterly_result_narrative"] = ""
    qtr_filing = nse_api.latest_quarterly_result_filing(symbol)
    if qtr_filing:
        path = nse_api.download(qtr_filing["url"], subdir="quarterly_results")
        if path:
            ev["quarterly_result_narrative"] = extract_quarterly_result_text(path)
            ev["quarterly_result_date"] = qtr_filing["date"]

    note(f"{symbol}: promoter holding...")
    ev["promoter"] = nse_api.promoter_trend(symbol)

    note(f"{symbol}: corporate actions...")
    ev["corporate_actions"] = nse_api.upcoming_corporate_actions(symbol)

    ev["annual_reports"] = []
    if read_annual_reports:
        for rep in nse_api.latest_annual_report_pdf(symbol, n_years):
            url = rep.get("url") or ""
            if not url.lower().endswith(".pdf"):
                continue  # skip .zip bundles
            note(f"{symbol}: annual report {rep['from']}-{rep['to']}...")
            path = nse_api.download(url, subdir="annual_reports")
            if not path:
                continue
            secs = extract_sections(path)
            if secs and "_error" not in secs:
                ev["annual_reports"].append({
                    "period": f"{rep['from']}-{rep['to']}", "sections": secs})
            if len(ev["annual_reports"]) >= 2:
                break  # latest 2 reports carry the decision-relevant content
    return ev


# ---------------------------------------------------------------------------
# Deterministic scoring (runs regardless of AI availability)
# ---------------------------------------------------------------------------

def deterministic_verdict(ev: dict) -> dict:
    """Hard signals only. This is what you'd trust if the LLM were offline."""
    eq = ev.get("earnings_quality") or {}
    pr = ev.get("promoter") or {}
    ann = ev.get("announcements") or {}

    flags = [f["type"] for f in ann.get("red_flags", [])]
    flags += eq.get("quality_warnings", [])
    if pr.get("promoter_trend") == "decreasing":
        flags.append(f"promoter stake down {abs(pr.get('promoter_change_1y', 0)):.2f}pp in 1y")

    score, reasons = 50.0, []
    if (eq.get("pat_yoy") or 0) > 15:
        score += 10; reasons.append(f"PAT +{eq['pat_yoy']:.0f}% YoY")
    if (eq.get("pat_growth_accel") or 0) > 0:
        score += 10; reasons.append("earnings growth accelerating")
    if (eq.get("rev_yoy") or 0) > 10:
        score += 8; reasons.append(f"revenue +{eq['rev_yoy']:.0f}% YoY")
    if (eq.get("ebitda_margin_trend_pp") or 0) > 0.5:
        score += 8; reasons.append(f"margins +{eq['ebitda_margin_trend_pp']:.1f}pp")
    if (eq.get("yoy_win_rate_pct") or 0) >= 75:
        score += 6; reasons.append(f"{eq['yoy_win_rate_pct']:.0f}% of quarters grew YoY")
    if pr.get("promoter_trend") == "increasing":
        score += 10; reasons.append(f"promoters bought +{pr['promoter_change_1y']:.2f}pp")
    if ann.get("catalysts"):
        score += 4; reasons.append(", ".join(c["type"] for c in ann["catalysts"][:3]))
    score -= 12 * len(flags)

    return {
        "fundamental_score": round(max(0, min(100, score)), 1),
        "reasons": reasons,
        "red_flags": flags,
        "blocking": len(ann.get("red_flags", [])) > 0,  # hard events block
    }


# ---------------------------------------------------------------------------
# AI synthesis layer
# ---------------------------------------------------------------------------

ANALYST_SYSTEM = """You are a sceptical equity analyst reading Indian (NSE)
company filings. You are the last line of defence against a retail trader
buying something that looks good on a screener and is rotten underneath.

You will receive: parsed quarterly financials (from the company's own XBRL),
computed earnings-quality metrics, deterministic red flags, promoter
shareholding trend, recent announcements, the latest quarter's Notes and
auditor's Limited Review Report (from the results filing PDF, published the
same evening as results — audited/unaudited status applies to THIS quarter,
not the annual report), and extracted sections of annual reports (auditor's
report, MD&A, related-party transactions, contingent liabilities).

Your job:
1. Judge whether reported earnings growth is REAL (volume/pricing/operating
   leverage) or ENGINEERED (other income, one-offs, tax breaks, related-party
   revenue, capitalised costs, receivable stretching).
2. Read the auditor's report/limited review report (quarterly and annual,
   whichever you have) for qualifications, emphasis of matter, key audit
   matters. Say plainly if the auditor is signalling discomfort.
3. Check related-party transactions and contingent liabilities for anything
   that could impair the business.
4. Assess whether the growth is DURABLE over 1-3 years (moat, capex cycle,
   order book, industry structure) as opposed to a cyclical peak.

Rules:
- NEVER dismiss or downgrade a deterministic red flag you were handed. You
  may add context; you may not overrule it.
- Cite the specific number or filing phrase behind each claim. No vibes.
- If evidence is missing or thin, say so and lower confidence. Do not fill
  gaps with plausible-sounding narrative.
- Be explicit about what would falsify your view.

Respond ONLY with JSON, no markdown fences:
{"symbol": str,
 "earnings_real": "real"|"mixed"|"engineered"|"unclear",
 "earnings_evidence": str,
 "auditor_concerns": [str],
 "durability": "high"|"medium"|"low"|"unclear",
 "durability_reasoning": str,
 "key_risks": [str],
 "catalysts_next_12m": [str],
 "verdict": "strong"|"watch"|"avoid",
 "confidence": "high"|"medium"|"low",
 "what_would_change_my_mind": str,
 "summary": "<=80 words"}"""


def _evidence_to_prompt(ev: dict, det: dict) -> str:
    parts = [f"SYMBOL: {ev['symbol']}"]

    qdf = ev.get("quarterly")
    if isinstance(qdf, pd.DataFrame) and not qdf.empty:
        cols = [c for c in ["qe_date", "revenue", "ebitda_margin", "pbt",
                            "pat", "net_margin", "other_income", "eps_basic",
                            "audited"] if c in qdf.columns]
        parts.append("QUARTERLY FINANCIALS (from company XBRL, oldest first):\n"
                     + qdf[cols].to_string(index=False))

    eq = ev.get("earnings_quality") or {}
    if eq:
        parts.append("EARNINGS QUALITY METRICS:\n" + json.dumps(
            {k: v for k, v in eq.items() if k != "quality_warnings"},
            indent=1, default=str))
        if eq.get("quality_warnings"):
            parts.append("COMPUTED QUALITY WARNINGS:\n- "
                         + "\n- ".join(eq["quality_warnings"]))

    if ev.get("quarterly_result_narrative"):
        parts.append(
            f"LATEST QUARTERLY RESULTS FILING ({ev.get('quarterly_result_date', '')}) "
            "— NOTES & AUDITOR'S LIMITED REVIEW REPORT (from the actual PDF, "
            "not just tagged XBRL numbers):\n"
            + ev["quarterly_result_narrative"])

    pr = ev.get("promoter") or {}
    if pr.get("promoter_series"):
        hist = "; ".join(f"{d}: {v}%" for d, v in pr["promoter_series"][-8:])
        parts.append(f"PROMOTER HOLDING TREND ({pr.get('promoter_trend')}, "
                     f"1y change {pr.get('promoter_change_1y')}pp):\n{hist}")

    ann = ev.get("announcements") or {}
    if ann.get("red_flags"):
        parts.append("DETERMINISTIC RED FLAGS (you may NOT overrule these):\n"
                     + json.dumps(ann["red_flags"], indent=1))
    if ann.get("catalysts"):
        parts.append("DETECTED CATALYSTS:\n" + json.dumps(ann["catalysts"], indent=1))
    if ann.get("recent_announcements"):
        rows = [f"{a['date']} [{a['desc']}] {a['text'][:180]}"
                for a in ann["recent_announcements"][:15]]
        parts.append("RECENT ANNOUNCEMENTS:\n" + "\n".join(rows))

    if ev.get("corporate_actions"):
        parts.append("UPCOMING CORPORATE ACTIONS:\n"
                     + json.dumps(ev["corporate_actions"], indent=1))

    for rep in ev.get("annual_reports", []):
        for sec, text in rep["sections"].items():
            parts.append(f"ANNUAL REPORT {rep['period']} — {sec.upper()}:\n"
                         f"{text[:8000]}")

    parts.append("DETERMINISTIC SCORE: " + json.dumps(det, default=str))
    return "\n\n".join(parts)


# Schema the model must fill. Coerced/defaulted if the model omits fields —
# small open models drop keys and invent enum values constantly.
ANALYST_SCHEMA = {
    "symbol": {"type": "str", "default": ""},
    "earnings_real": {"type": "enum",
                      "values": ["real", "mixed", "engineered", "unclear"],
                      "default": "unclear"},
    "earnings_evidence": {"type": "str", "default": ""},
    "auditor_concerns": {"type": "list"},
    "durability": {"type": "enum", "values": ["high", "medium", "low", "unclear"],
                   "default": "unclear"},
    "durability_reasoning": {"type": "str", "default": ""},
    "key_risks": {"type": "list"},
    "catalysts_next_12m": {"type": "list"},
    "verdict": {"type": "enum", "values": ["strong", "watch", "avoid"],
                "default": "watch"},
    "confidence": {"type": "enum", "values": ["high", "medium", "low"],
                   "default": "low"},
    "what_would_change_my_mind": {"type": "str", "default": ""},
    "summary": {"type": "str", "default": ""},
}


def ai_analyze(ev: dict, det: dict) -> dict:
    """LLM synthesis over gathered evidence, via whatever provider is
    configured (Ollama/Groq/OpenRouter/... — see llm.py). Returns {} if the
    provider is unavailable, in which case the deterministic verdict stands
    on its own."""
    ok, msg = llm.is_available()
    if not ok:
        return {}
    evidence = _evidence_to_prompt(ev, det)
    question = ("Analyze this company per your instructions and return the "
                "JSON object.")
    try:
        # map_reduce_json falls back to chunking only if the evidence exceeds
        # the model's context — a 32k+ model does it in one pass.
        out = llm.map_reduce_json(ANALYST_SYSTEM, evidence, question,
                                  schema=ANALYST_SCHEMA, max_tokens=2000)
        out["symbol"] = ev.get("symbol", "")
        out["_model"] = llm.describe()
        return out
    except Exception as e:
        # Marked _ai_failed so analyze() treats this the same as "AI
        # unavailable" for the deterministic-strong fallback below — a rate
        # limit or network error mid-call is not the AI's judgement, and
        # hardcoding verdict='watch' here was silently capping every
        # LLM-failure case at watch regardless of how strong the
        # deterministic score was (verified: a 20-stock run where every
        # single row showed WATCH, including symbols scoring 90-100 on
        # fundamentals, because Groq's daily quota was exhausted for the
        # whole run — the deterministic score should stand fully on its own
        # in that situation, same as when the LLM was never configured).
        return {"symbol": ev.get("symbol"), "verdict": "watch",
                "confidence": "low", "earnings_real": "unclear",
                "durability": "unclear",
                "summary": f"LLM analysis failed ({e}). Deterministic score "
                           f"{det['fundamental_score']} still applies.",
                "_model": llm.describe(), "_ai_failed": True}


def analyze(symbol: str, read_annual_reports: bool = True,
            progress_cb=None) -> dict:
    """Full pipeline for one symbol: evidence -> deterministic -> AI."""
    ev = gather_evidence(symbol, read_annual_reports=read_annual_reports,
                         progress_cb=progress_cb)
    det = deterministic_verdict(ev)
    ai = ai_analyze(ev, det)

    # The AI can never soften a hard event flag. "AI failed to run" (rate
    # limit, network error) is treated the same as "AI unavailable" for the
    # deterministic-strong fallback — a failed call carries no judgement to
    # defer to, same as no call at all.
    ai_usable = ai and not ai.get("_ai_failed")
    final = "watch"
    if det["blocking"] or (ai_usable and ai.get("verdict") == "avoid"):
        final = "avoid"
    elif ai_usable and ai.get("verdict") == "strong" and det["fundamental_score"] >= 60:
        final = "strong"
    elif not ai_usable and det["fundamental_score"] >= 70:
        final = "strong"

    return {
        "symbol": symbol,
        "final_verdict": final,
        "fundamental_score": det["fundamental_score"],
        "det_reasons": det["reasons"],
        "red_flags": det["red_flags"],
        "ai": ai,
        "evidence": ev,
    }


def analyze_many(symbols: list[str], read_annual_reports: bool = True,
                 progress_cb=None) -> pd.DataFrame:
    rows = []
    for i, s in enumerate(symbols):
        if progress_cb:
            progress_cb(f"Analyzing {s} ({i+1}/{len(symbols)})...",
                        (i + 1) / len(symbols))
        r = analyze(s, read_annual_reports)
        ai = r.get("ai") or {}
        rows.append({
            "symbol": s,
            "verdict": r["final_verdict"],
            "fund_score": r["fundamental_score"],
            "earnings_real": ai.get("earnings_real"),
            "durability": ai.get("durability"),
            "confidence": ai.get("confidence"),
            "summary": ai.get("summary", "; ".join(r["det_reasons"])),
            "red_flags": "; ".join(r["red_flags"]),
            "risks": "; ".join(ai.get("key_risks", [])[:3]),
            "catalysts": "; ".join(ai.get("catalysts_next_12m", [])[:3]),
        })
    return pd.DataFrame(rows).set_index("symbol") if rows else pd.DataFrame()


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "HCLTECH"
    res = analyze(sym, read_annual_reports="--no-ar" not in sys.argv,
                  progress_cb=print)
    print(json.dumps({k: v for k, v in res.items() if k != "evidence"},
                     indent=2, default=str))
