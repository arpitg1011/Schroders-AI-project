"""Tests for the LLM-as-judge binary scoring path and match-response parsing."""

from __future__ import annotations

import json

import engine

TAX = {"theme": "semiconductors", "layer1": [
    {"sub_industry": "Integrated Device Manufacturers (IDMs)",
     "activities": ["fabricating wafers", "logic chips"]},
    {"sub_industry": "Fabless Chip Designers", "activities": ["designing ASICs"]},
]}
VALID_SUBS = {"Integrated Device Manufacturers (IDMs)", "Fabless Chip Designers"}


def _row(company, segs):
    return {
        "company": company, "sector": "IT", "has_segment_data": True,
        "segments": [{"name": n, "revenue": r, "revenue_present": True}
                     for n, r in segs],
        "business_description": "", "entity_name": company, "match_method": "exact",
    }


# ---- parsing ----

def test_parse_match_response_keeps_only_valid_matches():
    raw = json.dumps({"companies": [
        {"company": "A", "segments": [
            {"name": "Foundry", "match": True,
             "sub_theme": "Integrated Device Manufacturers (IDMs)", "reason": "chips"},
            {"name": "Catering", "match": False, "sub_theme": None, "reason": "no"},
            {"name": "Bogus", "match": True, "sub_theme": "Not A Real Sub", "reason": "x"},
        ]},
    ]})
    out = engine._parse_match_response(raw, VALID_SUBS)
    assert out["A"] == {
        "Foundry": {"sub_theme": "Integrated Device Manufacturers (IDMs)",
                    "reason": "chips"},
    }  # Catering (no match) and Bogus (invalid sub) are dropped


def test_parse_match_response_bad_json_returns_none():
    assert engine._parse_match_response("not json {", VALID_SUBS) is None


# ---- binary scoring: pure revenue ----

def test_binary_scoring_matched_segment_is_full_revenue():
    # One segment = 100% revenue, matched -> exposure 100.
    row = _row("TSMC", [("Foundry", 100.0)])
    mm = {"Foundry": {"sub_theme": "Integrated Device Manufacturers (IDMs)",
                      "reason": "contract chip manufacturing"}}
    sr = engine.score_company(row, TAX, match_map=mm)
    assert sr["company_exposure"] == 100.0
    assert sr["matched_sub_industries"] == ["Integrated Device Manufacturers (IDMs)"]
    assert sr["confidence"] is None  # numeric confidence retired
    assert sr["segment_breakdown"][0]["match_reason"] == "contract chip manufacturing"


def test_binary_scoring_partial_revenue_split():
    # Two equal-revenue segments, only one matched -> 50% exposure.
    row = _row("MIX", [("Chips", 60.0), ("Catering", 60.0)])
    mm = {"Chips": {"sub_theme": "Fabless Chip Designers", "reason": "designs chips"}}
    sr = engine.score_company(row, TAX, match_map=mm)
    assert sr["company_exposure"] == 50.0
    assert sr["sub_theme_attribution"] == {"Fabless Chip Designers": 50.0}


def test_binary_scoring_empty_map_is_zero_not_fallback():
    # An empty match map (company had no matching segment) -> 0, NOT embeddings.
    row = _row("BANK", [("Retail Banking", 100.0)])
    sr = engine.score_company(row, TAX, match_map={})
    assert sr["company_exposure"] == 0.0
    assert sr["insufficient_data"] is True
    assert sr["matched_sub_industries"] == []


def test_none_map_uses_embedding_fallback():
    # match_map=None -> legacy embedding path still runs (no crash, returns a score).
    row = _row("SK", [("Manufacture and Sale of Semiconductor Products", 100.0)])
    sr = engine.score_company(row, TAX, match_map=None)
    assert sr["ok"] is True
    assert 0.0 <= sr["company_exposure"] <= 100.0
    # Fallback path still produces the numeric confidence field (a float).
    assert isinstance(sr["confidence"], float)


# ---- cache key stability ----

def test_match_cache_key_stable_and_input_sensitive():
    comps = [{"company": "A", "segments": ["Foundry"]}]
    k1 = engine._match_cache_key("semiconductors", TAX, comps)
    k2 = engine._match_cache_key("semiconductors", TAX, comps)
    assert k1 == k2
    # Changing a segment name changes the key.
    comps2 = [{"company": "A", "segments": ["Packaging"]}]
    assert engine._match_cache_key("semiconductors", TAX, comps2) != k1
