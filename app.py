"""app.py - ThemeIQ: Schroders-branded Streamlit portal for thematic exposure analysis.

The only module that imports streamlit. All analysis logic lives in engine.py.
Phase 1: runs locally with no login. Renders handled errors via the engine's
{"ok": bool} convention and never crashes on expected failures.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from streamlit_echarts import st_echarts
from streamlit_option_menu import option_menu

import engine
import news as news_mod
import supabase_client as sb
import theme as T

DEFAULT_SEGMENTS = "data/spglobal_segments.xlsx"


# ---------------------------------------------------------------------------
# Cached data loaders
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _load_segments():
    return engine.load_segments(DEFAULT_SEGMENTS)


def _groq_ok() -> bool:
    return bool(engine.get_secret("GROQ_API_KEY"))


def _confidence_label(conf: float) -> str:
    if conf >= 0.66:
        return "High"
    if conf >= 0.33:
        return "Medium"
    return "Low"


# ---------------------------------------------------------------------------
# Authentication (Phase 2 - only active when Supabase is configured)
# ---------------------------------------------------------------------------
def _is_logged_in() -> bool:
    return bool(st.session_state.get("auth"))


def render_login_gate() -> bool:
    """Render login/signup. Returns True if the user may proceed.

    If Supabase is not configured, returns True immediately (anonymous Phase 1,
    Req 11.5). If configured, requires login before showing the app.
    """
    if not sb.is_configured():
        return True  # anonymous mode - full Phase 1 experience
    if _is_logged_in():
        return True

    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.markdown('<div class="panel" style="margin-top:8px;">', unsafe_allow_html=True)
        st.markdown('<div class="panel-title">Sign in to ThemeIQ</div>'
                    '<div class="panel-sub">Log in to save and auto-load your portfolios</div>',
                    unsafe_allow_html=True)
        tab_login, tab_signup = st.tabs(["Log in", "Create account"])
        with tab_login:
            email = st.text_input("Email", key="li_email")
            pw = st.text_input("Password", type="password", key="li_pw")
            if st.button("Log in", type="primary", use_container_width=True):
                res = sb.sign_in(email.strip(), pw)
                if res["ok"]:
                    st.session_state["auth"] = {"user": res["user"], "session": res["session"]}
                    st.rerun()
                else:
                    st.error(res["message"])
        with tab_signup:
            email2 = st.text_input("Email", key="su_email")
            pw2 = st.text_input("Password (min 6 chars)", type="password", key="su_pw")
            if st.button("Create account", use_container_width=True):
                res = sb.sign_up(email2.strip(), pw2)
                if res["ok"]:
                    st.session_state["auth"] = {"user": res["user"], "session": res["session"]}
                    st.rerun()
                else:
                    st.error(res["message"])
        st.markdown('</div>', unsafe_allow_html=True)
    return False


def _auto_load_saved_portfolio():
    """On first login, auto-load the user's most recent saved portfolio (Req 12.2)."""
    if not _is_logged_in() or st.session_state.get("_autoloaded"):
        return
    st.session_state["_autoloaded"] = True
    res = sb.load_latest_portfolio(st.session_state["auth"]["session"])
    if res["ok"] and res.get("portfolio") is not None:
        st.session_state["portfolio_df"] = res["portfolio"]
        st.session_state["portfolio_label"] = f"Saved - {res.get('name') or 'my portfolio'}"


def render_account_sidebar():
    """Account controls in the sidebar (only when logged in)."""
    if not _is_logged_in():
        return
    with st.sidebar:
        st.markdown("### Account")
        if st.button("Save current portfolio to my profile", use_container_width=True):
            df = st.session_state.get("portfolio_df")
            if df is None:
                st.warning("No portfolio to save.")
            else:
                name = st.session_state.get("portfolio_label") or "my portfolio"
                r = sb.save_portfolio(st.session_state["auth"]["session"], name, df)
                st.success("Saved to your profile.") if r["ok"] else st.error(r["message"])
        st.caption("Logged-in users auto-load their most recent saved portfolio on sign-in.")
        st.divider()


# ---------------------------------------------------------------------------
# Branded chrome
# ---------------------------------------------------------------------------
def render_header():
    groq = "Connected" if _groq_ok() else "No API key"
    pill_color = T.GOLD if _groq_ok() else T.BAD

    # Right-side chrome: Groq status + (when logged in) the user's email.
    right = f'<div class="sch-pill">Groq&nbsp;API&nbsp;<b style="color:{pill_color}">&#9679; {groq}</b></div>'
    if _is_logged_in():
        email = st.session_state["auth"]["user"].get("email", "account")
        right = (f'<div class="sch-pill">&#128100; <b style="color:{T.TEXT}">{email}</b></div>'
                 + right)

    st.markdown(
        f"""
        <div class="sch-header">
          <div class="sch-brand">
            <div class="sch-logo">IQ</div>
            <div>
              <div class="sch-title">ThemeIQ</div>
              <div class="sch-sub">AI-Powered Thematic Exposure &middot; Schroders</div>
            </div>
          </div>
          <div style="display:flex;gap:10px;align-items:center;">{right}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # A real logout button, right-aligned just under the header bar.
    if _is_logged_in():
        spacer, btn = st.columns([6, 1])
        with btn:
            if st.button("Log out", use_container_width=True, key="header_logout"):
                sb.sign_out()
                for k in ("auth", "_autoloaded", "analysis", "news", "news_window",
                          "portfolio_df", "portfolio_label", "_last_upload"):
                    st.session_state.pop(k, None)
                st.rerun()


def _init_portfolio_state():
    """Initialize portfolio session state. No portfolio is loaded until the
    user uploads one (there is no preloaded default)."""
    if "portfolio_df" not in st.session_state:
        st.session_state["portfolio_df"] = None
        st.session_state["portfolio_label"] = None


def _handle_upload(uploaded, container):
    """Apply an uploaded portfolio, keeping the previous one on failure (Req 1.6)."""
    if uploaded is None or uploaded == st.session_state.get("_last_upload"):
        return
    st.session_state["_last_upload"] = uploaded
    res = engine.load_portfolio(uploaded)
    if res["ok"]:
        st.session_state["portfolio_df"] = res["portfolio"]
        st.session_state["portfolio_label"] = f"Uploaded - {uploaded.name}"
        container.success(f"Loaded {uploaded.name}")
    else:
        container.error(res["message"])  # keep previous portfolio


def render_portfolio_panel():
    """Portfolio status + upload shown on the MAIN page so it is never hidden."""
    df = st.session_state.get("portfolio_df")
    n = len(df) if df is not None else 0
    label = st.session_state.get("portfolio_label") or "none uploaded yet"
    with st.expander(f"Portfolio: {label}  ({n} holdings)", expanded=(df is None)):
        up = st.file_uploader("Upload a portfolio (CSV or XLSX)", type=["csv", "xlsx"],
                              key="main_uploader")
        _handle_upload(up, st)
        st.caption("Upload a holdings file (columns: Company, Bloomberg ticker, "
                   "Country, Sector, SEDOL, Portfolio wt, Reference wt, Active wt) "
                   "to run an analysis.")


def render_sidebar():
    with st.sidebar:
        st.markdown("### Portfolio")
        uploaded = st.file_uploader("Upload (CSV or XLSX)", type=["csv", "xlsx"],
                                    key="sidebar_uploader")
        _handle_upload(uploaded, st.sidebar)

        label = st.session_state.get("portfolio_label") or "none uploaded yet"
        df = st.session_state.get("portfolio_df")
        n = len(df) if df is not None else 0
        st.caption(f"Active: {label}")
        st.caption(f"{n} holdings loaded")

        st.divider()
        st.caption(
            "Exposure is computed deterministically from reported S&P segment "
            "revenue using offline embeddings. The LLM only expands the theme "
            "into sub-themes. Same theme -> same result."
        )


# ---------------------------------------------------------------------------
# ECharts visuals
# ---------------------------------------------------------------------------
def _gauge(value: float, title: str) -> dict:
    return {
        "series": [{
            "type": "gauge", "startAngle": 210, "endAngle": -30,
            "min": 0, "max": 100, "radius": "92%",
            "progress": {"show": True, "width": 14,
                         "itemStyle": {"color": T.BLUE}},
            "axisLine": {"lineStyle": {"width": 14, "color": [[1, T.PANEL_2]]}},
            "axisTick": {"show": False},
            "splitLine": {"length": 10, "lineStyle": {"color": T.BORDER}},
            "axisLabel": {"color": T.MUTED, "fontSize": 10, "distance": 14},
            "pointer": {"itemStyle": {"color": T.GOLD}},
            "anchor": {"show": True, "size": 10, "itemStyle": {"color": T.GOLD}},
            "title": {"show": True, "offsetCenter": [0, "72%"],
                      "color": T.MUTED, "fontSize": 12},
            "detail": {"valueAnimation": True, "fontSize": 30, "fontWeight": "bolder",
                       "offsetCenter": [0, "40%"], "color": T.TEXT,
                       "formatter": "{value}%"},
            "data": [{"value": round(value), "name": title}],
        }]
    }


def _donut(sub_exposure: dict) -> dict:
    items = [{"value": round(v, 2), "name": k} for k, v in sub_exposure.items() if v > 0]
    return {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c}% ({d}%)"},
        "legend": {"bottom": 0, "textStyle": {"color": T.MUTED, "fontSize": 11}},
        "color": T.CHART_RAMP,
        "series": [{
            "type": "pie", "radius": ["48%", "72%"], "center": ["50%", "44%"],
            "avoidLabelOverlap": True,
            "itemStyle": {"borderColor": T.PANEL, "borderWidth": 2},
            "label": {"show": False},
            "data": items or [{"value": 1, "name": "No exposure"}],
        }]
    }


def _hbar(contribs: list) -> dict:
    rows = [c for c in contribs if c["company_exposure"] > 0][:10][::-1]
    names = [c["company"][:22] for c in rows]
    vals = [round(c["company_exposure"]) for c in rows]
    colors = [T.BAND_COLORS.get(c.get("exposure_band", ""), T.BLUE) for c in rows]
    data = [{"value": v, "itemStyle": {"color": col}} for v, col in zip(vals, colors)]
    return {
        "grid": {"left": 150, "right": 30, "top": 10, "bottom": 20},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"},
                    "formatter": "{b}: {c}%"},
        "xAxis": {"type": "value", "max": 100,
                  "axisLabel": {"color": T.MUTED}, "splitLine": {"lineStyle": {"color": T.BORDER}}},
        "yAxis": {"type": "category", "data": names,
                  "axisLabel": {"color": T.TEXT, "fontSize": 11},
                  "axisLine": {"lineStyle": {"color": T.BORDER}}},
        "series": [{"type": "bar", "data": data, "barWidth": "60%",
                    "label": {"show": True, "position": "right",
                              "formatter": "{c}%", "color": T.MUTED}}],
    }


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def _kpi_card(col, label, value, sub, cls=""):
    col.markdown(
        f"""<div class="kpi {cls}">
              <div class="kpi-label">{label}</div>
              <div class="kpi-value">{value}</div>
              <div class="kpi-sub">{sub}</div>
            </div>""",
        unsafe_allow_html=True,
    )


def section_overview(analysis: dict):
    m = analysis["metrics"]
    pexp = m["portfolio_thematic_exposure"]
    bexp = m["benchmark_exposure"]
    tilt = m["active_tilt"]
    direction = "Overweight" if tilt > 0 else ("Underweight" if tilt < 0 else "Neutral")
    tilt_cls = "pos" if tilt > 0 else ("neg" if tilt < 0 else "")

    c1, c2, c3 = st.columns(3)
    _kpi_card(c1, "Portfolio Thematic Exposure", f"{pexp:.0f}%",
              f"{T.band_badge(m.get('portfolio_band','-'))} &nbsp; exact {pexp:.2f}%", "kpi-accent")
    _kpi_card(c2, "Benchmark Exposure", f"{bexp:.0f}%",
              f"{T.band_badge(m.get('benchmark_band','-'))} &nbsp; exact {bexp:.2f}%")
    _kpi_card(c3, "Active Thematic Tilt", f"{tilt:+.0f}%",
              f"<span class='{tilt_cls}'>{direction}</span> vs benchmark &middot; exact {tilt:+.2f}%", "kpi-gold")

    st.write("")
    left, right = st.columns(2)
    with left:
        st.markdown('<div class="panel-title">Portfolio Exposure</div>'
                    '<div class="panel-sub">Share of portfolio revenue aligned to the theme</div>',
                    unsafe_allow_html=True)
        st_echarts(_gauge(pexp, "Exposure"), height="300px")
    with right:
        st.markdown('<div class="panel-title">Exposure by Sub-Theme</div>'
                    '<div class="panel-sub">How the thematic exposure decomposes</div>',
                    unsafe_allow_html=True)
        st_echarts(_donut(m.get("sub_theme_exposure", {})), height="300px")

    _render_coverage(m.get("coverage", {}))
    _render_methodology(analysis.get("metadata", {}))


def _render_coverage(cov: dict):
    """Data-coverage transparency: what was classified vs flagged."""
    if not cov:
        return
    st.write("")
    st.markdown('<div class="panel-title">Data Coverage</div>'
                '<div class="panel-sub">What the analysis could and could not classify</div>',
                unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    _kpi_card(c1, "Holdings", str(cov.get("total_companies", 0)), "in portfolio")
    _kpi_card(c2, "Classified", str(cov.get("matched_with_exposure", 0)),
              "segment data matched the theme", "kpi-accent")
    _kpi_card(c3, "No theme match", str(cov.get("matched_no_exposure", 0)),
              "had segments, none aligned")
    _kpi_card(c4, "No segment data", str(cov.get("no_segment_data", 0)),
              "not in the S&P export")

    no_seg = cov.get("no_segment_companies", [])
    insuf = cov.get("insufficient_companies", [])
    if no_seg or insuf:
        with st.expander("Show flagged companies (not silently hidden)"):
            if no_seg:
                st.markdown("**No S&P segment data** (exposure set to 0, excluded from classification):")
                st.write(", ".join(no_seg))
            if insuf:
                st.markdown("**Had segment data but nothing matched this theme** (0% exposure):")
                st.write(", ".join(insuf))


def _render_methodology(meta: dict):
    """Plain-English 'how this was computed' trace."""
    if not meta:
        return
    with st.expander("How this was computed (methodology & assumptions)"):
        st.markdown(meta.get("notes", ""))
        a, b = st.columns(2)
        a.markdown(
            f"- **Taxonomy model (LLM):** `{meta.get('taxonomy_model','-')}`\n"
            f"- **Embedding model:** `{meta.get('embedding_model','-')}` (offline, deterministic)\n"
            f"- **Similarity threshold:** {meta.get('similarity_threshold','-')} "
            "(segments below this score 0)"
        )
        bands = meta.get("exposure_bands", [])
        band_txt = "  ·  ".join(f"**{label}** &ge;{int(low)}%" for low, label in bands)
        b.markdown(
            f"- **Company matching:** normalized + fuzzy (threshold {meta.get('fuzzy_match_threshold','-')})\n"
            f"- **Exposure bands:** {band_txt}\n"
            "- **Reproducibility:** same theme is cached, so results do not drift run-to-run.",
            unsafe_allow_html=True,
        )


def section_taxonomy(analysis: dict):
    taxonomy = analysis["taxonomy"]
    m = analysis["metrics"]
    sub_exposure = m.get("sub_theme_exposure", {})
    total = sum(sub_exposure.values()) or 1.0

    st.markdown(f'<div class="panel-title">{taxonomy.get("theme","Theme")}</div>'
                '<div class="panel-sub">Theme &rarr; sub-themes &rarr; activities, '
                'with portfolio exposure attributed to each sub-theme</div>',
                unsafe_allow_html=True)

    # Ordered by exposure (inverted-pyramid feel: biggest driver on top).
    subs = sorted(taxonomy.get("layer1", []),
                  key=lambda s: sub_exposure.get(s["sub_industry"], 0.0), reverse=True)
    for sub in subs:
        name = sub["sub_industry"]
        exp = sub_exposure.get(name, 0.0)
        share = min(1.0, exp / total)
        chips = "".join(f'<span class="chip">{a}</span>' for a in sub.get("activities", []))
        bar = f"""<div style="height:6px;background:{T.PANEL_2};border-radius:4px;margin:8px 0 6px;">
                    <div style="height:6px;width:{share*100:.0f}%;background:{T.BLUE};border-radius:4px;"></div>
                  </div>"""
        st.markdown(
            f"""<div class="sub-row">
                  <div style="display:flex;justify-content:space-between;align-items:center;">
                    <span class="sub-name">{name}</span>
                    <span class="sub-exp">{exp:.1f}%</span>
                  </div>
                  {bar}
                  <div>{chips}</div>
                </div>""",
            unsafe_allow_html=True,
        )


def section_contributors(analysis: dict):
    m = analysis["metrics"]
    st.markdown('<div class="panel-title">Top Contributors</div>'
                '<div class="panel-sub">Ranked by contribution to portfolio thematic exposure</div>',
                unsafe_allow_html=True)
    st_echarts(_hbar(m["contributions"]), height="340px")

    rows = [c for c in m["contributions"] if c["company_exposure"] > 0] or m["contributions"]
    df = pd.DataFrame([{
        "Company": c["company"],
        "Sector": c["sector"],
        "Portfolio Wt %": round(c["portfolio_wt"] * 100, 1),
        "Exposure %": round(c["company_exposure"]),
        "Band": c.get("exposure_band", "-"),
        "Contribution %": round(c["contribution"], 2),
        "Confidence": _confidence_label(c.get("confidence", 0.0)),
    } for c in rows])
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption("Exposure shown as whole-number %; exact values drive all portfolio math.")


def section_company(analysis: dict):
    scores = analysis["scores"]
    companies = sorted(scores.keys())
    choice = st.selectbox("Select a company", companies)
    sr = scores[choice]

    if not sr["has_segment_data"]:
        st.warning(f"{choice}: no S&P segment data available - flagged, exposure 0%.")
        return

    c1, c2, c3 = st.columns(3)
    _kpi_card(c1, "Company Exposure", f"{sr['company_exposure']:.0f}%",
              f"{T.band_badge(sr.get('exposure_band','-'))} &nbsp; exact {sr['company_exposure']:.2f}%", "kpi-accent")
    _kpi_card(c2, "Confidence", _confidence_label(sr.get("confidence", 0.0)),
              f"score {sr.get('confidence',0.0):.2f}")
    _kpi_card(c3, "Matched Sub-Themes", str(len(sr["matched_sub_industries"])),
              ", ".join(sr["matched_sub_industries"]) or "none")

    if sr.get("insufficient_data"):
        st.warning("Insufficient data: no segment confidently matched this theme.")

    # Confidence rationale in plain English (why High/Medium/Low).
    matched_rev = sum(b["revenue_pct"] for b in sr["segment_breakdown"]
                      if b["alignment_score"] > 0)
    st.caption(
        f"Confidence rationale: {matched_rev:.0f}% of this company's segment revenue "
        f"matched the theme, at a revenue-weighted alignment of {sr.get('confidence',0.0):.2f}."
    )

    st.write("")
    left, right = st.columns([1, 1])
    with left:
        attr = sr.get("sub_theme_attribution", {})
        st.markdown('<div class="panel-title">Exposure by Sub-Theme</div>', unsafe_allow_html=True)
        if attr:
            st_echarts(_donut(attr), height="260px")
        else:
            st.caption("No sub-theme attribution for this company.")
    with right:
        st.markdown('<div class="panel-title">Segment Revenue &amp; Alignment</div>'
                    '<div class="panel-sub">Per-segment similarity (0-1) to the best-matching '
                    'sub-theme &mdash; auditable</div>', unsafe_allow_html=True)
        seg_df = pd.DataFrame([{
            "Segment": (b["name"] or f"Segment {i+1}"),
            "Revenue Weight %": round(b["revenue_pct"], 1),
            "Similarity": b["alignment_score"],
            "Matched Sub-Theme": b.get("matched_sub_industry") or "-",
        } for i, b in enumerate(sr["segment_breakdown"])])
        if not seg_df.empty:
            st.dataframe(seg_df, use_container_width=True, hide_index=True)

    if sr["evidence_snippet"]:
        st.markdown('<div class="panel-title">Supporting Evidence</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="panel" style="border-left:4px solid {T.GOLD};">'
            f'<em>&ldquo;{sr["evidence_snippet"]}&rdquo;</em></div>',
            unsafe_allow_html=True,
        )

    # Source traceability: show where the data came from (Schroders brief).
    with st.expander("Source & traceability"):
        st.markdown(
            f"- **S&P Capital IQ entity:** {sr.get('entity_name') or 'n/a'}\n"
            f"- **Portfolio -> S&P match method:** `{sr.get('match_method','n/a')}`"
            + (f" (fuzzy score {sr.get('match_score')})" if sr.get('match_score') else "") + "\n"
            f"- **Segments read from the Capital IQ export:** "
            f"{len([b for b in sr['segment_breakdown'] if b['name']])}\n"
            "- Revenue weights come from reported segment revenue (negatives and "
            "elimination/write-off lines are cleaned to zero before weighting)."
        )


_LOOKBACK_OPTIONS = {"Today": 1, "Last 3 days": 3, "Last week": 7, "Last month": 30}


def section_news(analysis: dict):
    """News & Events: baseline vs news-adjusted exposure (Req 16, rubric-based)."""
    m = analysis["metrics"]
    taxonomy = analysis["taxonomy"]
    theme = taxonomy.get("theme", "")
    baseline = m["portfolio_thematic_exposure"]

    st.markdown('<div class="panel-title">News-Driven Exposure Overlay</div>'
                '<div class="panel-sub">Recent events applied as a bounded, '
                'rubric-based adjustment on top of the filings-based baseline. '
                'Point-in-time signal, shown separately from the deterministic '
                'baseline.</div>', unsafe_allow_html=True)

    c1, c2 = st.columns([1.4, 1])
    window_label = c1.select_slider("News lookback window",
                                    options=list(_LOOKBACK_OPTIONS.keys()),
                                    value="Last week")
    lookback = _LOOKBACK_OPTIONS[window_label]
    refresh = c2.button("Fetch news & compute overlay", type="primary", use_container_width=True)

    st.caption("Note: the free news API returns only the most recent articles, so "
               "longer windows (e.g. Last month) are shown for completeness but in "
               "practice return mostly recent items. With full API access the window "
               "would scan the entire selected period.")

    # Recompute when the user refreshes or changes the window.
    cache = analysis.get("news")
    cached_window = analysis.get("news_window")
    if refresh or cache is None or cached_window != lookback:
        with st.spinner("Fetching headlines and assessing impact against the rubric..."):
            companies = [c["company"] for c in m["contributions"]]
            res = news_mod.news_overlay(theme, taxonomy, companies,
                                        lookback_days=lookback, force_refresh=refresh)
        analysis["news"] = res
        analysis["news_window"] = lookback
        st.session_state["analysis"] = analysis
        cache = res

    if not cache or not cache.get("ok"):
        msg = (cache or {}).get("message", "News overlay is unavailable right now.")
        st.info(f"{msg}  Baseline exposure stands at {baseline:.0f}%.")
        _render_rubric()
        return

    total = cache.get("total_adjustment", 0.0)
    adjusted = max(0.0, min(100.0, baseline + total))

    k1, k2, k3 = st.columns(3)
    _kpi_card(k1, "Baseline Exposure", f"{baseline:.0f}%", "from S&P filings", "kpi-accent")
    sign = "+" if total >= 0 else ""
    tone = "pos" if total > 0 else ("neg" if total < 0 else "")
    _kpi_card(k2, "News Adjustment", f"<span class='{tone}'>{sign}{total:.1f}%</span>",
              f"bounded to +/-{int(news_mod.NEWS_ADJUSTMENT_CAP)} pts (rubric)")
    _kpi_card(k3, "News-Adjusted Exposure", f"{adjusted:.0f}%",
              f"as of {cache.get('as_of','today')} &middot; {cache.get('source','')}", "kpi-gold")

    wc = cache.get("window_count")
    fc = cache.get("fetched_count")
    if wc is not None:
        st.caption(f"{wc} headline(s) within '{window_label}' "
                   f"(of {fc} most-recent fetched) via {cache.get('source','')}.")

    per = cache.get("per_sub_theme", [])
    heads = cache.get("headlines", [])
    if not per:
        st.success("No recent events materially change exposure to this theme. "
                   "News-adjusted exposure equals the baseline.")
    else:
        st.write("")
        st.markdown('<div class="panel-title">Per Sub-Theme Impact</div>'
                    '<div class="panel-sub">Direction &amp; strength are judged by the '
                    'model; the point value comes from the rubric below.</div>',
                    unsafe_allow_html=True)
        for p in per:
            adj = p["adjustment"]
            tone = "pos" if adj > 0 else ("neg" if adj < 0 else "")
            direction = p.get("direction", "Neutral")
            strength = p.get("strength", "None")
            cited = "".join(
                f'<div class="chip">{heads[i]["date"]} &middot; {heads[i]["domain"]}</div>'
                for i in p.get("cited_headlines", []) if i < len(heads)
            )
            affected = ", ".join(p.get("affected_companies", [])) or "none identified"
            st.markdown(
                f"""<div class="sub-row">
                      <div style="display:flex;justify-content:space-between;align-items:center;">
                        <span class="sub-name">{p['sub_industry']}</span>
                        <span class="sub-exp {tone}">{direction} &middot; {strength} &rarr; {'+' if adj>=0 else ''}{adj:.0f} pts</span>
                      </div>
                      <div style="color:{T.MUTED};font-size:13px;margin:6px 0;">{p['rationale']}</div>
                      <div style="font-size:12px;color:{T.MUTED};"><b>Affected:</b> {affected}</div>
                      <div style="margin-top:6px;">{cited}</div>
                    </div>""",
                unsafe_allow_html=True,
            )

    if heads:
        with st.expander(f"All {len(heads)} headlines used"):
            for i, h in enumerate(heads):
                title = h.get("title", "")
                url = h.get("url", "")
                link = f"[{title}]({url})" if url else title
                st.markdown(f"{i}. {h.get('date','')} &middot; *{h.get('domain','')}* - {link}")

    _render_rubric()


def _render_rubric():
    """Show the scoring rubric so the analyst can audit where numbers come from."""
    r = news_mod.RUBRIC
    with st.expander("Scoring rubric - how the adjustment is calculated"):
        st.markdown(f"**What the model judges:** {r['what_llm_judges']}")
        st.markdown("**Strength definitions:**")
        for k, v in r["strength_definitions"].items():
            st.markdown(f"- **{k}** ({r['point_mapping'][k]}): {v}")
        st.markdown(
            f"**Direction** sets the sign: Tailwind = positive (more exposed), "
            f"Headwind = negative, Neutral = 0.  \n"
            f"**Caps:** per sub-theme {r['caps']['per_sub_theme']}; total "
            f"{r['caps']['total']}.")
        st.info(r["note"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="ThemeIQ", layout="wide",
                       page_icon="📊", initial_sidebar_state="expanded")
    st.markdown(T.inject_css(), unsafe_allow_html=True)

    render_header()

    # Phase 2 auth gate (no-op when Supabase is not configured - Req 11.5).
    if not render_login_gate():
        return

    _init_portfolio_state()
    _auto_load_saved_portfolio()
    render_account_sidebar()
    render_sidebar()
    render_portfolio_panel()

    # Theme input row.
    if "theme" not in st.session_state:
        st.session_state["theme"] = ""
    tc = st.columns([5, 1, 1.4])
    theme = tc[0].text_input(
        "Investment theme", value=st.session_state["theme"], label_visibility="collapsed",
        placeholder="Enter an investment theme, e.g. Critical Power Grid Modernization",
    )
    st.session_state["theme"] = theme
    analyze = tc[1].button("Analyze", type="primary", use_container_width=True)
    force_refresh = tc[2].checkbox("Regenerate", value=False,
                                   help="Bypass the cache and ask the model for a fresh taxonomy.")

    portfolio_df = st.session_state.get("portfolio_df")
    if portfolio_df is None:
        st.markdown(
            '<div class="panel" style="text-align:center;padding:40px;">'
            '<div class="panel-title" style="font-size:18px;">Upload a portfolio to begin</div>'
            '<div class="panel-sub">Use the <b>Portfolio</b> panel above to upload your '
            'holdings (CSV or XLSX). Once loaded, enter an investment theme and click '
            '<b>Analyze</b>.</div></div>',
            unsafe_allow_html=True,
        )
        return

    if analyze and not theme.strip():
        st.warning("Enter an investment theme to analyze.")

    seg_res = _load_segments()
    if not seg_res["ok"]:
        st.error(seg_res["message"])
        return
    segments = seg_res["segments"]

    if analyze and theme.strip():
        with st.spinner("Expanding theme and scoring portfolio..."):
            tax_res = engine.generate_taxonomy(theme.strip(), force_refresh=force_refresh)
            if not tax_res["ok"]:
                # Clear any stale result so a rejected theme does not keep
                # showing an old analysis underneath the error.
                st.session_state.pop("analysis", None)
                if tax_res.get("error_code") == "INVALID_THEME":
                    st.warning(tax_res["message"])
                else:
                    st.error(tax_res["message"])
                return
            result = engine.analyze_portfolio(portfolio_df, segments, tax_res["taxonomy"])
        st.session_state["analysis"] = {
            "taxonomy": tax_res["taxonomy"],
            "metrics": result["metrics"],
            "scores": result["scores"],
            "metadata": result.get("metadata", {}),
            "cached": tax_res.get("cached", False),
        }

    analysis = st.session_state.get("analysis")
    if not analysis:
        st.markdown(
            '<div class="panel" style="text-align:center;padding:48px;">'
            '<div class="panel-title" style="font-size:18px;">Run a thematic audit</div>'
            '<div class="panel-sub">Enter an investment theme above and click '
            '<b>Analyze</b> to break it into sub-themes and measure portfolio exposure.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    if analysis.get("cached"):
        st.caption("Taxonomy served from cache (reproducible). Tick 'Regenerate' to refresh.")

    nav = option_menu(
        None, ["Overview", "Taxonomy", "Contributors", "Company Detail", "News & Events"],
        icons=["speedometer2", "diagram-3", "bar-chart-line", "building", "newspaper"],
        orientation="horizontal",
        styles={
            "container": {"background-color": T.PANEL, "border": f"1px solid {T.BORDER}",
                          "border-radius": "12px", "padding": "6px"},
            "nav-link": {"color": T.MUTED, "font-size": "14px", "font-weight": "600",
                         "--hover-color": T.PANEL_2},
            "nav-link-selected": {"background-color": T.BLUE, "color": "white"},
            "icon": {"color": T.GOLD},
        },
    )

    st.write("")
    if nav == "Overview":
        section_overview(analysis)
    elif nav == "Taxonomy":
        section_taxonomy(analysis)
    elif nav == "Contributors":
        section_contributors(analysis)
    elif nav == "Company Detail":
        section_company(analysis)
    elif nav == "News & Events":
        section_news(analysis)


if __name__ == "__main__":
    main()
