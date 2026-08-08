"""Regresión: el actuador respeta escalas de ánimo configurables."""

from engine.types import DayRecord, TimingParams
from harness.behavior import derive_behavior


def test_non_default_mood_scale_is_normalized_correctly() -> None:
    record = DayRecord(
        t=0,
        m=0.0,
        g=1.0,
        arg=0.0,
        p=1.0,
        M=5,
        score=0.0,
        mu=0.0,
        eta=0.0,
        cycle_day=0.0,
        phase_label="follicular",
        seed=1,
    )

    directive = derive_behavior(record, TimingParams(), hour=14.0, mood_scale=5)

    assert directive.valence == 1.0

