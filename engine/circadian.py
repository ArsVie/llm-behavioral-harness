"""Valencia experimental, energía y envolvente circadiana.

La energía está separada del ánimo. Su nivel y amplitud diaria dependen de fase:
ovulatoria es alta y relativamente estable; menstrual es baja y presenta el
arco circadiano más amplio. ``c(h)`` permanece fuera de la ruta de valencia.
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
    """Desplazamiento circadiano experimental de valencia en unidades logit."""
    h_norm = h % 24.0
    angle = 2.0 * math.pi * (h_norm - params.peak_hour) / 24.0
    return params.diurnal_amp * math.cos(angle)


def energy(h: float, phase_label: str, params: TimingParams) -> float:
    """Energía en ``[0, 1]`` con nivel y amplitud dependientes de fase."""
    h_norm = h % 24.0
    angle = 2.0 * math.pi * (h_norm - params.peak_hour) / 24.0
    amplitude_multiplier = ENERGY_PHASE_AMPLITUDE_MULTIPLIERS.get(phase_label, 1.0)
    diurnal = amplitude_multiplier * params.diurnal_amp * math.cos(angle)
    phase_offset = ENERGY_PHASE_OFFSETS.get(phase_label, 0.0)
    raw_energy = ENERGY_BASE + diurnal + phase_offset
    return max(0.0, min(1.0, raw_energy))


def envelope(h: float, params: TimingParams) -> float:
    """Envolvente diurna en ``[0, 1]``, cero exacto en quiet hours."""
    h_norm = h % 24.0
    quiet_ini, quiet_fin = params.quiet_hours
    ramp_h = ENVELOPE_RAMP_H

    if quiet_ini > quiet_fin:
        in_quiet = (h_norm >= quiet_ini) or (h_norm < quiet_fin)
    else:
        in_quiet = quiet_ini <= h_norm < quiet_fin

    if in_quiet:
        return 0.0

    if quiet_ini > quiet_fin:
        in_ramp_up = quiet_fin <= h_norm <= quiet_fin + ramp_h
        in_ramp_down = quiet_ini - ramp_h <= h_norm < quiet_ini
    else:
        in_ramp_up = quiet_fin <= h_norm <= quiet_fin + ramp_h
        in_ramp_down = quiet_ini - ramp_h <= h_norm < quiet_ini

    if in_ramp_up:
        alpha = (h_norm - quiet_fin) / ramp_h
        return 0.5 * (1.0 - math.cos(math.pi * alpha))
    if in_ramp_down:
        alpha = (h_norm - (quiet_ini - ramp_h)) / ramp_h
        return 0.5 * (1.0 + math.cos(math.pi * alpha))
    return 1.0

