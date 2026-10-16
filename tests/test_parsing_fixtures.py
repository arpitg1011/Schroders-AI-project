"""Fixture and error-path tests for the data loaders (Requirements 1-2)."""

from __future__ import annotations

import io

import pandas as pd

import engine


# ---- load_portfolio: real fixture (Req 1.3, 1.4) ----

def test_load_portfolio_real_fixture():
    res = engine.load_portfolio("data/portfolio_80.xlsx")
    assert res["ok"], res
    df = res["portfolio"]
    assert list(df.columns) == engine.PORTFOLIO_COLUMNS
    assert len(df) == 80
    # Company values are UPPERCASE in the source.
    assert df["Company"].iloc[0] == df["Company"].iloc[0].upper()
    # Weights are decimal fractions (0.132 == 13.2%), not percentages.
    assert 0.0 <= df["Portfolio wt"].max() <= 1.0
    assert abs(df["Portfolio wt"].sum() - 1.0) < 0.05


# ---- load_portfolio: error paths (Req 1.6, 1.7) ----

def test_load_portfolio_missing_file():
    res = engine.load_portfolio("data/__nope__.xlsx")
    assert res["ok"] is False
    assert res["error_code"] == engine.FILE_NOT_FOUND


def test_load_portfolio_missing_column_names_it():
    buf = io.BytesIO()
    pd.DataFrame({"Company": ["ACME"], "Portfolio wt": [0.5]}).to_csv(buf, index=False)
    buf.seek(0)
    buf.name = "bad.csv"
    res = engine.load_portfolio(buf)
    assert res["ok"] is False
    assert res["error_code"] == engine.MISSING_COLUMN
    # The message names a missing column.
    assert "Bloomberg ticker" in res["message"]


# ---- load_segments: real fixture (Req 2.1, 2.2, 2.4, 2.5, 2.7) ----

def test_load_segments_real_fixture():
    res = engine.load_segments("data/spglobal_segments.xlsx")
    assert res["ok"], res
    segs = res["segments"]
    # ~20 companies carry data in the current prototype export.
    assert 15 <= len(segs) <= 40
    # Every company has at most 5 positional segments.
    for rec in segs.values():
        assert len(rec["segments"]) <= 5
    # At least one company exposes a parsed exchange:ticker (Req 2.7).
    assert any(rec["ticker"] for rec in segs.values())
    # Spot-check Alibaba's ticker extraction.
    ali = [r for k, r in segs.items() if "ALIBABA" in k]
    assert ali and ali[0]["ticker"] == "BABA" and ali[0]["exchange"] == "NYSE"


def test_load_segments_missing_file():
    res = engine.load_segments("data/__missing_segments__.xlsx")
    assert res["ok"] is False
    assert res["error_code"] == engine.FILE_NOT_FOUND


def test_load_segments_missing_sheet(tmp_path):
    import pandas as pd
    p = tmp_path / "no_sheet1.xlsx"
    with pd.ExcelWriter(p, engine="openpyxl") as w:
        pd.DataFrame({"x": [1]}).to_excel(w, sheet_name="Other", index=False)
    res = engine.load_segments(str(p))
    assert res["ok"] is False
    assert res["error_code"] == engine.MISSING_SHEET
