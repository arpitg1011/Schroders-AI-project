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


# ---------------------------------------------------------------------------
# Taxonomy resolution (per-profile permanent cache + count slider)
# ---------------------------------------------------------------------------
def _rank_subs_by_exposure(layer1: list, portfolio_df, segments, theme: str) -> list:
    """Order a saved sub-theme list by the portfolio's exposure to each sub-theme
    (descending). Used to trim a larger saved taxonomy down to the top N.

    Falls back to the stored order if scoring is unavailable.
    """
    tax = {"theme": theme, "layer1": layer1}
    try:
        result = engine.analyze_portfolio(portfolio_df, segments, tax)
        sub_exp = result["metrics"].get("sub_theme_exposure", {}) or {}
    except Exception:
        return list(layer1)
    return sorted(layer1,
                  key=lambda s: sub_exp.get(s.get("sub_industry", ""), 0.0),
                  reverse=True)


def _resolve_taxonomy(theme: str, n: int, portfolio_df, segments,
                      force_refresh: bool) -> dict:
    """Decide which sub-themes to analyze, honouring the per-profile saved set.

    Rules:
      * Anonymous users -> the engine's file/mem cache (original behaviour),
        requesting exactly n sub-themes.
      * Logged-in users -> a permanent per-profile taxonomy in Supabase:
          - Regenerate ticked -> generate a fresh set of n and overwrite.
          - No saved set      -> generate n and save.
          - Saved M, want N==M-> reuse the saved set as-is.
          - Saved M, want N<M -> rank saved subs by exposure, return top N
                                  (the full saved set stays stored).
          - Saved M, want N>M -> keep all M, generate (N-M) NEW non-duplicate
                                  sub-themes, merge, and save the enlarged set.

    Returns the engine's {ok, taxonomy, cached} shape, optionally plus a
    "notice" string for the UI.
    """
    n = max(engine._MIN_SUBS, min(engine._MAX_SUBS, int(n)))

    # --- Global preset themes: canonical 10-sub taxonomy shipped for EVERYONE
    # (logged-in or not). Any count 3..10 is served by taking the top-N
    # sub-themes by portfolio exposure from the canonical 10 - instant, no LLM.
    # Regenerate still forces a fresh live generation (and, if logged in, saves
    # it to the profile, overriding the preset for that user). ---
    if not force_refresh:
        preset = engine.get_preset_taxonomy(theme)
        if preset is not None:
            canonical = preset["layer1"]
            if n >= len(canonical):
                layer1 = canonical
            else:
                ranked = _rank_subs_by_exposure(canonical, portfolio_df, segments, theme)
                layer1 = ranked[:n]
            return {"ok": True, "cached": True,
                    "taxonomy": {"theme": preset["theme"], "layer1": layer1}}

    # --- Anonymous: original file-cache behaviour, now count-aware. ---
    if not _is_logged_in():
        return engine.generate_taxonomy(theme, n=n, force_refresh=force_refresh)

    session = st.session_state["auth"]["session"]

    # --- Regenerate: fresh set, overwrite the saved taxonomy. ---
    if force_refresh:
        gen = engine.generate_taxonomy(theme, n=n, force_refresh=True)
        if not gen["ok"]:
            return gen
        sb.save_taxonomy(session, theme, gen["taxonomy"]["layer1"])
        return gen

    # --- Look up the saved per-profile taxonomy. ---
    saved = sb.load_taxonomy(session, theme)
    saved_layer1 = saved.get("layer1") if saved.get("ok") else None

    # No saved set yet -> generate n and persist.
    if not saved_layer1:
        gen = engine.generate_taxonomy(theme, n=n, force_refresh=force_refresh)
        if not gen["ok"]:
            return gen
        if saved.get("ok"):
            sb.save_taxonomy(session, theme, gen["taxonomy"]["layer1"])
        return gen

    m = len(saved_layer1)

    # Saved set already matches the requested count.
    if n == m:
        return {"ok": True, "cached": True,
                "taxonomy": {"theme": theme, "layer1": saved_layer1}}

    # Want fewer than saved -> rank by exposure, return the top N.
    if n < m:
        ranked = _rank_subs_by_exposure(saved_layer1, portfolio_df, segments, theme)
        return {"ok": True, "cached": True,
                "taxonomy": {"theme": theme, "layer1": ranked[:n]},
                "notice": f"Showing the {n} highest-exposure sub-themes from your "
                          f"saved set of {m}. The full set stays saved."}

    # Want more than saved -> top up with (n - m) new non-duplicate sub-themes.
    need = n - m
    existing_names = [s.get("sub_industry", "") for s in saved_layer1]
    gen = engine.generate_taxonomy(theme, n=need, force_refresh=True,
                                   existing=existing_names)
    if not gen["ok"]:
        return gen
    merged = engine.merge_sub_themes(saved_layer1, gen["taxonomy"]["layer1"],
                                     limit=engine._MAX_SUBS)
    sb.save_taxonomy(session, theme, merged)
    notice = None
    if len(merged) < n:
        notice = (f"Added {len(merged) - m} new sub-theme(s); the model could not "
                  f"produce {need} genuinely distinct ones, so your set now has "
                  f"{len(merged)}.")
    return {"ok": True, "cached": False,
            "taxonomy": {"theme": theme, "layer1": merged}, "notice": notice}


def _compare_theme_summary(theme: str, portfolio_df, segments,
                           default_n: int = engine._DEFAULT_SUBS) -> dict:
    """Resolve + analyze a theme and return a compact summary for the
    Comparison tab. Uses the same cache/preset path as the main flow, so preset
    themes return instantly. Returns {ok, summary} or a handled error dict.

    summary keys mirror metrics shown elsewhere (Overview KPIs + Contributors):
      theme, portfolio_exposure, portfolio_band, benchmark_exposure,
      benchmark_band, active_tilt, direction, top (list of {company, contribution}).
    """
    theme = (theme or "").strip()
    if not theme:
        return {"ok": False, "message": "Empty theme."}
    tax_res = _resolve_taxonomy(theme, default_n, portfolio_df, segments,
                                force_refresh=False)
    if not tax_res["ok"]:
        return {"ok": False, "message": tax_res.get("message", "Could not resolve theme."),
                "error_code": tax_res.get("error_code")}
    result = engine.analyze_portfolio(portfolio_df, segments, tax_res["taxonomy"])
    m = result["metrics"]
    tilt = m["active_tilt"]
    direction = "Overweight" if tilt > 0 else ("Underweight" if tilt < 0 else "Neutral")
    top = [{"company": c["company"], "contribution": round(c["contribution"], 2)}
           for c in m["contributions"] if c["contribution"] > 0][:5]
    return {"ok": True, "summary": {
        "theme": tax_res["taxonomy"].get("theme", theme),
        "portfolio_exposure": m["portfolio_thematic_exposure"],
        "portfolio_band": m.get("portfolio_band", "-"),
        "benchmark_exposure": m["benchmark_exposure"],
        "benchmark_band": m.get("benchmark_band", "-"),
        "active_tilt": tilt,
        "direction": direction,
        "top": top,
    }}


def _apply_curation(theme: str, kept_names: list, portfolio_df, segments) -> dict:
    """Trim the current analysis to the chosen sub-themes, re-score, persist.

    For a logged-in user the curated set REPLACES their saved taxonomy, so only
    the kept sub-themes are reused on future runs for this profile + theme.

    Returns {ok, analysis} with a freshly scored analysis dict, or a handled
    error.
    """
    analysis = st.session_state.get("analysis") or {}
    full_layer1 = (analysis.get("taxonomy") or {}).get("layer1", [])
    kept = [s for s in full_layer1 if s["sub_industry"] in set(kept_names)]
    if not kept:
        return {"ok": False, "message": "Keep at least one sub-theme."}

    tax = {"theme": theme, "layer1": kept}
    result = engine.analyze_portfolio(portfolio_df, segments, tax)

    if _is_logged_in():
        sb.save_taxonomy(st.session_state["auth"]["session"], theme, kept)

    return {"ok": True, "analysis": {
        "taxonomy": tax,
        "metrics": result["metrics"],
        "scores": result["scores"],
        "metadata": result.get("metadata", {}),
        "cached": False,
    }}


# ---------------------------------------------------------------------------
# Authentication (Phase 2 - only active when Supabase is configured)
# ---------------------------------------------------------------------------
def _is_logged_in() -> bool:
    return bool(st.session_state.get("auth"))


# ---------------------------------------------------------------------------
# Favourite themes - Supabase for logged-in users, session for anonymous.
# Stored as a list of theme strings (display form). Comparison tab reads these.
# ---------------------------------------------------------------------------
def _get_favourites() -> list:
    """Return the current user's favourite theme strings (newest first)."""
    if _is_logged_in():
        res = sb.list_favourites(st.session_state["auth"]["session"])
        if res.get("ok"):
            return [f["theme"] for f in res["favourites"]]
        return []
    return list(st.session_state.get("_fav_themes", []))


def _is_favourite(theme: str) -> bool:
    key = (theme or "").strip().lower()
    return any(key == t.strip().lower() for t in _get_favourites())


def _add_favourite(theme: str) -> None:
    theme = (theme or "").strip()
    if not theme:
        return
    if _is_logged_in():
        sb.add_favourite(st.session_state["auth"]["session"], theme)
    else:
        favs = st.session_state.setdefault("_fav_themes", [])
        if not any(theme.lower() == t.lower() for t in favs):
            favs.insert(0, theme)


def _remove_favourite(theme: str) -> None:
    theme = (theme or "").strip()
    if _is_logged_in():
        sb.remove_favourite(st.session_state["auth"]["session"], theme)
    else:
        favs = st.session_state.get("_fav_themes", [])
        st.session_state["_fav_themes"] = [t for t in favs
                                           if t.lower() != theme.lower()]


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
        st.session_state["portfolio_label"] = f"{res.get('name') or 'my portfolio'} (saved)"
        st.session_state["_last_upload"] = "_autoloaded_"  # avoid re-upload churn


def render_account_sidebar():
    """Account controls in the sidebar (only when logged in)."""
    if not _is_logged_in():
        return
    with st.sidebar:
        st.markdown("### Account")
        st.caption("Your uploaded portfolio is saved to your profile automatically "
                   "and reloads on your next login.")
        if st.button("Re-save current portfolio", use_container_width=True):
            df = st.session_state.get("portfolio_df")
            if df is None:
                st.warning("No portfolio to save.")
            else:
                r = sb.save_portfolio(st.session_state["auth"]["session"],
                                      _SAVED_PORTFOLIO_NAME, df)
                st.success("Saved to your profile.") if r["ok"] else st.error(r["message"])
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


# Stable name so re-saving updates the same row (no duplicate drift).
_SAVED_PORTFOLIO_NAME = "my portfolio"


def _handle_upload(uploaded, container):
    """Apply an uploaded portfolio, keeping the previous one on failure (Req 1.6).

    When the user is logged in, the uploaded portfolio is AUTOMATICALLY saved to
    their Supabase profile so it reloads on next login - no manual step needed.
    """
    if uploaded is None or uploaded == st.session_state.get("_last_upload"):
        return
    st.session_state["_last_upload"] = uploaded
    res = engine.load_portfolio(uploaded)
    if not res["ok"]:
        container.error(res["message"])  # keep previous portfolio
        return
    st.session_state["portfolio_df"] = res["portfolio"]
    st.session_state["portfolio_label"] = uploaded.name
    container.success(f"Loaded {uploaded.name}")

    # Auto-persist for logged-in users (Req 12.1).
    if _is_logged_in():
        r = sb.save_portfolio(st.session_state["auth"]["session"],
                              _SAVED_PORTFOLIO_NAME, res["portfolio"])
        if r["ok"]:
            container.success("Saved to your profile - it will reload next login.")
            st.session_state["portfolio_label"] = f"{uploaded.name} (saved)"
        else:
            container.warning(f"Loaded, but could not save to profile: {r['message']}")


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
            "The LLM expands the theme into sub-themes and judges which segments "
            "belong (yes/no). Exposure is then the revenue share of the matched "
            "segments - computed, never guessed by the model. Results are cached, "
            "so the same theme + portfolio reproduces exactly."
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

    # Deterministic drill-down: a button per sub-theme that jumps to the
    # Taxonomy tab with that sub-theme opened. (Reliable across reruns, unlike
    # chart click events on this component version.)
    sub_exp = m.get("sub_theme_exposure", {})
    if sub_exp:
        st.caption("Drill into a sub-theme:")
        cols = st.columns(max(1, len(sub_exp)))
        for i, (sub_name, exp_val) in enumerate(sub_exp.items()):
            label = f"{sub_name}  ({exp_val:.1f}%)"
            if cols[i].button(label, key=f"drill_{i}", use_container_width=True):
                st.session_state["_focus_sub_theme"] = sub_name
                st.session_state["_goto_tab_index"] = 1  # Taxonomy
                st.rerun()

    _render_coverage(m.get("coverage", {}))
    _render_methodology(analysis.get("metadata", {}), m)


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


def _render_methodology(meta: dict, metrics: dict | None = None):
    """Plain-English 'how this was computed' trace."""
    if not meta:
        return
    metrics = metrics or {}
    mode = metrics.get("scoring_mode", "llm")
    with st.expander("How this was computed (methodology & assumptions)"):
        st.markdown(meta.get("notes", ""))
        if mode == "llm":
            st.markdown(
                "**Scoring:** the LLM reads each company's reported segment names "
                "and decides, per segment, whether it belongs to the theme "
                "(a yes/no judgement by business meaning, with a short reason). "
                "A matched segment contributes **100%** of its revenue share to "
                "exposure; unmatched segments contribute 0%. The model never "
                "outputs a number - every percentage comes from reported S&P "
                "segment revenue. The decision is cached per theme + portfolio, "
                "so results reproduce exactly."
            )
        else:
            st.warning(
                "**Scoring (fallback mode):** the LLM matcher was unavailable "
                "(no Groq key or a rate limit), so exposure was computed with the "
                "offline embedding similarity fallback. Numbers are approximate; "
                "re-run once the LLM is reachable for the authoritative result."
            )
        a, b = st.columns(2)
        a.markdown(
            f"- **Taxonomy model (LLM):** `{meta.get('taxonomy_model','-')}`\n"
            f"- **Segment matcher (LLM):** `{meta.get('taxonomy_model','-')}` "
            "(binary, cached)\n"
            f"- **Fallback embedding model:** `{meta.get('embedding_model','-')}`"
        )
        bands = meta.get("exposure_bands", [])
        band_txt = "  ·  ".join(f"**{label}** &ge;{int(low)}%" for low, label in bands)
        b.markdown(
            f"- **Company matching:** normalized + fuzzy (threshold {meta.get('fuzzy_match_threshold','-')})\n"
            f"- **Exposure bands:** {band_txt}\n"
            "- **Reproducibility:** matches are cached per theme + portfolio.",
            unsafe_allow_html=True,
        )


def _companies_by_sub_theme(analysis: dict) -> dict:
    """Invert per-company sub_theme_attribution -> {sub_theme: [(company, pts, pwt, contrib)]}.

    'pts' is the company's exposure attributed to that sub-theme (0-100).
    'contrib' is that weighted by the company's portfolio weight (what actually
    flows into the portfolio sub-theme exposure).
    """
    scores = analysis.get("scores", {})
    contribs = {c["company"]: c for c in analysis["metrics"]["contributions"]}
    out: dict[str, list] = {}
    for company, sr in scores.items():
        pwt = float(contribs.get(company, {}).get("portfolio_wt", 0.0))
        for sub, pts in (sr.get("sub_theme_attribution") or {}).items():
            out.setdefault(sub, []).append({
                "company": company,
                "sub_exposure": round(float(pts), 2),
                "portfolio_wt": pwt,
                "contribution": round(float(pts) * pwt, 3),
            })
    for sub in out:
        out[sub].sort(key=lambda r: r["contribution"], reverse=True)
    return out


def _render_curation_savebar(analysis: dict, keep: list):
    """Render the Save/Apply bar below the sub-theme accordions.

    `keep` is the list of sub-theme names whose Keep checkbox is ticked.
    Logged-in users: saving REPLACES their saved taxonomy for this profile +
    theme with only the kept sub-themes. Anonymous users: session-only apply.
    """
    layer1 = analysis.get("taxonomy", {}).get("layer1", [])
    theme = st.session_state.get("theme", "").strip()
    portfolio_df = st.session_state.get("portfolio_df")
    seg_res = _load_segments()
    if portfolio_df is None or not theme or not seg_res["ok"]:
        return
    segments = seg_res["segments"]

    dropped = len(layer1) - len(keep)
    hint = ("Saving removes the unticked sub-themes from the set stored on your "
            "profile for this theme (Regenerate restores a full set)."
            if _is_logged_in() else
            "Applying trims this session's view only. Log in to save a curated "
            "set to your profile.")

    st.write("")
    st.markdown(f'<div style="color:{T.MUTED};font-size:12px;margin-bottom:6px;">'
                f'{hint}</div>', unsafe_allow_html=True)
    c1, c2 = st.columns([1.5, 4])
    with c1:
        label = ("Save curated set" if _is_logged_in() else "Apply to session")
        if dropped > 0:
            label += f" ({len(keep)}/{len(layer1)})"
        if st.button(label, type="primary", use_container_width=True,
                     key="curate_save", disabled=(len(keep) == 0)):
            if len(keep) == 0:
                st.warning("Keep at least one sub-theme.")
            elif dropped == 0:
                st.info("No change — every sub-theme is kept.")
            else:
                res = _apply_curation(theme, keep, portfolio_df, segments)
                if not res["ok"]:
                    st.warning(res["message"])
                else:
                    st.session_state["analysis"] = res["analysis"]
                    st.success(
                        f"Kept {len(keep)} of {len(layer1)} sub-themes."
                        + (" Saved to your profile." if _is_logged_in() else ""))
                    st.rerun()
    with c2:
        if len(keep) == 0:
            st.caption("Tick at least one sub-theme to keep.")
        elif dropped > 0:
            st.caption(f"Will drop {dropped} unticked sub-theme(s) and re-score.")
        else:
            st.caption("All sub-themes kept — untick any to curate.")


def section_taxonomy(analysis: dict):
    taxonomy = analysis["taxonomy"]
    m = analysis["metrics"]
    sub_exposure = m.get("sub_theme_exposure", {})
    total = sum(sub_exposure.values()) or 1.0
    by_sub = _companies_by_sub_theme(analysis)
    focus = st.session_state.pop("_focus_sub_theme", None)  # from an Overview click

    st.markdown(f'<div class="panel-title">{taxonomy.get("theme","Theme")}</div>'
                '<div class="panel-sub">Theme &rarr; sub-themes &rarr; activities. '
                'Expand a sub-theme to see the holdings driving it. Use the '
                '<b>Keep</b> checkbox on the right of each sub-theme, then '
                '<b>Save</b> below to curate your set. '
                '(Tip: use the drill buttons on the Overview tab to jump straight '
                'to a sub-theme.)</div>',
                unsafe_allow_html=True)

    # Ordered by exposure (inverted-pyramid feel: biggest driver on top).
    subs = sorted(taxonomy.get("layer1", []),
                  key=lambda s: sub_exposure.get(s["sub_industry"], 0.0), reverse=True)

    # Curation is available whenever there is more than one sub-theme to choose
    # between; each accordion carries its own Keep checkbox on the right.
    curating = len(subs) > 1
    keep: list[str] = []

    for idx, sub in enumerate(subs):
        name = sub["sub_industry"]
        exp = sub_exposure.get(name, 0.0)
        share = min(1.0, exp / total)
        companies = by_sub.get(name, [])
        header = f"{name}   ·   {exp:.1f}% exposure   ·   {len(companies)} holding(s)"

        # Accordion on the left; a right-aligned Keep checkbox lined up with the
        # accordion header.
        if curating:
            left, right = st.columns([10, 1.4])
        else:
            left, right = st.container(), None

        with left:
            # Auto-open the sub-theme the user drilled into from the Overview tab.
            with st.expander(header, expanded=(focus == name)):
                # Branded share bar with the gold exposure figure.
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;'
                    f'align-items:center;margin-bottom:4px;">'
                    f'<span style="color:{T.MUTED};font-size:12px;">Share of total thematic exposure</span>'
                    f'<span class="sub-exp">{exp:.1f}%</span></div>'
                    f'<div style="height:8px;background:{T.NAVY_BG};border-radius:5px;margin-bottom:14px;">'
                    f'<div style="height:8px;width:{share*100:.0f}%;background:'
                    f'linear-gradient(90deg,{T.BLUE},{T.BLUE_LIGHT});border-radius:5px;"></div>'
                    f'</div>', unsafe_allow_html=True)

                # Activities as branded chips.
                chips = "".join(f'<span class="chip">{a}</span>' for a in sub.get("activities", []))
                st.markdown(
                    f'<div style="color:{T.MUTED};font-size:12px;margin-bottom:4px;">Activities / keywords</div>'
                    f'<div style="margin-bottom:14px;">{chips}</div>', unsafe_allow_html=True)

                # Holdings driving this sub-theme.
                if companies:
                    st.markdown(
                        f'<div style="color:{T.MUTED};font-size:12px;margin-bottom:6px;">'
                        'Holdings contributing (contribution = sub-theme exposure &times; '
                        'portfolio weight)</div>', unsafe_allow_html=True)
                    df = pd.DataFrame([{
                        "Company": r["company"],
                        "Sub-Theme Exposure %": r["sub_exposure"],
                        "Portfolio Wt %": round(r["portfolio_wt"] * 100, 2),
                        "Contribution %": r["contribution"],
                    } for r in companies])
                    st.dataframe(df, use_container_width=True, hide_index=True)
                else:
                    st.markdown(
                        f'<div style="color:{T.MUTED};font-size:13px;">No holdings currently '
                        'contribute to this sub-theme (no matched segment revenue).</div>',
                        unsafe_allow_html=True)

        if curating:
            with right:
                # Nudge the checkbox down so it sits on the accordion header line.
                st.markdown('<div style="height:6px;"></div>', unsafe_allow_html=True)
                if st.checkbox("Keep", value=True, key=f"curate_keep_{idx}_{name}"):
                    keep.append(name)
        else:
            keep.append(name)

    if curating:
        _render_curation_savebar(analysis, keep)


def section_contributors(analysis: dict):
    m = analysis["metrics"]
    st.markdown('<div class="panel-title">Top Contributors</div>'
                '<div class="panel-sub">Ranked by contribution to portfolio thematic '
                'exposure. Click a company to open its detail &rarr;</div>',
                unsafe_allow_html=True)

    rows = [c for c in m["contributions"] if c["contribution"] > 0] or m["contributions"]
    rows = sorted(rows, key=lambda c: c["contribution"], reverse=True)[:12]
    if not rows:
        st.info("No companies currently contribute to this theme.")
        return

    scores = analysis.get("scores", {})
    max_contrib = max((c["contribution"] for c in rows), default=1.0) or 1.0

    # Clickable rows: a company-name button on the left, an HTML bar on the right
    # sized to its contribution. Clicking jumps to Company Detail for that name.
    # (Native buttons are 100% reliable, unlike chart click events on this
    # component version.)
    for i, c in enumerate(rows):
        name = c["company"]
        contrib = c["contribution"]
        width = max(2.0, 100.0 * contrib / max_contrib)
        has_data = scores.get(name, {}).get("has_segment_data", True)
        bc1, bc2 = st.columns([2, 5])
        with bc1:
            if st.button(name[:26], key=f"contrib_{i}", use_container_width=True,
                         disabled=not has_data,
                         help=None if has_data else "No segment data for this company"):
                st.session_state["_focus_company"] = name
                st.session_state["_goto_tab_index"] = 3  # Company Detail
                st.rerun()
        with bc2:
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:10px;height:38px;">'
                f'<div style="flex:1;height:22px;background:{T.NAVY_BG};border-radius:5px;'
                f'overflow:hidden;">'
                f'<div style="height:22px;width:{width:.1f}%;background:'
                f'linear-gradient(90deg,{T.GOLD},#FFD24D);border-radius:5px;"></div>'
                f'</div>'
                f'<span style="color:{T.TEXT};font-weight:700;font-size:13px;'
                f'min-width:64px;text-align:right;">{contrib:.2f}%</span>'
                f'</div>', unsafe_allow_html=True)

    st.caption("Bar length = contribution to portfolio exposure "
               "(company exposure x portfolio weight). Full figures below.")

    # Full detail table (same order).
    scores = analysis.get("scores", {})
    df = pd.DataFrame([{
        "Company": c["company"],
        "Sector": c["sector"],
        "Portfolio Wt %": round(c["portfolio_wt"] * 100, 1),
        "Exposure %": round(c["company_exposure"]),
        "Contribution %": round(c["contribution"], 2),
        "Confidence": scores.get(c["company"], {}).get("confidence_level", "None"),
    } for c in rows])
    st.dataframe(df, use_container_width=True, hide_index=True)


def section_company(analysis: dict):
    scores = analysis["scores"]
    companies = sorted(scores.keys())
    # Honor a company chosen by clicking its row on the Contributors tab.
    focus = st.session_state.pop("_focus_company", None)
    default_idx = companies.index(focus) if focus in companies else 0
    choice = st.selectbox("Select a company", companies, index=default_idx)
    sr = scores[choice]

    if not sr["has_segment_data"]:
        st.warning(f"{choice}: no S&P segment data available - flagged, exposure 0%.")
        return

    matched_rev = sr.get("matched_revenue_pct", sr["company_exposure"])
    n_matched = sum(1 for b in sr["segment_breakdown"] if b["alignment_score"] > 0)
    conf_level = sr.get("confidence_level", "None")

    c1, c2, c3 = st.columns(3)
    _kpi_card(c1, "Company Exposure", f"{sr['company_exposure']:.0f}%",
              f"{T.band_badge(sr.get('exposure_band','-'))} &nbsp; exact {sr['company_exposure']:.2f}%", "kpi-accent")
    _kpi_card(c2, "Confidence", conf_level if conf_level != "None" else "n/a",
              "how strongly the evidence supports this classification "
              f"({n_matched} of {len(sr['segment_breakdown'])} segments matched)")
    _kpi_card(c3, "Matched Sub-Themes", str(len(sr["matched_sub_industries"])),
              ", ".join(sr["matched_sub_industries"]) or "none")

    if sr.get("insufficient_data"):
        st.warning("Insufficient data: no segment matched this theme, exposure 0%.")

    # Exposure is purely revenue-based: a segment the model judged part of the
    # theme contributes 100% of its revenue share, everything else 0%. Confidence
    # is a deterministic High/Medium/Low from the matched-revenue share and how
    # many segments corroborate - not an LLM number.
    st.caption(
        f"Exposure = the {matched_rev:.0f}% of this company's segment revenue in "
        f"segments judged part of the theme ({n_matched} segment"
        f"{'s' if n_matched != 1 else ''} matched). Confidence is **{conf_level}** — "
        f"derived deterministically from that revenue share and segment breadth, "
        f"not an LLM number. See the per-segment decision and rationale below."
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
        st.markdown('<div class="panel-title">Segment Revenue &amp; Match</div>'
                    '<div class="panel-sub">Per-segment yes/no decision, the sub-theme '
                    'it was mapped to, and why &mdash; auditable</div>',
                    unsafe_allow_html=True)
        seg_df = pd.DataFrame([{
            "Segment": (b["name"] or f"Segment {i+1}"),
            "Revenue Weight %": round(b["revenue_pct"], 1),
            "In Theme": "Yes" if b["alignment_score"] > 0 else "No",
            "Matched Sub-Theme": b.get("matched_sub_industry") or "-",
            "Why": b.get("match_reason") or "-",
        } for i, b in enumerate(sr["segment_breakdown"])])
        if not seg_df.empty:
            st.dataframe(seg_df, use_container_width=True, hide_index=True)

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


def section_comparison(analysis: dict, portfolio_df, segments):
    """Compare the summarized view of up to 3 themes side by side.

    Slot 1 auto-fills with the currently-open theme. Slots 2 and 3 are chosen
    from the user's favourites (dropdown) or typed free-text (which the backend
    resolves + analyzes - instant for preset/cached themes). Metrics shown are
    exactly those already on Overview (exposure / benchmark / tilt + bands) and
    Contributors (top 5 by contribution) - nothing new is invented here.
    """
    st.markdown('<div class="panel-title">Theme Comparison</div>'
                '<div class="panel-sub">Compare up to three themes side by side. '
                'Slot 1 is the theme you have open; pick the others from your '
                'favourites or type any theme.</div>', unsafe_allow_html=True)

    current = (analysis.get("taxonomy", {}) or {}).get("theme", "").strip()
    favourites = _get_favourites()
    fav_options = ["(none)"] + favourites

    # --- Slot pickers ---
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(f'<div class="kpi-label">Theme 1 (current)</div>',
                    unsafe_allow_html=True)
        st.text_input("Theme 1", value=current, key="cmp_t1",
                      label_visibility="collapsed",
                      placeholder="Analyze a theme first")
    with c2:
        st.markdown('<div class="kpi-label">Theme 2</div>', unsafe_allow_html=True)
        fav2 = st.selectbox("Favourite 2", fav_options, key="cmp_fav2",
                            label_visibility="collapsed")
        free2 = st.text_input("Theme 2 (or type)", key="cmp_t2",
                              label_visibility="collapsed",
                              placeholder="…or type a theme")
    with c3:
        st.markdown('<div class="kpi-label">Theme 3</div>', unsafe_allow_html=True)
        fav3 = st.selectbox("Favourite 3", fav_options, key="cmp_fav3",
                            label_visibility="collapsed")
        free3 = st.text_input("Theme 3 (or type)", key="cmp_t3",
                              label_visibility="collapsed",
                              placeholder="…or type a theme")

    # Free text wins over the dropdown when both are provided.
    theme1 = st.session_state.get("cmp_t1", current).strip()
    theme2 = (free2.strip() or (fav2 if fav2 != "(none)" else "")).strip()
    theme3 = (free3.strip() or (fav3 if fav3 != "(none)" else "")).strip()

    if not st.button("Compare", type="primary"):
        st.caption("Choose themes and click Compare.")
        return

    # De-duplicate while preserving order; keep up to 3.
    chosen, seen = [], set()
    for t in (theme1, theme2, theme3):
        k = t.lower()
        if t and k not in seen:
            seen.add(k)
            chosen.append(t)
    if not chosen:
        st.warning("Enter at least one theme to compare.")
        return

    with st.spinner("Resolving and scoring the selected themes…"):
        summaries = []
        for t in chosen:
            res = _compare_theme_summary(t, portfolio_df, segments)
            summaries.append((t, res))

    cols = st.columns(len(summaries))
    for col, (theme_in, res) in zip(cols, summaries):
        with col:
            if not res["ok"]:
                st.markdown(f'<div class="panel"><div class="panel-title" '
                            f'style="font-size:15px;">{theme_in}</div>'
                            f'<div class="panel-sub">Could not analyze: '
                            f'{res.get("message","")}</div></div>',
                            unsafe_allow_html=True)
                continue
            s = res["summary"]
            tilt = s["active_tilt"]
            tilt_cls = "pos" if tilt > 0 else ("neg" if tilt < 0 else "")
            top_rows = "".join(
                f'<tr><td style="padding:2px 0;color:{T.TEXT};">{r["company"][:22]}</td>'
                f'<td style="padding:2px 0;text-align:right;color:{T.GOLD};'
                f'font-weight:700;">{r["contribution"]:.2f}%</td></tr>'
                for r in s["top"]) or \
                f'<tr><td style="color:{T.MUTED};">No contributors</td><td></td></tr>'
            st.markdown(
                f'<div class="panel">'
                f'<div class="panel-title" style="font-size:15px;">{s["theme"]}</div>'
                f'<div style="margin:10px 0;">'
                f'<div class="kpi-label">Portfolio Exposure</div>'
                f'<div class="kpi-value">{s["portfolio_exposure"]:.0f}%</div>'
                f'<div class="kpi-sub">{T.band_badge(s["portfolio_band"])} '
                f'&nbsp; exact {s["portfolio_exposure"]:.2f}%</div></div>'
                f'<div style="margin:10px 0;">'
                f'<div class="kpi-label">Benchmark Exposure</div>'
                f'<div class="kpi-value" style="font-size:20px;">{s["benchmark_exposure"]:.0f}%</div>'
                f'<div class="kpi-sub">{T.band_badge(s["benchmark_band"])}</div></div>'
                f'<div style="margin:10px 0;">'
                f'<div class="kpi-label">Active Tilt</div>'
                f'<div class="kpi-value" style="font-size:20px;">{tilt:+.0f}%</div>'
                f'<div class="kpi-sub"><span class="{tilt_cls}">{s["direction"]}</span> '
                f'vs benchmark</div></div>'
                f'<div class="kpi-label" style="margin-top:12px;">Top contributors</div>'
                f'<table style="width:100%;font-size:12px;border-collapse:collapse;">'
                f'{top_rows}</table>'
                f'</div>', unsafe_allow_html=True)


_LOOKBACK_OPTIONS = {"Today": 1, "Last 3 days": 3, "Last week": 7, "Last month": 30}


def section_news(analysis: dict):
    """News & Events: a QUALITATIVE Tailwind/Headwind signal (Req 16, R4).

    News never changes the revenue-based exposure number. It is a point-in-time
    read of which way the theme is being pushed, with a deterministic overall
    rollup weighted by each sub-theme's share of the real exposure.
    """
    m = analysis["metrics"]
    taxonomy = analysis["taxonomy"]
    theme = taxonomy.get("theme", "")
    baseline = m["portfolio_thematic_exposure"]

    top = st.columns([6, 1])
    top[0].markdown('<div class="panel-title">News Signal</div>'
                    '<div class="panel-sub">A qualitative read of whether recent '
                    'events are a tailwind or headwind for this theme. This never '
                    'changes the exposure figure - exposure always comes from '
                    'reported revenue.</div>', unsafe_allow_html=True)
    with top[1]:
        with st.popover("ⓘ Methodology"):
            _render_methodology_popover()

    c1, c2 = st.columns([1.4, 1])
    window_label = c1.select_slider("News lookback window",
                                    options=list(_LOOKBACK_OPTIONS.keys()),
                                    value="Last week")
    lookback = _LOOKBACK_OPTIONS[window_label]
    refresh = c2.button("Fetch news & assess signal", type="primary", use_container_width=True)

    st.caption("Note: the free news API returns only the most recent articles, so "
               "longer windows are shown for completeness but in practice return "
               "mostly recent items. With full API access the window would scan the "
               "entire selected period.")

    # Only run on explicit button click (changing the slider does not fire a call).
    cache = analysis.get("news")
    if refresh:
        with st.spinner("Fetching headlines and assessing the news signal..."):
            companies = [c["company"] for c in m["contributions"]]
            sub_exp = m.get("sub_theme_exposure", {})
            res = news_mod.news_overlay(theme, taxonomy, companies,
                                        sub_theme_exposure=sub_exp,
                                        lookback_days=lookback, force_refresh=True)
        analysis["news"] = res
        analysis["news_window"] = lookback
        st.session_state["analysis"] = analysis
        cache = res

    if cache is None:
        st.info("Pick a lookback window and click **Fetch news & assess signal**. "
                "No news is fetched until you do.")
        return

    if not cache or not cache.get("ok"):
        msg = (cache or {}).get("message", "News signal is unavailable right now.")
        st.info(f"{msg}  Exposure stands at {baseline:.0f}% (from filings, unchanged).")
        return

    overall = cache.get("overall", {"direction": "Neutral", "strength": "None"})
    per = cache.get("per_sub_theme", [])
    heads = cache.get("headlines", [])

    # Overall signal, shown next to the (unchanged) exposure for context.
    oc1, oc2 = st.columns([1, 1])
    _kpi_card(oc1, "Thematic Exposure (unchanged)", f"{baseline:.0f}%",
              "always from reported revenue - news does not change this", "kpi-accent")
    oc2.markdown(
        f"""<div class="kpi kpi-gold">
              <div class="kpi-label">Overall News Signal</div>
              <div style="margin:8px 0;">{T.signal_badge(overall['direction'], overall['strength'])}</div>
              <div class="kpi-sub">as of {cache.get('as_of','today')} &middot; {cache.get('source','')}</div>
            </div>""",
        unsafe_allow_html=True,
    )

    # Explain how the overall rolls up from the sub-theme signals, so a strong
    # signal on a small sub-theme sensibly nets to a milder overall read.
    n_signalled = len(per)
    st.caption(
        f"The overall signal is an **exposure-weighted rollup** of the "
        f"{n_signalled} sub-theme{'s' if n_signalled != 1 else ''} with material "
        "news (below): each sub-theme's tailwind/headwind is weighted by its share "
        "of your revenue-based exposure. A strong signal on a sub-theme that is only "
        "a small part of your exposure therefore softens at the portfolio level. "
        "Exposure itself is never changed by news."
    )

    wc = cache.get("window_count")
    fc = cache.get("fetched_count")
    if wc is not None:
        st.caption(f"News read: {wc} recent headline(s) within '{window_label}' "
                   f"(of {fc} fetched) via {cache.get('source','')}. The model scanned "
                   "all of them; only sub-themes with material news appear below.")

    if not per:
        st.success("No recent events are materially pushing this theme either way. "
                   "The exposure figure stands on its own.")
    else:
        st.write("")
        st.markdown('<div class="panel-title">Per Sub-Theme Signal</div>'
                    '<div class="panel-sub">Sub-themes where recent news is material. '
                    'The chips on each row are the specific headlines behind that '
                    'signal (a subset of all headlines scanned).</div>',
                    unsafe_allow_html=True)
        for p in per:
            cited = "".join(
                f'<div class="chip">{heads[i]["date"]} &middot; {heads[i]["domain"]}</div>'
                for i in p.get("cited_headlines", []) if i < len(heads)
            )
            affected = ", ".join(p.get("affected_companies", [])) or "none identified"
            st.markdown(
                f"""<div class="sub-row">
                      <div style="display:flex;justify-content:space-between;align-items:center;">
                        <span class="sub-name">{p['sub_industry']}</span>
                        {T.signal_badge(p['direction'], p['strength'])}
                      </div>
                      <div style="color:{T.MUTED};font-size:13px;margin:8px 0 6px;">{p['rationale']}</div>
                      <div style="font-size:12px;color:{T.MUTED};"><b>Affected holdings:</b> {affected}</div>
                      <div style="margin-top:6px;">{cited}</div>
                    </div>""",
                unsafe_allow_html=True,
            )

    if heads:
        with st.expander(f"All {len(heads)} headlines scanned (the full pool read by the model)"):
            st.caption("Every headline below was read when forming the signal. Only "
                       "those relevant to a sub-theme are cited on the rows above; "
                       "the rest were scanned but found immaterial.")
            for i, h in enumerate(heads):
                title = h.get("title", "")
                url = h.get("url", "")
                link = f"[{title}]({url})" if url else title
                st.markdown(f"{i}. {h.get('date','')} &middot; *{h.get('domain','')}* - {link}")


def _render_methodology_popover():
    """(i) methodology: how the qualitative signal is derived (deterministic)."""
    r = news_mod.RUBRIC
    st.markdown("#### How the news signal is estimated")
    st.markdown(f"**What the model judges:** {r['what_llm_judges']}")
    st.markdown("**Strength definitions:**")
    for k, v in r["strength_definitions"].items():
        if k == "None":
            continue
        st.markdown(f"- **{k}:** {v}")
    st.markdown("**Overall rollup (deterministic):**")
    st.markdown(r["rollup"])
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
    tc = st.columns([4.2, 1.1, 1, 1.4])
    theme = tc[0].text_input(
        "Investment theme", value=st.session_state["theme"], label_visibility="collapsed",
        placeholder="Enter an investment theme, e.g. Critical Power Grid Modernization",
    )
    st.session_state["theme"] = theme
    sub_count = tc[1].slider(
        "Sub-themes", min_value=engine._MIN_SUBS, max_value=engine._MAX_SUBS,
        value=engine._DEFAULT_SUBS, label_visibility="collapsed",
        help="How many sub-themes to break the theme into (saved taxonomies are "
             "reused unless you tick Regenerate).",
    )
    analyze = tc[2].button("Analyze", type="primary", use_container_width=True)
    force_refresh = tc[3].checkbox("Regenerate", value=False,
                                   help="Bypass the saved taxonomy and ask the model for a "
                                        "fresh set of sub-themes.")

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
            tax_res = _resolve_taxonomy(theme.strip(), sub_count, portfolio_df,
                                        segments, force_refresh)
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
        if tax_res.get("notice"):
            st.info(tax_res["notice"])

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

    # Favourite toggle for the analyzed theme (feeds the Comparison tab).
    _analyzed_theme = (analysis.get("taxonomy", {}) or {}).get("theme", "").strip()
    if _analyzed_theme:
        fav_col, cap_col = st.columns([1.4, 5])
        with fav_col:
            if _is_favourite(_analyzed_theme):
                if st.button("★ Favourited", key="fav_toggle",
                             use_container_width=True,
                             help="Remove from My Favourites"):
                    _remove_favourite(_analyzed_theme)
                    st.rerun()
            else:
                if st.button("☆ Favourite", key="fav_toggle",
                             use_container_width=True,
                             help="Save to My Favourites (for the Comparison tab)"):
                    _add_favourite(_analyzed_theme)
                    st.rerun()
        with cap_col:
            if analysis.get("cached"):
                st.caption("Taxonomy served from cache (reproducible). "
                           "Tick 'Regenerate' to refresh.")
    elif analysis.get("cached"):
        st.caption("Taxonomy served from cache (reproducible). Tick 'Regenerate' to refresh.")

    if (analysis.get("metrics", {}) or {}).get("scoring_mode") == "embedding_fallback":
        st.warning(
            "Scored with the offline fallback (the LLM segment matcher was "
            "unavailable - missing Groq key or a rate limit). Exposure numbers "
            "are approximate; re-run Analyze to get the LLM-judged result."
        )

    # A programmatic jump (e.g. a sub-theme drill button) sets _goto_tab_index;
    # pass it via manual_select so the menu moves, then clear it so manual
    # clicks work normally afterwards.
    _manual = st.session_state.pop("_goto_tab_index", None)
    nav = option_menu(
        None, ["Overview", "Taxonomy", "Contributors", "Company Detail",
               "Comparison", "News & Events"],
        icons=["speedometer2", "diagram-3", "bar-chart-line", "building",
               "columns-gap", "newspaper"],
        orientation="horizontal",
        default_index=0,
        manual_select=_manual,
        key="main_nav",
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
    elif nav == "Comparison":
        section_comparison(analysis, portfolio_df, segments)
    elif nav == "News & Events":
        section_news(analysis)


if __name__ == "__main__":
    main()
