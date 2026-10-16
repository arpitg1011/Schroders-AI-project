"""Property-based tests for revenue cleaning and segment weighting (Req 4)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import engine

WRITEOFF = ["Intersegment Elimination", "Adjustment and Write-Off",
            "Internal Write-Off", "Number of Write-Offs"]
NORMAL_NAMES = ["Power Supply", "Automation", "Retail Banking", "Kitchen Hardware",
                "Real Estate", "Iron Ore", "Cloud Services"]

TAXONOMY = {"theme": "t", "layer1": [
    {"sub_industry": "S1", "activities": ["power supply", "automation"]},
]}


def _segment(name, revenue):
    present = revenue is not None
    return {"position": 1, "name": name, "revenue_raw": revenue,
            "revenue": revenue, "revenue_present": present}


def _score(segments):
    row = {"company": "C", "has_segment_data": True, "segments": segments,
           "business_description": "power supply and automation products"}
    return engine.score_company(row, TAXONOMY)


revenue_vals = st.one_of(
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.none(),
    st.just(0.0),
)
seg_names = st.sampled_from(NORMAL_NAMES + WRITEOFF)


@settings(max_examples=150)
@given(st.lists(st.tuples(seg_names, revenue_vals), min_size=0, max_size=5))
def test_property_1_valid_negative_safe_distribution(pairs):
    # Feature: thematic-portfolio-analysis, Property 1: segment weighting is a
    # valid, negative-safe distribution (pcts in [0,100], sum to 100 when any positive).
    segs = [_segment(n, r) for i, (n, r) in enumerate(pairs)]
    for i, s in enumerate(segs):
        s["position"] = i + 1
    res = _score(segs)
    pcts = [b["revenue_pct"] for b in res["segment_breakdown"]]
    for p in pcts:
        assert 0.0 <= p <= 100.0 + 1e-6
    if res["has_positive_revenue"]:
        assert abs(sum(pcts) - 100.0) < 1e-3


@settings(max_examples=150)
@given(
    st.lists(st.tuples(st.sampled_from(NORMAL_NAMES),
                       st.floats(min_value=1.0, max_value=1e6, allow_nan=False)),
             min_size=1, max_size=4),
    st.sampled_from(WRITEOFF),
    revenue_vals,
)
def test_property_2_writeoff_does_not_affect_distribution(normal_pairs, wo_name, wo_rev):
    # Feature: thematic-portfolio-analysis, Property 2: write-off/elimination
    # segments do not affect the Segment_Rev_Pct distribution of the rest.
    base_segs = [_segment(n, r) for n, r in normal_pairs]
    for i, s in enumerate(base_segs):
        s["position"] = i + 1
    with_wo = base_segs + [_segment(wo_name, wo_rev)]

    def pct_by_name(res):
        return {b["name"]: b["revenue_pct"] for b in res["segment_breakdown"]
                if b["name"] not in WRITEOFF}

    a = pct_by_name(_score(base_segs))
    b = pct_by_name(_score(with_wo))
    for name in a:
        assert abs(a[name] - b.get(name, 0.0)) < 1e-6


@settings(max_examples=150)
@given(st.lists(st.tuples(seg_names,
                          st.one_of(st.floats(min_value=-1e6, max_value=0.0,
                                              allow_nan=False), st.none())),
                min_size=0, max_size=5))
def test_property_3_no_positive_revenue_zero_exposure(pairs):
    # Feature: thematic-portfolio-analysis, Property 3: no positive revenue -> 0 exposure.
    segs = [_segment(n, r) for n, r in pairs]
    for i, s in enumerate(segs):
        s["position"] = i + 1
    res = _score(segs)
    assert res["has_positive_revenue"] is False
    assert res["company_exposure"] == 0.0
