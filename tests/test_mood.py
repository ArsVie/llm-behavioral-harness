"""Acceptance tests for engine/mood.py (W1.1).

OWNERSHIP: task W1.1 (this file + engine/mood.py). Uses the shared fixtures
from tests/conftest.py (rng, persona, timing) — frozen, do not touch. Seeds
and tolerances documented in each test.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest
from scipy import stats

from engine.types import MoodState, MoodVariant, PersonaParams
from engine import mood


# 1. nu = inf: M reproduces Binomial(N, p) exactly (no Beta sampling)


def test_step_nu_inf_matches_binomial_distribution(persona: PersonaParams) -> None:
    """With nu=inf, the empirical distribution of M must match Binomial(N, p)."""
    assert math.isinf(persona.nu)  # precondition for this case

    p_target = 0.35
    mu = mood.logit(p_target) - mood.logit(persona.lam)
    state = MoodState(mu=mu, eta=0.0)

    n_samples = 8000
    rng = np.random.default_rng(20260703)
    samples = np.empty(n_samples, dtype=int)
    for i in range(n_samples):
        M, p, arg = mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED, rng=rng)
        samples[i] = M

    # p and arg are deterministic, identical on every draw.
    assert p == pytest.approx(p_target, abs=1e-9)

    N = persona.N
    observed_counts = np.bincount(samples, minlength=N + 1).astype(float)
    expected_probs = stats.binom.pmf(np.arange(N + 1), N, p_target)
    expected_counts = expected_probs * n_samples

    # Merge bins with expected count < 5 (chi-square rule), starting from the tails.
    obs = list(observed_counts)
    exp = list(expected_counts)

    def merge_left_tail(obs_l, exp_l):
        while len(exp_l) > 1 and exp_l[0] < 5.0:
            exp_l[1] += exp_l[0]
            obs_l[1] += obs_l[0]
            del exp_l[0]
            del obs_l[0]
        return obs_l, exp_l

    obs, exp = merge_left_tail(obs, exp)
    obs.reverse()
    exp.reverse()
    obs, exp = merge_left_tail(obs, exp)
    obs.reverse()
    exp.reverse()

    obs_arr = np.asarray(obs)
    exp_arr = np.asarray(exp)
    # Renormalize expected counts to the observed total.
    exp_arr = exp_arr * (obs_arr.sum() / exp_arr.sum())

    chi2, pvalue = stats.chisquare(f_obs=obs_arr, f_exp=exp_arr)
    assert pvalue > 0.01, f"chi-square rejects H0 (Binomial): chi2={chi2}, p={pvalue}"


def test_step_nu_inf_ks_matches_binomial_cdf(persona: PersonaParams) -> None:
    """Independent check with the DKW band: D = sup|ecdf - cdf|, alpha=0.01."""
    p_target = 0.62
    mu = mood.logit(p_target) - mood.logit(persona.lam)
    state = MoodState(mu=mu, eta=0.0)

    n_samples = 6000
    rng = np.random.default_rng(777)
    samples = np.empty(n_samples, dtype=int)
    for i in range(n_samples):
        M, _, _ = mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.ORIGINAL, rng=rng)
        samples[i] = M

    xs = np.arange(persona.N + 1)
    ecdf = np.array([(samples <= x).mean() for x in xs])
    cdf_theoretical = stats.binom.cdf(xs, persona.N, p_target)
    D = np.max(np.abs(ecdf - cdf_theoretical))

    alpha = 0.01
    eps = math.sqrt(math.log(2.0 / alpha) / (2.0 * n_samples))
    assert D <= eps, f"KS/DKW rejects H0 (Binomial): D={D:.5f} > eps={eps:.5f} (alpha={alpha})"


class _BetaSpyRng:
    """Wrapper around a real Generator that counts calls to .beta().

    numpy.random.Generator is a native (Cython) object with read-only
    attributes: it does not accept monkeypatch.setattr directly on an
    instance. Composition (delegation via __getattr__) is used instead of
    patching the object.
    """

    def __init__(self, inner: np.random.Generator) -> None:
        self._inner = inner
        self.beta_calls = 0

    def beta(self, *args, **kwargs):
        self.beta_calls += 1
        return self._inner.beta(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_step_nu_inf_never_calls_beta(persona: PersonaParams) -> None:
    """The nu=inf case is STRUCTURAL: it must not invoke Generator.beta at all
    (it is not a numerical limit with huge alpha/beta). It uses a spy that
    delegates to a real Generator but counts calls to .beta().
    """
    state = MoodState(mu=0.1, eta=0.0)
    spy = _BetaSpyRng(np.random.default_rng(1))

    M, p, arg = mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED, rng=spy)

    assert spy.beta_calls == 0, "step called rng.beta() with nu=inf (it should be pure Binomial)"
    assert 0 <= M <= persona.N


def test_step_finite_nu_does_call_beta(persona: PersonaParams) -> None:
    """Counterpart of the previous test: with finite nu it MUST go through
    rng.beta() (beta-binomial branch), so an implementation that ignores nu
    entirely does not slip through."""
    params_finite_nu = dataclasses.replace(persona, nu=8.0)
    state = MoodState(mu=0.1, eta=0.0)
    spy = _BetaSpyRng(np.random.default_rng(1))

    mood.step(state, params_finite_nu, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED, rng=spy)

    assert spy.beta_calls == 1


# 2. Constant-score mu recursion converges to the closed form


def test_update_converges_to_closed_form(persona: PersonaParams) -> None:
    """mu' = rho*mu + k*(s - score_neutral) iterated ~50 times under constant
    score s converges to mu_inf = k*(s - score_neutral)/(1 - rho).
    Tolerance 1e-6 (absolute) with rho=0.70 FIXED explicitly (not the
    default, which may be tuned) ⇒ fast geometric convergence
    (rho^50 ~ 8e-8), comfortably within the requested tolerance.
    """
    s = 0.8
    persona = dataclasses.replace(persona, k=0.15, rho=0.70)
    state = MoodState(mu=0.0, eta=0.0)
    for _ in range(50):
        state = mood.update(state, persona, score=s)

    mu_inf_expected = persona.k * (s - persona.score_neutral) / (1.0 - persona.rho)
    assert state.mu == pytest.approx(mu_inf_expected, abs=1e-6)


def test_update_converges_to_closed_form_negative_score(persona: PersonaParams) -> None:
    """Same criterion as the previous test but with negative score (mu_inf < 0),
    so as not to rely only on the positive branch of the recursion."""
    s = -0.5
    persona = dataclasses.replace(persona, k=0.15, rho=0.70)  # same params as the previous test
    state = MoodState(mu=0.3, eta=-0.2)  # arbitrary non-zero initial state
    for _ in range(60):
        state = mood.update(state, persona, score=s)

    mu_inf_expected = persona.k * (s - persona.score_neutral) / (1.0 - persona.rho)
    assert state.mu == pytest.approx(mu_inf_expected, abs=1e-6)


# 3. Stationary sd of eta ~ sigma_e / sqrt(1 - rho_e^2)


def test_step_endogenous_stationary_sd(persona: PersonaParams) -> None:
    """Iterates step_endogenous >=20000 steps, discards burn-in (first 2000,
    ~irrelevant relative to the autocorrelation scale rho_e=0.5) and compares
    the sample sd of eta with the theoretical stationary sd sigma_e/sqrt(1-rho_e^2),
    relative tolerance 5%. Fixed seed for reproducibility.
    """
    rng = np.random.default_rng(4242)
    n_steps = 25000
    burn_in = 2000

    state = MoodState(mu=0.0, eta=0.0)
    etas = np.empty(n_steps)
    for i in range(n_steps):
        state = mood.step_endogenous(state, persona, rng)
        etas[i] = state.eta

    sample_sd = np.std(etas[burn_in:], ddof=1)
    theoretical_sd = persona.sigma_e / math.sqrt(1.0 - persona.rho_e ** 2)

    rel_error = abs(sample_sd - theoretical_sd) / theoretical_sd
    assert rel_error < 0.05, (
        f"sample sd {sample_sd:.5f} vs theoretical {theoretical_sd:.5f} "
        f"(relative error {rel_error:.3%}, tolerance 5%)"
    )


# 4. The 3 variants are identical when m=0, eta=0, g=1


@pytest.mark.parametrize(
    "variant",
    [MoodVariant.ORIGINAL, MoodVariant.DECOUPLED, MoodVariant.DECOUPLED_OFFSETS],
)
def test_variants_identical_arg_when_degenerate(persona: PersonaParams, variant: MoodVariant) -> None:
    """With m=0, eta=0, g=1 the three variants must produce EXACTLY the same
    arg (exact comparison, not approx: they are the same floating-point
    arithmetic operations under these degenerate conditions)."""
    state = MoodState(mu=0.37, eta=0.0)
    arg = mood.compute_arg(state, persona, m=0.0, g=1.0, variant=variant)
    expected = mood.logit(persona.lam) + state.mu
    assert arg == expected


def test_variants_same_M_with_same_rng_when_degenerate(persona: PersonaParams) -> None:
    """step with the same Generator (same seed, rebuilt for each variant)
    must produce the same M when m=0, eta=0, g=1, since arg (and therefore p)
    is identical across variants and the randomness consumption of step
    (rng.binomial, no rng.beta because nu=inf) is the same."""
    state = MoodState(mu=0.2, eta=0.0)
    results = {}
    for variant in MoodVariant:
        rng = np.random.default_rng(999)
        M, p, arg = mood.step(state, persona, m=0.0, g=1.0, variant=variant, rng=rng)
        results[variant] = (M, p, arg)

    Ms = [v[0] for v in results.values()]
    ps = [v[1] for v in results.values()]
    args_ = [v[2] for v in results.values()]
    assert Ms[0] == Ms[1] == Ms[2]
    assert ps[0] == ps[1] == ps[2]
    assert args_[0] == args_[1] == args_[2]


# 5. step does not mutate state; update/step_endogenous return new instances


def test_step_does_not_mutate_state(persona: PersonaParams) -> None:
    state = MoodState(mu=0.25, eta=-0.1)
    mu_before, eta_before = state.mu, state.eta
    rng = np.random.default_rng(5)

    mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED_OFFSETS, rng=rng)

    assert state.mu == mu_before
    assert state.eta == eta_before


def test_update_returns_new_instance_without_mutating_original(persona: PersonaParams) -> None:
    state = MoodState(mu=0.1, eta=0.2)
    new_state = mood.update(state, persona, score=0.5)

    assert new_state is not state
    assert isinstance(new_state, MoodState)
    # Original unchanged.
    assert state.mu == 0.1
    assert state.eta == 0.2
    # update leaves eta unchanged (touches only mu).
    assert new_state.eta == state.eta
    # mu changed (score != score_neutral).
    assert new_state.mu != state.mu


def test_step_endogenous_returns_new_instance_without_mutating_original(persona: PersonaParams) -> None:
    state = MoodState(mu=0.3, eta=0.4)
    rng = np.random.default_rng(6)
    new_state = mood.step_endogenous(state, persona, rng)

    assert new_state is not state
    assert isinstance(new_state, MoodState)
    # Original unchanged.
    assert state.mu == 0.3
    assert state.eta == 0.4
    # step_endogenous leaves mu unchanged (touches only eta).
    assert new_state.mu == state.mu
    assert new_state.eta != state.eta


# 6. Determinism: same Generator (same seed) => same M


def test_step_deterministic_with_same_seed(persona: PersonaParams) -> None:
    state = MoodState(mu=0.15, eta=-0.05)

    rng_a = np.random.default_rng(31415)
    rng_b = np.random.default_rng(31415)

    result_a = mood.step(state, persona, m=0.1, g=0.9, variant=MoodVariant.DECOUPLED_OFFSETS, rng=rng_a)
    result_b = mood.step(state, persona, m=0.1, g=0.9, variant=MoodVariant.DECOUPLED_OFFSETS, rng=rng_b)

    assert result_a == result_b


def test_step_deterministic_with_same_seed_finite_nu(persona: PersonaParams) -> None:
    """Same criterion as above but with finite nu (exercises rng.beta in
    addition to rng.binomial) so the beta-binomial branch is not left uncovered."""
    params_finite_nu = dataclasses.replace(persona, nu=8.0)
    state = MoodState(mu=0.15, eta=-0.05)

    rng_a = np.random.default_rng(2718)
    rng_b = np.random.default_rng(2718)

    result_a = mood.step(state, params_finite_nu, m=0.1, g=0.9, variant=MoodVariant.DECOUPLED_OFFSETS, rng=rng_a)
    result_b = mood.step(state, params_finite_nu, m=0.1, g=0.9, variant=MoodVariant.DECOUPLED_OFFSETS, rng=rng_b)

    assert result_a == result_b


def test_step_different_seeds_generally_differ(persona: PersonaParams) -> None:
    """Sanity check complementary to determinism: different seeds should
    (typically) produce different sequences of M. 30 consecutive draws are
    compared between two rngs with different seeds; with N=10 and intermediate
    p, the probability that the 30 sequences match by chance is negligible,
    so it is not a flaky test in practice."""
    state = MoodState(mu=0.0, eta=0.0)
    rng_a = np.random.default_rng(1)
    rng_b = np.random.default_rng(2)

    Ms_a = [mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED, rng=rng_a)[0] for _ in range(30)]
    Ms_b = [mood.step(state, persona, m=0.0, g=1.0, variant=MoodVariant.DECOUPLED, rng=rng_b)[0] for _ in range(30)]

    assert Ms_a != Ms_b


# Extras: logit/sigmoid base utilities.


def test_sigmoid_logit_are_inverse() -> None:
    for p in (0.001, 0.1, 0.35, 0.5, 0.62, 0.9, 0.999):
        assert mood.sigmoid(mood.logit(p)) == pytest.approx(p, abs=1e-9)


def test_sigmoid_stable_for_large_magnitude() -> None:
    # Must not overflow or produce nan; saturates to 0/1 within eps.
    assert mood.sigmoid(1000.0) == pytest.approx(1.0, abs=1e-12)
    assert mood.sigmoid(-1000.0) == pytest.approx(0.0, abs=1e-12)
    assert math.isfinite(mood.sigmoid(1000.0))
    assert math.isfinite(mood.sigmoid(-1000.0))
