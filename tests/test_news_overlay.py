"""Tests for the qualitative news signal (Req 16, R4).

News produces a Tailwind/Headwind x Low/Med/High signal and a deterministic
exposure-weighted rollup. It NEVER adds a number to exposure. The LLM call is
mocked and headlines are injected, so these are deterministic and need no
network or API key.
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


# ---- Rollup: deterministic, exposure-weighted, qualitative-only ----

def test_rollup_empty_is_neutral():
    assert news.rollup_signal([], {})["direction"] == "Neutral"
    assert news.rollup_signal([], {})["strength"] == "None"


def test_rollup_exposure_weighted():
    # Foundry is a tailwind, Memory a headwind. Foundry carries far more of the
    # exposure, so the overall signal must come out Tailwind.
    per = [
        {"sub_industry": "Foundry", "direction": "Tailwind", "strength": "High"},
        {"sub_industry": "Memory", "direction": "Headwind", "strength": "High"},
    ]
    exp = {"Foundry": 90.0, "Memory": 10.0}
    out = news.rollup_signal(per, exp)
    assert out["direction"] == "Tailwind"
    assert out["score"] > 0


def test_rollup_is_deterministic():
    per = [
        {"sub_industry": "Foundry", "direction": "Tailwind", "strength": "Medium"},
        {"sub_industry": "Memory", "direction": "Headwind", "strength": "Low"},
    ]
    exp = {"Foundry": 60.0, "Memory": 40.0}
    a = news.rollup_signal(per, exp)
    b = news.rollup_signal(per, exp)
    assert a == b  # identical inputs -> identical output


def test_no_headlines_neutral(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    res = news.compute_news_signal("semiconductors", TAX, [], COMPANIES, {})
    assert res["ok"] and res["overall"]["direction"] == "Neutral"
    assert res["per_sub_theme"] == []


def test_signal_parsed_no_numbers(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    payload = json.dumps({"assessments": [
        {"sub_industry": "Foundry", "direction": "Tailwind", "strength": "Medium",
         "rationale": "capacity expansion", "cited_headlines": [1],
         "affected_companies": ["TAIWAN SEMICONDUCTOR MFG"]},
        {"sub_industry": "Memory", "direction": "Headwind", "strength": "Low",
         "rationale": "demand softness", "cited_headlines": [0],
         "affected_companies": ["SK HYNIX"]},
    ]})
    res = news.compute_news_signal("semiconductors", TAX, HEADS, COMPANIES,
                                   {"Foundry": 70.0, "Memory": 30.0},
                                   _client=_client(payload))
    assert res["ok"]
    # Qualitative only: items carry direction/strength, NOT a numeric adjustment.
    for p in res["per_sub_theme"]:
        assert "adjustment" not in p
        assert p["direction"] in news.DIRECTIONS
        assert p["strength"] in news.STRENGTHS
    assert res["overall"]["direction"] == "Tailwind"  # Foundry dominates exposure


def test_none_or_neutral_omitted(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    payload = json.dumps({"assessments": [
        {"sub_industry": "Foundry", "direction": "Neutral", "strength": "None",
         "rationale": "", "cited_headlines": [], "affected_companies": []},
    ]})
    res = news.compute_news_signal("semiconductors", TAX, HEADS, COMPANIES, {},
                                   _client=_client(payload))
    assert res["ok"] and res["per_sub_theme"] == []
    assert res["overall"]["direction"] == "Neutral"


def test_unknown_subtheme_ignored(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    payload = json.dumps({"assessments": [
        {"sub_industry": "Not A Real Sub", "direction": "Tailwind", "strength": "High",
         "rationale": "x", "cited_headlines": [], "affected_companies": []},
    ]})
    res = news.compute_news_signal("semiconductors", TAX, HEADS, COMPANIES, {},
                                   _client=_client(payload))
    assert res["ok"] and res["per_sub_theme"] == []


def test_dedupe_collapses_same_story():
    heads = [
        {"title": "TSMC expands 2nm capacity in Arizona", "domain": "ft", "date": "2026-10-15", "url": ""},
        {"title": "TSMC Expands 2nm Capacity In Arizona!", "domain": "reuters", "date": "2026-10-15", "url": ""},
        {"title": "Rare earth export curbs tighten globally", "domain": "wsj", "date": "2026-10-15", "url": ""},
    ]
    out = news._dedupe_headlines(heads)
    assert len(out) == 2


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
