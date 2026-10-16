"""Property test: scoring is deterministic and reproducible (Req 6.9)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import engine

TAX = {"theme": "t", "layer1": [
    {"sub_industry": "S1", "activities": ["power supply", "automation"]},
    {"sub_industry": "S2", "activities": ["battery storage"]},
]}

seg = st.fixed_dictionaries({
    "position": st.integers(min_value=1, max_value=5),
    "name": st.sampled_from(["Power Supply", "Automation Group", "Kitchen Hardware", None]),
    "revenue_raw": st.floats(min_value=-1e5, max_value=1e5, allow_nan=False),
    "revenue": st.floats(min_value=-1e5, max_value=1e5, allow_nan=False),
    "revenue_present": st.booleans(),
})


@settings(max_examples=150)
@given(st.lists(seg, min_size=0, max_size=5),
       st.text(min_size=0, max_size=100))
def test_property_7_scoring_is_deterministic(segments, description):
    # Feature: thematic-portfolio-analysis, Property 7: scoring deterministic & reproducible.
    row = {"company": "C", "has_segment_data": True, "segments": segments,
           "business_description": description}
    a = engine.score_company(dict(row), TAX)
    b = engine.score_company(dict(row), TAX)
    assert a == b
