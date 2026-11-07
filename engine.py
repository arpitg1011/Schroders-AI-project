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

# Abbreviation expansion applied per-token BEFORE suffix-stripping, so that
# portfolio short-forms line up with the fuller S&P entity names
# (e.g. "GRP HLDG" -> "GROUP HOLDING", "TELECOM" -> "TELECOMMUNICATIONS").
# Keys and values are upper-case. Multi-word expansions are allowed.
_ABBREVIATIONS = {
    "GRP": "GROUP",
    "HLDG": "HOLDING",
    "HLDGS": "HOLDING",
    "HLDGNS": "HOLDING",
    "TELECOM": "TELECOMMUNICATIONS",
    "TELECOMS": "TELECOMMUNICATIONS",
    "INTL": "INTERNATIONAL",
    "INT": "INTERNATIONAL",
    "TECH": "TECHNOLOGY",
    "MFG": "MANUFACTURING",
    "IND": "INDUSTRIES",
    "INDS": "INDUSTRIES",
    "FIN": "FINANCIAL",
    "NATL": "NATIONAL",
    "BK": "BANK",
    "COMMS": "COMMUNICATIONS",
    "SVCS": "SERVICES",
    "SEMICON": "SEMICONDUCTOR",
    "ELEC": "ELECTRONICS",
}

# Explicit aliases for names that NO general algorithm can bridge: acronyms
# and trade names that share no tokens with the official entity name. Keys are
# normalized portfolio names; values are normalized target names. Kept small
# and specific on purpose (an over-broad alias map causes silent mis-matches).
_NAME_ALIASES = {
    "OCBC BANK": "OVERSEA CHINESE BANKING",
    "OCBC": "OVERSEA CHINESE BANKING",
}

# Single-letter share-class tokens (A/B/H shares) and 2-letter listing codes
# that trail a portfolio name (e.g. "PING AN INSURANCE H", "... A") and should
# not block a match. Only stripped when they appear as the LAST token.
_SHARE_CLASS_TOKENS = {"A", "B", "H", "N", "R", "S"}
_PAREN_RE = re.compile(r"\([^)]*\)")
_PUNCT_RE = re.compile(r"[.,&/'\-]")
_WS_RE = re.compile(r"\s+")


def _normalize_company_name(name: str | None) -> str:
    """Normalize a company name for matching (Req 3.2). Idempotent.

    Steps: upper-case, drop parentheticals and punctuation, expand common
    abbreviations, strip a trailing share-class letter, then drop corporate
    suffix words. Idempotent because every step is stable when re-applied to
    its own output (expansions map to already-expanded forms; suffix words are
    removed, so re-running finds none).
    """
    if not name:
        return ""
    s = str(name).upper().strip()
    s = _PAREN_RE.sub(" ", s)        # remove parentheticals, e.g. "(HK)"
    s = _PUNCT_RE.sub(" ", s)        # remove punctuation
    s = _WS_RE.sub(" ", s).strip()

    tokens = [t for t in s.split(" ") if t]

    # Expand abbreviations token-by-token (may introduce multi-word forms).
    expanded: list[str] = []
    for t in tokens:
        rep = _ABBREVIATIONS.get(t)
        if rep:
            expanded.extend(rep.split(" "))
        else:
            expanded.append(t)
    tokens = expanded

    # Strip a single trailing share-class letter (keep if it is the only token).
    if len(tokens) > 1 and tokens[-1] in _SHARE_CLASS_TOKENS:
        tokens = tokens[:-1]

    # Drop corporate suffix words.
    tokens = [t for t in tokens if t not in _CORP_SUFFIXES]
    result = " ".join(tokens)

    # Resolve explicit acronym/trade-name aliases (idempotent: alias targets are
    # themselves already-normalized and not alias keys).
    return _NAME_ALIASES.get(result, result)


def _portfolio_ticker_symbol(bloomberg_ticker: str | None) -> str | None:
    """Leading symbol of a Bloomberg ticker, e.g. '2330 TT Equity' -> '2330'."""
    if not bloomberg_ticker:
        return None
    parts = str(bloomberg_ticker).strip().split()
    return parts[0].upper() if parts else None


def _subset_name_match(norm: str, seg_keys: list) -> str | None:
    """Return the one segment key whose tokens form a superset/subset of the
    portfolio name's tokens, sharing a distinctive multi-token prefix.

    Used only as a last resort after exact/ticker/fuzzy have failed. To avoid
    false matches it requires: at least two shared leading tokens, every token
    of the shorter name present in the longer, and a UNIQUE winner (if two
    segment keys both qualify, we abstain).
    """
    p = [t for t in norm.split(" ") if t]
    if len(p) < 2:
        return None
    pset = set(p)
    candidates = []
    for key in seg_keys:
        k = [t for t in key.split(" ") if t]
        if len(k) < 2:
            continue
        kset = set(k)
        shorter, longer = (pset, kset) if len(pset) <= len(kset) else (kset, pset)
        # Every token of the shorter name must appear in the longer name.
        if not shorter.issubset(longer):
            continue
        # Require the first two tokens to agree (a distinctive shared prefix).
        if p[0] == k[0] and p[1] == k[1]:
            candidates.append(key)
    return candidates[0] if len(candidates) == 1 else None


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
        # Combine token_sort_ratio with token_set_ratio: the latter is robust
        # when one name is a subset of the other (e.g. "PING AN INSURANCE" vs
        # "PING AN INSURANCE OF CHINA", or "SINGAPORE TELECOMMUNICATIONS" vs a
        # longer official form) which plain sort-ratio penalises for length.
        best_key, best_score = None, 0.0
        for key in seg_keys:
            score = max(fuzz.token_sort_ratio(norm, key),
                        fuzz.token_set_ratio(norm, key))
            if score > best_score:
                best_key, best_score = key, score
        if best_key is not None and best_score >= FUZZY_THRESHOLD:
            result.update(
                matched=True, entity_key=best_key, match_method="fuzzy",
                match_score=round(float(best_score), 2),
            )
        else:
            # Subset bridge: accept when the portfolio name's tokens are fully
            # contained in a single segment key's tokens (or vice versa) and
            # share a distinctive multi-token prefix. This catches abbreviated
            # portfolio names without loosening the global fuzzy threshold.
            sub_key = _subset_name_match(norm, seg_keys)
            if sub_key is not None:
                result.update(
                    matched=True, entity_key=sub_key, match_method="subset",
                    match_score=None,
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


def _score_company_binary(base: dict, working: list, total: float,
                          match_map: dict, business_description: str) -> dict:
    """Binary pure-revenue scoring for one company given the LLM match map.

    base: the pre-filled ScoreResult skeleton (has_positive_revenue already set).
    working: cleaned segments with clean_rev. total: sum of clean_rev (>0).
    match_map: {segment_name: {"sub_theme", "reason"}} for matched segments.
    """
    exposure = 0.0
    matched_subs: list[str] = []
    matched_reasons: list[str] = []
    breakdown = []
    sub_attr: dict[str, float] = {}
    evidence_sub = None

    for w in working:
        seg_name = (w["name"] or "").strip()
        seg_pct = 100.0 * w["clean_rev"] / total
        hit = match_map.get(seg_name)
        is_match = hit is not None
        sub = hit["sub_theme"] if is_match else None
        reason = hit.get("reason", "") if is_match else ""

        if is_match:
            exposure += seg_pct  # 100% of this segment's revenue share
            if sub and sub not in matched_subs:
                matched_subs.append(sub)
            if sub:
                sub_attr[sub] = sub_attr.get(sub, 0.0) + seg_pct
            if reason:
                matched_reasons.append(reason)
            if evidence_sub is None:
                evidence_sub = (seg_pct, sub, reason)

        breakdown.append({
            "name": w["name"], "revenue": w["revenue"],
            "revenue_pct": round(seg_pct, 6),
            # alignment_score is now binary (1.0 matched / 0.0 not) so existing
            # UI that reads it still works, but it is no longer a similarity.
            "alignment_score": 1.0 if is_match else 0.0,
            "matched_activities": [],
            "matched_sub_industry": sub,
            "match_reason": reason,
            "excluded_missing_revenue": w["excluded_missing_revenue"],
            "cleaned_to_zero": w["cleaned_to_zero"],
        })

    company_exposure = round(min(100.0, max(0.0, exposure)), 2)
    base["company_exposure"] = company_exposure
    base["exposure_band"] = exposure_band(company_exposure)
    base["matched_sub_industries"] = matched_subs
    base["matched_activities"] = []
    base["segment_breakdown"] = breakdown
    base["sub_theme_attribution"] = {k: round(v, 2) for k, v in sub_attr.items()}
    # Numeric confidence is retired (it was exposure relabeled). A qualitative
    # confidence will replace it later; keep the field as None so the UI can
    # detect "not computed" rather than show a fake 0.
    base["confidence"] = None
    base["match_reasons"] = matched_reasons
    base["insufficient_data"] = (company_exposure == 0.0)

    # Evidence snippet from the best matched segment's business description.
    if evidence_sub is not None and business_description:
        # Use the matched sub-theme name as the search anchor for a verbatim window.
        anchor = [evidence_sub[1]] if evidence_sub[1] else []
        base["evidence_snippet"] = _extract_evidence(business_description, anchor)
    return base


def score_company(company_row: dict, taxonomy: dict,
                  match_map: dict | None = None) -> dict:
    """Score one company's thematic exposure from reported segment revenue.

    company_row keys used: company, sector (optional), segments (list),
    business_description (str), has_segment_data (bool).

    match_map: when provided (the primary path), it is a dict
    {segment_name: {"sub_theme", "reason"}} of the segments the LLM judged to
    belong to the theme. Exposure is then PURELY revenue-based: a matched
    segment contributes 100% of its revenue share, an unmatched one 0%. No
    similarity, no partial credit.

    When match_map is None (offline fallback, no Groq), the legacy embedding
    cosine path is used so the app still produces a result without the LLM.

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

    # --- Primary path: binary LLM match -> pure revenue exposure (Req 6) ---
    # A segment the LLM judged part of the theme contributes 100% of its
    # revenue share; everything else contributes 0%. No similarity, no partial
    # credit. match_map is {segment_name: {"sub_theme", "reason"}}.
    if match_map is not None:
        return _score_company_binary(base, working, total, match_map,
                                     business_description)

    # --- Fallback path: offline embedding cosine (no Groq available) ---
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
_TAXONOMY_MAX_TOKENS = 3000  # generous: gpt-oss emits reasoning before JSON; up to 10 subs
_MIN_SUBS, _MAX_SUBS = 3, 10   # user-selectable count range (slider 3-10, default 4)
_DEFAULT_SUBS = 4
_MIN_ACTS, _MAX_ACTS = 2, 3

_TAXONOMY_SYSTEM_PROMPT = (
    "You are an equity research assistant that decomposes INVESTMENT THEMES for "
    "a portfolio-exposure tool.\n\n"
    "VALIDITY TEST - be INCLUSIVE. The input is a VALID theme if companies could "
    "plausibly have DIFFERENTIAL business exposure to it - i.e. an analyst could "
    "reasonably map it to industries, products, or revenue streams that it helps "
    "or hurts. This is broad on purpose and includes:\n"
    "- sector / technology trends (e.g. 'AI infrastructure', 'digital payments');\n"
    "- macroeconomic forces (e.g. 'rising interest rates', 'inflation');\n"
    "- commodities & materials (e.g. 'copper demand', 'rare earth minerals');\n"
    "- GEOPOLITICAL or POLICY events (e.g. 'US-China trade tensions', 'carbon "
    "tax', 'Middle East conflict');\n"
    "- WEATHER / CLIMATE / NATURAL events with economic transmission (e.g. "
    "'El Nino', 'drought', 'hurricane season') - these ARE valid because they "
    "move agriculture, food producers, insurers, energy, water and commodities.\n\n"
    "Only mark INVALID when there is genuinely NO economic/business transmission "
    "to companies: a person's name, a greeting, a random string, or pure "
    "gibberish. When in doubt, lean towards VALID and build the taxonomy around "
    "the industries the event or trend most affects. Do NOT reject something "
    "merely because it is 'not a traditional investment theme' - if it moves "
    "industries, it qualifies.\n\n"
    "Output ONLY a single JSON object, no prose and no markdown fences, with: "
    "\"valid_theme\" (boolean), \"reason\" (short string explaining the "
    "decision), and - ONLY when valid_theme is true - \"layer1\": an array of "
    "EXACTLY {n} objects, each with \"sub_industry\" (short string naming an "
    "affected industry, each DISTINCT from the others) and \"activities\" "
    "(array of 2 to 3 short, concrete business-activity keyword phrases; "
    "multi-word phrases beat single generic words). "
    "When valid_theme is false, omit layer1 or set it to an empty array."
)

# Prompt fragment appended when topping up an existing saved taxonomy with
# additional, non-duplicate sub-themes.
_TAXONOMY_TOPUP_TEMPLATE = (
    "The following sub-industries ALREADY exist for this theme and MUST NOT be "
    "repeated or duplicated (choose genuinely different ones):\n{existing}\n"
    "Generate EXACTLY {n} NEW, distinct sub-industries not in that list."
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
    clean_subs = clean_subs[:_MAX_SUBS]  # hard safety cap at 10
    return {"theme": theme, "layer1": clean_subs}


# ---------------------------------------------------------------------------
# Taxonomy caching for reproducibility (Revision R2, Req 13).
# Keyed by the exact theme string. temperature=0 alone does not guarantee
# identical LLM output; caching does, for a given theme.
# ---------------------------------------------------------------------------
import os.path as _osp  # noqa: E402

# Committed cache dir (not hidden, not gitignored) so precomputed demo-theme
# caches ship with the app and load instantly on the hosted site. Runtime
# writes for new themes also land here locally.
_TAXONOMY_CACHE_DIR = "cache/taxonomy"
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


# ---------------------------------------------------------------------------
# Global preset themes (committed, available to EVERYONE).
# A curated set of themes each with a canonical 10-sub-theme taxonomy and a
# precomputed match map, shipped in cache/. These load instantly for any user
# (logged-in or anonymous) and any sub-theme count 3..10 is served by taking
# the top-N of the canonical 10 - no live LLM call. Themes NOT in this set go
# through the normal per-profile (Supabase) or live-generation path.
# ---------------------------------------------------------------------------
_PRESETS_PATH = "cache/presets.json"
_presets_cache: dict | None = None


def load_presets() -> dict:
    """Load the committed preset taxonomies: {theme_key: {theme, layer1[10]}}.
    Memoized. Missing/invalid file -> empty dict (no presets)."""
    global _presets_cache
    if _presets_cache is not None:
        return _presets_cache
    data: dict = {}
    try:
        if _osp.exists(_PRESETS_PATH):
            with open(_PRESETS_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if (isinstance(v, dict) and isinstance(v.get("layer1"), list)
                            and v["layer1"]):
                        data[k.strip().lower()] = v
    except Exception:
        data = {}
    _presets_cache = data
    return data


def get_preset_taxonomy(theme: str) -> dict | None:
    """Canonical taxonomy for a preset theme (exact, case-insensitive match),
    else None. Returns a copy so callers can trim/mutate freely."""
    presets = load_presets()
    hit = presets.get((theme or "").strip().lower())
    if not hit:
        return None
    return {"theme": hit.get("theme", theme),
            "layer1": [dict(s) for s in hit["layer1"]]}


def is_preset_theme(theme: str) -> bool:
    return (theme or "").strip().lower() in load_presets()


def generate_taxonomy(theme: str, n: int = _DEFAULT_SUBS, force_refresh: bool = False,
                      existing: list | None = None) -> dict:
    """Expand a theme into a taxonomy of n sub-themes via a single Groq call.

    n: how many sub-themes to produce (clamped to 3..10).
    existing: when provided, generate n NEW sub-themes distinct from these
              (used to "top up" a saved taxonomy) and the result contains ONLY
              the new ones (the caller merges).
    force_refresh: bypass the anonymous file/mem cache.

    Returns {"ok": True, "taxonomy": Taxonomy, "cached": bool} or a handled error.
    Note: per-profile permanent caching lives in supabase_client; the file/mem
    cache here is only the anonymous fallback and is used only for the default
    count with no top-up.
    """
    n = max(_MIN_SUBS, min(_MAX_SUBS, int(n)))
    topping_up = bool(existing)

    # Anonymous fallback cache: only for a plain (default-count, no top-up) call.
    if not force_refresh and not topping_up and n == _DEFAULT_SUBS:
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

    def _call(messages):
        return _groq_chat(messages, _TAXONOMY_MAX_TOKENS)

    system = _TAXONOMY_SYSTEM_PROMPT.format(n=n)
    user = theme
    if topping_up:
        existing_names = "\n".join(f"- {name}" for name in existing)
        user = theme + "\n\n" + _TAXONOMY_TOPUP_TEMPLATE.format(
            existing=existing_names, n=n)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
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
        # Only the anonymous default-count, non-top-up call writes the file cache.
        if not topping_up and n == _DEFAULT_SUBS:
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
        if not topping_up and n == _DEFAULT_SUBS:
            _store_cached_taxonomy(theme, taxonomy)
        return ok(taxonomy=taxonomy, cached=False)

    return err(LLM_BAD_JSON, "The model did not return a valid taxonomy.", detail=raw2[:300])


def _sub_theme_key(name: str) -> str:
    """Normalized key for comparing sub-theme names (dedupe on top-up).

    Lowercase, collapse whitespace, drop punctuation so "Solar PV" and
    "solar-pv" are treated as the same sub-theme.
    """
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def merge_sub_themes(existing: list, new: list, limit: int = _MAX_SUBS) -> list:
    """Append `new` sub-themes to `existing`, skipping near-duplicate names.

    Both arguments are lists of {"sub_industry", "activities"} dicts. Existing
    entries are always kept (and come first). Returns the merged list capped at
    `limit`. De-duplication is by normalized sub_industry name.
    """
    merged = list(existing or [])
    seen = {_sub_theme_key(s.get("sub_industry", "")) for s in merged}
    for sub in (new or []):
        key = _sub_theme_key(sub.get("sub_industry", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(sub)
        if len(merged) >= limit:
            break
    return merged


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
# Groq client with multi-key failover.
# A primary key (GROQ_API_KEY) is used for every call; if it is rate-limited
# (429 TPM) or a request is too large for its tier (413), we transparently fail
# over to one or more backup keys (GROQ_API_KEY_2, _3, ...). This roughly
# multiplies the free-tier TPM budget by the number of keys. Keys live only in
# .env / st.secrets - never in source.
# ---------------------------------------------------------------------------
_GROQ_KEY_NAMES = ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3")
_groq_clients: dict[str, object] = {}


def _groq_keys() -> list[str]:
    """Ordered list of configured Groq keys (primary first). Deduplicated."""
    keys: list[str] = []
    for name in _GROQ_KEY_NAMES:
        val = get_secret(name)
        if val and val not in keys:
            keys.append(val)
    return keys


def _groq_client_for(api_key: str):
    """Cached Groq client per key."""
    client = _groq_clients.get(api_key)
    if client is None:
        from groq import Groq  # lazy import
        client = Groq(api_key=api_key)
        _groq_clients[api_key] = client
    return client


def _is_rate_limit(exc) -> bool:
    """True for a throttle / too-large-for-tier error worth failing over on."""
    status = getattr(exc, "status_code", None)
    if status in (429, 413):
        return True
    msg = str(exc).lower()
    return "rate_limit" in msg or "rate limit" in msg or "too large" in msg


def _is_json_validation_error(exc) -> bool:
    """True for a 400 where the model's JSON-mode output failed validation
    (usually truncation on a large batch). The caller splits the batch."""
    status = getattr(exc, "status_code", None)
    if status != 400:
        return False
    msg = str(exc).lower()
    return "json" in msg and ("validate" in msg or "failed_generation" in msg)


def _groq_chat(messages: list, max_tokens: int, model: str | None = None):
    """Run a chat completion, failing over across configured keys on a rate
    limit. `model` defaults to the taxonomy model when not given. Returns the
    SDK response. Raises the last exception if every key is exhausted (the
    caller maps it to a handled error).
    """
    model = model or TAXONOMY_MODEL
    keys = _groq_keys()
    if not keys:
        raise RuntimeError("No Groq API key configured.")
    last_exc = None
    for api_key in keys:
        client = _groq_client_for(api_key)
        try:
            return client.chat.completions.create(
                model=model, messages=messages,
                response_format={"type": "json_object"},
                temperature=0, max_tokens=max_tokens,
            )
        except Exception as exc:
            last_exc = exc
            if _is_rate_limit(exc):
                continue  # try the next key
            raise  # non-rate-limit error: surface immediately
    # Every key was rate-limited.
    raise last_exc


# ---------------------------------------------------------------------------
# LLM segment matcher (primary scoring path).
# One batched, cached Groq call per (theme, taxonomy, segment-set) classifies
# each company segment to a sub-theme (or none). It returns ONLY a binary
# match + the sub-theme name + a short reason - NEVER a number. Exposure is
# computed deterministically from reported segment revenue afterwards, so the
# "LLM never invents exposure percentages" guarantee holds (Req 6.9).
#
# Reproducibility: the full match map is cached keyed by a hash of the exact
# inputs (theme, sub-theme names + activities, and every company/segment name),
# mirroring the taxonomy cache. Identical inputs reuse the cached decision.
# ---------------------------------------------------------------------------
import time  # noqa: E402

_MATCH_MODEL = TAXONOMY_MODEL
_MATCH_MAX_TOKENS = 3000  # output cap per chunk (JSON for a chunk of companies)
# Keep each request (input + its JSON output) under the Groq free-tier ~8000
# TPM per-request cap. We budget the INPUT side to this many tokens so the
# model's reasoning + JSON output still fits; chunks of companies are sent
# across multiple calls and merged.
_MATCH_CHUNK_TOKEN_BUDGET = 1200   # smaller chunks -> smaller JSON output, fewer truncations
_MATCH_MAX_RETRIES = 4
_MATCH_RETRY_WAIT_S = 20   # TPM windows are per-minute; wait out the throttle
_MATCH_CACHE_DIR = "cache/match"   # committed so precomputed caches ship with the app
_MATCH_CACHE_VERSION = 1
_match_mem_cache: dict[str, dict] = {}


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) for request-size budgeting."""
    return max(1, len(text) // 4)

_MATCH_SYSTEM_PROMPT = (
    "You are an equity research assistant deciding, for a THEMATIC portfolio "
    "audit, whether each reported BUSINESS SEGMENT of a company is part of a "
    "given investment THEME.\n\n"
    "You are given the theme, a list of its SUB-THEMES (each a sub-industry "
    "with example activities), and a list of companies with their reported "
    "segment names. For EVERY segment decide:\n"
    "- Does this segment's business genuinely belong to the theme? Judge by "
    "ECONOMIC MEANING, not word overlap. A chip foundry IS core to "
    "'semiconductors'; a bank's 'Insurance' segment is NOT part of 'electric "
    "vehicles' just because of a shared word; 'Power Supply' (electronics) is "
    "NOT 'water supply'.\n"
    "- If yes, assign the SINGLE best-fitting sub-theme by its exact name.\n"
    "- Generic/eliminations/unallocated/'others'/corporate segments are NOT a "
    "match.\n\n"
    "Return ONLY a JSON object, no prose, no markdown fences, shaped as:\n"
    '{\"companies\": [{\"company\": <exact company string given>, '
    '\"segments\": [{\"name\": <exact segment name given>, '
    '\"match\": true|false, \"sub_theme\": <exact sub-theme name or null>, '
    '\"reason\": <short phrase>}]}]}\n'
    "Rules: use the EXACT company and segment strings provided; sub_theme MUST "
    "be one of the given sub-theme names or null; NEVER output any number, "
    "percentage, weight, or score. Decide match purely on business relevance."
)


def _match_cache_key(theme: str, taxonomy: dict, companies: list) -> str:
    """Stable hash over theme + sub-theme names/activities + company/segment
    names. Any change to those invalidates the cached match decision."""
    import hashlib
    subs = [
        {"s": s.get("sub_industry", ""), "a": list(s.get("activities", []) or [])}
        for s in (taxonomy.get("layer1", []) if isinstance(taxonomy, dict) else [])
    ]
    payload = {
        "v": _MATCH_CACHE_VERSION,
        "theme": theme.strip().lower(),
        "subs": subs,
        "companies": [
            {"c": c["company"], "segs": list(c["segments"])} for c in companies
        ],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _match_cache_path(key: str) -> str:
    return _osp.join(_MATCH_CACHE_DIR, f"{key}.json")


def _load_cached_match(key: str):
    if key in _match_mem_cache:
        return _match_mem_cache[key]
    path = _match_cache_path(key)
    if _osp.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict) and payload.get("_v") == _MATCH_CACHE_VERSION:
                mp = payload.get("match_map")
                if isinstance(mp, dict):
                    _match_mem_cache[key] = mp
                    return mp
        except Exception:
            return None
    return None


def _store_cached_match(key: str, match_map: dict) -> None:
    _match_mem_cache[key] = match_map
    try:
        os.makedirs(_MATCH_CACHE_DIR, exist_ok=True)
        with open(_match_cache_path(key), "w", encoding="utf-8") as f:
            json.dump({"_v": _MATCH_CACHE_VERSION, "match_map": match_map}, f, indent=2)
    except Exception:
        pass


def _parse_match_response(raw: str, valid_subs: set) -> dict | None:
    """Parse the LLM match JSON into {company: {segment: {sub_theme, reason}}}.

    Only segments the model marked matched AND whose sub_theme is a known
    sub-industry are kept. Returns None on unparseable output.
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        extracted = _extract_json_object(raw)
        if extracted is None:
            return None
        try:
            obj = json.loads(extracted)
        except Exception:
            return None
    companies = obj.get("companies") if isinstance(obj, dict) else None
    if not isinstance(companies, list):
        return None
    out: dict[str, dict] = {}
    for c in companies:
        if not isinstance(c, dict):
            continue
        cname = c.get("company")
        segs = c.get("segments")
        if not isinstance(cname, str) or not isinstance(segs, list):
            continue
        seg_map: dict[str, dict] = {}
        for s in segs:
            if not isinstance(s, dict):
                continue
            sname = s.get("name")
            if not isinstance(sname, str):
                continue
            if not s.get("match"):
                continue
            sub = s.get("sub_theme")
            if not isinstance(sub, str) or sub not in valid_subs:
                continue
            reason = s.get("reason")
            seg_map[sname.strip()] = {
                "sub_theme": sub,
                "reason": (reason.strip() if isinstance(reason, str) else "")[:200],
            }
        out[cname.strip()] = seg_map
    return out


def _preset_match_map(theme: str, companies: list, valid_subs: set) -> dict | None:
    """If `theme` is a global preset, return its canonical match map filtered to
    `valid_subs` (the requested sub-theme subset), else None.

    The canonical map is cached once against the preset's full 10-sub taxonomy
    and the given company/segment set. Filtering to the requested sub-themes
    means every count 3..10 is served from that single cache with no LLM call.
    """
    canonical = get_preset_taxonomy(theme)
    if canonical is None:
        return None
    canonical_key = _match_cache_key(theme, canonical, companies)
    full = _load_cached_match(canonical_key)
    if full is None:
        return None  # preset defined but canonical map not cached for this portfolio
    # Filter: keep only matched segments whose sub_theme is in the requested set.
    filtered: dict[str, dict] = {}
    for company, seg_map in full.items():
        kept = {seg: info for seg, info in seg_map.items()
                if info.get("sub_theme") in valid_subs}
        filtered[company] = kept
    return filtered


def match_segments_llm(taxonomy: dict, company_rows: list,
                       force_refresh: bool = False) -> dict:
    """Classify every company segment to a sub-theme via one cached Groq call.

    company_rows: the build_company_rows output (only those with segment data
    are sent). Returns {ok, match_map, cached} where match_map is
    {company: {segment_name: {"sub_theme", "reason"}}} containing only matched
    segments. On any failure returns a handled error (the caller falls back to
    the offline embedding path).
    """
    layer1 = taxonomy.get("layer1", []) if isinstance(taxonomy, dict) else []
    theme = (taxonomy.get("theme", "") if isinstance(taxonomy, dict) else "").strip()
    valid_subs = {s.get("sub_industry", "") for s in layer1 if s.get("sub_industry")}

    # Build the compact company->segments payload (only companies with data).
    companies = []
    for row in company_rows:
        if not row.get("has_segment_data"):
            continue
        seg_names = [(w.get("name") or "").strip()
                     for w in (row.get("segments") or [])
                     if (w.get("name") or "").strip()]
        if seg_names:
            companies.append({"company": row["company"], "segments": seg_names})

    if not companies or not valid_subs:
        return ok(match_map={}, cached=False)

    # --- Preset short-circuit: a global preset theme has a canonical 10-sub
    # match map cached once. ANY requested sub-theme set that is a subset of the
    # canonical 10 is served by filtering that map to the requested sub-themes -
    # no live LLM call, and it covers every count 3..10 from one cache. ---
    if not force_refresh:
        preset_mm = _preset_match_map(theme, companies, valid_subs)
        if preset_mm is not None:
            return ok(match_map=preset_mm, cached=True)

    key = _match_cache_key(theme, taxonomy, companies)
    if not force_refresh:
        cached = _load_cached_match(key)
        if cached is not None:
            return ok(match_map=cached, cached=True)

    api_key = get_secret("GROQ_API_KEY")
    if not api_key:
        return err(MISSING_API_KEY, "Groq API key is missing.")

    try:
        from groq import Groq
    except Exception as exc:  # pragma: no cover
        return err(LLM_UNREACHABLE, "Groq client library is unavailable.", detail=str(exc))

    # Sub-theme block is sent with every chunk (small, shared context).
    sub_lines = []
    for s in layer1:
        acts = ", ".join(s.get("activities", []) or [])
        sub_lines.append(f"- {s['sub_industry']} (e.g. {acts})")
    sub_block = "SUB-THEMES:\n" + "\n".join(sub_lines)

    # Chunk companies so each request stays under the Groq TPM per-request cap
    # (free tier ~8000 TPM). We budget by estimated tokens; a batched call per
    # chunk is still far cheaper than one call per company, and the whole merged
    # result is cached, so re-runs of the same theme/portfolio cost nothing.
    base_tokens = _estimate_tokens(_MATCH_SYSTEM_PROMPT + sub_block) + 400
    merged: dict[str, dict] = {}
    chunk: list[dict] = []
    chunk_tokens = base_tokens

    def _call_batch(batch: list[dict]) -> dict:
        """One Groq call for a batch. Returns {"__err__": ...} on failure, or
        the parsed {company: {seg: {...}}} map (only for companies present)."""
        comp_lines = [f"- {c['company']}: {'; '.join(c['segments'])}" for c in batch]
        user = (f"THEME: {theme}\n\n{sub_block}\n\n"
                f"COMPANIES AND THEIR SEGMENTS:\n" + "\n".join(comp_lines))
        messages = [
            {"role": "system", "content": _MATCH_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        # _groq_chat fails over across all configured keys on a rate limit. Only
        # when EVERY key is throttled do we wait out the per-minute window and
        # retry (free TPM tiers reset each minute).
        resp = None
        for attempt in range(_MATCH_MAX_RETRIES):
            try:
                resp = _groq_chat(messages, _MATCH_MAX_TOKENS, model=_MATCH_MODEL)
                break
            except Exception as exc:
                if _is_rate_limit(exc) and attempt < _MATCH_MAX_RETRIES - 1:
                    time.sleep(_MATCH_RETRY_WAIT_S)
                    continue
                # A JSON-validation 400 means the model's output for this batch
                # was malformed/too long for json mode. Signal the caller to
                # split the batch (smaller output) rather than failing outright.
                if _is_json_validation_error(exc):
                    return {"__split__": True}
                return {"__err__": _map_groq_exception(exc)}
        raw = resp.choices[0].message.content or ""
        parsed = _parse_match_response(raw, valid_subs)
        if parsed is None:
            # Unparseable (often truncated) output -> split and retry smaller.
            return {"__split__": True}
        return parsed

    def _flush(batch: list[dict]) -> dict:
        """Classify a batch, guaranteeing EVERY company is accounted for.

        A batch where the model dropped/truncated companies is a silent data
        loss (those companies would wrongly score 0). So we verify completeness:
        if any company is missing, split the batch in half and retry each half
        (down to a single company). A single company that still cannot be
        classified is recorded as an empty map (genuine no-match) rather than
        lost. Returns {"__err__": ...} only on a real API/JSON failure.
        """
        res = _call_batch(batch)
        if "__err__" in res:
            return res
        needs_split = "__split__" in res
        if not needs_split:
            sent = {c["company"] for c in batch}
            missing = sent - set(res.keys())
            if not missing:
                return res
            needs_split = True  # some companies dropped -> split and retry

        if len(batch) == 1:
            # A single company that still won't classify cleanly: record it as a
            # genuine no-match (empty map) rather than losing or erroring on it.
            return {batch[0]["company"]: {}}

        # Split and retry the two halves: smaller output avoids json-mode
        # truncation, and smaller input dodges the per-request TPM cap.
        mid = len(batch) // 2
        out: dict = {}
        for half in (batch[:mid], batch[mid:]):
            r = _flush(half)
            if "__err__" in r:
                return r
            out.update(r)
        return out

    for c in companies:
        c_tokens = _estimate_tokens(f"- {c['company']}: {'; '.join(c['segments'])}")
        # Keep each request's INPUT budget well under the TPM cap, leaving room
        # for the model's JSON output which also counts toward TPM.
        if chunk and (chunk_tokens + c_tokens) > _MATCH_CHUNK_TOKEN_BUDGET:
            res = _flush(chunk)
            if "__err__" in res:
                return res["__err__"]
            merged.update(res)
            chunk, chunk_tokens = [], base_tokens
        chunk.append(c)
        chunk_tokens += c_tokens

    if chunk:
        res = _flush(chunk)
        if "__err__" in res:
            return res["__err__"]
        merged.update(res)

    _store_cached_match(key, merged)
    return ok(match_map=merged, cached=False)


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

    # Primary scoring path: one cached, batched LLM call classifies every
    # company segment to a sub-theme (binary). If Groq is unavailable or the
    # call fails, match_map stays None and score_company falls back to the
    # offline embedding path so the app still produces a result.
    llm_res = match_segments_llm(taxonomy, rows)
    llm_match_map = llm_res.get("match_map") if llm_res.get("ok") else None
    scoring_mode = "llm" if llm_match_map is not None else "embedding_fallback"
    scoring_cached = bool(llm_res.get("cached")) if llm_res.get("ok") else False

    scores: dict[str, dict] = {}
    score_records = []
    for row in rows:
        # When the LLM path is active, a company absent from the map simply had
        # NO matching segment (exposure 0) - we pass an empty map, NOT None, so
        # it does not silently fall back to embeddings. None (full fallback) is
        # used only when the whole LLM call failed.
        if llm_match_map is not None:
            company_mm = llm_match_map.get(row["company"], {})
        else:
            company_mm = None
        sr = score_company(row, taxonomy, match_map=company_mm)
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

    # Attach flags to each contribution row for the UI. Numeric confidence is
    # retired (None); a qualitative level will replace it later.
    for c in metrics["contributions"]:
        sr = scores.get(c["company"], {})
        c["confidence"] = sr.get("confidence")
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

    metrics["scoring_mode"] = scoring_mode
    metrics["scoring_cached"] = scoring_cached

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
