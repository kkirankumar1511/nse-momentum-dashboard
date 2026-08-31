"""
Local-only diagnostic: cross-check each config.UNIVERSE symbol's sector
against NSE's own PER-SYMBOL index membership (GetQuoteApi's
secInfo.indexList), instead of relying solely on the existing
sector_universe.resolve_sector_profiles() PER-INDEX getConstituents
aggregation.

Motivation: getConstituents("NIFTY ENERGY") was found to be missing
ADANIPOWER and GVT&D even though NSE's own per-symbol indexList for both
includes "NIFTY ENERGY" -- a real data-completeness gap in the
constituent-list endpoint (likely lag on recently-listed/renamed names),
not an encoding bug (requests' params= already percent-encodes "&" ->
"%26" correctly, verified separately).

For each symbol: fetch its indexList, keep only entries the allIndices
catalog marks SECTORAL (falling back to THEMATIC, excluding
_NON_INDUSTRY_THEMES, same convention as resolve_sector_profiles()). If
more than one candidate remains, break ties by weightage looked up from
the EXISTING cached getConstituents data (same source resolve_sector_
profiles() already uses for its own weightage) -- if no weightage is
available for any candidate (the symbol is absent from getConstituents
under all of them, the same gap this script exists to work around), the
first candidate (index list order) is used and flagged.

Not wired into any live pipeline -- pure local research/verification
output (a CSV) for manual review before deciding whether to adopt this
as a fallback in sector_universe.py.

Run with: python scripts/crosscheck_sector_indexlist.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
import nse_api
import sector_universe as su

QUOTE_URL = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"
ALL_INDICES_URL = "https://www.nseindia.com/api/allIndices"


def fetch_index_list(session, symbol: str) -> list[str]:
    params = {"functionName": "getSymbolData", "marketType": "N",
             "series": "EQ", "symbol": symbol}
    r = session.get(QUOTE_URL, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    resp = data.get("equityResponse")
    if not resp:
        return []
    return resp[0].get("secInfo", {}).get("indexList", []) or []


def fetch_fullname_alias_map(session) -> dict[str, tuple[str, str]]:
    """2026-08-14 fix: GetQuoteApi's secInfo.indexList returns each index's
    FULL name (e.g. "NIFTY MIDSMALL FINANCIAL SERVICES"), but sector_
    universe.fetch_index_catalog() keys its dict by the ABBREVIATED
    indexSymbol (e.g. "NIFTY MS FIN SERV") -- a straight string match
    between the two silently misses almost every abbreviated index name
    (confirmed on real data: 48/52 symbols wrongly flagged unmapped in the
    first version of this script were exactly this mismatch, e.g. 360ONE's
    real sectoral membership in "NIFTY MS FIN SERV" never matched because
    its indexList only contains the spelled-out name).

    allIndices' OWN response has both fields on every row ("index" = full
    name, "indexSymbol" = abbreviation) -- this builds {full_name:
    (indexSymbol, category)} straight from that same endpoint, so indexList
    entries can be resolved to the canonical indexSymbol used everywhere
    else in this codebase (getConstituents, sector_composite_score, etc.)."""
    r = session.get(ALL_INDICES_URL, timeout=15)
    r.raise_for_status()
    alias = {}
    for row in r.json()["data"]:
        if row["key"] in ("SECTORAL INDICES", "THEMATIC INDICES"):
            alias[row["index"]] = (row["indexSymbol"], row["key"])
    return alias


def main():
    catalog = su.fetch_index_catalog(verbose=True)
    constituents = su.fetch_index_constituents(verbose=True)
    existing_profiles = su.resolve_sector_profiles(config.UNIVERSE, verbose=False)

    s = nse_api.session()
    alias_map = fetch_fullname_alias_map(s)
    print(f"Built full-name alias map for {len(alias_map)} sectoral/thematic indices.")

    def weightage_of(symbol: str, index_name: str):
        return constituents.get(index_name, {}).get(symbol)

    rows = []
    n = len(config.UNIVERSE)
    for i, sym in enumerate(config.UNIVERSE):
        if i % 20 == 0:
            print(f"  {i + 1}/{n} ({sym})...")
        try:
            idx_list_raw = fetch_index_list(s, sym)
        except Exception as e:
            print(f"  {sym}: FAILED ({e})")
            idx_list_raw = []
        time.sleep(0.3)

        # Resolve each raw (full-name) indexList entry to its canonical
        # indexSymbol via the alias map; entries with no match (index not
        # SECTORAL/THEMATIC at all, e.g. a broad-market or strategy index)
        # are dropped, same as before.
        idx_list = [alias_map[name][0] for name in idx_list_raw if name in alias_map]

        sectoral = [name for name in idx_list if catalog.get(name) == "SECTORAL INDICES"]
        thematic = [name for name in idx_list
                   if catalog.get(name) == "THEMATIC INDICES" and name not in su._NON_INDUSTRY_THEMES]

        candidates = sectoral if sectoral else thematic
        category = "SECTORAL INDICES" if sectoral else ("THEMATIC INDICES" if thematic else None)

        chosen = None
        chosen_weight = None
        weight_available = False
        if candidates:
            weighted = [(name, weightage_of(sym, name)) for name in candidates]
            if any(w is not None for _, w in weighted):
                weight_available = True
                chosen, chosen_weight = max(weighted, key=lambda t: (t[1] if t[1] is not None else -1))
            else:
                chosen = candidates[0]

        old_sector = (existing_profiles.get(sym) or {}).get("primary_sector")

        rows.append({
            "symbol": sym,
            "old_sector_constituents_based": old_sector,
            "new_sector_indexlist_based": chosen,
            "category": category,
            "weight_available": weight_available,
            "chosen_weightage": chosen_weight,
            "n_sectoral_candidates": len(sectoral),
            "n_thematic_candidates": len(thematic),
            "changed": (old_sector != chosen),
            "newly_filled": (old_sector is None and chosen is not None),
        })

    out = pd.DataFrame(rows)
    out_path = "sector_crosscheck_indexlist.csv"
    out.to_csv(out_path, index=False)

    n_old_missing = (out["old_sector_constituents_based"].isna()).sum()
    n_new_missing = (out["new_sector_indexlist_based"].isna()).sum()
    n_filled = out["newly_filled"].sum()
    n_changed = out["changed"].sum()
    print(f"\nSaved {len(out)} rows to {out_path}")
    print(f"Previously unmapped (constituents-based): {n_old_missing}")
    print(f"Still unmapped (indexList-based, no sectoral/thematic match at all): {n_new_missing}")
    print(f"Newly filled in (was None, now has a sector): {n_filled}")
    print(f"Sector CHANGED vs before (for symbols that had one either way): {n_changed - n_filled}")


if __name__ == "__main__":
    main()
