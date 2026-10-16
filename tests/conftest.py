"""Shared pytest configuration for the engine test suite."""

from __future__ import annotations

import pytest
from hypothesis import settings, HealthCheck

# Embedding the first call loads the static model (slow once); property tests
# generate many cases, so disable the per-example deadline globally.
settings.register_profile(
    "engine",
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("engine")


@pytest.fixture(scope="session", autouse=True)
def _warm_embedding_model():
    """Load the embedding model once up front so timing is not attributed to a test."""
    import engine
    try:
        engine._embed("warmup text")
    except Exception:
        pass
