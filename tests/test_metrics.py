"""Acceptance tests for sim.metrics (W1.5).

Each metric is validated against a synthetic series with a KNOWN value,
built by hand or analytically (the implementation's own results are never
compared against themselves).
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from sim.metrics import (
    autocorr_lag1,
    daily_rate,
    envelope_violations,
    gap_stats,
    hourly_histogram,
    mean_sd,
    reversion_days,
    var_ratio_by_gain,
)


class TestMeanSd:
    """Test 1: mean_sd over a short fixed series computed by hand."""

    def test_known_series(self):
        """x = [2,4,4,4,5,5,7,9] -> mean=5.0, sd (ddof=1) = 2.138089935299395.

        Hand calculation: mean = 40/8 = 5. Squared deviations:
        9+1+1+1+0+0+4+16 = 32; sd = sqrt(32/7) = 2.1380899...
        """
        x = np.array([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
        mean, sd = mean_sd(x)
        assert mean == pytest.approx(5.0, abs=1e-12)
        assert sd == pytest.approx(math.sqrt(32.0 / 7.0), abs=1e-12)

    def test_constant_series_zero_sd(self):
        """A constant series has sd = 0."""
        x = np.full(10, 3.5)
        mean, sd = mean_sd(x)
        assert mean == pytest.approx(3.5)
        assert sd == pytest.approx(0.0, abs=1e-12)


class TestAutocorrLag1:
    """Test 2: simulated AR(1), i.i.d., and a deterministic alternating series."""

    def test_ar1_phi_0_6(self):
        """AR(1) x[t] = 0.6*x[t-1] + eps[t], eps ~ N(0,1), n=20000, fixed seed.

        The theoretical lag-1 autocorrelation of an AR(1) is phi=0.6; with
        large n the Pearson estimator converges to that value.
        """
        rng = np.random.default_rng(42)
        n = 20000
        phi = 0.6
        eps = rng.normal(0.0, 1.0, n)
        x = np.zeros(n)
        for t in range(1, n):
            x[t] = phi * x[t - 1] + eps[t]

        result = autocorr_lag1(x)
        assert result == pytest.approx(0.6, abs=0.03)

    def test_iid_series_near_zero(self):
        """An i.i.d. series (no temporal structure) has autocorrelation ~0."""
        rng = np.random.default_rng(123)
        x = rng.normal(0.0, 1.0, 20000)
        result = autocorr_lag1(x)
        assert result == pytest.approx(0.0, abs=0.03)

    def test_alternating_series_minus_one(self):
        """[1,-1,1,-1,...] is perfectly anticorrelated at lag-1: exactly -1."""
        x = np.array([1.0, -1.0] * 10)
        result = autocorr_lag1(x)
        assert result == pytest.approx(-1.0, abs=1e-12)


class TestVarRatioByGain:
    """Test 3: M with var 4 where g=2 and var 1 where g=1 (two blocks)."""

    def test_two_block_variance_ratio(self):
        """Two blocks with var(M)=1 and var(M)=4 -> ratio ~4 (25% tolerance)."""
        rng = np.random.default_rng(123)
        n_block = 5000
        g = np.concatenate([np.full(n_block, 1.0), np.full(n_block, 2.0)])
        M = np.concatenate(
            [
                rng.normal(0.0, 1.0, n_block),  # sd=1 -> var=1
                rng.normal(0.0, 2.0, n_block),  # sd=2 -> var=4
            ]
        )
        ratio = var_ratio_by_gain(M, g, q=0.25)
        assert ratio == pytest.approx(4.0, rel=0.25)

    def test_no_amplification_ratio_near_one(self):
        """If g does not affect the variance of M, the ratio should be near 1."""
        rng = np.random.default_rng(456)
        n = 10000
        g = rng.uniform(0.5, 1.5, n)
        M = rng.normal(0.0, 1.0, n)
        ratio = var_ratio_by_gain(M, g, q=0.25)
        assert ratio == pytest.approx(1.0, abs=0.25)


class TestReversionDays:
    """Test 4: deterministic series mu[t] = mu0*rho^(t-t_shock), rho=0.7."""

    def test_deterministic_ar1_decay_rounds_to_three(self):
        """Theoretical e-folding -1/ln(0.7) ~= 2.8037; first integer below the
        threshold

        1/e of the peak is t-t_peak=3 (dev at t_peak+3 is 0.7^3=0.343 of the
        peak, which is <= 1/e ~= 0.3679; at t_peak+2, 0.7^2=0.49 > 1/e, not
        enough).
        """
        rho = 0.7
        mu0 = 10.0
        t_shock = 5
        n = 30
        mu = np.zeros(n)
        for t in range(t_shock, n):
            mu[t] = mu0 * rho ** (t - t_shock)

        result = reversion_days(mu, t_shock=t_shock, baseline=0.0)
        assert result == pytest.approx(3.0)

        theoretical = -1.0 / math.log(rho)
        assert theoretical < result < theoretical + 1.0

    def test_never_reverts_returns_inf(self):
        """A series that moves away from baseline and never returns -> inf."""
        t_shock = 2
        n = 20
        mu = np.zeros(n)
        for t in range(t_shock, n):
            mu[t] = 1.0 + 0.1 * (t - t_shock)  # grows without bound

        result = reversion_days(mu, t_shock=t_shock, baseline=0.0)
        assert math.isinf(result)

    def test_peak_at_shock_immediate_full_reversion(self):
        """If the peak occurs right at t_shock and the next value already falls
        below the threshold, t-t_peak must be the first integer that satisfies
        it."""
        # dev: [5, 5, 1] from t_shock=0: peak at t=0, threshold 5/e ~= 1.839.
        mu = np.array([5.0, 5.0, 1.0])
        result = reversion_days(mu, t_shock=0, baseline=0.0)
        assert result == pytest.approx(2.0)


class TestDailyRate:
    """Test 5: daily_rate exact trivial case."""

    def test_trivial_case(self):
        """10 events in 5 days -> 2.0 events/day."""
        times_h = np.arange(10) * 12.0  # evenly spaced
        assert daily_rate(times_h, horizon_days=5.0) == pytest.approx(2.0)

    def test_zero_events(self):
        """No events -> rate 0."""
        times_h = np.array([])
        assert daily_rate(times_h, horizon_days=10.0) == pytest.approx(0.0)


class TestGapStats:
    """Test 6: hand-built gaps [1,1,1,9]."""

    def test_hand_built_gaps(self):
        """4 events at t=[0,1,2,3,12] -> gaps=[1,1,1,9] and hand-computed
        stats (mean=3, median=1, cv=4/3, burstiness=1/7, mode_h=1.5)."""
        times_h = np.array([0.0, 1.0, 2.0, 3.0, 12.0])
        stats = gap_stats(times_h, bin_width_h=1.0)

        assert stats["mean_h"] == pytest.approx(3.0)
        assert stats["median_h"] == pytest.approx(1.0)
        assert stats["cv"] == pytest.approx(4.0 / 3.0)
        assert stats["burstiness"] == pytest.approx(1.0 / 7.0)
        assert stats["mode_h"] == pytest.approx(1.5)

    def test_unsorted_input_same_result(self):
        """The input time order must not matter (sorted before computing the
        gaps)."""
        times_h = np.array([12.0, 0.0, 3.0, 1.0, 2.0])
        stats = gap_stats(times_h, bin_width_h=1.0)
        assert stats["mean_h"] == pytest.approx(3.0)
        assert stats["median_h"] == pytest.approx(1.0)

    def test_poisson_like_burstiness_near_zero(self):
        """Poisson process (exponential gaps, cv=1) -> burstiness ~ 0."""
        rng = np.random.default_rng(999)
        n = 20000
        gaps = rng.exponential(scale=2.0, size=n)
        times_h = np.concatenate([[0.0], np.cumsum(gaps)])
        stats = gap_stats(times_h, bin_width_h=0.5)
        assert stats["burstiness"] == pytest.approx(0.0, abs=0.03)
        assert stats["cv"] == pytest.approx(1.0, abs=0.03)


class TestHourlyHistogram:
    """Test 7: events at known hours, including t>24 (wrap)."""

    def test_hand_placed_events_with_wrap(self):
        """Local hours wrap into 24 buckets; the histogram is (24,)."""
        times_h = np.array([0.5, 24.5, 5.0, 13.0, 37.0, 23.9])
        hist = hourly_histogram(times_h, bins=24)

        assert hist.shape == (24,)
        expected = np.zeros(24, dtype=hist.dtype)
        expected[0] = 2
        expected[5] = 1
        expected[13] = 2
        expected[23] = 1
        np.testing.assert_array_equal(hist, expected)
        assert hist.sum() == 6

    def test_custom_bins_count(self):
        """bins=4 groups the day into 6h quarters; shape must reflect it."""
        times_h = np.array([1.0, 7.0, 13.0, 19.0])  # one per quarter
        hist = hourly_histogram(times_h, bins=4)
        assert hist.shape == (4,)
        np.testing.assert_array_equal(hist, np.array([1, 1, 1, 1]))


class TestEnvelopeViolations:
    """Test 8: step envelope_fn, events on both sides."""

    def test_step_envelope_exact_count(self):
        """Envelope = 0 (violation) on [23,24)U[0,8) (quiet hours), 1 outside.

        Events: 2 at allowed hours (10, 15), 3 in quiet hours
        (0.5, 23.5, 7.9) -> exactly 3 violations.
        """

        def step_envelope(h: float) -> float:
            local = h % 24.0
            if local >= 23.0 or local < 8.0:
                return 0.0
            return 1.0

        times_h = np.array([10.0, 15.0, 0.5, 23.5, 7.9])
        count = envelope_violations(times_h, step_envelope, eps=1e-9)
        assert count == 3

    def test_no_violations_when_all_inside_envelope(self):
        """If no event falls in the forbidden zone, the count is 0."""

        def step_envelope(h: float) -> float:
            local = h % 24.0
            if local >= 23.0 or local < 8.0:
                return 0.0
            return 1.0

        times_h = np.array([9.0, 10.0, 20.0, 22.9])
        count = envelope_violations(times_h, step_envelope, eps=1e-9)
        assert count == 0
