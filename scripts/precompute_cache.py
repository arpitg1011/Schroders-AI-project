"""Precompute taxonomy + segment-match caches for demo themes.

Run once (locally, with GROQ keys in .env) to populate cache/taxonomy and
cache/match. Those directories are committed, so the hosted app serves these
themes instantly with no live LLM calls. Re-run to add themes or refresh.

Usage:  python scripts/precompute_cache.py
"""

from __future__ import annotations

import sys
import time

# Allow running from repo root (scripts/ is a subdir).
sys.path.insert(0, ".")

import engine

THEMES = [
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
]

SEGMENTS_PATH = "data/spglobal_segments.xlsx"
PORTFOLIO_PATH = "data/portfolio_80.xlsx"


def main() -> int:
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

    print(f"Precomputing {len(THEMES)} themes "
          f"({len(portfolio_df)} holdings, {len(segments)} segment records)\n")

    ok_count = 0
    for i, theme in enumerate(THEMES, 1):
        t0 = time.time()
        print(f"[{i}/{len(THEMES)}] {theme!r} ...", flush=True)

        tax = engine.generate_taxonomy(theme)
        if not tax["ok"]:
            print(f"    taxonomy FAILED: {tax.get('error_code')} "
                  f"{tax.get('message')}")
            continue
        taxonomy = tax["taxonomy"]
        print(f"    taxonomy ({'cached' if tax.get('cached') else 'new'}): "
              f"{[s['sub_industry'] for s in taxonomy['layer1']]}")

        # analyze_portfolio runs the (cached-on-write) LLM matcher internally.
        res = engine.analyze_portfolio(portfolio_df, segments, taxonomy)
        mode = res["metrics"].get("scoring_mode")
        cached = res["metrics"].get("scoring_cached")
        exp = res["metrics"].get("portfolio_thematic_exposure")
        dt = time.time() - t0
        if mode == "llm":
            ok_count += 1
            print(f"    matched (mode={mode}, cached={cached}) "
                  f"exposure={exp}%  [{dt:.0f}s]")
        else:
            print(f"    WARNING: fell back to {mode} (LLM unavailable / rate "
                  f"limited). Re-run to populate this theme.  [{dt:.0f}s]")
        print(flush=True)

    print(f"Done. {ok_count}/{len(THEMES)} themes cached via the LLM matcher.")
    print("Commit the cache/ directory to ship these with the app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
