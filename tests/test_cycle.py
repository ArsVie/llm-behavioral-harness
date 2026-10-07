"""Acceptance tests for engine.cycle (W1.2)."""
from __future__ import annotations

from dataclasses import replace

import numpy as np

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
    """Test 1: init_state draws L_0 ~ Normal(L_mean, L_sd) truncated >= 1."""

    def test_init_state_creates_valid_state(self, persona: PersonaParams):
        """init_state returns CycleState with cycle_day = phi % L_0."""
        rng = np.random.default_rng(42)
        state = init_state(persona, rng)

        assert isinstance(state, CycleState)
        assert state.L_current >= 1.0
        assert 0 <= state.cycle_day < state.L_current
        # With phi=0, cycle_day is near 0.
        assert abs(state.cycle_day) < 1e-10

    def test_init_state_respects_phi(self, persona: PersonaParams):
        """init_state with phi != 0: cycle_day = phi % L_0."""
        rng = np.random.default_rng(42)
        params = replace(persona, phi=10.0)
        state = init_state(params, rng)

        # L_0 is drawn from the RNG, so cycle_day = 10.0 % L_0.
        assert 0 <= state.cycle_day < state.L_current
        expected = 10.0 % state.L_current
        assert abs(state.cycle_day - expected) < 1e-10


class TestPhaseOf:
    """Test 2: phase_of maps cycle_day to a phase label with exact boundaries."""

    def test_phase_boundaries_L28(self):
        """Exact boundaries with L=28: menstrual [0,5), follicular [5,12), etc."""
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
        """Phases scale with L_current (PHASE_FRACTIONS fractions)."""
        # With L=14, menstrual spans [0, 2.5).
        L = 14.0
        assert phase_of(0.0, L) == PHASE_MENSTRUAL
        assert phase_of(2.4, L) == PHASE_MENSTRUAL
        # 5/28 * 14 = 2.5
        assert phase_of(2.5, L) == PHASE_FOLLICULAR


class TestStepNoMutation:
    """Test 3: step does not mutate the input state."""

    def test_step_does_not_mutate(self, persona: PersonaParams):
        """Successive calls to step do not alter the previous state."""
        rng = np.random.default_rng(42)
        state1 = init_state(persona, rng)

        # Save a snapshot.
        original_cycle_day = state1.cycle_day
        original_L = state1.L_current

        # Call step (consumes RNG).
        m, g, phase, state2 = step(state1, persona, rng)

        # state1 is unchanged.
        assert state1.cycle_day == original_cycle_day
        assert state1.L_current == original_L
        # state2 differs.
        assert not (state2.cycle_day == state1.cycle_day and state2.L_current == state1.L_current)


class TestStepDeterminism:
    """Test 4: determinism with the same seed."""

    def test_step_deterministic(self, persona: PersonaParams):
        """Two sequences with the same seed yield the same (m, g, phase, next_state)."""
        # First run.
        rng1 = np.random.default_rng(999)
        state1 = init_state(persona, rng1)
        m1, g1, phase1, next1 = step(state1, persona, rng1)

        # Second run.
        rng2 = np.random.default_rng(999)
        state2 = init_state(persona, rng2)
        m2, g2, phase2, next2 = step(state2, persona, rng2)

        assert m1 == m2
        assert g1 == g2
        assert phase1 == phase2
        assert next1.cycle_day == next2.cycle_day
        assert next1.L_current == next2.L_current


class TestMeanAndAmplitude:
    """Test 5: m has mean ~0 and amplitude ~B; g has mean ~1 and amplitude ~A."""

    def test_m_mean_and_amplitude_fixed_L(self, persona: PersonaParams):
        """m(d) = B·sin(2π·d/L) over a full cycle with fixed L has mean ~0, amplitude ~B."""
        # Isolate g's noise with sigma_eps=0.
        params = replace(persona, sigma_eps=0.0)

        rng = np.random.default_rng(7777)
        state = init_state(params, rng)
        L = state.L_current

        # Simulate one full cycle (L steps).
        m_values = []
        d = state.cycle_day
        for _ in range(int(np.ceil(L))):
            state_temp = CycleState(cycle_day=d, L_current=L)
            m, g, phase, next_state = step(state_temp, params, rng)
            m_values.append(m)
            d = next_state.cycle_day
            if d < 0.1:  # Wraparound: cycle_day returns near 0.
                break

        m_values = np.array(m_values)
        mean_m = np.mean(m_values)
        max_m = np.max(m_values)
        min_m = np.min(m_values)
        amplitude_m = (max_m - min_m) / 2

        # Mean is near 0.
        assert abs(mean_m) < 0.05, f"m mean {mean_m} outside [-0.05, 0.05]"
        # Amplitude is near B.
        assert abs(amplitude_m - params.B) < 0.05, f"m amplitude {amplitude_m} != B={params.B}"

    def test_g_mean_and_amplitude_fixed_L(self, persona: PersonaParams):
        """g(d) = 1 + A·sin(2π·d/L) + ε over a cycle with epsilon ~ N(0, σ_ε)."""
        # With small sigma_eps, mean is near 1 and amplitude near A.
        params = replace(persona, sigma_eps=0.0)

        rng = np.random.default_rng(8888)
        state = init_state(params, rng)
        L = state.L_current

        # Simulate one cycle.
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

        # Mean is near 1.
        assert abs(mean_g - 1.0) < 0.05, f"g mean {mean_g} is not close to 1.0"
        # Amplitude is near A.
        assert abs(amplitude_g - params.A) < 0.05, f"g amplitude {amplitude_g} != A={params.A}"


class TestLRedraw:
    """Test 6: redraw of L ~ Normal(L_mean, L_sd) truncated >= 1 over ≥200 cycles."""

    def test_L_redraw_statistics(self, persona: PersonaParams):
        """Runs ≥200 full cycles; mean of L ≈ L_mean, sd ≈ L_sd (±10%)."""
        rng = np.random.default_rng(6666)
        state = init_state(persona, rng)

        L_values = []
        cycle_count = 0

        # Simulate up to 200 full cycles.
        d = state.cycle_day
        L = state.L_current
        steps = 0
        max_steps = 30000  # Guard against infinite iteration.

        while cycle_count < 200 and steps < max_steps:
            state_temp = CycleState(cycle_day=d, L_current=L)
            m, g, phase, next_state = step(state_temp, persona, rng)

            d = next_state.cycle_day
            L_new = next_state.L_current

            # A cycle completes when L changes.
            if L_new != L:
                L_values.append(L)
                cycle_count += 1
                L = L_new

            steps += 1

        assert len(L_values) >= 200, f"Only {len(L_values)} full cycles"

        mean_L = np.mean(L_values)
        sd_L = np.std(L_values, ddof=1)

        # Mean is within 10% of L_mean.
        lower_mean = persona.L_mean * 0.9
        upper_mean = persona.L_mean * 1.1
        assert lower_mean <= mean_L <= upper_mean, (
            f"Mean L = {mean_L}, expected ~{persona.L_mean} "
            f"(range [{lower_mean}, {upper_mean}])"
        )

        # sd is within 10% of L_sd.
        lower_sd = persona.L_sd * 0.9
        upper_sd = persona.L_sd * 1.1
        assert lower_sd <= sd_L <= upper_sd, (
            f"SD L = {sd_L}, expected ~{persona.L_sd} "
            f"(range [{lower_sd}, {upper_sd}])"
        )


class TestPeriodicityAutocorrelation:
    """Test 7: m(t) series over ~10 cycles has high autocorrelation (>0.8) at lag ≈ L_mean."""

    def test_m_autocorrelation(self, persona: PersonaParams):
        """m(t) is periodic: autocorrelation at lag ≈ L_mean is > 0.8."""
        # Isolate m with sigma_eps=0.
        params = replace(persona, sigma_eps=0.0)

        rng = np.random.default_rng(5555)
        state = init_state(params, rng)

        m_series = []
        d = state.cycle_day
        L = state.L_current
        cycle_count = 0

        # Simulate about 10 cycles (~280 days).
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

        # Autocorrelation at lag ≈ L_mean via scipy.signal.correlate, normalized.
        lag = int(np.round(params.L_mean))
        acf = np.correlate(m_series - np.mean(m_series), m_series - np.mean(m_series), mode='full')
        acf_normalized = acf / acf[len(acf) // 2]  # Normalized by the lag-0 autocovariance.
        acf_at_lag = acf_normalized[len(acf) // 2 + lag]

        # With sigma_eps=0, lag-L_mean autocorrelation is high (>0.8).
        assert acf_at_lag > 0.8, (
            f"Autocorrelation at lag {lag} is {acf_at_lag}, "
            f"expected > 0.8 (with sigma_eps=0)"
        )
