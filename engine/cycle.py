"""Hormonal cycle ~28 days — level, reactivity and phases.

The level ``m`` and the gain ``g`` use smooth periodic curves between phase
centers. Ovulatory has high level and low reactivity; menstrual has low level
and high reactivity. B and A keep their role as separate amplitudes.

When the cycle completes, ``L ~ Normal(L_mean, L_sd)`` is redrawn and truncated
to >= 1. The initial phase ``phi`` enters as ``cycle_day``. The RNG consumption
of ``step`` is preserved: first g noise and then an L redraw only on wraparound.
"""
from __future__ import annotations

import numpy as np

from engine.types import PHASE_FRACTIONS, CycleState, PersonaParams


# Dimensionless shapes at the center of each phase. They are product
# semantics, not clinical hormonal levels.
MOOD_PHASE_ANCHORS: dict[str, float] = {
    "menstrual": -1.2,
    "follicular": 0.3,
    "ovulatory": 0.81,
    "luteal_early": 0.3,
    "luteal_late": -0.5,
}

REACTIVITY_PHASE_ANCHORS: dict[str, float] = {
    "menstrual": 1.0,
    "follicular": 0.0,
    "ovulatory": -1.0,
    "luteal_early": -0.2,
    "luteal_late": 0.7,
}


def _sample_truncated_normal(
    rng: np.random.Generator, mean: float, sd: float, lower: float
) -> float:
    """Sample a Normal truncated to ``>= lower`` via rejection sampling."""
    while True:
        x = rng.normal(mean, sd)
        if x >= lower:
            return x


def _phase_anchor_curve(
    cycle_day: float, length: float, anchors: dict[str, float]
) -> float:
    """C1 periodic curve by cosine interpolation between phase centers."""
    u = (cycle_day / length) % 1.0
    points = [
        ((start + end) / 2.0, anchors[label])
        for label, start, end in PHASE_FRACTIONS
    ]

    if u > points[-1][0]:
        left_x, left_y = points[-1]
        right_x, right_y = points[0][0] + 1.0, points[0][1]
    elif u < points[0][0]:
        left_x, left_y = points[-1][0] - 1.0, points[-1][1]
        right_x, right_y = points[0]
    else:
        for index in range(1, len(points)):
            if u <= points[index][0]:
                left_x, left_y = points[index - 1]
                right_x, right_y = points[index]
                break
        else:  # pragma: no cover - guarded by the earlier branches
            raise AssertionError("phase interval not found")

    alpha = (u - left_x) / (right_x - left_x)
    weight = 0.5 * (1.0 - np.cos(np.pi * alpha))
    return float(left_y + (right_y - left_y) * weight)


def init_state(params: PersonaParams, rng: np.random.Generator) -> CycleState:
    """Initializes L and ``cycle_day = phi % L``."""
    L_0 = _sample_truncated_normal(rng, params.L_mean, params.L_sd, 1.0)
    cycle_day = params.phi % L_0
    return CycleState(cycle_day=cycle_day, L_current=L_0)


def phase_of(cycle_day: float, L: float) -> str:
    """Phase label for ``cycle_day`` according to ``PHASE_FRACTIONS``."""
    normalized = cycle_day / L
    for label, frac_ini, frac_fin in PHASE_FRACTIONS:
        if frac_ini <= normalized < frac_fin:
            return label
    return PHASE_FRACTIONS[-1][0]


def step(
    state: CycleState, params: PersonaParams, rng: np.random.Generator
) -> tuple[float, float, str, CycleState]:
    """Advances one day without mutating ``state`` and returns ``m, g, phase, next``."""
    L = state.L_current
    d = state.cycle_day

    m = params.B * _phase_anchor_curve(d, L, MOOD_PHASE_ANCHORS)

    eps = rng.normal(0, params.sigma_eps)
    g = 1 + params.A * _phase_anchor_curve(d, L, REACTIVITY_PHASE_ANCHORS) + eps

    phase_label = phase_of(d, L)
    next_cycle_day = d + 1.0
    next_L = L

    if next_cycle_day >= L:
        next_cycle_day -= L
        next_L = _sample_truncated_normal(rng, params.L_mean, params.L_sd, 1.0)

    return m, g, phase_label, CycleState(
        cycle_day=next_cycle_day,
        L_current=next_L,
    )

