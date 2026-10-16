"""Property tests for matching, normalization, ticker extraction, unmatched flagging."""

from __future__ import annotations

import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

import engine

TAX = {"theme": "t", "layer1": [{"sub_industry": "S", "activities": ["power supply"]}]}


@settings(max_examples=200)
@given(st.text(min_size=0, max_size=60))
def test_property_11_normalization_idempotent(name):
    # Feature: thematic-portfolio-analysis, Property 11: normalization idempotent.
    once = engine._normalize_company_name(name)
    twice = engine._normalize_company_name(once)
    assert once == twice


names = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ ", min_size=2, max_size=20)
exch = st.sampled_from(["NYSE", "SEHK", "ASX", "TWSE", "SGX"])
tick = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=1, max_size=6)


@settings(max_examples=200)
@given(names, st.booleans(), exch, tick)
def test_property_10_ticker_extraction_matches_pattern(base, has_paren, e, t):
    # Feature: thematic-portfolio-analysis, Property 10: ticker extracted iff (EXCHANGE:TICKER) suffix.
    entity = base.strip()
    if has_paren:
        entity = f"{entity} ({e}:{t})"
    ex, tk = engine._extract_exchange_ticker(entity)
    if has_paren:
        assert ex == e and tk == t
    else:
        assert ex is None and tk is None


def test_property_9_unmatched_company_flagged_zero():
    # Feature: thematic-portfolio-analysis, Property 9: unmatched -> flagged, 0 exposure.
    row = {"company": "NO SUCH CO", "has_segment_data": False, "segments": [],
           "business_description": ""}
    res = engine.score_company(row, TAX)
    assert res["has_segment_data"] is False
    assert res["company_exposure"] == 0.0
    assert res["matched_sub_industries"] == []
    assert res["evidence_snippet"] == ""


def test_match_companies_unmatched_retained():
    pf = pd.DataFrame({
        "Company": ["ALPHA CORP", "ZZZ UNKNOWN"],
        "Bloomberg ticker": ["AAA US Equity", "ZZZ US Equity"],
        "Country": ["US", "US"], "Sector": ["Tech", "Tech"], "SEDOL": ["1", "2"],
        "Portfolio wt": [0.5, 0.5], "Reference wt": [0.4, 0.6], "Active wt": [0.1, -0.1],
    })
    segs = {"ALPHA": {"entity_name": "Alpha Corporation", "company_name": "Alpha Corporation",
                      "ticker": "AAA", "exchange": "NYSE", "business_description": "x",
                      "segments": [], "sector": "Tech", "entity_id": None,
                      "classification": {}}}
    m = engine.match_companies(pf, segs)["matches"]
    assert m["ALPHA CORP"]["matched"] is True
    assert m["ZZZ UNKNOWN"]["matched"] is False
    assert m["ZZZ UNKNOWN"]["match_method"] == "none"
