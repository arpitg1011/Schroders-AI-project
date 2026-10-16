"""Integration tests for generate_taxonomy with a mocked Groq client (Req 5, R2)."""

from __future__ import annotations

import json
from unittest import mock

import pytest

import engine


@pytest.fixture(autouse=True)
def _isolate_taxonomy_cache(tmp_path, monkeypatch):
    """Point the taxonomy cache at a temp dir and clear memory for each test,
    so cross-test cache hits never mask the LLM path (Req 13)."""
    monkeypatch.setattr(engine, "_TAXONOMY_CACHE_DIR", str(tmp_path / "tax_cache"))
    engine._taxonomy_mem_cache.clear()
    yield
    engine._taxonomy_mem_cache.clear()

GOOD = json.dumps({"layer1": [
    {"sub_industry": "Grid Equipment", "activities": ["power transformers", "switchgear"]},
    {"sub_industry": "Energy Storage", "activities": ["battery storage", "grid storage"]},
    {"sub_industry": "Grid Automation", "activities": ["smart meters", "scada"]},
]})
BAD = "not json at all {"


class _Msg:
    def __init__(self, content): self.message = type("M", (), {"content": content})


class _Resp:
    def __init__(self, content): self.choices = [_Msg(content)]


def _fake_client(contents):
    """Return a fake Groq client whose create() yields the given contents in order."""
    seq = iter(contents)

    class _Completions:
        def create(self, **kwargs):
            return _Resp(next(seq))

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    return _Client()


def test_missing_api_key_no_network(monkeypatch):
    monkeypatch.setattr(engine, "get_secret", lambda k, d=None: None)
    res = engine.generate_taxonomy("X")
    assert res["ok"] is False and res["error_code"] == engine.MISSING_API_KEY


def test_valid_first_try(monkeypatch):
    monkeypatch.setattr(engine, "get_secret", lambda k, d=None: "key")
    with mock.patch("groq.Groq", return_value=_fake_client([GOOD])):
        res = engine.generate_taxonomy("Power Grid")
    assert res["ok"] is True
    assert 3 <= len(res["taxonomy"]["layer1"]) <= 4


def test_malformed_then_repair(monkeypatch):
    monkeypatch.setattr(engine, "get_secret", lambda k, d=None: "key")
    with mock.patch("groq.Groq", return_value=_fake_client([BAD, GOOD])):
        res = engine.generate_taxonomy("Power Grid")
    assert res["ok"] is True


def test_malformed_twice_bad_json(monkeypatch):
    monkeypatch.setattr(engine, "get_secret", lambda k, d=None: "key")
    with mock.patch("groq.Groq", return_value=_fake_client([BAD, BAD])):
        res = engine.generate_taxonomy("Power Grid")
    assert res["ok"] is False and res["error_code"] == engine.LLM_BAD_JSON


INVALID = json.dumps({"valid_theme": False, "reason": "Input is a personal name."})


def test_invalid_theme_is_rejected(monkeypatch):
    # A non-theme input (e.g. a name) must be rejected, not turned into a taxonomy.
    monkeypatch.setattr(engine, "get_secret", lambda k, d=None: "key")
    with mock.patch("groq.Groq", return_value=_fake_client([INVALID])):
        res = engine.generate_taxonomy("arpit")
    assert res["ok"] is False
    assert res["error_code"] == engine.INVALID_THEME


def test_invalid_theme_not_cached(monkeypatch):
    # A rejected theme must not be cached (so a later valid run can succeed).
    monkeypatch.setattr(engine, "get_secret", lambda k, d=None: "key")
    with mock.patch("groq.Groq", return_value=_fake_client([INVALID])):
        engine.generate_taxonomy("arpit")
    # Second call still reaches the model (nothing cached) -> provide a response.
    with mock.patch("groq.Groq", return_value=_fake_client([INVALID])):
        res2 = engine.generate_taxonomy("arpit")
    assert res2["ok"] is False and res2["error_code"] == engine.INVALID_THEME


def test_old_format_cache_is_ignored(monkeypatch, tmp_path):
    # A pre-gate cache file (no _v version wrapper) must NOT be served; the
    # model is consulted again. Guards against grandfathered junk like "heyyyyyaa".
    import json as _json
    monkeypatch.setattr(engine, "get_secret", lambda k, d=None: "key")
    # Write a legacy cache file in the OLD format (raw taxonomy, no _v).
    cache_dir = engine._TAXONOMY_CACHE_DIR
    import os
    os.makedirs(cache_dir, exist_ok=True)
    legacy = {"theme": "junkword", "layer1": [
        {"sub_industry": "X", "activities": ["a", "b"]}]}
    with open(engine._cache_path("junkword"), "w", encoding="utf-8") as f:
        _json.dump(legacy, f)
    engine._taxonomy_mem_cache.clear()
    # The legacy entry must be ignored -> the model is called (we feed INVALID).
    with mock.patch("groq.Groq", return_value=_fake_client([INVALID])):
        res = engine.generate_taxonomy("junkword")
    assert res["ok"] is False and res["error_code"] == engine.INVALID_THEME


def test_cache_reuse_avoids_second_llm_call(monkeypatch):
    # Req 13: a repeated theme reuses the cache instead of calling the LLM again.
    monkeypatch.setattr(engine, "get_secret", lambda k, d=None: "key")
    with mock.patch("groq.Groq", return_value=_fake_client([GOOD])) as patched:
        first = engine.generate_taxonomy("Reproducible Theme")
    assert first["ok"] and first.get("cached") is False
    # Second call must NOT hit Groq (no fake responses provided -> would StopIteration).
    second = engine.generate_taxonomy("Reproducible Theme")
    assert second["ok"] and second.get("cached") is True
    assert second["taxonomy"] == first["taxonomy"]


def test_force_refresh_bypasses_cache(monkeypatch):
    # Req 13.4: force_refresh regenerates even when cached.
    monkeypatch.setattr(engine, "get_secret", lambda k, d=None: "key")
    with mock.patch("groq.Groq", return_value=_fake_client([GOOD])):
        engine.generate_taxonomy("Forced Theme")
    with mock.patch("groq.Groq", return_value=_fake_client([GOOD])):
        again = engine.generate_taxonomy("Forced Theme", force_refresh=True)
    assert again["ok"] and again.get("cached") is False


def test_validator_rejects_bad_shapes():
    assert engine._validate_taxonomy({"layer1": []}, "t") is None
    assert engine._validate_taxonomy({"nope": 1}, "t") is None
    assert engine._validate_taxonomy({"layer1": [{"sub_industry": "S", "activities": "x"}]}, "t") is None
    good = engine._validate_taxonomy(json.loads(GOOD), "t")
    assert good is not None and good["theme"] == "t"
