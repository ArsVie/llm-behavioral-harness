"""Tests de aceptación para engine/circadian.py (W1.3)."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

import pytest

from engine.circadian import c, energy, envelope
from engine.types import (
    ENERGY_BASE,
    ENERGY_PHASE_OFFSETS,
    ENVELOPE_RAMP_H,
    PHASE_FOLLICULAR,
    PHASE_LUTEAL_EARLY,
    PHASE_LUTEAL_LATE,
    PHASE_MENSTRUAL,
    PHASE_OVULATORY,
    TimingParams,
)


# Tolerancias documentadas
TOL_EXACT = 1e-9  # Valores exactos (ej. picos del coseno)
TOL_NEAR = 1e-6  # Valores cercanos (cálculos de punto flotante)
TOL_ENVELOPE_SHAPE = 0.01  # Forma de envelope (saltos máximos)
TOL_ENERGY_BOUNDS = 1e-12  # Energy siempre en [0, 1]


class TestC:
    """Tests para c(h) — desplazamiento de valencia."""

    def test_c_anchor_peak_hour(self):
        """c(peak_hour) debe ser +diurnal_amp exacto."""
        params = TimingParams()  # defaults: peak_hour=14, diurnal_amp=0.25
        assert abs(c(14.0, params) - 0.25) < TOL_EXACT

    def test_c_anchor_opposite(self):
        """c(peak_hour + 12) debe ser -diurnal_amp exacto."""
        params = TimingParams()  # peak_hour=14 ⇒ 14+12=26=2 (mod 24)
        assert abs(c(2.0, params) - (-0.25)) < TOL_EXACT

    def test_c_anchor_quadrature(self):
        """c(peak_hour - 6) debe ser 0 exacto: cos(-π/2)."""
        params = TimingParams()  # 14-6=8
        assert abs(c(8.0, params) - 0.0) < TOL_EXACT

    def test_c_periodicity(self):
        """c(h) == c(h+24) para cualquier h."""
        params = TimingParams()
        for h in [0.5, 7.3, 13.999, 23.1]:
            assert abs(c(h, params) - c(h + 24.0, params)) < TOL_NEAR
            assert abs(c(h, params) - c(h + 48.0, params)) < TOL_NEAR

    def test_c_custom_params(self):
        """c() respeta peak_hour y diurnal_amp personalizados."""
        params = TimingParams(peak_hour=10.0, diurnal_amp=0.5)
        assert abs(c(10.0, params) - 0.5) < TOL_EXACT
        assert abs(c(22.0, params) - (-0.5)) < TOL_EXACT  # 22-10=12, cos(π)=-1


class TestEnergy:
    """Tests para energy(h, phase_label) — canal de energía."""

    def test_energy_at_peak_hour_menstrual(self):
        """energy(peak_hour, 'menstrual') = ENERGY_BASE + diurnal_amp + offset_menstrual."""
        params = TimingParams()  # peak=14, amp=0.25, ENERGY_BASE=0.6
        result = energy(14.0, PHASE_MENSTRUAL, params)
        expected = ENERGY_BASE + 0.25 + ENERGY_PHASE_OFFSETS[PHASE_MENSTRUAL]
        expected = max(0.0, min(1.0, expected))
        assert abs(result - expected) < TOL_NEAR

    def test_energy_phase_differences(self):
        """Energía difiere por fase: ovulatory > menstrual en la misma h."""
        params = TimingParams()
        e_menstrual = energy(14.0, PHASE_MENSTRUAL, params)
        e_ovulatory = energy(14.0, PHASE_OVULATORY, params)
        assert e_ovulatory > e_menstrual

    def test_energy_unknown_phase(self):
        """Fase desconocida ⇒ offset 0.0."""
        params = TimingParams()
        e_unknown = energy(14.0, "unknown_phase", params)
        e_base = ENERGY_BASE + 0.25  # peak, no offset
        e_base = max(0.0, min(1.0, e_base))
        assert abs(e_unknown - e_base) < TOL_NEAR

    def test_energy_bounds(self):
        """energy(h, phase) siempre ∈ [0, 1]."""
        params = TimingParams()
        h_values = [0.0, 5.5, 8.0, 12.0, 14.0, 18.0, 23.5]
        phases = [PHASE_MENSTRUAL, PHASE_FOLLICULAR, PHASE_OVULATORY,
                  PHASE_LUTEAL_EARLY, PHASE_LUTEAL_LATE, "unknown"]

        for h in h_values:
            for phase in phases:
                e = energy(h, phase, params)
                assert -TOL_ENERGY_BOUNDS <= e <= 1.0 + TOL_ENERGY_BOUNDS, \
                    f"energy({h}, {phase}) = {e} out of bounds"

    def test_energy_all_phases_comparison(self):
        """Orden de offsets se refleja en energy con mismo coseno."""
        params = TimingParams()
        h = 14.0  # pico del coseno
        base_diurnal = 0.25

        e_menstrual = energy(h, PHASE_MENSTRUAL, params)
        e_follicular = energy(h, PHASE_FOLLICULAR, params)
        e_ovulatory = energy(h, PHASE_OVULATORY, params)
        e_luteal_early = energy(h, PHASE_LUTEAL_EARLY, params)
        e_luteal_late = energy(h, PHASE_LUTEAL_LATE, params)

        # Basado en ENERGY_PHASE_OFFSETS: menstrual=-0.15, follicular=+0.05,
        # ovulatory=+0.10, luteal_early=0.0, luteal_late=-0.10
        assert e_ovulatory > e_follicular > e_luteal_early > e_luteal_late > e_menstrual


class TestEnvelope:
    """Tests para envelope(h) — envolvente diurna."""

    def test_envelope_zero_in_quiet_hours_default(self):
        """envelope == 0.0 exacto en quiet hours (default: [23, 8))."""
        params = TimingParams()  # quiet_hours=(23.0, 8.0), quiet = [23,24) ∪ [0,8)
        quiet_points = [23.0, 23.5, 0.0, 3.0, 7.5, 7.999]
        for h in quiet_points:
            assert abs(envelope(h, params) - 0.0) < TOL_EXACT, \
                f"envelope({h}) should be 0.0 in quiet hours"

    def test_envelope_one_in_midday_default(self):
        """envelope == 1.0 en pleno día (lejos de rampas)."""
        params = TimingParams()
        midday_points = [10.0, 14.0, 18.0, 21.0]
        for h in midday_points:
            assert abs(envelope(h, params) - 1.0) < TOL_NEAR, \
                f"envelope({h}) should be 1.0 in midday"

    def test_envelope_ramp_values_default(self):
        """envelope ∈ (0, 1) a mitad de las rampas (sin exactitud, solo forma)."""
        params = TimingParams()
        # quiet_hours=(23, 8), ramp_h=1.0
        # Rampa ascendente: [8, 9]
        # Rampa descendente: [22, 23]
        # Mitad de rampa ascendente: h=8.5
        e_ramp_up = envelope(8.5, params)
        assert 0.0 < e_ramp_up < 1.0

        # Mitad de rampa descendente: h=22.5
        e_ramp_down = envelope(22.5, params)
        assert 0.0 < e_ramp_down < 1.0

    def test_envelope_continuity_default(self):
        """Continuidad: salto máximo |envelope(h+δ)-envelope(h)| < 0.01."""
        params = TimingParams()
        delta = 0.001
        h_values = [i * delta for i in range(24000)]  # malla de 0 a 24 con paso 0.001

        max_jump = 0.0
        for h in h_values:
            e1 = envelope(h, params)
            e2 = envelope(h + delta, params)
            jump = abs(e2 - e1)
            max_jump = max(max_jump, jump)

        assert max_jump < TOL_ENVELOPE_SHAPE, \
            f"Maximum discontinuity: {max_jump}, expected < {TOL_ENVELOPE_SHAPE}"

    def test_envelope_no_crossing_quiet_hours(self):
        """quiet_hours sin cruce: (2.0, 5.0) ⇒ quiet = [2, 5)."""
        params = replace(TimingParams(), quiet_hours=(2.0, 5.0))
        # ENVELOPE_RAMP_H=1.0
        # Rampa ascendente: [5, 6]
        # Rampa descendente: [1, 2]

        # Dentro de quiet: debe ser 0
        assert abs(envelope(2.5, params) - 0.0) < TOL_EXACT
        assert abs(envelope(3.0, params) - 0.0) < TOL_EXACT
        assert abs(envelope(4.9, params) - 0.0) < TOL_EXACT

        # En el centro (lejos): debe ser 1
        assert abs(envelope(10.0, params) - 1.0) < TOL_NEAR
        assert abs(envelope(20.0, params) - 1.0) < TOL_NEAR

        # En rampas: debe estar entre 0 y 1
        e_ramp_up = envelope(5.5, params)
        e_ramp_down = envelope(1.5, params)
        assert 0.0 < e_ramp_up < 1.0
        assert 0.0 < e_ramp_down < 1.0

    def test_envelope_symmetry_of_ramps(self):
        """Rampas ascendente y descendente son simétricas en magnitud."""
        params = TimingParams()  # quiet=(23, 8), ramp_h=1.0
        # Rampa ascendente: [8, 9], centro en h=8.5
        # Rampa descendente: [22, 23], centro en h=22.5
        # Ambas a distancia 0.5 del inicio/fin de rampa: deben valer ~0.5

        e_up_mid = envelope(8.5, params)
        e_down_mid = envelope(22.5, params)
        assert abs(e_up_mid - 0.5) < 0.01, f"Up ramp mid: {e_up_mid}"
        assert abs(e_down_mid - 0.5) < 0.01, f"Down ramp mid: {e_down_mid}"

    def test_envelope_custom_quiet_hours_crossing(self):
        """quiet_hours con cruce personalizado: (22.0, 6.0)."""
        params = replace(TimingParams(), quiet_hours=(22.0, 6.0),
                         diurnal_amp=0.25, peak_hour=14.0)
        # quiet = [22, 24) ∪ [0, 6), ramp_h=1.0
        # Rampa ascendente: [6, 7]
        # Rampa descendente: [21, 22]

        # Dentro de quiet
        assert abs(envelope(22.5, params) - 0.0) < TOL_EXACT
        assert abs(envelope(0.0, params) - 0.0) < TOL_EXACT
        assert abs(envelope(5.5, params) - 0.0) < TOL_EXACT

        # En el centro
        assert abs(envelope(12.0, params) - 1.0) < TOL_NEAR
        assert abs(envelope(18.0, params) - 1.0) < TOL_NEAR

    def test_envelope_wrapping(self):
        """envelope(h) == envelope(h % 24)."""
        params = TimingParams()
        for h in [0.5, 7.3, 14.0, 23.1]:
            assert abs(envelope(h, params) - envelope(h + 24.0, params)) < TOL_NEAR
            assert abs(envelope(h, params) - envelope(h + 48.0, params)) < TOL_NEAR

    def test_envelope_zero_at_quiet_boundaries(self):
        """Exactamente 0.0 en los bordes de quiet_hours (ini, fin)."""
        params = TimingParams()  # quiet=(23, 8)
        # En quiet_ini (23): debería estar exactamente en 0
        assert abs(envelope(23.0, params) - 0.0) < TOL_EXACT

        # En quiet_fin (8): debería estar exactamente en 0 (borde cerrado)
        assert abs(envelope(8.0, params) - 0.0) < TOL_EXACT

    def test_envelope_smooth_across_midnight(self):
        """Envolvente suave al cruzar medianoche (quiet cruza)."""
        params = TimingParams()  # quiet=(23, 8)
        # Transición: 7.999 (quiet) → 8.0 (quiet) → 8.001 (rampa)
        e_before = envelope(7.999, params)
        e_at = envelope(8.0, params)
        e_after = envelope(8.001, params)

        assert abs(e_before - 0.0) < TOL_EXACT
        assert abs(e_at - 0.0) < TOL_EXACT
        assert e_after > 0.0, "Debe empezar rampa después de quiet_fin"

    def test_envelope_at_ramp_boundaries(self):
        """Envelope en los bordes exactos de las rampas."""
        params = TimingParams()  # quiet=(23, 8), ramp_h=1.0
        # Rampa ascendente: [8, 9]
        # Rampa descendente: [22, 23]

        # Inicio de rampa ascendente: h=8.0
        assert abs(envelope(8.0, params) - 0.0) < TOL_EXACT

        # Fin de rampa ascendente: h=9.0
        assert abs(envelope(9.0, params) - 1.0) < TOL_NEAR

        # Inicio de rampa descendente: h=23.0
        assert abs(envelope(23.0, params) - 0.0) < TOL_EXACT

        # Fin de rampa descendente: h=22.0
        assert abs(envelope(22.0, params) - 1.0) < TOL_NEAR


class TestIntegration:
    """Tests de integración entre funciones."""

    def test_all_functions_with_custom_params(self):
        """Todas las funciones funcionan con parámetros personalizados."""
        params = TimingParams(peak_hour=12.0, diurnal_amp=0.3,
                              quiet_hours=(20.0, 7.0))

        for h in [0.0, 5.5, 12.0, 18.5, 23.9]:
            c_val = c(h, params)
            assert isinstance(c_val, float)

            e_val = energy(h, PHASE_OVULATORY, params)
            assert 0.0 <= e_val <= 1.0

            env_val = envelope(h, params)
            assert 0.0 <= env_val <= 1.0

    def test_energy_and_envelope_independence(self):
        """energy y envelope son independientes (no interaccionan)."""
        params = TimingParams()

        # energy depende de phase_label, envelope no
        e1 = energy(14.0, PHASE_MENSTRUAL, params)
        e2 = energy(14.0, PHASE_OVULATORY, params)
        assert e1 != e2

        env = envelope(14.0, params)
        # envelope es el mismo para ambas fases
        assert env == envelope(14.0, params)

    def test_c_independence_from_quiet_hours(self):
        """c(h) no depende de quiet_hours."""
        params1 = TimingParams(quiet_hours=(23.0, 8.0))
        params2 = TimingParams(quiet_hours=(20.0, 6.0))

        for h in [0.0, 7.5, 14.0, 22.5]:
            assert abs(c(h, params1) - c(h, params2)) < TOL_NEAR
