"""Semántica de producto para fase, energía y variación observable."""

from __future__ import annotations

import numpy as np

from engine import circadian, cycle
from engine.types import (
    PHASE_FRACTIONS,
    PHASE_MENSTRUAL,
    PHASE_OVULATORY,
    CycleState,
    DayRecord,
    PersonaParams,
    TimingParams,
)
from harness.behavior import derive_behavior


def _phase_center(label: str, length: float = 28.0) -> float:
    for phase, start, end in PHASE_FRACTIONS:
        if phase == label:
            return length * (start + end) / 2.0
    raise AssertionError(f"fase desconocida: {label}")


def _signals(label: str) -> tuple[float, float]:
    params = PersonaParams(sigma_eps=0.0)
    state = CycleState(cycle_day=_phase_center(label), L_current=28.0)
    m, g, actual_label, _ = cycle.step(state, params, np.random.default_rng(1))
    assert actual_label == label
    return m, g


def _record(*, mood: int, phase: str) -> DayRecord:
    return DayRecord(
        t=0,
        m=0.0,
        g=1.0,
        arg=0.0,
        p=mood / 10,
        M=mood,
        score=0.0,
        mu=0.0,
        eta=0.0,
        cycle_day=0.0,
        phase_label=phase,
        seed=1,
    )


def test_ovulatory_mood_signal_is_high_and_less_reactive() -> None:
    menstrual_m, menstrual_g = _signals(PHASE_MENSTRUAL)
    ovulatory_m, ovulatory_g = _signals(PHASE_OVULATORY)

    assert ovulatory_m > 0.8 * PersonaParams().B
    assert menstrual_m < 0.0
    assert ovulatory_g < 1.0
    assert menstrual_g > 1.0
    assert menstrual_g - ovulatory_g > 0.3


def test_ovulatory_energy_is_higher_and_flatter_than_menstrual_energy() -> None:
    params = TimingParams()
    hours = np.linspace(0.0, 24.0, 97, endpoint=False)
    menstrual = np.array([circadian.energy(h, PHASE_MENSTRUAL, params) for h in hours])
    ovulatory = np.array([circadian.energy(h, PHASE_OVULATORY, params) for h in hours])

    assert float(np.mean(ovulatory)) > float(np.mean(menstrual)) + 0.15
    assert float(np.ptp(ovulatory)) < float(np.ptp(menstrual)) * 0.7


def test_same_mood_can_have_different_energy_without_changing_valence() -> None:
    record = _record(mood=6, phase=PHASE_OVULATORY)
    low_energy = derive_behavior(record, TimingParams(), hour=2.0)
    high_energy = derive_behavior(record, TimingParams(), hour=14.0)

    assert low_energy.valence == high_energy.valence
    assert high_energy.energy > low_energy.energy
    assert high_energy.expressiveness > low_energy.expressiveness

