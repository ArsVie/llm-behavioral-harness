"""Ciclo hormonal ~28 días — nivel, reactividad y fases.

El nivel ``m`` y la ganancia ``g`` usan curvas periódicas suaves entre centros
de fase. Ovulatoria tiene nivel alto y reactividad baja; menstrual tiene nivel
bajo y reactividad alta. B y A conservan su papel como amplitudes separadas.

Al completar el ciclo se redibuja ``L ~ Normal(L_mean, L_sd)`` truncada a uno.
La fase inicial ``phi`` entra como ``cycle_day``. El consumo RNG de ``step`` se
mantiene: primero ruido de g y luego redraw de L solo si hay wraparound.
"""
from __future__ import annotations

import numpy as np

from engine.types import PHASE_FRACTIONS, CycleState, PersonaParams


# Formas adimensionales en el centro de cada fase. Son semántica de producto,
# no niveles hormonales clínicos.
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
    """Muestrear Normal truncada a ``>= lower`` con rejection sampling."""
    while True:
        x = rng.normal(mean, sd)
        if x >= lower:
            return x


def _phase_anchor_curve(
    cycle_day: float, length: float, anchors: dict[str, float]
) -> float:
    """Curva periódica C1 por interpolación cosenoidal entre centros de fase."""
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
        else:  # pragma: no cover - protegido por las ramas anteriores
            raise AssertionError("intervalo de fase no encontrado")

    alpha = (u - left_x) / (right_x - left_x)
    weight = 0.5 * (1.0 - np.cos(np.pi * alpha))
    return float(left_y + (right_y - left_y) * weight)


def init_state(params: PersonaParams, rng: np.random.Generator) -> CycleState:
    """Inicializa L y ``cycle_day = phi % L``."""
    L_0 = _sample_truncated_normal(rng, params.L_mean, params.L_sd, 1.0)
    cycle_day = params.phi % L_0
    return CycleState(cycle_day=cycle_day, L_current=L_0)


def phase_of(cycle_day: float, L: float) -> str:
    """Etiqueta de fase para ``cycle_day`` según ``PHASE_FRACTIONS``."""
    normalized = cycle_day / L
    for label, frac_ini, frac_fin in PHASE_FRACTIONS:
        if frac_ini <= normalized < frac_fin:
            return label
    return PHASE_FRACTIONS[-1][0]


def step(
    state: CycleState, params: PersonaParams, rng: np.random.Generator
) -> tuple[float, float, str, CycleState]:
    """Avanza un día sin mutar ``state`` y devuelve ``m, g, fase, next``."""
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

