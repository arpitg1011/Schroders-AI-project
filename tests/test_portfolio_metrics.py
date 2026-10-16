"""Property tests for portfolio-level metric identities (Req 7)."""

from __future__ import annotations

import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

import engine

co = st.text(alphabet="ABCDE", min_size=1, max_size=3)
expo = st.floats(min_value=0.0, max_value=100.0, allow_nan=False)
wt = st.floats(min_value=0.0, max_value=0.3, allow_nan=False)


@settings(max_examples=150)
@given(st.lists(st.tuples(co, expo, wt, wt), min_size=1, max_size=10, unique_by=lambda x: x[0]))
def test_property_12_and_13_metric_identities(rows):
    # Feature: thematic-portfolio-analysis, Property 12: portfolio metric identities.
    # Feature: thematic-portfolio-analysis, Property 13: contribution identity & additivity.
    companies = [r[0] for r in rows]
    pf = pd.DataFrame({
        "Company": companies,
        "Sector": ["S"] * len(rows),
        "Bloomberg ticker": ["T"] * len(rows),
        "Country": ["C"] * len(rows), "SEDOL": ["1"] * len(rows),
        "Portfolio wt": [r[2] for r in rows],
        "Reference wt": [r[3] for r in rows],
        "Active wt": [0.0] * len(rows),
    })
    scores = pd.DataFrame({
        "company": companies,
        "company_exposure": [r[1] for r in rows],
        "has_segment_data": [True] * len(rows),
    })
    m = engine.calculate_portfolio_metrics(scores, pf)

    exp_pf = round(sum(r[1] * r[2] for r in rows), 2)
    exp_bm = round(sum(r[1] * r[3] for r in rows), 2)
    assert abs(m["portfolio_thematic_exposure"] - exp_pf) < 0.02
    assert abs(m["benchmark_exposure"] - exp_bm) < 0.02
    assert abs(m["active_tilt"] - round(exp_pf - exp_bm, 2)) < 0.02

    # Each contribution == exposure * portfolio_wt; sum ~ portfolio exposure.
    total_contrib = 0.0
    for c in m["contributions"]:
        assert abs(c["contribution"] - c["company_exposure"] * c["portfolio_wt"]) < 0.02
        total_contrib += c["contribution"]
    assert abs(total_contrib - m["portfolio_thematic_exposure"]) < 0.1


def test_exposure_band_mapping():
    import engine
    assert engine.exposure_band(85) == "Core"
    assert engine.exposure_band(60) == "Core"
    assert engine.exposure_band(59.9) == "High"
    assert engine.exposure_band(40) == "High"
    assert engine.exposure_band(25) == "Moderate"
    assert engine.exposure_band(10) == "Low"
    assert engine.exposure_band(2) == "Negligible"
    assert engine.exposure_band(0) == "Negligible"
