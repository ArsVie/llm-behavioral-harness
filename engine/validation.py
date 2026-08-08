"""Validación de configuración + cota de estabilidad del lazo (W1.7).

PROPIEDAD: tarea W1.7 (este archivo + tests/test_validation.py). Implementar
contra engine/types.py (CONGELADO).

Chequeos congelados (cada violación produce UN string que NOMBRA el campo):

PersonaParams:
  - N >= 1 (entero); 0 < lam < 1; nu > 0 (math.inf válido); k >= 0;
    0 <= rho < 1; 0 <= rho_e < 1; sigma_e >= 0; B >= 0; A >= 0;
    sigma_eps >= 0; L_mean > 0; L_sd >= 0.
  - Cota de estabilidad del lazo juez→μ (research/05 §2):
        k < 2·(1 − rho)/g_max,   g_max = 1 + A + 3·sigma_eps
    (peor caso p(1−p)=0.25 ya absorbido en la constante 2 de la cota).

TimingParams:
  - k_w > 0; theta_h > 0; 0 <= peak_hour < 24; diurnal_amp >= 0.
  - quiet_hours: ambos extremos en [0, 24) y distintos entre sí (ini > fin
    válido = cruza medianoche).
  - phase_multipliers: exactamente las 5 fases de types.PHASE_FRACTIONS como
    llaves; todos los valores > 0.
  - adj_bounds ⊂ [0.5, 1.5] con lo <= hi.
  - min_gap_min >= 0; daily_cap >= 1; max_gap_h > min_gap_min/60.
"""
from __future__ import annotations

import math

from engine.types import PHASE_FRACTIONS, PersonaParams, TimingParams


def check(persona: PersonaParams, timing: TimingParams) -> list[str]:
    """Lista de errores legibles (vacía = configuración válida).

    Cada mensaje incluye el nombre del campo violado (p. ej. "k: ...").
    La cota de estabilidad se reporta con los dos lados evaluados.
    """
    errors: list[str] = []

    # PersonaParams checks
    errors.extend(_check_persona(persona))
    # TimingParams checks
    errors.extend(_check_timing(timing))

    return errors


def _check_persona(persona: PersonaParams) -> list[str]:
    """Valida PersonaParams según el contrato congelado."""
    errors: list[str] = []

    # N >= 1 (entero, no bool)
    if not isinstance(persona.N, int) or isinstance(persona.N, bool):
        errors.append(f"N: must be an integer, got {type(persona.N).__name__}")
    elif persona.N < 1:
        errors.append(f"N: must be >= 1, got {persona.N}")

    # 0 < lam < 1
    if not (0 < persona.lam < 1):
        errors.append(f"lam: must be in (0, 1), got {persona.lam}")

    # nu > 0 (math.inf válido)
    if persona.nu <= 0:
        errors.append(f"nu: must be > 0, got {persona.nu}")

    # k >= 0
    if persona.k < 0:
        errors.append(f"k: must be >= 0, got {persona.k}")

    # 0 <= rho < 1
    if not (0 <= persona.rho < 1):
        errors.append(f"rho: must be in [0, 1), got {persona.rho}")

    # 0 <= rho_e < 1
    if not (0 <= persona.rho_e < 1):
        errors.append(f"rho_e: must be in [0, 1), got {persona.rho_e}")

    # sigma_e >= 0
    if persona.sigma_e < 0:
        errors.append(f"sigma_e: must be >= 0, got {persona.sigma_e}")

    # B >= 0
    if persona.B < 0:
        errors.append(f"B: must be >= 0, got {persona.B}")

    # A >= 0
    if persona.A < 0:
        errors.append(f"A: must be >= 0, got {persona.A}")

    # sigma_eps >= 0
    if persona.sigma_eps < 0:
        errors.append(f"sigma_eps: must be >= 0, got {persona.sigma_eps}")

    # L_mean > 0
    if persona.L_mean <= 0:
        errors.append(f"L_mean: must be > 0, got {persona.L_mean}")

    # L_sd >= 0
    if persona.L_sd < 0:
        errors.append(f"L_sd: must be >= 0, got {persona.L_sd}")

    # Cota de estabilidad: k < 2·(1 − rho)/g_max
    # donde g_max = 1 + A + 3·sigma_eps
    g_max = 1 + persona.A + 3 * persona.sigma_eps
    stability_bound = 2 * (1 - persona.rho) / g_max
    if persona.k >= stability_bound:  # violación si k >= cota (desigualdad estricta)
        errors.append(
            f"k: violates stability bound k < 2(1−rho)/g_max "
            f"({persona.k} >= {stability_bound:.6f}, g_max={g_max:.2f})"
        )

    return errors


def _check_timing(timing: TimingParams) -> list[str]:
    """Valida TimingParams según el contrato congelado."""
    errors: list[str] = []

    # k_w > 0
    if timing.k_w <= 0:
        errors.append(f"k_w: must be > 0, got {timing.k_w}")

    # theta_h > 0
    if timing.theta_h <= 0:
        errors.append(f"theta_h: must be > 0, got {timing.theta_h}")

    # 0 <= peak_hour < 24
    if not (0 <= timing.peak_hour < 24):
        errors.append(f"peak_hour: must be in [0, 24), got {timing.peak_hour}")

    # diurnal_amp >= 0
    if timing.diurnal_amp < 0:
        errors.append(f"diurnal_amp: must be >= 0, got {timing.diurnal_amp}")

    # quiet_hours: ambos extremos en [0, 24) y distintos
    ini, fin = timing.quiet_hours
    if not (0 <= ini < 24):
        errors.append(f"quiet_hours: ini must be in [0, 24), got {ini}")
    if not (0 <= fin < 24):
        errors.append(f"quiet_hours: fin must be in [0, 24), got {fin}")
    if ini == fin:
        errors.append(f"quiet_hours: ini and fin must be different, got ({ini}, {fin})")

    # phase_multipliers: exactamente las 5 fases como llaves, todos > 0
    expected_phases = {phase[0] for phase in PHASE_FRACTIONS}
    actual_phases = set(timing.phase_multipliers.keys())

    if actual_phases != expected_phases:
        errors.append(
            f"phase_multipliers: must have exactly the phases from PHASE_FRACTIONS, "
            f"got {actual_phases}"
        )
    else:
        for phase, multiplier in timing.phase_multipliers.items():
            if multiplier <= 0:
                errors.append(
                    f"phase_multipliers[{phase}]: must be > 0, got {multiplier}"
                )

    # adj_bounds: lo, hi in [0.5, 1.5] con lo <= hi
    lo, hi = timing.adj_bounds
    if not (0.5 <= lo <= 1.5):
        errors.append(f"adj_bounds[0]: must be in [0.5, 1.5], got {lo}")
    if not (0.5 <= hi <= 1.5):
        errors.append(f"adj_bounds[1]: must be in [0.5, 1.5], got {hi}")
    if lo > hi:
        errors.append(f"adj_bounds: lo must be <= hi, got ({lo}, {hi})")

    # min_gap_min >= 0
    if timing.min_gap_min < 0:
        errors.append(f"min_gap_min: must be >= 0, got {timing.min_gap_min}")

    # daily_cap >= 1
    if timing.daily_cap < 1:
        errors.append(f"daily_cap: must be >= 1, got {timing.daily_cap}")

    # max_gap_h > min_gap_min/60
    if timing.max_gap_h <= timing.min_gap_min / 60:
        errors.append(
            f"max_gap_h: must be > min_gap_min/60 "
            f"({timing.max_gap_h} <= {timing.min_gap_min / 60})"
        )

    return errors
