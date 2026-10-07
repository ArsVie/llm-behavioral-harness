"""Configuration validation + loop stability bound.

Checks (each violation produces ONE string that NAMES the field):

PersonaParams:
  - N >= 1 (integer); 0 < lam < 1; nu > 0 (math.inf is valid); k >= 0;
    0 <= rho < 1; 0 <= rho_e < 1; sigma_e >= 0; B >= 0; A >= 0;
    sigma_eps >= 0; L_mean > 0; L_sd >= 0.
  - Stability bound of the judge→μ loop (research/05 §2):
        k < 2·(1 − rho)/g_max,   g_max = 1 + A + 3·sigma_eps
    (worst case p(1−p)=0.25 already absorbed in the constant 2 of the bound).

TimingParams:
  - k_w > 0; theta_h > 0; 0 <= peak_hour < 24; diurnal_amp >= 0.
  - quiet_hours: both endpoints in [0, 24) and distinct (start > end
    is valid = crosses midnight).
  - phase_multipliers: exactly the 5 phases of types.PHASE_FRACTIONS as
    keys; all values > 0.
  - adj_bounds ⊂ [0.5, 1.5] with lo <= hi.
  - min_gap_min >= 0; daily_cap >= 1; max_gap_h > min_gap_min/60.
"""
from __future__ import annotations

from engine.types import PHASE_FRACTIONS, PersonaParams, TimingParams


def check(persona: PersonaParams, timing: TimingParams) -> list[str]:
    """List of readable errors (empty = valid configuration).

    Each message includes the name of the violated field (e.g. "k: ...").
    The stability bound is reported with both sides evaluated.
    """
    return _check_persona(persona) + _check_timing(timing)


def _check_persona(persona: PersonaParams) -> list[str]:
    """Validates PersonaParams against the frozen contract."""
    errors: list[str] = []

    # N >= 1 (integer, not bool)
    if not isinstance(persona.N, int) or isinstance(persona.N, bool):
        errors.append(f"N: must be an integer, got {type(persona.N).__name__}")
    elif persona.N < 1:
        errors.append(f"N: must be >= 1, got {persona.N}")
    if not (0 < persona.lam < 1):
        errors.append(f"lam: must be in (0, 1), got {persona.lam}")
    if persona.nu <= 0:
        errors.append(f"nu: must be > 0, got {persona.nu}")
    if persona.k < 0:
        errors.append(f"k: must be >= 0, got {persona.k}")
    if not (0 <= persona.rho < 1):
        errors.append(f"rho: must be in [0, 1), got {persona.rho}")
    if not (0 <= persona.rho_e < 1):
        errors.append(f"rho_e: must be in [0, 1), got {persona.rho_e}")
    if persona.sigma_e < 0:
        errors.append(f"sigma_e: must be >= 0, got {persona.sigma_e}")
    if persona.B < 0:
        errors.append(f"B: must be >= 0, got {persona.B}")
    if persona.A < 0:
        errors.append(f"A: must be >= 0, got {persona.A}")
    if persona.sigma_eps < 0:
        errors.append(f"sigma_eps: must be >= 0, got {persona.sigma_eps}")
    if persona.L_mean <= 0:
        errors.append(f"L_mean: must be > 0, got {persona.L_mean}")
    if persona.L_sd < 0:
        errors.append(f"L_sd: must be >= 0, got {persona.L_sd}")

    # Stability bound: k < 2·(1 − rho)/g_max
    # where g_max = 1 + A + 3·sigma_eps
    g_max = 1 + persona.A + 3 * persona.sigma_eps
    stability_bound = 2 * (1 - persona.rho) / g_max
    if persona.k >= stability_bound:
        errors.append(
            f"k: violates stability bound k < 2(1−rho)/g_max "
            f"({persona.k} >= {stability_bound:.6f}, g_max={g_max:.2f})"
        )

    return errors


def _check_timing(timing: TimingParams) -> list[str]:
    """Validates TimingParams against the frozen contract."""
    errors: list[str] = []

    if timing.k_w <= 0:
        errors.append(f"k_w: must be > 0, got {timing.k_w}")
    if timing.theta_h <= 0:
        errors.append(f"theta_h: must be > 0, got {timing.theta_h}")
    if not (0 <= timing.peak_hour < 24):
        errors.append(f"peak_hour: must be in [0, 24), got {timing.peak_hour}")
    if timing.diurnal_amp < 0:
        errors.append(f"diurnal_amp: must be >= 0, got {timing.diurnal_amp}")

    start, end = timing.quiet_hours
    if not (0 <= start < 24):
        errors.append(f"quiet_hours: start must be in [0, 24), got {start}")
    if not (0 <= end < 24):
        errors.append(f"quiet_hours: end must be in [0, 24), got {end}")
    if start == end:
        errors.append(f"quiet_hours: start and end must be different, got ({start}, {end})")

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

    lo, hi = timing.adj_bounds
    if not (0.5 <= lo <= 1.5):
        errors.append(f"adj_bounds[0]: must be in [0.5, 1.5], got {lo}")
    if not (0.5 <= hi <= 1.5):
        errors.append(f"adj_bounds[1]: must be in [0.5, 1.5], got {hi}")
    if lo > hi:
        errors.append(f"adj_bounds: lo must be <= hi, got ({lo}, {hi})")
    if timing.min_gap_min < 0:
        errors.append(f"min_gap_min: must be >= 0, got {timing.min_gap_min}")
    if timing.daily_cap < 1:
        errors.append(f"daily_cap: must be >= 1, got {timing.daily_cap}")
    if timing.max_gap_h <= timing.min_gap_min / 60:
        errors.append(
            f"max_gap_h: must be > min_gap_min/60 "
            f"({timing.max_gap_h} <= {timing.min_gap_min / 60})"
        )

    return errors
