"""Experimental valence, energy and circadian envelope.

Energy is separate from mood. Its level and daily amplitude depend on phase:
ovulatory is high and relatively stable; menstrual is low and shows the widest
circadian arc. ``c(h)`` stays outside the valence path.
"""
from __future__ import annotations

import math

from engine.types import ENERGY_BASE, ENERGY_PHASE_OFFSETS, ENVELOPE_RAMP_H, TimingParams


ENERGY_PHASE_AMPLITUDE_MULTIPLIERS: dict[str, float] = {
    "menstrual": 1.0,
    "follicular": 0.45,
    "ovulatory": 0.50,
    "luteal_early": 0.55,
    "luteal_late": 0.88,
}


def c(h: float, params: TimingParams) -> float:
    """Experimental circadian valence offset in logit units."""
    h_norm = h % 24.0
    angle = 2.0 * math.pi * (h_norm - params.peak_hour) / 24.0
    return params.diurnal_amp * math.cos(angle)


def energy(h: float, phase_label: str, params: TimingParams) -> float:
    """Energy in ``[0, 1]`` with phase-dependent level and amplitude."""
    h_norm = h % 24.0
    angle = 2.0 * math.pi * (h_norm - params.peak_hour) / 24.0
    amplitude_multiplier = ENERGY_PHASE_AMPLITUDE_MULTIPLIERS.get(phase_label, 1.0)
    diurnal = amplitude_multiplier * params.diurnal_amp * math.cos(angle)
    phase_offset = ENERGY_PHASE_OFFSETS.get(phase_label, 0.0)
    raw_energy = ENERGY_BASE + diurnal + phase_offset
    return max(0.0, min(1.0, raw_energy))


def envelope(h: float, params: TimingParams) -> float:
    """Diurnal envelope in ``[0, 1]``, exactly zero during quiet hours."""
    h_norm = h % 24.0
    quiet_ini, quiet_fin = params.quiet_hours
    ramp_h = ENVELOPE_RAMP_H

    if quiet_ini > quiet_fin:
        in_quiet = (h_norm >= quiet_ini) or (h_norm < quiet_fin)
    else:
        in_quiet = quiet_ini <= h_norm < quiet_fin

    if in_quiet:
        return 0.0
    if quiet_fin <= h_norm <= quiet_fin + ramp_h:
        alpha = (h_norm - quiet_fin) / ramp_h
        return 0.5 * (1.0 - math.cos(math.pi * alpha))
    if quiet_ini - ramp_h <= h_norm < quiet_ini:
        alpha = (h_norm - (quiet_ini - ramp_h)) / ramp_h
        return 0.5 * (1.0 + math.cos(math.pi * alpha))
    return 1.0

