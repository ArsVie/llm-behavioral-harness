"""Fixtures compartidos (Ola 0 — CONGELADO tras W0.1).

Cada tarea escribe SOLO su tests/test_<módulo>.py; este archivo no se toca.
"""
from __future__ import annotations

import numpy as np
import pytest

from engine.types import PersonaParams, TimingParams


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
