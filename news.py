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


def _fetch_currents(theme: str, api_key: str) -> list[dict]:
    """CurrentsAPI (currentsapi.services) - 250 req/day. Real keyword search
    (verified), returns a usable URL. /v1/search with keywords + apiKey."""
    url = "https://api.currentsapi.services/v1/search?" + urllib.parse.urlencode({
        "keywords": theme, "language": "en", "apiKey": api_key,
    })
    data = _http_json(url)
    if data.get("status") != "ok":
        return []
    out = []
    for a in data.get("news", []):
        out.append({
            "title": (a.get("title") or "").encode("utf-8", "ignore").decode("utf-8"),
            "domain": _domain_from_url(a.get("url") or "") or "",
            "date": (a.get("published") or "")[:10],
            "url": a.get("url") or "",
        })
    return out


def _domain_from_url(u: str) -> str:
    try:
        return urllib.parse.urlparse(u).netloc.replace("www.", "")
    except Exception:
        return ""


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
    # Preferred: CurrentsAPI - real keyword search (verified on-theme per query)
    # with a usable article URL, works server-side.
    currents_key = get_secret("CURRENTS_API_KEY")
    if currents_key:
        try:
            heads = _fetch_currents(theme, currents_key)
            if heads:
                raw, source = heads, "CurrentsAPI"
        except Exception:
            pass
    # Secondary: newsdata.io - also real keyword search, works server-side.
    if not raw:
        newsdata_key = get_secret("NEWSDATA_API_KEY")
        if newsdata_key:
            try:
                heads = _fetch_newsdata(theme, newsdata_key)
                if heads:
                    raw, source = heads, "newsdata.io"
            except Exception:
                pass
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
# Qualitative news signal (NO numbers added to exposure).
#
# News is a QUALITATIVE read only: per sub-theme, is it a Tailwind or Headwind,
# at Low / Medium / High intensity. These signals NEVER change the revenue-based
# exposure number. The overall portfolio read is a DETERMINISTIC weighted rollup
# of the sub-theme signals (weighted by each sub-theme's share of the
# revenue-based exposure), so it reflects where the money actually is and is
# reproducible for the same inputs.
# ---------------------------------------------------------------------------
DIRECTIONS = ("Tailwind", "Headwind", "Neutral")
STRENGTHS = ("High", "Medium", "Low", "None")

# Strength -> a small intensity weight used ONLY for the qualitative rollup
# (never added to exposure). Direction sets the sign of the signal.
_STRENGTH_WEIGHT = {"High": 3.0, "Medium": 2.0, "Low": 1.0, "None": 0.0}

RUBRIC = {
    "what_llm_judges": (
        "For each sub-theme, the model classifies recent news on two axes only: "
        "DIRECTION (Tailwind = the theme is under upward/supportive pressure, "
        "Headwind = under downward pressure, Neutral) and STRENGTH (how material "
        "and direct the event is to that sub-theme's revenue drivers)."
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
    "rollup": (
        "The overall portfolio signal is a DETERMINISTIC rollup of the per-sub-theme "
        "signals. Each sub-theme's signal (Tailwind +, Headwind -, scaled by "
        "High=3 / Medium=2 / Low=1) is weighted by that sub-theme's share of the "
        "revenue-based thematic exposure, so a signal on the sub-theme you are most "
        "exposed to counts most. The weighted net is mapped to an overall direction "
        "(Tailwind / Headwind / Neutral) and intensity (Low / Medium / High) by "
        "fixed thresholds. Identical signals and exposure weights always yield the "
        "identical overall read."
    ),
    "note": (
        "This is a QUALITATIVE signal only - it never adds or subtracts any "
        "percentage from the exposure. Exposure always comes solely from reported "
        "segment revenue. News tells you which way the theme is being pushed right "
        "now and where, with cited headlines; it is a point-in-time read that "
        "changes as the news changes."
    ),
}

_NEWS_SYSTEM_PROMPT = (
    "You are an equity strategist assessing how RECENT NEWS is pushing a "
    "portfolio's exposure to an investment theme - UP (tailwind) or DOWN "
    "(headwind). Reason about the VALUE CHAIN, not sentiment: an event "
    "propagates to the businesses it affects (e.g. an oil-price shock is a "
    "tailwind for energy-derived names; rare-earth export curbs are a headwind "
    "for chip supply chains).\n\n"
    "For each sub-industry, classify the recent news on TWO axes only:\n"
    "- direction: one of Tailwind (supportive/upward pressure), Headwind "
    "(downward pressure), Neutral.\n"
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


def _validate_assessments(obj, taxonomy: dict, headlines: list) -> list:
    """Validate LLM direction/strength output (no numbers; purely qualitative)."""
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
        if strength == "None" or direction == "Neutral":
            continue  # omit sub-themes with no material signal
        cites = [i for i in it.get("cited_headlines", [])
                 if isinstance(i, int) and 0 <= i < len(headlines)]
        companies = [c for c in it.get("affected_companies", []) if isinstance(c, str)]
        rationale = str(it.get("rationale", "")).strip()[:300]
        rationale = rationale.encode("utf-8", "ignore").decode("utf-8")
        out.append({
            "sub_industry": sub,
            "direction": direction,
            "strength": strength,
            "rationale": rationale,
            "cited_headlines": cites,
            "affected_companies": companies[:8],
        })
    return out


def _signed_weight(direction: str, strength: str) -> float:
    """Signal value for the rollup: +/- intensity weight (never added to exposure)."""
    mag = _STRENGTH_WEIGHT.get(strength, 0.0)
    return mag if direction == "Tailwind" else (-mag if direction == "Headwind" else 0.0)


def rollup_signal(per_sub: list, sub_theme_exposure: dict) -> dict:
    """Deterministic overall signal from per-sub-theme signals (option b).

    Each sub-theme's signed intensity is weighted by its SHARE of the
    revenue-based thematic exposure, then mapped to a Tailwind/Headwind +
    Low/Med/High label by fixed thresholds. Purely qualitative, reproducible.
    """
    if not per_sub:
        return {"direction": "Neutral", "strength": "None", "score": 0.0}

    exp = {k: max(0.0, float(v)) for k, v in (sub_theme_exposure or {}).items()}
    total_exp = sum(exp.values())

    net = 0.0
    for p in per_sub:
        sw = _signed_weight(p["direction"], p["strength"])
        if total_exp > 0:
            weight = exp.get(p["sub_industry"], 0.0) / total_exp
        else:
            # No exposure weights -> equal-weight the signalled sub-themes.
            weight = 1.0 / len(per_sub)
        net += sw * weight

    # net is in roughly [-3, +3] (signed intensity, exposure-weighted).
    direction = "Tailwind" if net > 0 else ("Headwind" if net < 0 else "Neutral")
    mag = abs(net)
    if mag >= 2.0:
        strength = "High"
    elif mag >= 1.0:
        strength = "Medium"
    elif mag > 0:
        strength = "Low"
    else:
        strength = "None"
    return {"direction": direction, "strength": strength, "score": round(net, 3)}


def compute_news_signal(theme: str, taxonomy: dict, headlines: list,
                        portfolio_companies: list, sub_theme_exposure: dict = None,
                        _client=None) -> dict:
    """Given headlines, produce a QUALITATIVE per-sub-theme news signal plus a
    deterministic overall rollup. NO numbers are added to exposure.

    headlines is injected (not fetched here) so this is fully testable.
    Returns {ok, per_sub_theme, overall, headlines, as_of}.
    """
    as_of = _dt.date.today().isoformat()
    if not headlines:
        return ok(per_sub_theme=[],
                  overall={"direction": "Neutral", "strength": "None", "score": 0.0},
                  headlines=[], as_of=as_of)

    api_key = get_secret("GROQ_API_KEY")
    if not api_key:
        return err(NEWS_LLM_FAILED, "News signal needs the Groq API key.")

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
        return err(NEWS_LLM_FAILED, "Could not compute the news signal.", detail=str(exc))

    per_sub = _validate_assessments(obj, taxonomy, headlines)
    overall = rollup_signal(per_sub, sub_theme_exposure or {})
    return ok(per_sub_theme=per_sub, overall=overall,
              headlines=headlines, as_of=as_of)


# ---------------------------------------------------------------------------
# Orchestration + per-(theme, day) cache (Req 16.8: stable within a day)
# ---------------------------------------------------------------------------
_overlay_cache: dict[str, dict] = {}


def news_overlay(theme: str, taxonomy: dict, portfolio_companies: list,
                 sub_theme_exposure: dict = None, lookback_days: int = 7,
                 force_refresh: bool = False) -> dict:
    """Fetch headlines (within lookback window) + compute the QUALITATIVE signal.

    Cached by (theme, lookback, today) so the signal is stable/reproducible for
    the same day. Returns the news-signal dict, or a handled error.
    """
    cache_key = (f"{theme.strip().lower()}|{lookback_days}|"
                 f"{_dt.date.today().isoformat()}")
    if not force_refresh and cache_key in _overlay_cache:
        return _overlay_cache[cache_key]

    fetched = fetch_headlines(theme, lookback_days=lookback_days)
    if not fetched["ok"]:
        return fetched  # NEWS_UNAVAILABLE - UI falls back to baseline (Req 16.9)

    result = compute_news_signal(theme, taxonomy, fetched["headlines"],
                                 portfolio_companies, sub_theme_exposure)
    if result["ok"]:
        result["source"] = fetched.get("source")
        result["lookback_days"] = lookback_days
        result["fetched_count"] = fetched.get("fetched_count")
        result["window_count"] = fetched.get("window_count")
        _overlay_cache[cache_key] = result
    return result
