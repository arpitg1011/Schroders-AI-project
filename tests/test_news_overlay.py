"""Tests for the news-driven exposure overlay (Req 16, R3, rubric-based).

The LLM call is mocked and headlines are injected, so these are deterministic
and need no network or API key.
"""

from __future__ import annotations

import json
from unittest import mock

import news

TAX = {"theme": "semiconductors", "layer1": [
    {"sub_industry": "Foundry", "activities": ["wafer fabrication"]},
    {"sub_industry": "Memory", "activities": ["DRAM", "NAND"]},
]}
HEADS = [
    {"title": "Rare earth export curbs tighten", "domain": "reuters", "date": "2026-10-15", "url": "u0"},
    {"title": "TSMC expands capacity", "domain": "ft", "date": "2026-10-14", "url": "u1"},
]
COMPANIES = ["TAIWAN SEMICONDUCTOR MFG", "SK HYNIX"]


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})})]


def _client(content):
    class _Comp:
        def create(self, **kw):
            return _Resp(content)
    return type("Cl", (), {"chat": type("Ch", (), {"completions": _Comp()})})()


def test_rubric_point_mapping():
    # The deterministic rubric: strength -> magnitude, direction -> sign.
    assert news._points_for("Tailwind", "High") == 4.0
    assert news._points_for("Headwind", "High") == -4.0
    assert news._points_for("Tailwind", "Medium") == 2.0
    assert news._points_for("Headwind", "Low") == -1.0
    assert news._points_for("Neutral", "High") == 0.0
    assert news._points_for("Tailwind", "None") == 0.0


def test_no_headlines_zero_adjustment(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    res = news.compute_news_adjustment("semiconductors", TAX, [], COMPANIES)
    assert res["ok"] and res["total_adjustment"] == 0.0 and res["per_sub_theme"] == []


def test_direction_strength_mapped_and_attributed(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    payload = json.dumps({"assessments": [
        {"sub_industry": "Foundry", "direction": "Tailwind", "strength": "Medium",
         "rationale": "capacity expansion", "cited_headlines": [1],
         "affected_companies": ["TAIWAN SEMICONDUCTOR MFG"]},
        {"sub_industry": "Memory", "direction": "Headwind", "strength": "Low",
         "rationale": "demand softness", "cited_headlines": [0],
         "affected_companies": ["SK HYNIX"]},
    ]})
    res = news.compute_news_adjustment("semiconductors", TAX, HEADS, COMPANIES,
                                       _client=_client(payload))
    assert res["ok"]
    assert abs(res["total_adjustment"] - 1.0) < 1e-6  # +2 (med) - 1 (low)
    subs = {p["sub_industry"]: p for p in res["per_sub_theme"]}
    assert subs["Foundry"]["adjustment"] == 2.0 and subs["Foundry"]["direction"] == "Tailwind"
    assert subs["Memory"]["adjustment"] == -1.0 and subs["Memory"]["strength"] == "Low"
    assert subs["Foundry"]["affected_companies"] == ["TAIWAN SEMICONDUCTOR MFG"]


def test_none_strength_omitted(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    payload = json.dumps({"assessments": [
        {"sub_industry": "Foundry", "direction": "Neutral", "strength": "None",
         "rationale": "", "cited_headlines": [], "affected_companies": []},
    ]})
    res = news.compute_news_adjustment("semiconductors", TAX, HEADS, COMPANIES,
                                       _client=_client(payload))
    assert res["ok"] and res["per_sub_theme"] == [] and res["total_adjustment"] == 0.0


def test_total_adjustment_is_capped(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    # Both sub-themes High tailwind -> +4 each = +8; still under the 10 cap.
    # Add a third conceptually is not possible (only 2 subs), so test per-sub cap
    # cannot be exceeded and total respects the cap.
    payload = json.dumps({"assessments": [
        {"sub_industry": "Foundry", "direction": "Tailwind", "strength": "High",
         "rationale": "x", "cited_headlines": [], "affected_companies": []},
        {"sub_industry": "Memory", "direction": "Tailwind", "strength": "High",
         "rationale": "y", "cited_headlines": [], "affected_companies": []},
    ]})
    res = news.compute_news_adjustment("semiconductors", TAX, HEADS, COMPANIES,
                                       _client=_client(payload))
    assert res["ok"]
    assert abs(res["total_adjustment"]) <= news.NEWS_ADJUSTMENT_CAP + 1e-6
    for p in res["per_sub_theme"]:
        assert abs(p["adjustment"]) <= news._PER_SUB_CAP + 1e-6


def test_unknown_subtheme_ignored(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    payload = json.dumps({"assessments": [
        {"sub_industry": "Not A Real Sub", "direction": "Tailwind", "strength": "High",
         "rationale": "x", "cited_headlines": [], "affected_companies": []},
    ]})
    res = news.compute_news_adjustment("semiconductors", TAX, HEADS, COMPANIES,
                                       _client=_client(payload))
    assert res["ok"] and res["per_sub_theme"] == [] and res["total_adjustment"] == 0.0


def test_dedupe_collapses_same_story():
    # The same event from multiple outlets should collapse to one headline,
    # so a single event is not double-counted (consistency).
    heads = [
        {"title": "TSMC expands 2nm capacity in Arizona", "domain": "ft", "date": "2026-10-15", "url": ""},
        {"title": "TSMC Expands 2nm Capacity In Arizona!", "domain": "reuters", "date": "2026-10-15", "url": ""},
        {"title": "Rare earth export curbs tighten globally", "domain": "wsj", "date": "2026-10-15", "url": ""},
    ]
    out = news._dedupe_headlines(heads)
    assert len(out) == 2  # the two TSMC variants collapse to one


def test_window_filter():
    import datetime as dt
    today = dt.date.today()
    heads = [
        {"title": "recent", "date": (today - dt.timedelta(days=1)).isoformat(), "domain": "a", "url": ""},
        {"title": "old", "date": (today - dt.timedelta(days=20)).isoformat(), "domain": "b", "url": ""},
        {"title": "undated", "date": "", "domain": "c", "url": ""},
    ]
    kept = news._filter_by_window(heads, 3)
    titles = {h["title"] for h in kept}
    assert "recent" in titles and "old" not in titles and "undated" in titles
