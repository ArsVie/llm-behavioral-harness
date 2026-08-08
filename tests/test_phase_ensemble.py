"""Aceptación estadística de la semántica menstrual/ovulatoria por defecto."""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from engine.types import MoodVariant, PHASE_MENSTRUAL, PHASE_OVULATORY
from sim.run_daily import run


def test_default_ensemble_is_low_variable_menstrual_and_high_steady_ovulatory() -> None:
    by_phase: dict[str, list[int]] = defaultdict(list)
    for seed in range(200):
        result = run(days=30, seed=seed, variant=MoodVariant.DECOUPLED_OFFSETS)
        for record in result.records:
            by_phase[record.phase_label].append(record.M)

    menstrual = np.asarray(by_phase[PHASE_MENSTRUAL], dtype=float)
    ovulatory = np.asarray(by_phase[PHASE_OVULATORY], dtype=float)

    assert float(np.mean(menstrual)) < 5.0
    assert float(np.mean(ovulatory)) > 7.0
    assert float(np.std(ovulatory)) < float(np.std(menstrual)) * 0.85

