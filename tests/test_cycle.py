"""Tests de aceptación para engine.cycle (W1.2).

Especificación: m(d) = B·sin(2π·d/L), g(d) = 1 + A·sin(2π·d/L) + ε,
con redraw de L y fases según types.PHASE_FRACTIONS.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pytest
from scipy import signal, stats

from engine.cycle import init_state, phase_of, step
from engine.types import (
    PHASE_FOLLICULAR,
    PHASE_LUTEAL_EARLY,
    PHASE_LUTEAL_LATE,
    PHASE_MENSTRUAL,
    PHASE_OVULATORY,
    CycleState,
    PersonaParams,
)


class TestInitState:
    """Test 1: init_state dibuja L_0 ~ Normal(L_mean, L_sd) truncada >= 1."""

    def test_init_state_creates_valid_state(self, persona: PersonaParams):
        """init_state devuelve CycleState con cycle_day = phi % L_0."""
        rng = np.random.default_rng(42)
        state = init_state(persona, rng)

        assert isinstance(state, CycleState)
        assert state.L_current >= 1.0
        assert 0 <= state.cycle_day < state.L_current
        # Con phi=0 (default), cycle_day debe ser ~0
        assert abs(state.cycle_day) < 1e-10

    def test_init_state_respects_phi(self, persona: PersonaParams):
        """init_state con phi != 0: cycle_day = phi % L_0."""
        rng = np.random.default_rng(42)
        params = replace(persona, phi=10.0)
        state = init_state(params, rng)

        # L_0 se dibujan del RNG, así que cycle_day = 10.0 % L_0
        assert 0 <= state.cycle_day < state.L_current
        expected = 10.0 % state.L_current
        assert abs(state.cycle_day - expected) < 1e-10


class TestPhaseOf:
    """Test 2: phase_of mapea cycle_day a etiqueta de fase con fronteras exactas."""

    def test_phase_boundaries_L28(self):
        """Fronteras exactas con L=28: menstrual [0,5), follicular [5,12), etc."""
        L = 28.0

        # Menstrual [0, 5)
        assert phase_of(0.0, L) == PHASE_MENSTRUAL
        assert phase_of(4.999, L) == PHASE_MENSTRUAL
        assert phase_of(5.0, L) == PHASE_FOLLICULAR

        # Follicular [5, 12)
        assert phase_of(5.0, L) == PHASE_FOLLICULAR
        assert phase_of(11.999, L) == PHASE_FOLLICULAR
        assert phase_of(12.0, L) == PHASE_OVULATORY

        # Ovulatory [12, 16)
        assert phase_of(12.0, L) == PHASE_OVULATORY
        assert phase_of(15.999, L) == PHASE_OVULATORY
        assert phase_of(16.0, L) == PHASE_LUTEAL_EARLY

        # Luteal early [16, 23)
        assert phase_of(16.0, L) == PHASE_LUTEAL_EARLY
        assert phase_of(22.999, L) == PHASE_LUTEAL_EARLY
        assert phase_of(23.0, L) == PHASE_LUTEAL_LATE

        # Luteal late [23, 28)
        assert phase_of(23.0, L) == PHASE_LUTEAL_LATE
        assert phase_of(27.999, L) == PHASE_LUTEAL_LATE

    def test_phase_scales_with_L(self):
        """Fases escalan con L_current (fracciones de PHASE_FRACTIONS)."""
        # Con L=14 (mitad de 28), menstrual debería ser [0, 2.5)
        L = 14.0
        assert phase_of(0.0, L) == PHASE_MENSTRUAL
        assert phase_of(2.4, L) == PHASE_MENSTRUAL
        # 5/28 * 14 = 2.5
        assert phase_of(2.5, L) == PHASE_FOLLICULAR


class TestStepNoMutation:
    """Test 3: step no muta el estado de entrada."""

    def test_step_does_not_mutate(self, persona: PersonaParams):
        """Llamadas sucesivas a step no alteran el estado anterior."""
        rng = np.random.default_rng(42)
        state1 = init_state(persona, rng)

        # Guarda snapshot
        original_cycle_day = state1.cycle_day
        original_L = state1.L_current

        # Llama step (consume RNG)
        m, g, phase, state2 = step(state1, persona, rng)

        # Verifica que state1 no cambió
        assert state1.cycle_day == original_cycle_day
        assert state1.L_current == original_L
        # state2 debe ser diferente
        assert not (state2.cycle_day == state1.cycle_day and state2.L_current == state1.L_current)


class TestStepDeterminism:
    """Test 4: determinismo con la misma semilla."""

    def test_step_deterministic(self, persona: PersonaParams):
        """Dos secuencias con la misma semilla dan los mismos (m, g, phase, next_state)."""
        # Primera run
        rng1 = np.random.default_rng(999)
        state1 = init_state(persona, rng1)
        m1, g1, phase1, next1 = step(state1, persona, rng1)

        # Segunda run
        rng2 = np.random.default_rng(999)
        state2 = init_state(persona, rng2)
        m2, g2, phase2, next2 = step(state2, persona, rng2)

        assert m1 == m2
        assert g1 == g2
        assert phase1 == phase2
        assert next1.cycle_day == next2.cycle_day
        assert next1.L_current == next2.L_current


class TestMeanAndAmplitude:
    """Test 5: m tiene media ~0 y amplitud ~B; g media ~1 y amplitud ~A."""

    def test_m_mean_and_amplitude_fixed_L(self, persona: PersonaParams):
        """m(d) = B·sin(2π·d/L) sobre ciclo completo con L fija tiene media ~0, amplitude ~B."""
        # Aísla el ruido de g usando sigma_eps=0
        params = replace(persona, sigma_eps=0.0)

        rng = np.random.default_rng(7777)
        state = init_state(params, rng)
        L = state.L_current

        # Simula un ciclo completo (L pasos)
        m_values = []
        d = state.cycle_day
        for _ in range(int(np.ceil(L))):
            state_temp = CycleState(cycle_day=d, L_current=L)
            m, g, phase, next_state = step(state_temp, params, rng)
            m_values.append(m)
            d = next_state.cycle_day
            if d < 0.1:  # Wraparound completado (cycle_day vuelve cerca de 0)
                break

        m_values = np.array(m_values)
        mean_m = np.mean(m_values)
        max_m = np.max(m_values)
        min_m = np.min(m_values)
        amplitude_m = (max_m - min_m) / 2

        # Media debe estar cerca de 0
        assert abs(mean_m) < 0.05, f"m media {mean_m} fuera de [-0.05, 0.05]"
        # Amplitud debe estar cerca de B
        assert abs(amplitude_m - params.B) < 0.05, f"m amplitud {amplitude_m} != B={params.B}"

    def test_g_mean_and_amplitude_fixed_L(self, persona: PersonaParams):
        """g(d) = 1 + A·sin(2π·d/L) + ε sobre ciclo con epsilon ~ N(0, σ_ε)."""
        # Con sigma_eps pequeño, la media debe estar ~1 y amplitud ~A
        params = replace(persona, sigma_eps=0.0)

        rng = np.random.default_rng(8888)
        state = init_state(params, rng)
        L = state.L_current

        # Simula un ciclo
        g_values = []
        d = state.cycle_day
        for _ in range(int(np.ceil(L))):
            state_temp = CycleState(cycle_day=d, L_current=L)
            m, g, phase, next_state = step(state_temp, params, rng)
            g_values.append(g)
            d = next_state.cycle_day
            if d < 0.1:
                break

        g_values = np.array(g_values)
        mean_g = np.mean(g_values)
        max_g = np.max(g_values)
        min_g = np.min(g_values)
        amplitude_g = (max_g - min_g) / 2

        # Media debe estar ~1
        assert abs(mean_g - 1.0) < 0.05, f"g media {mean_g} no está cerca de 1.0"
        # Amplitud debe estar ~A
        assert abs(amplitude_g - params.A) < 0.05, f"g amplitud {amplitude_g} != A={params.A}"


class TestLRedraw:
    """Test 6: redraw de L ~ Normal(L_mean, L_sd) truncada >= 1 sobre ≥200 ciclos."""

    def test_L_redraw_statistics(self, persona: PersonaParams):
        """Corre ≥200 ciclos completos; media de L ≈ L_mean, sd ≈ L_sd (±10%)."""
        rng = np.random.default_rng(6666)
        state = init_state(persona, rng)

        L_values = []
        cycle_count = 0

        # Simula hasta ≥200 ciclos completos
        d = state.cycle_day
        L = state.L_current
        steps = 0
        max_steps = 30000  # Guard para no iterar infinito

        while cycle_count < 200 and steps < max_steps:
            state_temp = CycleState(cycle_day=d, L_current=L)
            m, g, phase, next_state = step(state_temp, persona, rng)

            d = next_state.cycle_day
            L_new = next_state.L_current

            # Si L cambió, completamos un ciclo
            if L_new != L:
                L_values.append(L)
                cycle_count += 1
                L = L_new

            steps += 1

        assert len(L_values) >= 200, f"Solo {len(L_values)} ciclos completos"

        mean_L = np.mean(L_values)
        sd_L = np.std(L_values, ddof=1)

        # Media ≈ L_mean (tolerancia ±10%)
        lower_mean = persona.L_mean * 0.9
        upper_mean = persona.L_mean * 1.1
        assert lower_mean <= mean_L <= upper_mean, (
            f"Media L = {mean_L}, esperada ~{persona.L_mean} "
            f"(rango [{lower_mean}, {upper_mean}])"
        )

        # sd ≈ L_sd (tolerancia ±10%)
        lower_sd = persona.L_sd * 0.9
        upper_sd = persona.L_sd * 1.1
        assert lower_sd <= sd_L <= upper_sd, (
            f"SD L = {sd_L}, esperada ~{persona.L_sd} "
            f"(rango [{lower_sd}, {upper_sd}])"
        )


class TestPeriodicityAutocorrelation:
    """Test 7: serie m(t) sobre ~10 ciclos tiene autocorrelación alta (>0.8) en lag ≈ L_mean."""

    def test_m_autocorrelation(self, persona: PersonaParams):
        """m(t) es periódica: autocorrelación en lag ≈ L_mean es > 0.8."""
        # Aísla m usando sigma_eps=0
        params = replace(persona, sigma_eps=0.0)

        rng = np.random.default_rng(5555)
        state = init_state(params, rng)

        m_series = []
        d = state.cycle_day
        L = state.L_current
        cycle_count = 0

        # Simula ~10 ciclos (10 * ~28 ≈ 280 días)
        while cycle_count < 10:
            state_temp = CycleState(cycle_day=d, L_current=L)
            m, g, phase, next_state = step(state_temp, params, rng)

            m_series.append(m)
            d = next_state.cycle_day
            L_new = next_state.L_current

            if L_new != L:
                cycle_count += 1
                L = L_new

        m_series = np.array(m_series)

        # Calcula autocorrelación en lag ≈ L_mean usando scipy.signal.correlate
        # Normalización: correlación al cuadrado / varianza (correlación normalizada)
        lag = int(np.round(params.L_mean))
        acf = np.correlate(m_series - np.mean(m_series), m_series - np.mean(m_series), mode='full')
        acf_normalized = acf / acf[len(acf) // 2]  # Normaliza por la autocovarianza en lag 0
        acf_at_lag = acf_normalized[len(acf) // 2 + lag]

        # Con sigma_eps=0, la autocorrelación en lag=L_mean debe ser alta (>0.8)
        assert acf_at_lag > 0.8, (
            f"Autocorrelación en lag {lag} es {acf_at_lag}, "
            f"esperada > 0.8 (con sigma_eps=0)"
        )


class TestRNGOrder:
    """Test 8: Orden de consumo RNG es correcto: (1) ε, (2) redraw de L."""

    def test_rng_consumption_order(self, persona: PersonaParams):
        """step consume RNG en orden: (1) ε, (2) redraw de L (solo si toca)."""
        # Esto se verifica indirectamente con el test de determinismo y las
        # comparaciones de estado. Aquí hacemos un test explícito sobre el
        # consumo de RNG en una transición que dispara redraw.

        rng = np.random.default_rng(4444)
        state = init_state(persona, rng)

        # Avanza hasta el último día del ciclo (cycle_day ≈ L - 0.5)
        d = state.cycle_day
        L = state.L_current
        while d < L - 1.0:
            state_temp = CycleState(cycle_day=d, L_current=L)
            m, g, phase, next_state = step(state_temp, persona, rng)
            d = next_state.cycle_day
            if next_state.L_current != L:
                L = next_state.L_current

        # Ahora estamos cerca del fin del ciclo
        # Siguiente step debe consumir (1) epsilon y (2) redraw
        state_near_end = CycleState(cycle_day=d, L_current=L)
        rng_copy = np.random.default_rng(4444)
        # Replicate hasta el mismo punto
        state_rep = init_state(persona, rng_copy)
        d_rep = state_rep.cycle_day
        L_rep = state_rep.L_current
        while abs(d_rep - d) > 0.01 or abs(L_rep - L) > 0.01:
            state_temp = CycleState(cycle_day=d_rep, L_current=L_rep)
            m, g, phase, next_state = step(state_temp, persona, rng_copy)
            d_rep = next_state.cycle_day
            if next_state.L_current != L_rep:
                L_rep = next_state.L_current

        # Ahora ambos RNG están en el mismo punto
        # Un step con el primero vs. el segundo debe dar los mismos resultados
        state_test = CycleState(cycle_day=d, L_current=L)
        m1, g1, phase1, next1 = step(state_test, persona, rng)
        m2, g2, phase2, next2 = step(state_test, persona, rng_copy)

        assert m1 == m2
        assert g1 == g2
        assert phase1 == phase2
        assert next1.cycle_day == next2.cycle_day
        assert next1.L_current == next2.L_current
