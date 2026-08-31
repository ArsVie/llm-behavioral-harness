"""Tests de aceptación para engine/timing.py (W1.4).

Semillas fijas; tests estadísticos con alpha=0.01 y n>=2000 (KS) o
tolerancia +-3*sem (medias). Ver docstring de engine/timing.py y el
contrato en engine/types.py para la especificación del modelo.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats

from engine.timing import hazard, next_event
from engine.types import TimingParams

ALPHA = 0.01
N_SAMPLES = 2000


def _params(k_w: float, theta_h: float = 13.5) -> TimingParams:
    return TimingParams(k_w=k_w, theta_h=theta_h)


def _mod_const(c: float):
    def _m(_t_h: float) -> float:
        return c

    return _m


def _sample_gaps(
    n: int,
    params: TimingParams,
    rng: np.random.Generator,
    modulator=None,
    mod_ub: float = 1.0,
    tau0_h: float = 0.0,
) -> np.ndarray:
    """Muestrea `n` gaps encadenando next_event: t_last fijo (tau0_h antes de
    t_now), t_now avanza al evento aceptado, tau vuelve a arrancar en 0 salvo
    que se pida tau0_h != 0 (solo relevante para el primer gap)."""
    if modulator is None:
        modulator = _mod_const(1.0)
    gaps = np.empty(n)
    t_now = 0.0
    t_last = t_now - tau0_h
    for i in range(n):
        t_next = next_event(
            t_now, t_last, modulator, params, rng, mod_ub=mod_ub
        )
        assert math.isfinite(t_next)
        gaps[i] = t_next - t_now
# Chaining: the accepted event becomes the last interaction; the next candidate is searched from there (tau restarts at 0).
        t_now = t_next
        t_last = t_next
    return gaps


# ---------------------------------------------------------------------------
# 1. modulator == 1, mod_ub=1 -> gaps ~ Weibull(k_w, theta_h) (KS)


def test_gaps_follow_weibull_k2():
    k_w, theta_h = 2.0, 13.5
    params = _params(k_w, theta_h)
    rng = np.random.default_rng(1)
    gaps = _sample_gaps(N_SAMPLES, params, rng, mod_ub=1.0)

    stat, pvalue = stats.kstest(gaps, stats.weibull_min(k_w, scale=theta_h).cdf)
    assert pvalue > ALPHA, f"KS contra Weibull({k_w},{theta_h}) fallo: p={pvalue}"


# ---------------------------------------------------------------------------
# 2. k_w == 1 -> exponential (KS) + memoryless (two different tau0, same distribution)


def test_gaps_follow_exponential_k1():
    k_w, theta_h = 1.0, 13.5
    params = _params(k_w, theta_h)
    rng = np.random.default_rng(2)
    gaps = _sample_gaps(N_SAMPLES, params, rng, mod_ub=1.0)

    stat, pvalue = stats.kstest(gaps, stats.expon(scale=theta_h).cdf)
    assert pvalue > ALPHA, f"KS contra Expon({theta_h}) fallo: p={pvalue}"


def test_exponential_is_memoryless():
    k_w, theta_h = 1.0, 13.5
    params = _params(k_w, theta_h)

    modulator = _mod_const(1.0)
    rng_a = np.random.default_rng(20)
    rng_b = np.random.default_rng(21)

    # tau_0 = 0: t_last == t_now.
    gaps_tau0 = np.array(
        [
            next_event(0.0, 0.0, modulator, params, rng_a, mod_ub=1.0)
            for _ in range(N_SAMPLES)
        ]
    )
# tau_0 = 20h: t_last much earlier than t_now (memoryless => same distribution).
    gaps_tau20 = np.array(
        [
            next_event(20.0, 0.0, modulator, params, rng_b, mod_ub=1.0) - 20.0
            for _ in range(N_SAMPLES)
        ]
    )

    stat, pvalue = stats.ks_2samp(gaps_tau0, gaps_tau20)
    assert pvalue > ALPHA, f"KS 2-sample (memoryless) fallo: p={pvalue}"


# ---------------------------------------------------------------------------
# 3. k_w == 2 -> increasing hazard: histogram mode > 0, mean ~ theta*Gamma(1.5)


def test_increasing_hazard_mode_and_mean():
    k_w, theta_h = 2.0, 13.5
    params = _params(k_w, theta_h)
    rng = np.random.default_rng(3)
    gaps = _sample_gaps(N_SAMPLES, params, rng, mod_ub=1.0)

# Histogram with 1h bins; bin [0,1) is not the most populated.
    max_gap = gaps.max()
    n_bins = max(1, int(math.ceil(max_gap)))
    counts, edges = np.histogram(gaps, bins=n_bins, range=(0.0, float(n_bins)))
    mode_bin = int(np.argmax(counts))
    assert mode_bin > 0, (
        "La moda del histograma de gaps cae en [0,1) pese a k_w=2 "
        f"(hazard creciente); counts[:5]={counts[:5]}"
    )

    mean_expected = theta_h * math.gamma(1.0 + 1.0 / k_w)  # theta*Gamma(1.5)
    sem = gaps.std(ddof=1) / math.sqrt(gaps.size)
    assert abs(gaps.mean() - mean_expected) <= 3 * sem, (
        f"media muestral {gaps.mean():.4f} fuera de "
        f"{mean_expected:.4f} +- {3 * sem:.4f}"
    )


# ---------------------------------------------------------------------------
# 4. Step modulator (8-20 schedule): no events outside the window


def test_step_modulator_confines_events_to_window():
    k_w, theta_h = 2.0, 13.5
    params = _params(k_w, theta_h)
    rng = np.random.default_rng(4)

    def step_modulator(t_h: float) -> float:
        local_hour = t_h % 24.0
        return 1.0 if 8.0 <= local_hour < 20.0 else 0.0

    horizon_h = 60 * 24.0  # ~60 days
    t_now = 0.0
    t_last = 0.0
    n_events = 0
    while t_now < horizon_h:
        t_next = next_event(
            t_now,
            t_last,
            step_modulator,
            params,
            rng,
            mod_ub=1.0,
            max_horizon_h=720.0,
        )
        if not math.isfinite(t_next) or t_next >= horizon_h:
            break
        local_hour = t_next % 24.0
        assert 8.0 <= local_hour < 20.0, (
            f"evento en t_h={t_next} (hora local {local_hour}) fuera de [8,20)"
        )
        n_events += 1
        t_now = t_next
        t_last = t_next
    assert n_events > 10, "muy pocos eventos generados para validar la ventana"


# ---------------------------------------------------------------------------
# 5. Rate scaling: k_w=1, modulator==c=2.0, mod_ub=2.0 -> mean gap ~ theta/c


def test_rate_scaling_with_constant_modulator():
    k_w, theta_h = 1.0, 13.5
    c = 2.0
    params = _params(k_w, theta_h)
    rng = np.random.default_rng(5)
    gaps = _sample_gaps(
        N_SAMPLES, params, rng, modulator=_mod_const(c), mod_ub=c
    )

    mean_expected = theta_h / c
    sem = gaps.std(ddof=1) / math.sqrt(gaps.size)
    assert abs(gaps.mean() - mean_expected) <= 3 * sem, (
        f"media muestral {gaps.mean():.4f} fuera de "
        f"{mean_expected:.4f} +- {3 * sem:.4f}"
    )


# ---------------------------------------------------------------------------
# 6. Determinism (same seed => same sequence) and max_horizon (modulator==0)


def test_determinism_same_seed_same_sequence():
    params = _params(k_w=2.0, theta_h=13.5)
    modulator = _mod_const(1.0)

    def _run_sequence(seed: int) -> list[float]:
        rng = np.random.default_rng(seed)
        t_now = 0.0
        t_last = 0.0
        seq = []
        for _ in range(50):
            t_next = next_event(t_now, t_last, modulator, params, rng, mod_ub=1.0)
            seq.append(t_next)
            t_now = t_next
            t_last = t_next
        return seq

    seq_a = _run_sequence(42)
    seq_b = _run_sequence(42)
    assert seq_a == seq_b

    seq_c = _run_sequence(43)
    assert seq_a != seq_c


def test_max_horizon_returns_inf_with_zero_modulator():
    params = _params(k_w=2.0, theta_h=13.5)
    rng = np.random.default_rng(6)
    t_next = next_event(
        0.0, 0.0, _mod_const(0.0), params, rng, mod_ub=2.0, max_horizon_h=48.0
    )
    assert t_next == math.inf


# ---------------------------------------------------------------------------
# Direct tests of hazard() (closed form, no RNG)


def test_hazard_matches_closed_form():
    params = _params(k_w=2.0, theta_h=13.5)
    modulator = _mod_const(3.0)
    tau_h = 5.0
    expected = (
        (params.k_w / params.theta_h)
        * (tau_h / params.theta_h) ** (params.k_w - 1.0)
        * 3.0
    )
    assert hazard(tau_h, 100.0, modulator, params) == pytest.approx(expected)


def test_hazard_zero_at_tau_zero_when_k_w_gt_1():
    params = _params(k_w=2.0, theta_h=13.5)
    assert hazard(0.0, 0.0, _mod_const(1.0), params) == 0.0


def test_hazard_finite_at_tau_zero_when_k_w_eq_1():
    params = _params(k_w=1.0, theta_h=13.5)
    expected = params.k_w / params.theta_h
    assert hazard(0.0, 0.0, _mod_const(1.0), params) == pytest.approx(expected)


# The latent-state term rides only the modulator slot.


def test_state_factor_enters_hazard_multiplicatively():
    """h(τ,t) = h0(τ) · modulator(t) with the modulator carrying a state
    factor — the coupling multiplies the frozen Weibull base, never alters
    its family or parameters."""
    params = _params(k_w=2.0)
    state = 1.35

    def state_modulator(_t_h: float) -> float:
        return state  # envelope·phase·adj·exp(w·(x−x₀)) collapsed to the state term

    h_state = hazard(5.0, 100.0, state_modulator, params)
    base = (
        (params.k_w / params.theta_h)
        * (5.0 / params.theta_h) ** (params.k_w - 1.0)
    )
    assert h_state == pytest.approx(base * state)
    # The base itself is unchanged: modulator ≡ 1 recovers the closed form.
    assert hazard(5.0, 100.0, _mod_const(1.0), params) == pytest.approx(base)


def test_state_factor_scales_rate_through_thinning():
    """k_w=1 exact rate scaling with a state-carrying modulator: mean gap ≈
    θ/c — the multiplicative coupling survives the thinning end-to-end."""
    params = _params(k_w=1.0)
    c = 1.6
    rng = np.random.default_rng(7)

    def state_modulator(_t_h: float) -> float:
        return c

    gaps = _sample_gaps(N_SAMPLES, params, rng, modulator=state_modulator, mod_ub=c)
    mean_expected = params.theta_h / c
    sem = gaps.std(ddof=1) / math.sqrt(gaps.size)
    assert abs(gaps.mean() - mean_expected) <= 3 * sem, (
        f"media muestral {gaps.mean():.4f} fuera de "
        f"{mean_expected:.4f} +- {3 * sem:.4f}"
    )
