"""theme.py - Schroders-branded visual system (palette + injected CSS).

Keeps all look-and-feel in one place so app.py stays about layout and data.
Direction A: Schroders brand identity - deep navy base, Schroders blue accent,
warm gold highlight. Dark-first institutional dashboard.
"""

from __future__ import annotations

# --- Palette (Schroders-inspired) ---
NAVY_BG = "#0A1A2F"       # app background
PANEL = "#10263F"         # cards / panels
PANEL_2 = "#15304D"       # elevated surfaces, table header
BORDER = "#20405F"        # hairline borders
BLUE = "#1E4FD6"          # primary Schroders blue
BLUE_LIGHT = "#4C7DF0"    # hover / secondary accent
GOLD = "#F5B800"          # highlight / positive accent
TEXT = "#EAF0F8"          # primary text
MUTED = "#9FB2C8"         # secondary text
GOOD = "#28C76F"          # overweight / positive
BAD = "#FF6B6B"           # underweight / negative

# Exposure band -> color (shared by badges and charts)
BAND_COLORS = {
    "Core": "#1E4FD6",
    "High": "#4C7DF0",
    "Moderate": "#F5B800",
    "Low": "#8CA3BF",
    "Negligible": "#5A6E86",
}

# Chart color ramp for sub-themes
CHART_RAMP = ["#1E4FD6", "#4C7DF0", "#F5B800", "#28C76F", "#9A6BF0", "#FF8A5B"]


def inject_css() -> str:
    """Return the global CSS. Call via st.markdown(inject_css(), unsafe_allow_html=True)."""
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

:root {{
  --navy:{NAVY_BG}; --panel:{PANEL}; --panel2:{PANEL_2}; --border:{BORDER};
  --blue:{BLUE}; --bluelt:{BLUE_LIGHT}; --gold:{GOLD};
  --text:{TEXT}; --muted:{MUTED};
}}

/* App background + base font */
.stApp {{ background: {NAVY_BG}; color: {TEXT};
  font-family: 'Inter', -apple-system, system-ui, sans-serif; }}

/* Hide default Streamlit chrome for a portal feel, but KEEP the header bar
   itself transparent so the sidebar expand/collapse control stays usable. */
#MainMenu, footer {{ visibility: hidden; }}
[data-testid="stToolbar"] {{ display: none; }}
header[data-testid="stHeader"] {{ background: transparent; height: 0; }}
/* Keep the sidebar-collapse button visible and on-brand. */
[data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapsedControl"] {{
  visibility: visible !important; color: {TEXT} !important;
}}
.block-container {{ padding-top: 0rem; padding-bottom: 3rem; max-width: 1280px; }}

h1, h2, h3, h4 {{ color: {TEXT}; font-weight: 700; letter-spacing: -0.01em; }}
p, span, label, .stMarkdown {{ color: {TEXT}; }}
small, .stCaption, [data-testid="stCaptionContainer"] {{ color: {MUTED} !important; }}

/* Branded header bar - full-bleed edge-to-edge across the viewport */
.sch-header {{
  display:flex; align-items:center; justify-content:space-between;
  background: linear-gradient(90deg, {PANEL} 0%, {NAVY_BG} 100%);
  border-bottom:1px solid {BORDER}; border-top: 3px solid {GOLD};
  padding: 16px 40px; margin-bottom: 22px;
  /* break out of the centered .block-container to span full width */
  width: 100vw;
  position: relative;
  left: 50%;
  right: 50%;
  margin-left: -50vw;
  margin-right: -50vw;
}}
.sch-brand {{ display:flex; align-items:center; gap:14px; }}
.sch-logo {{
  width:40px; height:40px; border-radius:9px;
  background: linear-gradient(135deg, {BLUE}, {BLUE_LIGHT});
  display:flex; align-items:center; justify-content:center;
  font-weight:800; color:white; font-size:19px;
}}
.sch-title {{ font-size:19px; font-weight:800; color:{TEXT}; line-height:1.1; }}
.sch-sub {{ font-size:12px; color:{MUTED}; letter-spacing:.03em; text-transform:uppercase; }}
.sch-pill {{
  font-size:12px; color:{MUTED}; border:1px solid {BORDER};
  padding:6px 12px; border-radius:999px; background:{PANEL};
}}
.sch-pill b {{ color:{GOLD}; }}

/* KPI cards */
.kpi {{
  background:{PANEL}; border:1px solid {BORDER}; border-radius:14px;
  padding:18px 20px; height:100%;
}}
.kpi-label {{ font-size:12px; color:{MUTED}; text-transform:uppercase;
  letter-spacing:.05em; margin-bottom:6px; }}
.kpi-value {{ font-size:34px; font-weight:800; color:{TEXT}; line-height:1; }}
.kpi-sub {{ font-size:13px; color:{MUTED}; margin-top:8px; }}
.kpi-accent {{ border-top: 3px solid {BLUE}; }}
.kpi-gold {{ border-top: 3px solid {GOLD}; }}

/* Band + confidence badges */
.badge {{
  display:inline-block; font-size:11px; font-weight:700; padding:3px 10px;
  border-radius:999px; letter-spacing:.03em; color:{NAVY_BG};
}}
.pos {{ color:{GOOD}; font-weight:700; }}
.neg {{ color:{BAD}; font-weight:700; }}

/* Generic panel container */
.panel {{
  background:{PANEL}; border:1px solid {BORDER}; border-radius:14px;
  padding:18px 20px; margin-bottom:16px;
}}
.panel-title {{ font-size:15px; font-weight:700; color:{TEXT};
  margin-bottom:4px; }}
.panel-sub {{ font-size:12px; color:{MUTED}; margin-bottom:14px; }}

/* Sub-theme hierarchy rows */
.sub-row {{ background:{PANEL_2}; border:1px solid {BORDER};
  border-radius:10px; padding:12px 14px; margin-bottom:10px; }}
.sub-name {{ font-weight:700; color:{TEXT}; font-size:14px; }}
.sub-exp {{ color:{GOLD}; font-weight:800; font-size:15px; }}
.chip {{ display:inline-block; background:{NAVY_BG}; border:1px solid {BORDER};
  color:{MUTED}; font-size:11px; padding:3px 9px; border-radius:7px;
  margin:3px 4px 0 0; }}

/* Inputs */
.stTextInput input {{ background:{PANEL} !important; color:{TEXT} !important;
  border:1px solid {BORDER} !important; border-radius:10px !important; }}
.stButton button {{ background:{BLUE} !important; color:white !important;
  border:0 !important; border-radius:10px !important; font-weight:600 !important;
  padding:.5rem 1.2rem !important; }}
.stButton button:hover {{ background:{BLUE_LIGHT} !important; }}

/* Dataframe */
[data-testid="stDataFrame"] {{ border:1px solid {BORDER}; border-radius:10px; }}

/* Sidebar */
section[data-testid="stSidebar"] {{ background:{PANEL}; border-right:1px solid {BORDER}; }}
section[data-testid="stSidebar"] * {{ color:{TEXT}; }}
</style>
"""


def band_badge(band: str) -> str:
    color = BAND_COLORS.get(band, MUTED)
    return f'<span class="badge" style="background:{color}">{band}</span>'
