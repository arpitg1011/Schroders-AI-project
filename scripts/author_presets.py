"""Author preset themes directly (agent-authored taxonomy + match decisions).

Each preset is a dict:
  {
    "theme": "<display theme>",
    "layer1": [ {"sub_industry": str, "activities": [str, ...]}, ... (10) ],
    "matches": { "<COMPANY>": { "<SEGMENT>": "<sub_industry>", ... }, ... },
  }
Only matched (segment -> sub_industry) pairs are listed; everything else is a
non-match (exposure 0). Company + segment strings MUST match data exactly
(see _companies.json dump).

This writes, for each preset, in the SAME format the runtime expects:
  - cache/presets.json   (theme -> {theme, layer1})
  - cache/match/<key>.json   canonical match map keyed by
    engine._match_cache_key(theme, taxonomy, companies)

Run: python scripts/author_presets.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, ".")
import engine

SEGMENTS_PATH = "data/spglobal_segments.xlsx"
PORTFOLIO_PATH = "data/portfolio_80.xlsx"

# PRESETS are loaded from the sibling JSON file so this script stays small and
# the data is easy to review/edit.
PRESETS_DATA = "scripts/presets_authored.json"


def _companies_payload(rows):
    out = []
    for row in rows:
        if not row.get("has_segment_data"):
            continue
        names = [(w.get("name") or "").strip() for w in (row.get("segments") or [])
                 if (w.get("name") or "").strip()]
        if names:
            out.append({"company": row["company"], "segments": names})
    return out


def main() -> int:
    with open(PRESETS_DATA, "r", encoding="utf-8") as f:
        authored = json.load(f)

    segments = engine.load_segments(SEGMENTS_PATH)["segments"]
    portfolio_df = engine.load_portfolio(PORTFOLIO_PATH)["portfolio"]
    matches = engine.match_companies(portfolio_df, segments)["matches"]
    rows = engine.build_company_rows(portfolio_df, segments, matches)
    companies = _companies_payload(rows)
    valid_company_segs = {c["company"]: set(c["segments"]) for c in companies}

    # Merge into existing presets.json (keep Groq-built ones).
    presets = {}
    if os.path.exists(engine._PRESETS_PATH):
        with open(engine._PRESETS_PATH, "r", encoding="utf-8") as f:
            presets = json.load(f)

    problems = []
    for item in authored:
        theme = item["theme"]
        layer1 = item["layer1"]
        sub_names = {s["sub_industry"] for s in layer1}
        if len(layer1) != 10:
            problems.append(f"{theme}: has {len(layer1)} subs (want 10)")

        # Build the canonical match map in runtime shape.
        match_map = {}
        for company, segs in valid_company_segs.items():
            seg_decisions = item.get("matches", {}).get(company, {})
            cmap = {}
            for seg, sub in seg_decisions.items():
                if seg not in segs:
                    problems.append(f"{theme}: {company!r} has no segment {seg!r}")
                    continue
                if sub not in sub_names:
                    problems.append(f"{theme}: {company!r}/{seg!r} -> unknown sub {sub!r}")
                    continue
                cmap[seg] = {"sub_theme": sub, "reason": item.get("_reason", "sector relevance")}
            match_map[company] = cmap

        taxonomy = {"theme": theme, "layer1": layer1}
        key = engine._match_cache_key(theme, taxonomy, companies)
        engine._store_cached_match(key, match_map)
        presets[theme.strip().lower()] = {"theme": theme, "layer1": layer1}
        n_match = sum(1 for v in match_map.values() if v)
        print(f"{theme}: wrote match map ({n_match} companies matched)")

    with open(engine._PRESETS_PATH, "w", encoding="utf-8") as f:
        json.dump(presets, f, indent=2, ensure_ascii=False)

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"\nOK. presets.json now has {len(presets)} themes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
