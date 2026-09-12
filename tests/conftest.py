"""Fixtures compartidos (Ola 0 — CONGELADO tras W0.1).

Cada tarea escribe SOLO su tests/test_<módulo>.py; este archivo no se toca.
"""
from __future__ import annotations

import os

# Durability is not under test: every store the suite opens writes to a
# throwaway tmp_path that is deleted at teardown, so the per-commit fsync
# SQLite does by default buys nothing and costs ~4.8 ms a commit. Set before
# harness.store is imported so the first connection already picks it up; a
# value already in the environment wins, so a durability-specific test can
# still ask for FULL.
os.environ.setdefault("HARNESS_SQLITE_SYNCHRONOUS", "OFF")

# The cold-start setup proposals (interest graph, routine catalog) cache to
# the real user cache by default. No test may write there: it pollutes the
# developer's home directory and makes tests order-dependent, since the first
# test to cache a stub's answer makes every later one resolve from cache
# instead of exercising its own path. Empty disables the cache outright; a
# test that wants to exercise caching sets its own tmp path via monkeypatch.
os.environ.setdefault("HARNESS_PROPOSAL_CACHE", "")

import numpy as np  # noqa: E402 - must follow the pragma default above
import pytest  # noqa: E402

from engine.types import PersonaParams, TimingParams  # noqa: E402


@pytest.fixture
def rng() -> np.random.Generator:
    """Generator determinista para tests (semilla fija 12345)."""
    return np.random.default_rng(12345)


@pytest.fixture
def persona() -> PersonaParams:
    """PersonaParams con los defaults de DESIGN.md."""
    return PersonaParams()


@pytest.fixture
def timing() -> TimingParams:
    """TimingParams con los defaults de DESIGN.md."""
    return TimingParams()
