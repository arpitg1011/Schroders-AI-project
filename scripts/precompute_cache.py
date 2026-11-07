"""Precompute GLOBAL PRESET themes: canonical 10-sub-theme taxonomy + match map.

For each preset theme this:
  1. Generates the taxonomy at the MAX sub-theme count (10) via Groq.
  2. Runs the LLM segment matcher once against the default portfolio, which
     writes the canonical match cache (cache/match/<hash>.json).
  3. Records the theme + its canonical 10 sub-themes into cache/presets.json.

The committed cache/ (presets.json + match/ + taxonomy/) then serves these
themes to EVERYONE instantly, and any sub-theme count 3..10 is served by taking
the top-N of the canonical 10 (engine._preset_match_map filters the cached map).

Run once locally with GROQ keys in .env. Re-run to add/refresh themes; it skips
themes whose canonical match map is already cached unless --force is passed.

Usage:  python scripts/precompute_cache.py [--force]
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, ".")  # allow running from repo root

import engine

# 20 global preset themes. Mix of sector/tech, macro, geopolitical, and
# "rogue-but-valid" event themes (weather/demographic/geopolitical) that the
# validity gate must ACCEPT - these double as gate regression checks.
THEMES = [
    # --- the original 10 ---
    "semiconductors",
    "El Nino",
    "US elections",
    "US-China relations",
    "artificial intelligence",
    "electric vehicles",
    "digital payments",
    "cloud computing",
    "data centers",
    "cybersecurity",
    # --- 10 additions (sector / macro / geopolitical / rogue-but-valid) ---
    "renewable energy",
    "5G and wireless infrastructure",
    "defense and military spending",
    "Taiwan Strait tensions",            # geopolitical, rogue-but-valid
    "Red Sea shipping disruption",       # recent event, rogue-but-valid
    "generative AI",
    "aging population",                  # demographic, rogue-but-valid
    "water scarcity",                    # climate, rogue-but-valid (like El Nino)
    "interest rate hikes",               # macro
    "gold and safe haven demand",        # commodity / macro
]

SEGMENTS_PATH = "data/spglobal_segments.xlsx"
PORTFOLIO_PATH = "data/portfolio_80.xlsx"
PRESET_COUNT = engine._MAX_SUBS  # canonical 10 sub-themes


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
    force = "--force" in sys.argv

    segs = engine.load_segments(SEGMENTS_PATH)
    if not segs["ok"]:
        print("FAILED to load segments:", segs.get("message"))
        return 1
    segments = segs["segments"]

    pf = engine.load_portfolio(PORTFOLIO_PATH)
    if not pf["ok"]:
        print("FAILED to load portfolio:", pf.get("message"))
        return 1
    portfolio_df = pf["portfolio"]

    matches = engine.match_companies(portfolio_df, segments)["matches"]
    rows = engine.build_company_rows(portfolio_df, segments, matches)
    companies = _companies_payload(rows)

    # Load any existing presets so re-runs are additive.
    presets: dict = {}
    if os.path.exists(engine._PRESETS_PATH):
        try:
            with open(engine._PRESETS_PATH, "r", encoding="utf-8") as f:
                presets = json.load(f)
        except Exception:
            presets = {}

    print(f"Precomputing {len(THEMES)} preset themes at {PRESET_COUNT} sub-themes "
          f"({len(companies)} companies with segment data)\n")

    ok_count = 0
    for i, theme in enumerate(THEMES, 1):
        key = theme.strip().lower()
        t0 = time.time()
        print(f"[{i}/{len(THEMES)}] {theme!r}", flush=True)

        # Taxonomy at the canonical count (force a fresh 10-sub generation).
        tax = engine.generate_taxonomy(theme, n=PRESET_COUNT, force_refresh=True)
        if not tax["ok"]:
            print(f"    taxonomy FAILED: {tax.get('error_code')} {tax.get('message')}")
            continue
        taxonomy = tax["taxonomy"]
        subs = [s["sub_industry"] for s in taxonomy["layer1"]]
        print(f"    {len(subs)} subs: {subs}")

        # Skip if the canonical match map is already cached (unless --force).
        canon_key = engine._match_cache_key(theme, taxonomy, companies)
        if not force and engine._load_cached_match(canon_key) is not None:
            presets[key] = {"theme": theme, "layer1": taxonomy["layer1"]}
            ok_count += 1
            print(f"    already cached, recorded preset.  [{time.time()-t0:.0f}s]\n")
            continue

        # Build + cache the canonical match map.
        mres = engine.match_segments_llm(taxonomy, rows, force_refresh=True)
        if not mres["ok"]:
            print(f"    matcher FAILED: {mres.get('error_code')} {mres.get('message')}")
            continue
        matched = sum(1 for v in mres["match_map"].values() if v)
        presets[key] = {"theme": theme, "layer1": taxonomy["layer1"]}
        ok_count += 1
        print(f"    matched {matched} companies.  [{time.time()-t0:.0f}s]\n", flush=True)

        # Persist presets incrementally so a crash keeps progress.
        with open(engine._PRESETS_PATH, "w", encoding="utf-8") as f:
            json.dump(presets, f, indent=2, ensure_ascii=False)

    with open(engine._PRESETS_PATH, "w", encoding="utf-8") as f:
        json.dump(presets, f, indent=2, ensure_ascii=False)

    print(f"Done. {ok_count}/{len(THEMES)} presets ready. "
          f"presets.json has {len(presets)} themes.")
    print("Commit cache/ to ship these with the app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
