"""Property + example tests for semantic alignment scoring, exposure, evidence (Req 6, R2)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import engine

RELATED = ["battery energy storage", "grid automation", "power transformers",
           "smart metering", "solar power generation"]
UNRELATED = ["kitchen hardware", "home appliances", "retail banking",
             "beverage distribution", "apparel manufacturing"]


def _taxonomy(activities):
    return {"theme": "t", "layer1": [{"sub_industry": "S", "activities": activities}]}


def _score_one(segment_name, description, taxonomy):
    row = {"company": "C", "has_segment_data": True,
           "segments": [{"position": 1, "name": segment_name, "revenue_raw": 100.0,
                         "revenue": 100.0, "revenue_present": True}],
           "business_description": description}
    return engine.score_company(row, taxonomy)


@settings(max_examples=60, deadline=None)
@given(st.lists(st.sampled_from(RELATED + UNRELATED), min_size=1, max_size=3, unique=True),
       st.sampled_from(RELATED + UNRELATED))
def test_property_5_alignment_score_range(activities, seg_name):
    # Feature: thematic-portfolio-analysis, Property 5: alignment score in [0,1].
    tax = _taxonomy(activities)
    res = _score_one(seg_name, "", tax)
    for b in res["segment_breakdown"]:
        assert 0.0 <= b["alignment_score"] <= 1.0


@settings(max_examples=60, deadline=None)
@given(st.lists(st.sampled_from(RELATED + UNRELATED), min_size=1, max_size=3, unique=True),
       st.sampled_from(RELATED + UNRELATED))
def test_property_6_exposure_bounded_weighted_sum(activities, seg_name):
    # Feature: thematic-portfolio-analysis, Property 6: exposure bounded weighted sum.
    tax = _taxonomy(activities)
    res = _score_one(seg_name, "", tax)
    assert 0.0 <= res["company_exposure"] <= 100.0
    max_align = max((b["alignment_score"] for b in res["segment_breakdown"]), default=0.0)
    assert res["company_exposure"] <= max_align * 100.0 + 1e-6


@settings(max_examples=40, deadline=None)
@given(st.sampled_from(RELATED), st.sampled_from(RELATED + UNRELATED))
def test_property_8_evidence_bound(description, seg_name):
    # Feature: thematic-portfolio-analysis, Property 8: evidence <=500 chars; empty iff no match.
    tax = _taxonomy(["battery energy storage", "grid automation"])
    res = _score_one(seg_name, description, tax)
    assert len(res["evidence_snippet"]) <= 500
    any_match = any(b["alignment_score"] > 0 for b in res["segment_breakdown"])
    if not any_match:
        assert res["evidence_snippet"] == ""


def test_semantic_matches_related_above_unrelated():
    # A directly related segment must out-score an unrelated one against the same taxonomy.
    tax = _taxonomy(["battery energy storage", "grid-scale lithium-ion"])
    related = _score_one("Energy Storage Systems", "grid-scale battery installations", tax)
    unrelated = _score_one("Kitchen Hardware", "household appliances and cookware", tax)
    assert related["company_exposure"] > unrelated["company_exposure"]


def test_example_unrelated_categories_score_low():
    # Req 6.3: unexposed categories stay below threshold -> ~0 exposure.
    tax = _taxonomy(["battery energy storage", "grid automation"])
    for name in ("Kitchen Hardware", "Real Estate", "Consumer DX"):
        res = _score_one(name, "", tax)
        assert res["company_exposure"] == 0.0


def test_wording_robustness_renewables():
    # The headline fix: for a genuinely on-theme segment, synonymous theme
    # phrasings ("renewables" vs "renewable energy") produce similar exposure.
    seg = ("Renewable Power Generation", "operates solar and wind renewable power plants")
    a = _score_one(*seg, _taxonomy(["renewables"]))
    b = _score_one(*seg, _taxonomy(["renewable energy"]))
    assert abs(a["company_exposure"] - b["company_exposure"]) < 15.0
