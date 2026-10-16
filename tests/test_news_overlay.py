"""Tests for the news-driven exposure overlay (Req 16, R3).

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


def test_no_headlines_zero_adjustment(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    res = news.compute_news_adjustment("semiconductors", TAX, [], COMPANIES)
    assert res["ok"] and res["total_adjustment"] == 0.0 and res["per_sub_theme"] == []


def test_adjustment_parsed_and_attributed(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    payload = json.dumps({"adjustments": [
        {"sub_industry": "Foundry", "adjustment": 3, "rationale": "capacity expansion",
         "cited_headlines": [1], "affected_companies": ["TAIWAN SEMICONDUCTOR MFG"]},
        {"sub_industry": "Memory", "adjustment": -2, "rationale": "demand softness",
         "cited_headlines": [0], "affected_companies": ["SK HYNIX"]},
    ]})
    res = news.compute_news_adjustment("semiconductors", TAX, HEADS, COMPANIES,
                                       _client=_client(payload))
    assert res["ok"]
    assert abs(res["total_adjustment"] - 1.0) < 1e-6  # +3 - 2
    subs = {p["sub_industry"]: p for p in res["per_sub_theme"]}
    assert subs["Foundry"]["affected_companies"] == ["TAIWAN SEMICONDUCTOR MFG"]
    assert subs["Memory"]["adjustment"] == -2.0


def test_total_adjustment_is_capped(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    # Two sub-themes each maxed; raw total would be 12, cap is 10.
    payload = json.dumps({"adjustments": [
        {"sub_industry": "Foundry", "adjustment": 50, "rationale": "x",
         "cited_headlines": [], "affected_companies": []},
        {"sub_industry": "Memory", "adjustment": 50, "rationale": "y",
         "cited_headlines": [], "affected_companies": []},
    ]})
    res = news.compute_news_adjustment("semiconductors", TAX, HEADS, COMPANIES,
                                       _client=_client(payload))
    assert res["ok"]
    assert abs(res["total_adjustment"]) <= news.NEWS_ADJUSTMENT_CAP + 1e-6


def test_unknown_subtheme_ignored(monkeypatch):
    monkeypatch.setattr(news, "get_secret", lambda k, d=None: "key")
    payload = json.dumps({"adjustments": [
        {"sub_industry": "Not A Real Sub", "adjustment": 5, "rationale": "x",
         "cited_headlines": [], "affected_companies": []},
    ]})
    res = news.compute_news_adjustment("semiconductors", TAX, HEADS, COMPANIES,
                                       _client=_client(payload))
    assert res["ok"] and res["per_sub_theme"] == [] and res["total_adjustment"] == 0.0
