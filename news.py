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


def fetch_headlines(theme: str) -> dict:
    """Fetch recent headlines for a theme from the best available source.

    Order: NewsAPI (if key present) -> GDELT fallback. Returns a handled error
    if all sources fail, so the UI can show the baseline normally (Req 16.9).
    """
    # Preferred: newsdata.io (works server-side, incl. Streamlit Cloud).
    newsdata_key = get_secret("NEWSDATA_API_KEY")
    if newsdata_key:
        try:
            heads = _fetch_newsdata(theme, newsdata_key)
            if heads:
                return ok(headlines=heads[:_MAX_HEADLINES], source="newsdata.io")
        except Exception:
            pass  # fall through
    # Secondary: NewsAPI (dev/localhost only on free tier).
    newsapi_key = get_secret("NEWSAPI_KEY")
    if newsapi_key:
        try:
            heads = _fetch_newsapi(theme, newsapi_key)
            if heads:
                return ok(headlines=heads[:_MAX_HEADLINES], source="NewsAPI")
        except Exception:
            pass
    # Fallback: GDELT (free, no key, but rate-limited).
    try:
        heads = _fetch_gdelt(theme)
        return ok(headlines=heads[:_MAX_HEADLINES], source="GDELT")
    except Exception as exc:
        return err(NEWS_UNAVAILABLE,
                   "Live news is temporarily unavailable; showing filings-based exposure only.",
                   detail=str(exc))


# ---------------------------------------------------------------------------
# LLM adjustment (takes headlines as input -> injectable & testable)
# ---------------------------------------------------------------------------
_NEWS_SYSTEM_PROMPT = (
    "You are an equity strategist assessing how RECENT NEWS changes a "
    "portfolio's exposure to an investment theme, relative to a slow-moving "
    "filings-based baseline. Reason about the VALUE CHAIN, not sentiment: an "
    "event propagates to the kinds of businesses it affects (e.g. an oil-price "
    "shock raises exposure for energy-derived names; rare-earth export curbs "
    "affect chip supply chains).\n"
    "You are given the theme's sub-industries, a list of dated headlines (each "
    "with an index), and the portfolio company names. Output ONLY a JSON object "
    "with key \"adjustments\": an array where each item has \"sub_industry\" "
    "(must match one given), \"adjustment\" (signed number of PERCENTAGE "
    "POINTS, typically between -6 and 6; positive = more exposed), \"rationale\" "
    "(one sentence citing the event), \"cited_headlines\" (array of integer "
    "indices into the headlines list), and \"affected_companies\" (array of the "
    "given company names most affected). Only include sub-industries genuinely "
    "affected by the news; omit the rest. If no headline is materially relevant, "
    "return an empty array."
)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _validate_adjustments(obj, taxonomy: dict, headlines: list) -> list:
    """Validate + clamp the LLM output into clean per-sub-theme adjustments."""
    if not isinstance(obj, dict):
        return []
    valid_subs = {s.get("sub_industry") for s in taxonomy.get("layer1", [])}
    items = obj.get("adjustments")
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sub = it.get("sub_industry")
        if sub not in valid_subs:
            continue
        try:
            adj = float(it.get("adjustment", 0))
        except (TypeError, ValueError):
            continue
        adj = _clamp(round(adj, 2), -_PER_SUB_CAP, _PER_SUB_CAP)
        cites = [i for i in it.get("cited_headlines", [])
                 if isinstance(i, int) and 0 <= i < len(headlines)]
        companies = [c for c in it.get("affected_companies", []) if isinstance(c, str)]
        out.append({
            "sub_industry": sub,
            "adjustment": adj,
            "rationale": str(it.get("rationale", "")).strip()[:300],
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

    per_sub = _validate_adjustments(obj, taxonomy, headlines)

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
                 force_refresh: bool = False) -> dict:
    """Fetch headlines + compute the bounded per-sub-theme adjustment.

    Cached by (theme, today) so the point-in-time overlay is stable within a
    day. Returns a NewsAdjustment dict, or a handled error (UI shows baseline).
    """
    cache_key = f"{theme.strip().lower()}|{_dt.date.today().isoformat()}"
    if not force_refresh and cache_key in _overlay_cache:
        return _overlay_cache[cache_key]

    fetched = fetch_headlines(theme)
    if not fetched["ok"]:
        return fetched  # NEWS_UNAVAILABLE - UI falls back to baseline (Req 16.9)

    result = compute_news_adjustment(theme, taxonomy, fetched["headlines"],
                                     portfolio_companies)
    if result["ok"]:
        result["source"] = fetched.get("source")
        _overlay_cache[cache_key] = result
    return result
