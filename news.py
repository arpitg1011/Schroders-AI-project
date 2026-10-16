"""news.py - News-driven exposure overlay (Requirement 16, design Revision R3).

A transparent, bounded overlay on top of the deterministic filings-based
Baseline_Exposure. News NEVER silently changes the baseline: it produces a
separate, cited, per-sub-theme adjustment that the UI shows alongside the
baseline so the contribution of news is always decomposable and auditable.

Two parts, deliberately decoupled:
  1. A pluggable headline FETCHER (NewsAPI -> GDELT fallback). Swappable via
     config without touching the adjustment logic.
  2. An LLM ADJUSTMENT step that takes headlines as input (injectable, so the
     whole pipeline is testable without any network/key) and returns a signed,
     bounded, per-sub-theme adjustment with rationale and affected companies.
"""

from __future__ import annotations

import datetime as _dt
import json
import urllib.parse
import urllib.request

from engine import TAXONOMY_MODEL, err, get_secret, ok

# Error codes specific to the news overlay.
NEWS_UNAVAILABLE = "NEWS_UNAVAILABLE"
NEWS_LLM_FAILED = "NEWS_LLM_FAILED"

# News informs but never dominates the structural baseline (Req 16.5).
NEWS_ADJUSTMENT_CAP = 10.0  # max absolute total adjustment in percentage points
_PER_SUB_CAP = 6.0          # max absolute adjustment any single sub-theme can carry
_HTTP_TIMEOUT = 25
_MAX_HEADLINES = 12


# ---------------------------------------------------------------------------
# Headline fetchers (pluggable)
# ---------------------------------------------------------------------------
def _http_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "ThemeIQ/1.0"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _fetch_newsapi(theme: str, api_key: str) -> list[dict]:
    """NewsAPI /everything. Clean results; free tier is dev/localhost only."""
    frm = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
    url = "https://newsapi.org/v2/everything?" + urllib.parse.urlencode({
        "q": theme, "from": frm, "sortBy": "publishedAt",
        "language": "en", "pageSize": _MAX_HEADLINES, "apiKey": api_key,
    })
    data = _http_json(url)
    out = []
    for a in data.get("articles", []):
        out.append({
            "title": a.get("title") or "",
            "domain": (a.get("source") or {}).get("name") or "",
            "date": (a.get("publishedAt") or "")[:10],
            "url": a.get("url") or "",
        })
    return out


def _fetch_freenews(theme: str, api_key: str) -> list[dict]:
    """freenewsapi.io - 5000 req/day. Auth via x-api-key header, /v1/news,
    'search' query param. Articles under 'data'; no URL on the free tier."""
    url = "https://api.freenewsapi.io/v1/news?" + urllib.parse.urlencode({
        "search": theme, "language": "en",
    })
    data = _http_json(url, headers={
        "User-Agent": "ThemeIQ/1.0", "Accept": "application/json",
        "x-api-key": api_key,
    })
    out = []
    for a in data.get("data", []):
        out.append({
            "title": (a.get("title") or "").encode("utf-8", "ignore").decode("utf-8"),
            "domain": a.get("publisher") or "",
            "date": (a.get("published_at") or "")[:10],
            "url": a.get("url") or a.get("link") or "",
        })
    return out


def _fetch_newsdata(theme: str, api_key: str) -> list[dict]:
    """newsdata.io - free tier works server-side (good for Streamlit Cloud)."""
    url = "https://newsdata.io/api/1/news?" + urllib.parse.urlencode({
        "apikey": api_key, "q": theme, "language": "en",
    })
    data = _http_json(url)
    if data.get("status") != "success":
        return []
    out = []
    for a in data.get("results", []):
        out.append({
            "title": (a.get("title") or "").encode("utf-8", "ignore").decode("utf-8"),
            "domain": a.get("source_id") or "",
            "date": (a.get("pubDate") or "")[:10],
            "url": a.get("link") or "",
        })
    return out


def _fetch_gdelt(theme: str) -> list[dict]:
    """GDELT doc API. Free, no key, server-side friendly, but rate-limited."""
    url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode({
        "query": theme, "mode": "artlist", "maxrecords": str(_MAX_HEADLINES),
        "format": "json", "sort": "datedesc", "timespan": "7d",
    })
    data = _http_json(url, headers={"User-Agent": "Mozilla/5.0 ThemeIQ"})
    out = []
    for a in data.get("articles", []):
        sd = str(a.get("seendate") or "")
        date = f"{sd[0:4]}-{sd[4:6]}-{sd[6:8]}" if len(sd) >= 8 else ""
        out.append({
            "title": a.get("title") or "",
            "domain": a.get("domain") or "",
            "date": date,
            "url": a.get("url") or "",
        })
    return out


def _filter_by_window(heads: list, lookback_days: int) -> list:
    """Keep only headlines within the lookback window (client-side).

    The free newsdata.io tier has no server-side date range, so we filter here.
    Headlines with no parseable date are kept (treated as current).
    """
    if not lookback_days or lookback_days <= 0:
        return heads
    cutoff = _dt.date.today() - _dt.timedelta(days=lookback_days)
    kept = []
    for h in heads:
        d = h.get("date", "")
        try:
            adate = _dt.date.fromisoformat(d[:10])
        except Exception:
            kept.append(h)  # undated -> keep
            continue
        if adate >= cutoff:
            kept.append(h)
    return kept


def fetch_headlines(theme: str, lookback_days: int = 7) -> dict:
    """Fetch recent headlines for a theme from the best available source,
    then filter to the lookback window client-side.

    Order: newsdata.io -> NewsAPI -> GDELT. Returns a handled error if all
    sources fail, so the UI can show the baseline normally (Req 16.9).
    Returns {ok, headlines, source, fetched_count, window_count}.
    """
    raw, source = [], None
    # Preferred: newsdata.io - its keyword search actually returns relevant,
    # on-theme results (verified), and it works server-side.
    newsdata_key = get_secret("NEWSDATA_API_KEY")
    if newsdata_key:
        try:
            heads = _fetch_newsdata(theme, newsdata_key)
            if heads:
                raw, source = heads, "newsdata.io"
        except Exception:
            pass
    # Note: freenewsapi.io has a generous quota but its free `search` ignores
    # the query and returns an irrelevant general feed, so it is NOT used for
    # themed search. _fetch_freenews is retained only as a last-resort stub.
    # Secondary: NewsAPI (dev/localhost only on free tier).
    if not raw:
        newsapi_key = get_secret("NEWSAPI_KEY")
        if newsapi_key:
            try:
                heads = _fetch_newsapi(theme, newsapi_key)
                if heads:
                    raw, source = heads, "NewsAPI"
            except Exception:
                pass
    # Fallback: GDELT (free, no key, but rate-limited).
    if not raw:
        try:
            heads = _fetch_gdelt(theme)
            if heads:
                raw, source = heads, "GDELT"
        except Exception as exc:
            return err(NEWS_UNAVAILABLE,
                       "Live news is temporarily unavailable; showing filings-based exposure only.",
                       detail=str(exc))

    if not raw:
        return err(NEWS_UNAVAILABLE,
                   "No recent news found for this theme; showing filings-based exposure only.")

    deduped = _dedupe_headlines(raw)
    windowed = _filter_by_window(deduped, lookback_days)
    # Stable ordering (date desc, then title) so the LLM sees a consistent set
    # regardless of source ordering -> more reproducible assessments.
    windowed.sort(key=lambda h: (h.get("date", ""), h.get("title", "")), reverse=True)
    windowed = windowed[:_MAX_HEADLINES]
    return ok(headlines=windowed, source=source,
              fetched_count=len(raw), window_count=len(windowed),
              lookback_days=lookback_days)


def _dedupe_headlines(heads: list) -> list:
    """Collapse near-duplicate stories (same event from multiple outlets) so a
    single event is not double-counted and the headline set is stable."""
    import re
    seen = set()
    out = []
    for h in heads:
        title = (h.get("title") or "").lower().strip()
        # Normalize to the first ~8 significant words as a crude fingerprint.
        words = [w for w in re.sub(r"[^a-z0-9 ]", " ", title).split() if w]
        fp = " ".join(words[:8])
        if not fp or fp in seen:
            continue
        seen.add(fp)
        out.append(h)
    return out


# ---------------------------------------------------------------------------
# LLM adjustment (takes headlines as input -> injectable & testable)
# ---------------------------------------------------------------------------
# --- Rubric: the LLM judges direction + strength; WE map to points. ---
# This is the auditable core: the magnitude is NOT the model's guess, it comes
# from this fixed, published table. The analyst can see this rubric in the UI.
DIRECTIONS = ("Tailwind", "Headwind", "Neutral")
STRENGTHS = ("High", "Medium", "Low", "None")

# strength -> magnitude in percentage points (direction sets the sign)
_STRENGTH_POINTS = {"High": 4.0, "Medium": 2.0, "Low": 1.0, "None": 0.0}

RUBRIC = {
    "what_llm_judges": (
        "For each sub-theme, the model classifies recent news on two axes only: "
        "DIRECTION (Tailwind = increases exposure, Headwind = decreases, Neutral) "
        "and STRENGTH (how material and direct the event is to that sub-theme's "
        "revenue drivers)."
    ),
    "strength_definitions": {
        "High": "A confirmed, material event that directly affects a core revenue "
                "driver of named portfolio holdings (e.g. export ban on a key input, "
                "major capacity commitment).",
        "Medium": "A credible development with a clear but partial or indirect link "
                  "to the sub-theme's revenue (e.g. demand signal, mid-size deal).",
        "Low": "A relevant but minor or speculative item (early-stage, rumoured, or "
               "small-scale).",
        "None": "No materially relevant news for this sub-theme.",
    },
    "point_mapping": {
        "High": "+/- 4 pts", "Medium": "+/- 2 pts", "Low": "+/- 1 pt", "None": "0 pts",
    },
    "caps": {
        "per_sub_theme": f"+/- {int(_PER_SUB_CAP)} pts",
        "total": f"+/- {int(NEWS_ADJUSTMENT_CAP)} pts (adjustments scaled down "
                 "proportionally if the sum exceeds this)",
    },
    "note": (
        "The point magnitude is assigned deterministically by this rubric, not by "
        "the model. The model only supplies direction, strength, a cited rationale, "
        "and the affected holdings. The overlay is a point-in-time signal and is "
        "shown separately from the deterministic filings-based baseline."
    ),
}

_NEWS_SYSTEM_PROMPT = (
    "You are an equity strategist assessing how RECENT NEWS changes a "
    "portfolio's exposure to an investment theme, relative to a slow-moving "
    "filings-based baseline. Reason about the VALUE CHAIN, not sentiment: an "
    "event propagates to the kinds of businesses it affects (e.g. an oil-price "
    "shock is a tailwind for energy-derived names; rare-earth export curbs are a "
    "headwind for chip supply chains).\n\n"
    "For each sub-industry, classify the recent news on TWO axes only:\n"
    "- direction: one of Tailwind (raises exposure), Headwind (lowers exposure), Neutral.\n"
    "- strength: one of High, Medium, Low, None, defined as:\n"
    "  High = confirmed, material event directly affecting a core revenue driver "
    "of named holdings;\n"
    "  Medium = credible development with a clear but partial/indirect link;\n"
    "  Low = relevant but minor or speculative;\n"
    "  None = no materially relevant news.\n"
    "Do NOT output any percentage or point number - only direction and strength.\n\n"
    "Output ONLY a JSON object with key \"assessments\": an array where each item "
    "has \"sub_industry\" (must match one given), \"direction\", \"strength\", "
    "\"rationale\" (one sentence citing the specific event), \"cited_headlines\" "
    "(array of integer indices into the headlines list), and \"affected_companies\" "
    "(array of the given company names most affected via the value chain). "
    "Include every sub-industry; use strength \"None\" / direction \"Neutral\" "
    "for those with no relevant news."
)


def _points_for(direction: str, strength: str) -> float:
    """Deterministic rubric mapping: (direction, strength) -> signed points."""
    mag = _STRENGTH_POINTS.get(strength, 0.0)
    if direction == "Tailwind":
        return mag
    if direction == "Headwind":
        return -mag
    return 0.0  # Neutral


def _validate_assessments(obj, taxonomy: dict, headlines: list) -> list:
    """Validate LLM direction/strength output and apply the rubric mapping."""
    if not isinstance(obj, dict):
        return []
    valid_subs = {s.get("sub_industry") for s in taxonomy.get("layer1", [])}
    items = obj.get("assessments")
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sub = it.get("sub_industry")
        if sub not in valid_subs:
            continue
        direction = it.get("direction") if it.get("direction") in DIRECTIONS else "Neutral"
        strength = it.get("strength") if it.get("strength") in STRENGTHS else "None"
        adj = max(-_PER_SUB_CAP, min(_PER_SUB_CAP, _points_for(direction, strength)))
        if adj == 0 and strength == "None":
            continue  # omit no-news sub-themes from the impact list
        cites = [i for i in it.get("cited_headlines", [])
                 if isinstance(i, int) and 0 <= i < len(headlines)]
        companies = [c for c in it.get("affected_companies", []) if isinstance(c, str)]
        rationale = str(it.get("rationale", "")).strip()[:300]
        rationale = rationale.encode("utf-8", "ignore").decode("utf-8")
        out.append({
            "sub_industry": sub,
            "direction": direction,
            "strength": strength,
            "adjustment": adj,
            "rationale": rationale,
            "cited_headlines": cites,
            "affected_companies": companies[:8],
        })
    return out


def compute_news_adjustment(theme: str, taxonomy: dict, headlines: list,
                            portfolio_companies: list,
                            _client=None) -> dict:
    """Given headlines, ask the LLM for a bounded per-sub-theme adjustment.

    headlines is injected (not fetched here) so this is fully testable. Returns
    a NewsAdjustment dict (always ok=True unless the LLM call itself fails).
    """
    as_of = _dt.date.today().isoformat()
    if not headlines:
        return ok(total_adjustment=0.0, per_sub_theme=[], headlines=[], as_of=as_of)

    api_key = get_secret("GROQ_API_KEY")
    if not api_key:
        return err(NEWS_LLM_FAILED, "News overlay needs the Groq API key.")

    # Build a compact, indexed headline list for the prompt.
    hlines = "\n".join(
        f"[{i}] ({h.get('date','')}) {h.get('title','')} - {h.get('domain','')}"
        for i, h in enumerate(headlines)
    )
    subs = ", ".join(s.get("sub_industry", "") for s in taxonomy.get("layer1", []))
    companies = ", ".join(portfolio_companies[:60])
    user = (f"Theme: {theme}\nSub-industries: {subs}\n"
            f"Portfolio companies: {companies}\n\nRecent headlines:\n{hlines}")

    try:
        client = _client
        if client is None:
            from groq import Groq
            client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=TAXONOMY_MODEL,
            messages=[{"role": "system", "content": _NEWS_SYSTEM_PROMPT},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0, max_tokens=1500,
        )
        raw = resp.choices[0].message.content or ""
        obj = json.loads(raw)
    except Exception as exc:
        return err(NEWS_LLM_FAILED, "Could not compute the news overlay.", detail=str(exc))

    per_sub = _validate_assessments(obj, taxonomy, headlines)

    # Enforce the overall cap: if the summed magnitude exceeds the total cap,
    # scale all adjustments down proportionally (Req 16.5).
    total = sum(p["adjustment"] for p in per_sub)
    if abs(total) > NEWS_ADJUSTMENT_CAP and total != 0:
        scale = NEWS_ADJUSTMENT_CAP / abs(total)
        for p in per_sub:
            p["adjustment"] = round(p["adjustment"] * scale, 2)
        total = sum(p["adjustment"] for p in per_sub)

    return ok(total_adjustment=round(total, 2), per_sub_theme=per_sub,
              headlines=headlines, as_of=as_of)


# ---------------------------------------------------------------------------
# Orchestration + per-(theme, day) cache (Req 16.8: stable within a day)
# ---------------------------------------------------------------------------
_overlay_cache: dict[str, dict] = {}


def news_overlay(theme: str, taxonomy: dict, portfolio_companies: list,
                 lookback_days: int = 7, force_refresh: bool = False) -> dict:
    """Fetch headlines (within lookback window) + compute the bounded overlay.

    Cached by (theme, lookback, today) so the point-in-time overlay is stable
    within a day. Returns a NewsAdjustment dict, or a handled error.
    """
    cache_key = (f"{theme.strip().lower()}|{lookback_days}|"
                 f"{_dt.date.today().isoformat()}")
    if not force_refresh and cache_key in _overlay_cache:
        return _overlay_cache[cache_key]

    fetched = fetch_headlines(theme, lookback_days=lookback_days)
    if not fetched["ok"]:
        return fetched  # NEWS_UNAVAILABLE - UI falls back to baseline (Req 16.9)

    result = compute_news_adjustment(theme, taxonomy, fetched["headlines"],
                                     portfolio_companies)
    if result["ok"]:
        result["source"] = fetched.get("source")
        result["lookback_days"] = lookback_days
        result["fetched_count"] = fetched.get("fetched_count")
        result["window_count"] = fetched.get("window_count")
        _overlay_cache[cache_key] = result
    return result
