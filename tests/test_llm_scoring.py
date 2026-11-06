"""Tests for the LLM-as-judge binary scoring path and match-response parsing."""

from __future__ import annotations

import json

import pytest

import engine


@pytest.fixture(autouse=True)
def _isolate_caches(tmp_path, monkeypatch):
    """Point the taxonomy + match caches at a temp dir and clear in-memory
    caches, so tests never read or write the committed cache/ directory."""
    monkeypatch.setattr(engine, "_TAXONOMY_CACHE_DIR", str(tmp_path / "tax"))
    monkeypatch.setattr(engine, "_MATCH_CACHE_DIR", str(tmp_path / "match"))
    engine._taxonomy_mem_cache.clear()
    engine._match_mem_cache.clear()
    engine._groq_clients.clear()
    yield
    engine._taxonomy_mem_cache.clear()
    engine._match_mem_cache.clear()
    engine._groq_clients.clear()


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


# ---- multi-key failover ----

class _RateLimit(Exception):
    status_code = 429


def _fake_client_factory(behaviour):
    """behaviour: dict key -> 'raise' or 'ok'. Returns a factory producing a
    fake Groq client that raises a rate limit or returns a stub response."""
    class _Completions:
        def __init__(self, mode):
            self._mode = mode

        def create(self, **kwargs):
            if self._mode == "raise":
                raise _RateLimit("rate_limit_exceeded")
            class _R:
                choices = [type("C", (), {"message": type("M", (), {"content": "{}"})})]
            return _R()

    class _Chat:
        def __init__(self, mode):
            self.completions = _Completions(mode)

    class _Client:
        def __init__(self, mode):
            self.chat = _Chat(mode)

    def factory(api_key):
        return _Client(behaviour[api_key])
    return factory


def test_groq_failover_uses_second_key_on_rate_limit(monkeypatch):
    monkeypatch.setattr(engine, "_groq_keys", lambda: ["K1", "K2"])
    engine._groq_clients.clear()
    monkeypatch.setattr(engine, "_groq_client_for",
                        _fake_client_factory({"K1": "raise", "K2": "ok"}))
    # K1 rate-limits, so it must fail over to K2 and succeed (no exception).
    resp = engine._groq_chat([{"role": "user", "content": "x"}], 100)
    assert resp.choices[0].message.content == "{}"


def test_groq_failover_raises_when_all_keys_rate_limited(monkeypatch):
    monkeypatch.setattr(engine, "_groq_keys", lambda: ["K1", "K2"])
    engine._groq_clients.clear()
    monkeypatch.setattr(engine, "_groq_client_for",
                        _fake_client_factory({"K1": "raise", "K2": "raise"}))
    import pytest
    with pytest.raises(Exception):
        engine._groq_chat([{"role": "user", "content": "x"}], 100)


def test_is_rate_limit_detects_statuses():
    assert engine._is_rate_limit(_RateLimit()) is True
    class _Big(Exception):
        status_code = 413
    assert engine._is_rate_limit(_Big()) is True
    assert engine._is_rate_limit(Exception("something else")) is False


# ---- batch completeness: a dropped company triggers a split, never silent loss ----

def test_matcher_splits_when_company_dropped(monkeypatch):
    # Simulate the model returning only SOME companies on a big batch, then all
    # of them once the batch is split small. Every sent company must appear.
    monkeypatch.setattr(engine, "get_secret", lambda k, d=None: "key")
    monkeypatch.setattr(engine, "_groq_keys", lambda: ["K"])
    engine._groq_clients.clear()
    monkeypatch.setattr(engine, "_MATCH_CHUNK_TOKEN_BUDGET", 10_000)  # one big chunk

    tax = {"theme": "t", "layer1": [
        {"sub_industry": "Sub A", "activities": ["x"]}]}
    rows = [
        {"company": f"CO{i}", "has_segment_data": True,
         "segments": [{"name": f"Seg{i}", "revenue": 1.0, "revenue_present": True}],
         "business_description": ""}
        for i in range(4)
    ]

    def fake_call(messages, max_tokens, model=None):
        # Count how many companies are in this request's user message.
        user = messages[1]["content"]
        names = [f"CO{i}" for i in range(4) if f"CO{i}:" in user]
        content_companies = []
        for n in names:
            # Drop the LAST company whenever more than one is requested, forcing
            # a split; a single-company request returns that company.
            if len(names) > 1 and n == names[-1]:
                continue
            content_companies.append(
                {"company": n, "segments": [
                    {"name": n.replace("CO", "Seg"), "match": True,
                     "sub_theme": "Sub A", "reason": "r"}]})
        payload = json.dumps({"companies": content_companies})

        class _R:
            choices = [type("C", (), {"message": type("M", (), {"content": payload})})]
        return _R()

    monkeypatch.setattr(engine, "_groq_chat", fake_call)
    res = engine.match_segments_llm(tax, rows, force_refresh=True)
    assert res["ok"]
    # All four companies present despite the model dropping the last each time.
    assert set(res["match_map"].keys()) == {"CO0", "CO1", "CO2", "CO3"}
