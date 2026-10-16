"""engine.py - pure analysis logic for the Thematic Portfolio Analysis prototype.

This module MUST NOT import streamlit at the top level (see design: "Why
engine.py has no Streamlit dependency"). It contains the deterministic
analysis engine plus a small configuration helper shared with app.py.

Public functions:
    get_secret(key, default)                         # config helper (Task 1)
    load_portfolio(path_or_buffer)                   # Task 3
    load_segments(path)                              # Task 4
    match_companies(portfolio_df, segments)          # Task 5
    generate_taxonomy(theme)                         # Task 10
    score_company(company_row, taxonomy)             # Tasks 6-7
    calculate_portfolio_metrics(df_scores, df_pf)    # Task 8
"""

from __future__ import annotations

import os

import pandas as pd
from dotenv import load_dotenv

# Load variables from a local .env once at import time. On Streamlit Cloud
# there is no .env; secrets come from st.secrets (handled in get_secret).
load_dotenv()


# ---------------------------------------------------------------------------
# Configuration helper (Task 1 - Requirements 5.5, 11.7)
# ---------------------------------------------------------------------------
def get_secret(key: str, default: str | None = None) -> str | None:
    """Return a configuration secret by name.

    Resolution order (Requirements 5.5, 11.7):
        1. st.secrets when running under Streamlit (lazy, guarded import so
           engine.py never hard-depends on streamlit).
        2. Process environment / .env (python-dotenv).
        3. The provided default.
    """
    try:
        import streamlit as st  # noqa: PLC0415 (intentional lazy import)

        try:
            if key in st.secrets:
                return st.secrets[key]
        except Exception:
            pass
    except Exception:
        pass
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# Error-handling convention (Task 2 - Requirements 10.1, 10.2, 10.3)
#
# Every engine function that can fail in an expected way returns a dict with an
# "ok" key instead of raising. app.py renders res["message"] on failure and
# never crashes. Only truly unexpected faults raise.
# ---------------------------------------------------------------------------

# Canonical machine-readable error codes (see design: Error-handling convention)
MISSING_API_KEY = "MISSING_API_KEY"
INVALID_API_KEY = "INVALID_API_KEY"
LLM_BAD_JSON = "LLM_BAD_JSON"
LLM_UNREACHABLE = "LLM_UNREACHABLE"
INVALID_THEME = "INVALID_THEME"
FILE_NOT_FOUND = "FILE_NOT_FOUND"
MISSING_COLUMN = "MISSING_COLUMN"
MISSING_SHEET = "MISSING_SHEET"


def err(error_code: str, message: str, detail: str | None = None) -> dict:
    """Construct a handled-error result (returned, not raised)."""
    return {"ok": False, "error_code": error_code, "message": message, "detail": detail}


def ok(**payload) -> dict:
    """Construct a success result; always carries ok=True."""
    return {"ok": True, **payload}


# ---------------------------------------------------------------------------
# Data loading: portfolio (Task 3 - Requirements 1.2-1.7)
# ---------------------------------------------------------------------------

# Required portfolio columns, in canonical order (Req 1.3).
PORTFOLIO_COLUMNS = [
    "Company",
    "Bloomberg ticker",
    "Country",
    "Sector",
    "SEDOL",
    "Portfolio wt",
    "Reference wt",
    "Active wt",
]

_WEIGHT_COLUMNS = ("Portfolio wt", "Reference wt", "Active wt")


def _read_tabular(path_or_buffer) -> pd.DataFrame:
    """Read a CSV or XLSX from a path or an uploaded buffer into a DataFrame.

    Header is on row index 0 (Req 1.3). Chooses the reader by file extension
    when available, falling back to trying Excel then CSV for buffers.
    """
    name = None
    if isinstance(path_or_buffer, str):
        name = path_or_buffer
    else:
        name = getattr(path_or_buffer, "name", None)

    is_csv = bool(name) and str(name).lower().endswith(".csv")
    if is_csv:
        return pd.read_csv(path_or_buffer, header=0)
    # Default to Excel for .xlsx/.xls and unknown-named buffers.
    return pd.read_excel(path_or_buffer, header=0, engine="openpyxl")


def load_portfolio(path_or_buffer) -> dict:
    """Load a portfolio from a path or uploaded buffer (CSV or XLSX).

    Returns:
        {"ok": True, "portfolio": pd.DataFrame}  on success, weights as floats
        {"ok": False, "error_code": FILE_NOT_FOUND | MISSING_COLUMN, ...}

    Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7.
    """
    # Req 1.7 / 10.3: missing file is a handled error, not a crash.
    if isinstance(path_or_buffer, str) and not os.path.exists(path_or_buffer):
        return err(
            FILE_NOT_FOUND,
            f"Portfolio file not found: {path_or_buffer}",
            detail=path_or_buffer,
        )

    try:
        df = _read_tabular(path_or_buffer)
    except FileNotFoundError:
        return err(FILE_NOT_FOUND, "Portfolio file not found.", detail=str(path_or_buffer))

    # Normalize column names: strip surrounding whitespace only (preserve the
    # canonical names the rest of the engine expects).
    df = df.rename(columns=lambda c: str(c).strip())

    # Req 1.6 / 1.3: a missing required column is a handled error naming it.
    missing = [c for c in PORTFOLIO_COLUMNS if c not in df.columns]
    if missing:
        return err(
            MISSING_COLUMN,
            f"Portfolio is missing required column(s): {', '.join(missing)}",
            detail=missing[0],
        )

    # Keep only the canonical columns, in order.
    df = df[PORTFOLIO_COLUMNS].copy()

    # Req 1.4: weights stay as decimal fractions (0.132 == 13.2%); coerce to float.
    for col in _WEIGHT_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return ok(portfolio=df)

# ---------------------------------------------------------------------------
# Data loading: S&P segment positional parse (Task 4 - Requirements 2.1-2.12)
# Implements design Low-Level Algorithm A.
# ---------------------------------------------------------------------------
import re  # noqa: E402

_SEGMENT_SHEET = "Sheet1"
_HEADER_ROW = 4          # Req 2.2 - real header row index
_DATA_START_ROW = 7      # Req 2.2 - data begins here (skip rows 5=FY0, 6=blank)
_SEG_NAME_HEADER = "IQ_BUS_SEG_ABS"       # repeats 5x (segment names)
_SEG_REV_HEADER = "IQ_BUS_SEG_REV_ABS"    # repeats 5x (segment revenues)
_MAX_SEGMENTS = 5

# Required named columns on the header row (Req 2.3).
_SEGMENT_NAMED_COLUMNS = [
    "SP_ENTITY_NAME",
    "SP_ENTITY_ID",
    "SP_COMPANY_NAME",
    "SP_EXCHANGE",
    "SP_LONG_BUSINESS_DESCRIPTION",
    "IQ_INDUSTRY_CLASSIFICATION",
    "IQ_SECTOR",
    "IQ_INDUSTRY_GROUP",
    "IQ_INDUSTRY",
    "IQ_PRIMARY_INDUSTRY",
]

# Parenthetical "(EXCHANGE:TICKER)" at the end of SP_ENTITY_NAME (Req 2.7-2.8).
_TICKER_RE = re.compile(r"\(([A-Za-z0-9.\- ]+):([A-Za-z0-9.\-]+)\)\s*$")

# Values that mean "absent" for a cell.
_NA_TOKENS = {"", "NA", "N/A", "NM", "NONE", "NAN"}


def _cell_is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    return str(value).strip() == ""


def _coerce_revenue(value):
    """Return (revenue_float_or_None, revenue_present_bool). Req 2.9-2.10."""
    if _cell_is_blank(value):
        return None, False
    if isinstance(value, (int, float)) and not (isinstance(value, float) and pd.isna(value)):
        return float(value), True
    s = str(value).strip()
    if s.upper() in _NA_TOKENS:
        return None, False
    try:
        return float(s.replace(",", "")), True
    except ValueError:
        # Non-numeric and not an NA token -> treat as absent (Req 2.10).
        return None, False


def _clean_name(value):
    """Return a stripped segment/text name or None when blank."""
    if _cell_is_blank(value):
        return None
    return str(value).strip()


def _extract_exchange_ticker(entity_name: str):
    """Return (exchange, ticker) from a trailing (EXCHANGE:TICKER), else (None, None)."""
    if not entity_name:
        return None, None
    m = _TICKER_RE.search(str(entity_name))
    if not m:
        return None, None
    return m.group(1).strip(), m.group(2).strip()


def load_segments(path) -> dict:
    """Parse the messy S&P Capital IQ segment export (design Algorithm A).

    Returns:
        {"ok": True, "segments": {entity_key: CompanySegments, ...}}
        {"ok": False, "error_code": FILE_NOT_FOUND | MISSING_SHEET | MISSING_COLUMN, ...}

    Requirements: 2.1 - 2.12.
    """
    if isinstance(path, str) and not os.path.exists(path):
        return err(FILE_NOT_FOUND, f"Segment file not found: {path}", detail=path)

    try:
        xls = pd.ExcelFile(path, engine="openpyxl")
    except FileNotFoundError:
        return err(FILE_NOT_FOUND, "Segment file not found.", detail=str(path))

    # Req 2.1 / 2.11: must have "Sheet1"; "Screening Criteria" is ignored.
    if _SEGMENT_SHEET not in xls.sheet_names:
        return err(
            MISSING_SHEET,
            f'Segment file has no "{_SEGMENT_SHEET}" sheet.',
            detail=_SEGMENT_SHEET,
        )

    # Req 2.2: read raw (header=None) so absolute row indices stay stable and
    # the identical repeated headers are not auto-deduped before we locate them.
    raw = pd.read_excel(xls, sheet_name=_SEGMENT_SHEET, header=None, engine="openpyxl")

    if len(raw) <= _DATA_START_ROW:
        return ok(segments={})

    header_cells = [
        None if _cell_is_blank(v) else str(v).strip()
        for v in raw.iloc[_HEADER_ROW].tolist()
    ]

    # Locate named columns by first exact header match (Req 2.3).
    col_index: dict[str, int] = {}
    for name in _SEGMENT_NAMED_COLUMNS:
        try:
            col_index[name] = header_cells.index(name)
        except ValueError:
            return err(
                MISSING_COLUMN,
                f'Segment file is missing required column "{name}".',
                detail=name,
            )

    # Locate the two repeated blocks by POSITION (Req 2.4-2.5).
    name_cols = sorted(i for i, h in enumerate(header_cells) if h == _SEG_NAME_HEADER)
    rev_cols = sorted(i for i, h in enumerate(header_cells) if h == _SEG_REV_HEADER)
    if not name_cols:
        return err(MISSING_COLUMN, f'Segment file is missing "{_SEG_NAME_HEADER}" columns.', detail=_SEG_NAME_HEADER)
    if not rev_cols:
        return err(MISSING_COLUMN, f'Segment file is missing "{_SEG_REV_HEADER}" columns.', detail=_SEG_REV_HEADER)
    name_cols = name_cols[:_MAX_SEGMENTS]
    rev_cols = rev_cols[:_MAX_SEGMENTS]

    segments: dict[str, dict] = {}
    data = raw.iloc[_DATA_START_ROW:]
    for _, row in data.iterrows():
        values = row.tolist()
        entity_name = _clean_name(values[col_index["SP_ENTITY_NAME"]])
        if entity_name is None:
            continue  # skip fully blank trailing rows

        exchange, ticker = _extract_exchange_ticker(entity_name)
        company_name = _clean_name(values[col_index["SP_COMPANY_NAME"]])
        business_description = _clean_name(values[col_index["SP_LONG_BUSINESS_DESCRIPTION"]]) or ""

        seg_list = []
        for pos in range(len(name_cols)):
            name_val = _clean_name(values[name_cols[pos]])
            rev_raw = values[rev_cols[pos]] if pos < len(rev_cols) else None
            revenue, present = _coerce_revenue(rev_raw)
            # Req 2.6: keep the position if either side exists.
            if name_val is None and not present:
                continue
            seg_list.append(
                {
                    "position": pos + 1,
                    "name": name_val,
                    "revenue_raw": None if _cell_is_blank(rev_raw) else rev_raw,
                    "revenue": revenue,
                    "revenue_present": present,
                }
            )

        entity_key = _normalize_company_name(company_name or entity_name)
        segments[entity_key] = {
            "entity_name": entity_name,
            "company_name": company_name,
            "exchange": exchange,
            "ticker": ticker,
            "entity_id": _clean_name(values[col_index["SP_ENTITY_ID"]]),
            "business_description": business_description,
            "sector": _clean_name(values[col_index["IQ_SECTOR"]]),
            "classification": {
                k: _clean_name(values[col_index[k]])
                for k in (
                    "IQ_INDUSTRY_CLASSIFICATION",
                    "IQ_INDUSTRY_GROUP",
                    "IQ_INDUSTRY",
                    "IQ_PRIMARY_INDUSTRY",
                )
            },
            "segments": seg_list,
        }

    return ok(segments=segments)

# ---------------------------------------------------------------------------
# Company matching (Task 5 - Requirements 3.1-3.5)
# Implements design Low-Level Algorithm B.
# ---------------------------------------------------------------------------
from rapidfuzz import fuzz  # noqa: E402

FUZZY_THRESHOLD = 88  # accept fuzzy matches at/above this token_sort_ratio

_CORP_SUFFIXES = {
    "LTD", "LIMITED", "INC", "INCORPORATED", "CO", "CORP", "CORPORATION",
    "PLC", "HOLDINGS", "HOLDING", "GROUP", "COMPANY", "AG", "SA", "NV", "SE",
}
_PAREN_RE = re.compile(r"\([^)]*\)")
_PUNCT_RE = re.compile(r"[.,&/'\-]")
_WS_RE = re.compile(r"\s+")


def _normalize_company_name(name: str | None) -> str:
    """Normalize a company name for matching (Req 3.2). Idempotent."""
    if not name:
        return ""
    s = str(name).upper().strip()
    s = _PAREN_RE.sub(" ", s)        # remove parentheticals
    s = _PUNCT_RE.sub(" ", s)        # remove punctuation
    s = _WS_RE.sub(" ", s).strip()
    tokens = [t for t in s.split(" ") if t and t not in _CORP_SUFFIXES]
    return " ".join(tokens)


def _portfolio_ticker_symbol(bloomberg_ticker: str | None) -> str | None:
    """Leading symbol of a Bloomberg ticker, e.g. '2330 TT Equity' -> '2330'."""
    if not bloomberg_ticker:
        return None
    parts = str(bloomberg_ticker).strip().split()
    return parts[0].upper() if parts else None


def match_companies(portfolio_df: "pd.DataFrame", segments: dict) -> dict:
    """Match each portfolio Company to a segment record (design Algorithm B).

    Returns {"ok": True, "matches": {company: MatchResult, ...}}.
    Requirements: 3.1 - 3.5.
    """
    # Pre-index segment records for exact + ticker matching.
    norm_to_key: dict[str, str] = {}
    ticker_to_key: dict[str, str] = {}
    for key, rec in segments.items():
        norm_to_key.setdefault(_normalize_company_name(rec.get("entity_name")), key)
        norm_to_key.setdefault(_normalize_company_name(rec.get("company_name")), key)
        if rec.get("ticker"):
            ticker_to_key.setdefault(str(rec["ticker"]).upper(), key)

    seg_keys = list(segments.keys())

    matches: dict[str, dict] = {}
    for _, prow in portfolio_df.iterrows():
        company = str(prow["Company"])
        norm = _normalize_company_name(company)
        result = {
            "company": company,
            "matched": False,
            "entity_key": None,
            "match_method": "none",
            "match_score": None,
        }

        # 1. Exact normalized name match.
        if norm and norm in norm_to_key:
            result.update(matched=True, entity_key=norm_to_key[norm], match_method="exact")
            matches[company] = result
            continue

        # 2. Ticker match.
        sym = _portfolio_ticker_symbol(prow.get("Bloomberg ticker"))
        if sym and sym in ticker_to_key:
            result.update(matched=True, entity_key=ticker_to_key[sym], match_method="exact")
            matches[company] = result
            continue

        # 3. Fuzzy fallback over normalized names (Req 3.3).
        best_key, best_score = None, 0.0
        for key in seg_keys:
            score = fuzz.token_sort_ratio(norm, key)
            if score > best_score:
                best_key, best_score = key, score
        if best_key is not None and best_score >= FUZZY_THRESHOLD:
            result.update(
                matched=True, entity_key=best_key, match_method="fuzzy",
                match_score=round(float(best_score), 2),
            )
        matches[company] = result

    return ok(matches=matches)

# ---------------------------------------------------------------------------
# Deterministic scoring (Tasks 6-7 - Requirements 4.1-4.6, 6.1-6.10)
# Implements design Low-Level Algorithms C, D, E, F.
# ---------------------------------------------------------------------------

# Write-off / elimination segment names forced to 0 revenue (Req 4.2).
_WRITEOFF_NAMES = {
    "intersegment elimination",
    "inter-segment elimination",
    "adjustment and write-off",
    "internal write-off",
    "number of write-offs",
}

# Generic filler tokens dropped before overlap scoring (design Algorithm D).
_STOPWORDS = {"and", "the", "of", "for", "to", "in", "a", "systems", "services"}


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords (design Algorithm D)."""
    if not text:
        return []
    lowered = re.sub(r"[^a-z0-9]+", " ", str(text).lower())
    return [t for t in lowered.split() if t and t not in _STOPWORDS]


def _normalized_text(text: str) -> str:
    """Normalized contiguous form for phrase matching."""
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


# ---------------------------------------------------------------------------
# Semantic matching via offline deterministic embeddings (Revision R2).
# Replaces keyword-overlap scoring. model2vec static model: CPU-only, no torch,
# offline after first download, bit-identical vectors for identical text.
# ---------------------------------------------------------------------------
import numpy as np  # noqa: E402

EMBEDDING_MODEL_NAME = "minishlab/potion-base-8M"
SIMILARITY_THRESHOLD = 0.35  # below this a segment scores 0 (Req 6.3)

# Exposure bands for presentation (Req 14/16). The continuous percentage stays
# the source of truth for all math; the band is an honest, committee-friendly
# label that avoids false precision (e.g. 21.7% vs 19.0% both read "Low").
# (lower_bound_inclusive, label). Checked high-to-low.
EXPOSURE_BANDS = [
    (60.0, "Core"),        # majority of revenue is thematic
    (40.0, "High"),        # substantial exposure
    (20.0, "Moderate"),    # meaningful but partial
    (5.0, "Low"),          # minor exposure
    (0.0, "Negligible"),   # effectively not exposed
]


def exposure_band(exposure_pct: float) -> str:
    """Map a continuous exposure percentage to a presentation band label."""
    for lower, label in EXPOSURE_BANDS:
        if exposure_pct >= lower:
            return label
    return "Negligible"

_embedding_model = None
_embed_cache: dict[str, "np.ndarray"] = {}


def _get_embedding_model():
    """Load and memoize the static embedding model (one instance per run)."""
    global _embedding_model
    if _embedding_model is None:
        from model2vec import StaticModel  # lazy import
        _embedding_model = StaticModel.from_pretrained(EMBEDDING_MODEL_NAME)
    return _embedding_model


def _embed(text: str) -> "np.ndarray":
    """Return an L2-normalized embedding vector for text (cached, deterministic)."""
    key = (text or "").strip().lower()
    if key in _embed_cache:
        return _embed_cache[key]
    model = _get_embedding_model()
    vec = np.asarray(model.encode([key]), dtype=np.float64)[0]
    norm = float(np.linalg.norm(vec))
    vec = vec / norm if norm > 0 else vec
    _embed_cache[key] = vec
    return vec


def _cosine(a: "np.ndarray", b: "np.ndarray") -> float:
    return float(np.dot(a, b))


def _semantic_similarity(text_a: str, text_b: str) -> float:
    """Cosine similarity in [0,1] between two texts (0 if either is empty)."""
    if not text_a or not text_a.strip() or not text_b or not text_b.strip():
        return 0.0
    sim = _cosine(_embed(text_a), _embed(text_b))
    return max(0.0, min(1.0, sim))


def _clean_segment_revenues(segments: list) -> list:
    """Apply Req 4 cleaning, returning per-segment working dicts with clean_rev."""
    working = []
    for seg in segments:
        name = seg.get("name")
        present = seg.get("revenue_present", False)
        revenue = seg.get("revenue")
        cleaned_to_zero = False
        clean_rev = 0.0
        if present and revenue is not None:
            clean_rev = max(0.0, float(revenue))  # Req 4.1 clamp negatives
            if clean_rev != float(revenue):
                cleaned_to_zero = True
        if name and name.strip().lower() in _WRITEOFF_NAMES:  # Req 4.2
            clean_rev = 0.0
            cleaned_to_zero = True
        working.append({
            "name": name,
            "revenue": revenue,
            "revenue_present": present,
            "clean_rev": clean_rev,
            "cleaned_to_zero": cleaned_to_zero,
            "excluded_missing_revenue": not present,  # Req 6.10
        })
    return working


def _extract_evidence(business_description: str, matched_activities: list) -> str:
    """Verbatim 1-500 char window around the first matched phrase (Req 6.7)."""
    if not business_description or not matched_activities:
        return ""
    desc = str(business_description)
    desc_lower = desc.lower()
    pos = -1
    for act in matched_activities:
        for token in _tokenize(act):
            idx = desc_lower.find(token)
            if idx != -1:
                pos = idx if pos == -1 else min(pos, idx)
    if pos == -1:
        return desc[:500]
    start = max(0, pos - 200)
    end = min(len(desc), pos + 200)
    snippet = desc[start:end].strip()
    return snippet[:500]


def score_company(company_row: dict, taxonomy: dict) -> dict:
    """Deterministically score one company's thematic exposure (no LLM).

    company_row keys used: company, sector (optional), segments (list),
    business_description (str), has_segment_data (bool).

    Returns a ScoreResult dict. Requirements: 4.*, 6.*, 3.4, 9.5, 10.4.
    """
    company = company_row.get("company", "")
    sector = company_row.get("sector")
    has_segment_data = company_row.get("has_segment_data", True)
    segments = company_row.get("segments", []) or []
    business_description = company_row.get("business_description", "") or ""

    base = {
        "ok": True,
        "company": company,
        "sector": sector,
        "company_exposure": 0.0,
        "has_segment_data": has_segment_data,
        "has_positive_revenue": False,
        "matched_sub_industries": [],
        "matched_activities": [],
        "evidence_snippet": "",
        "segment_breakdown": [],
        "sub_theme_attribution": {},
        "confidence": 0.0,
        "insufficient_data": True,
        "exposure_band": "Negligible",
    }

    # Unmatched / no-segment company -> flagged, exposure 0 (Req 3.4, 9.5, 10.4).
    if not has_segment_data or not segments:
        base["has_segment_data"] = False
        return base

    # --- Revenue cleaning + weighting (Req 4) ---
    working = _clean_segment_revenues(segments)
    total = sum(w["clean_rev"] for w in working)

    if total <= 0:  # Req 4.6 / 6.8
        base["segment_breakdown"] = [
            {
                "name": w["name"], "revenue": w["revenue"], "revenue_pct": 0.0,
                "alignment_score": 0.0, "matched_activities": [],
                "excluded_missing_revenue": w["excluded_missing_revenue"],
                "cleaned_to_zero": w["cleaned_to_zero"],
            }
            for w in working
        ]
        return base

    base["has_positive_revenue"] = True

    # --- Semantic alignment scoring per segment (Revision R2, Req 6.1-6.3) ---
    layer1 = taxonomy.get("layer1", []) if isinstance(taxonomy, dict) else []
    theme_text = (taxonomy.get("theme", "") if isinstance(taxonomy, dict) else "").strip()
    exposure = 0.0
    matched_subs: list[str] = []
    matched_acts: list[str] = []
    best_seg_score = 0.0
    best_seg_acts: list[str] = []
    breakdown = []
    # Sub-theme attribution: exposure points credited to each sub-industry (Req 14.2).
    sub_attr: dict[str, float] = {}

    for w in working:
        seg_pct = 100.0 * w["clean_rev"] / total
        # Score the SEGMENT NAME against each sub-theme. The full business
        # description is intentionally NOT mixed in here: it is the same for
        # every segment of a company and dilutes per-segment discrimination
        # (verified: appending it collapses distinct segments to a flat ~0.4).
        # The description is still used for the evidence snippet below.
        seg_name = (w["name"] or "").strip()

        seg_score = 0.0
        best_activity = None
        best_sub = None
        seg_matched_acts: list[str] = []

        best_activity = None
        # Best sub-theme by similarity, tracked independently so a segment that
        # matched the overall theme still gets attributed to a sub-theme.
        best_sub_score = -1.0

        # Direct match to the overall THEME rescues generic-but-on-theme segment
        # names (e.g. "Manufacture and Sale of Semiconductor Products" scores
        # 0.67 vs "semiconductors" but only ~0.3 vs a narrow sub-theme name).
        if theme_text:
            theme_sim = _semantic_similarity(seg_name, theme_text)
            if theme_sim > seg_score:
                seg_score = theme_sim

        for sub in layer1:
            sub_name = sub.get("sub_industry", "")
            # Candidate texts for this sub-theme: the sub-industry NAME plus its
            # activities. The sub-industry name is often the strongest signal
            # (e.g. segment "Foundry" vs "Semiconductor Foundry Services" = 0.75,
            # vs the narrower activity "CMOS process node fabrication" = 0.36).
            candidates = [(sub_name, None)] if sub_name else []
            candidates += [(activity, activity) for activity in (sub.get("activities", []) or [])]

            for cand_text, activity in candidates:
                sim = _semantic_similarity(seg_name, cand_text)
                if sim >= SIMILARITY_THRESHOLD and activity:
                    if activity not in seg_matched_acts:
                        seg_matched_acts.append(activity)
                    if activity not in matched_acts:
                        matched_acts.append(activity)
                if sim >= SIMILARITY_THRESHOLD and sub_name and sub_name not in matched_subs:
                    matched_subs.append(sub_name)
                if sim > seg_score:
                    seg_score = sim
                    best_activity = activity or sub_name
                # Track the best sub-theme for attribution regardless of theme match.
                if sim > best_sub_score:
                    best_sub_score = sim
                    best_sub = sub_name

        # Threshold weak matches to 0 so unrelated segments do not accrue exposure.
        if seg_score < SIMILARITY_THRESHOLD:
            seg_score = 0.0
            best_sub = None

        seg_score = round(min(1.0, max(0.0, seg_score)), 4)  # Req 6.2
        seg_contrib = seg_pct * seg_score
        exposure += seg_contrib

        # Credit this segment's exposure points to its best-matching sub-industry.
        if best_sub and seg_contrib > 0:
            sub_attr[best_sub] = sub_attr.get(best_sub, 0.0) + seg_contrib

        if seg_score > best_seg_score:
            best_seg_score = seg_score
            best_seg_acts = seg_matched_acts or ([best_activity] if best_activity else [])

        breakdown.append({
            "name": w["name"], "revenue": w["revenue"],
            "revenue_pct": round(seg_pct, 6), "alignment_score": seg_score,
            "matched_activities": seg_matched_acts,
            "matched_sub_industry": best_sub,
            "excluded_missing_revenue": w["excluded_missing_revenue"],
            "cleaned_to_zero": w["cleaned_to_zero"],
        })

    company_exposure = round(min(100.0, max(0.0, exposure)), 2)  # Req 6.4-6.5
    base["company_exposure"] = company_exposure
    base["exposure_band"] = exposure_band(company_exposure)
    base["matched_sub_industries"] = matched_subs
    base["matched_activities"] = matched_acts
    base["segment_breakdown"] = breakdown
    base["sub_theme_attribution"] = {k: round(v, 2) for k, v in sub_attr.items()}  # Req 14.2

    # Confidence = revenue-weighted average alignment across present segments (Req 14.1).
    # This reflects how much of the company's revenue is confidently thematic.
    conf = 0.0
    for b in breakdown:
        conf += (b["revenue_pct"] / 100.0) * b["alignment_score"]
    base["confidence"] = round(min(1.0, max(0.0, conf)), 4)
    # Insufficient data: positive revenue but nothing matched the theme (Req 14.4).
    base["insufficient_data"] = (company_exposure == 0.0)

    # Evidence from the highest-scoring segment (Req 6.7-6.8).
    if best_seg_score > 0:
        base["evidence_snippet"] = _extract_evidence(business_description, best_seg_acts)
    return base

# ---------------------------------------------------------------------------
# Portfolio-level metrics (Task 8 - Requirements 7.1-7.4, 10.4)
# Implements design Low-Level Algorithm G.
# ---------------------------------------------------------------------------
def calculate_portfolio_metrics(df_scores: "pd.DataFrame",
                                df_portfolio: "pd.DataFrame") -> dict:
    """Aggregate per-company exposures into portfolio-level metrics.

    df_scores: DataFrame with columns company, company_exposure, has_segment_data.
    df_portfolio: the loaded portfolio (Company, Sector, Portfolio wt, Reference wt).

    Returns a PortfolioMetrics dict. Requirements: 7.1 - 7.4, 10.4.
    """
    pf = df_portfolio.set_index("Company")
    scores = df_scores.set_index("company")

    portfolio_exp = 0.0
    benchmark_exp = 0.0
    contributions = []

    for company, prow in pf.iterrows():
        exposure = 0.0
        has_data = False
        if company in scores.index:
            exposure = float(scores.loc[company, "company_exposure"])
            has_data = bool(scores.loc[company, "has_segment_data"])

        pwt = float(prow.get("Portfolio wt") or 0.0)
        rwt = float(prow.get("Reference wt") or 0.0)

        contribution = exposure * pwt
        portfolio_exp += contribution       # Req 7.1
        benchmark_exp += exposure * rwt      # Req 7.2

        contributions.append({
            "company": company,
            "sector": prow.get("Sector"),
            "portfolio_wt": pwt,
            "reference_wt": rwt,
            "company_exposure": round(exposure, 2),
            "contribution": round(contribution, 4),
            "has_segment_data": has_data,
        })

    portfolio_exp = round(portfolio_exp, 2)
    benchmark_exp = round(benchmark_exp, 2)
    for c in contributions:
        c["exposure_band"] = exposure_band(c["company_exposure"])
    return ok(
        portfolio_thematic_exposure=portfolio_exp,
        benchmark_exposure=benchmark_exp,
        active_tilt=round(portfolio_exp - benchmark_exp, 2),  # Req 7.3
        portfolio_band=exposure_band(portfolio_exp),
        benchmark_band=exposure_band(benchmark_exp),
        contributions=sorted(contributions, key=lambda c: c["contribution"], reverse=True),
    )

# ---------------------------------------------------------------------------
# LLM taxonomy generation (Task 10 - Requirements 5.1-5.7, 10.1-10.2)
# The ONLY LLM call in the system. Implements design "LLM Taxonomy Design".
# ---------------------------------------------------------------------------
import json  # noqa: E402

TAXONOMY_MODEL = "openai/gpt-oss-120b"
_TAXONOMY_MAX_TOKENS = 2000  # generous: gpt-oss emits reasoning before JSON
_MIN_SUBS, _MAX_SUBS = 3, 4
_MIN_ACTS, _MAX_ACTS = 2, 3

_TAXONOMY_SYSTEM_PROMPT = (
    "You are an equity research assistant. First decide whether the user's "
    "input is a plausible INVESTMENT THEME - a macroeconomic, technological, "
    "or sector trend that companies can have business exposure to (e.g. "
    "'renewable energy', 'AI infrastructure', 'digital payments'). "
    "A person's name, a greeting, random words, or anything that is not an "
    "investable trend is NOT a valid theme. Do NOT invent a theme from "
    "nonsense input.\n"
    "Output ONLY a single JSON object, no prose and no markdown fences, with: "
    "\"valid_theme\" (boolean), \"reason\" (short string explaining the "
    "decision), and - ONLY when valid_theme is true - \"layer1\": an array of "
    "3 to 4 objects, each with \"sub_industry\" (short string) and "
    "\"activities\" (array of 2 to 3 short, concrete business-activity keyword "
    "phrases; multi-word phrases beat single generic words). "
    "When valid_theme is false, omit layer1 or set it to an empty array."
)


def _extract_json_object(text: str) -> str | None:
    """Strip fences and return the first balanced {...} block, else None."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    start = t.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    return None


# Sentinel returned when the model judged the input not to be a valid theme.
_INVALID_THEME = {"__invalid_theme__": True}


def _validate_taxonomy(obj, theme: str) -> dict | None:
    """Validate + normalize the taxonomy shape.

    Returns a Taxonomy dict on success, the _INVALID_THEME sentinel when the
    model flagged the input as not an investment theme, or None if the response
    is malformed. Lenient on counts, strict on types.
    """
    if not isinstance(obj, dict):
        return None
    # Explicit "not a valid investment theme" verdict from the model.
    if obj.get("valid_theme") is False:
        return dict(_INVALID_THEME, reason=str(obj.get("reason", "")).strip())
    layer1 = obj.get("layer1")
    if not isinstance(layer1, list) or not layer1:
        return None
    clean_subs = []
    for sub in layer1:
        if not isinstance(sub, dict):
            continue
        name = sub.get("sub_industry")
        acts = sub.get("activities")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(acts, list):
            continue
        str_acts = [a.strip() for a in acts if isinstance(a, str) and a.strip()]
        if not str_acts:
            continue
        clean_subs.append({"sub_industry": name.strip(), "activities": str_acts[:_MAX_ACTS]})
    if not clean_subs:
        return None
    clean_subs = clean_subs[:_MAX_SUBS]  # clamp upper bound
    return {"theme": theme, "layer1": clean_subs}


# ---------------------------------------------------------------------------
# Taxonomy caching for reproducibility (Revision R2, Req 13).
# Keyed by the exact theme string. temperature=0 alone does not guarantee
# identical LLM output; caching does, for a given theme.
# ---------------------------------------------------------------------------
import os.path as _osp  # noqa: E402

_TAXONOMY_CACHE_DIR = ".taxonomy_cache"
_taxonomy_mem_cache: dict[str, dict] = {}


# Bump when the taxonomy contract changes (e.g. adding the valid-theme gate),
# so pre-gate cached junk ("heyyyyyaa" etc.) is automatically invalidated.
_CACHE_VERSION = 2


def _cache_path(theme: str) -> str:
    import hashlib
    h = hashlib.sha256(theme.strip().lower().encode("utf-8")).hexdigest()[:16]
    return _osp.join(_TAXONOMY_CACHE_DIR, f"{h}.json")


def _load_cached_taxonomy(theme: str):
    key = theme.strip().lower()
    if key in _taxonomy_mem_cache:
        return _taxonomy_mem_cache[key]
    path = _cache_path(theme)
    if _osp.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            # Only honor cache entries written under the current version AND
            # that still pass taxonomy validation (grandfathered junk fails).
            if not isinstance(payload, dict) or payload.get("_v") != _CACHE_VERSION:
                return None
            tax = payload.get("taxonomy")
            validated = _validate_taxonomy(tax, theme)
            if validated is None or (
                isinstance(validated, dict) and validated.get("__invalid_theme__")
            ):
                return None
            _taxonomy_mem_cache[key] = tax
            return tax
        except Exception:
            return None
    return None


def _store_cached_taxonomy(theme: str, taxonomy: dict) -> None:
    key = theme.strip().lower()
    _taxonomy_mem_cache[key] = taxonomy
    try:
        os.makedirs(_TAXONOMY_CACHE_DIR, exist_ok=True)
        with open(_cache_path(theme), "w", encoding="utf-8") as f:
            json.dump({"_v": _CACHE_VERSION, "taxonomy": taxonomy}, f, indent=2)
    except Exception:
        pass  # cache write failure is non-fatal


def generate_taxonomy(theme: str, force_refresh: bool = False) -> dict:
    """Expand a theme into a two-layer taxonomy via a single Groq call.

    Reuses a cached Taxonomy for a previously seen theme unless force_refresh
    is set (Req 13). Returns {"ok": True, "taxonomy": Taxonomy, "cached": bool}
    or a handled error dict. Requirements: 5.1 - 5.7, 10.1 - 10.2, 13.1 - 13.5.
    """
    if not force_refresh:
        cached = _load_cached_taxonomy(theme)
        if cached is not None:
            return ok(taxonomy=cached, cached=True)

    api_key = get_secret("GROQ_API_KEY")
    if not api_key:  # Req 5.6 / 10.1 - before any network call
        return err(MISSING_API_KEY, "Groq API key is missing. Set GROQ_API_KEY in your .env.")

    try:
        from groq import Groq  # lazy import
    except Exception as exc:  # pragma: no cover
        return err(LLM_UNREACHABLE, "Groq client library is unavailable.", detail=str(exc))

    client = Groq(api_key=api_key)

    def _call(messages):
        return client.chat.completions.create(
            model=TAXONOMY_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=_TAXONOMY_MAX_TOKENS,
        )

    messages = [
        {"role": "system", "content": _TAXONOMY_SYSTEM_PROMPT},
        {"role": "user", "content": theme},
    ]

    # --- First (successful-path) call (Req 5.1) ---
    try:
        resp = _call(messages)
    except Exception as exc:
        return _map_groq_exception(exc)

    raw = resp.choices[0].message.content or ""
    taxonomy = _parse_and_validate(raw, theme)
    if isinstance(taxonomy, dict) and taxonomy.get("__invalid_theme__"):
        reason = taxonomy.get("reason") or "This does not look like an investment theme."
        return err(INVALID_THEME,
                   f"'{theme}' is not a recognised investment theme. {reason}")
    if taxonomy is not None:
        _store_cached_taxonomy(theme, taxonomy)
        return ok(taxonomy=taxonomy, cached=False)

    # --- One repair attempt (Req 5.7) ---
    repair_messages = messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": "That was not valid. Return corrected strict JSON only."},
    ]
    try:
        resp2 = _call(repair_messages)
    except Exception as exc:
        return _map_groq_exception(exc)

    raw2 = resp2.choices[0].message.content or ""
    taxonomy = _parse_and_validate(raw2, theme)
    if isinstance(taxonomy, dict) and taxonomy.get("__invalid_theme__"):
        reason = taxonomy.get("reason") or "This does not look like an investment theme."
        return err(INVALID_THEME,
                   f"'{theme}' is not a recognised investment theme. {reason}")
    if taxonomy is not None:
        _store_cached_taxonomy(theme, taxonomy)
        return ok(taxonomy=taxonomy, cached=False)

    return err(LLM_BAD_JSON, "The model did not return a valid taxonomy.", detail=raw2[:300])


def _parse_and_validate(raw: str, theme: str):
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        extracted = _extract_json_object(raw)
        if extracted is None:
            return None
        try:
            obj = json.loads(extracted)
        except json.JSONDecodeError:
            return None
    return _validate_taxonomy(obj, theme)


def _map_groq_exception(exc) -> dict:
    """Map a Groq SDK exception to a handled error (Req 5.6, 10.2)."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if status == 401 or "Authentication" in name or "PermissionDenied" in name:
        return err(INVALID_API_KEY, "Groq rejected the API key (invalid or unauthorized).", detail=str(exc))
    return err(LLM_UNREACHABLE, "Could not reach the Groq API.", detail=str(exc))

# ---------------------------------------------------------------------------
# Orchestration helper used by the UI (wires the engine together).
# ---------------------------------------------------------------------------
def build_company_rows(portfolio_df: "pd.DataFrame", segments: dict, matches: dict) -> list:
    """Assemble per-company inputs for score_company from matches + segments."""
    rows = []
    for _, prow in portfolio_df.iterrows():
        company = str(prow["Company"])
        mr = matches.get(company, {"matched": False, "entity_key": None})
        if mr.get("matched") and mr.get("entity_key") in segments:
            rec = segments[mr["entity_key"]]
            rows.append({
                "company": company, "sector": prow.get("Sector"),
                "has_segment_data": True, "segments": rec["segments"],
                "business_description": rec.get("business_description", ""),
                "entity_name": rec.get("entity_name"),
                "match_method": mr.get("match_method"),
            })
        else:
            rows.append({
                "company": company, "sector": prow.get("Sector"),
                "has_segment_data": False, "segments": [],
                "business_description": "", "entity_name": None,
                "match_method": "none",
            })
    return rows


def analyze_portfolio(portfolio_df: "pd.DataFrame", segments: dict, taxonomy: dict) -> dict:
    """Score every company and compute portfolio metrics for a given taxonomy.

    Returns {"ok": True, "metrics": PortfolioMetrics, "scores": {company: ScoreResult}}.
    """
    match_res = match_companies(portfolio_df, segments)
    matches = match_res["matches"]
    rows = build_company_rows(portfolio_df, segments, matches)

    scores: dict[str, dict] = {}
    score_records = []
    for row in rows:
        sr = score_company(row, taxonomy)
        # Carry match provenance into the result for source traceability.
        mr = matches.get(row["company"], {})
        sr["match_method"] = mr.get("match_method", "none")
        sr["match_score"] = mr.get("match_score")
        sr["entity_name"] = row.get("entity_name")
        scores[row["company"]] = sr
        score_records.append({
            "company": row["company"],
            "company_exposure": sr["company_exposure"],
            "has_segment_data": sr["has_segment_data"],
        })

    df_scores = pd.DataFrame(score_records)
    metrics = calculate_portfolio_metrics(df_scores, portfolio_df)

    # --- Portfolio-level sub-theme attribution + confidence (Req 14.3) ---
    pf = portfolio_df.set_index("Company")
    sub_theme_exposure: dict[str, float] = {}
    for company, sr in scores.items():
        if company not in pf.index:
            continue
        pwt = float(pf.loc[company].get("Portfolio wt") or 0.0)
        for sub, pts in sr.get("sub_theme_attribution", {}).items():
            sub_theme_exposure[sub] = sub_theme_exposure.get(sub, 0.0) + pts * pwt
    metrics["sub_theme_exposure"] = {k: round(v, 2) for k, v in
                                     sorted(sub_theme_exposure.items(),
                                            key=lambda kv: kv[1], reverse=True)}

    # Attach confidence + flags to each contribution row for the UI.
    for c in metrics["contributions"]:
        sr = scores.get(c["company"], {})
        c["confidence"] = sr.get("confidence", 0.0)
        c["insufficient_data"] = sr.get("insufficient_data", True)

    # --- Data-coverage diagnostics for transparency (Schroders brief part C) ---
    no_segment, insufficient, matched = [], [], []
    for company, sr in scores.items():
        if not sr.get("has_segment_data"):
            no_segment.append(company)
        elif sr.get("insufficient_data"):
            insufficient.append(company)
        else:
            matched.append(company)
    total = len(scores)
    metrics["coverage"] = {
        "total_companies": total,
        "matched_with_exposure": len(matched),
        "matched_no_exposure": len(insufficient),      # had segments but nothing matched the theme
        "no_segment_data": len(no_segment),            # not found in the S&P export
        "no_segment_companies": sorted(no_segment),
        "insufficient_companies": sorted(insufficient),
    }

    return ok(metrics=metrics, scores=scores, matches=matches,
              metadata=analysis_metadata())


def analysis_metadata() -> dict:
    """Return the methodology parameters that produced the scores (transparency)."""
    return {
        "embedding_model": EMBEDDING_MODEL_NAME,
        "similarity_threshold": SIMILARITY_THRESHOLD,
        "taxonomy_model": TAXONOMY_MODEL,
        "fuzzy_match_threshold": FUZZY_THRESHOLD,
        "exposure_bands": EXPOSURE_BANDS,
        "notes": (
            "The LLM is used only to expand the theme into sub-themes. "
            "Exposure is computed deterministically: each company segment's "
            "reported revenue share is weighted by the maximum semantic "
            "similarity (cosine, offline embeddings) between the segment name "
            "and the theme / sub-themes. Same inputs always yield the same result."
        ),
    }
