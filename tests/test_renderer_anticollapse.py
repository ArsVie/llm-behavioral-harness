"""Anti-collapse property of the briefs renderer.

Every vector of mood intended as a distinct state must render a
distinct brief: two collapsed states are indistinguishable to the model.
"""

from __future__ import annotations

from harness.behavior import _render_brief

# Unexplored channels: representative mean values, fixed across the
# whole battery. Only valence/energy vary.
_MID = {
    "momentum": 0.0,
    "warmth": 0.6,
    "playfulness": 0.5,
    "reflectiveness": 0.5,
}


def _brief(*, valence: float, energy: float) -> str:
    return _render_brief(valence=valence, energy=energy, **_MID)


def test_valence_0_6_and_1_0_must_render_distinct_briefs() -> None:
    """The original decision probe: valence 0.6 vs 1.0 (same energy).

    Today both fall in the '> 0.35' band and render the same brief
    ('quietly bright') -> the model cannot distinguish the state.
    """
    low = _brief(valence=0.6, energy=0.5)
    high = _brief(valence=1.0, energy=0.5)

    assert low != high


def test_energy_0_4_and_0_6_must_render_distinct_briefs() -> None:
    """Energy 0.4 vs 0.6 with everything else identical.

    Both fall in the middle band (0.35 <= e <= 0.7) and render 'calmly present'
    -> collapse of the energy channel. (The pair 0.4 vs 0.8 does NOT collapse
    today: 0.8 exceeds the 0.7 threshold and already renders 'lively and
    readily engaged'.)
    """
    low = _brief(valence=0.0, energy=0.4)
    high = _brief(valence=0.0, energy=0.6)

    assert low != high


def test_valence_energy_grid_must_render_injective_briefs() -> None:
    """Full valence x energy grid: 32 vectors -> 32 briefs.

    With 8 valence values and 4 energy values, the anti-collapse property
    requires 32 distinct briefs. Today the 3x3 band renderer collapses the
    grid to 9 briefs -> RED.
    """
    valences = (-1.0, -0.7, -0.4, -0.1, 0.2, 0.5, 0.8, 1.0)
    energies = (0.1, 0.4, 0.7, 0.9)

    briefs = {_brief(valence=v, energy=e) for v in valences for e in energies}

    assert len(briefs) == len(valences) * len(energies)
