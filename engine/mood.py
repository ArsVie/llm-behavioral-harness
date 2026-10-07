"""Daily mood — beta-binomial in logit space.

Does not import engine.cycle or engine.circadian: `m` and `g` arrive as floats.

Equations (DESIGN.md §"Daily mood"):
    arg  according to MoodVariant (see types.MoodVariant — the 3 formulas)
    p    = sigmoid(arg)
    M    ~ BetaBinomial(N, p, ν)     ν=∞ ⇒ Binomial(N, p) EXACT (no Beta sampling)
           finite ν implementation: p_day ~ Beta(p·ν, (1−p)·ν); M ~ Binomial(N, p_day)
    μ'   = ρ·μ + k·(score − score_neutral)
    η'   = ρ_e·η + Normal(0, σ_e)

The steps do NOT mutate the received state: they return a new MoodState.
"""
from __future__ import annotations

import math

import numpy as np

from engine.types import MoodState, MoodVariant, PersonaParams


def logit(x: float) -> float:
    """log(x/(1−x)). Domain (0,1)."""
    return math.log(x / (1.0 - x))


def sigmoid(x: float) -> float:
    """1/(1+exp(−x)), numerically stable for large |x|.

    Standard conditional form: avoids exp() overflow for very negative
    or very positive x by always evaluating exp() on an argument <= 0.
    """
    if x >= 0.0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def compute_arg(
    state: MoodState,
    params: PersonaParams,
    m: float,
    g: float,
    variant: MoodVariant,
) -> float:
    """Logit argument for the day according to the variant (see types.MoodVariant).

    ORIGINAL:           (logit λ + μ) · g       — ignores m (cycle offset)
    DECOUPLED:          logit λ + g·(μ + η)     — ignores m
    DECOUPLED_OFFSETS:  logit λ + m + g·(μ + η) — full model

    The three coincide exactly when m=0, η=0, g=1 (see the docstring of
    MoodVariant in types.py).
    """
    lam_logit = logit(params.lam)
    if variant is MoodVariant.ORIGINAL:
        return (lam_logit + state.mu) * g
    if variant is MoodVariant.DECOUPLED:
        return lam_logit + g * (state.mu + state.eta)
    if variant is MoodVariant.DECOUPLED_OFFSETS:
        return lam_logit + m + g * (state.mu + state.eta)
    raise ValueError(f"Unknown MoodVariant: {variant!r}")


def step(
    state: MoodState,
    params: PersonaParams,
    m: float,
    g: float,
    variant: MoodVariant,
    rng: np.random.Generator,
) -> tuple[int, float, float]:
    """Samples the mood of the day. Returns (M, p, arg).

    M ∈ 0..N. With params.nu == math.inf it uses an exact Binomial(N, p) (no
    Beta call — the special case is structural, not a numerical limit).
    Does not modify `state` (it only reads μ and η).
    """
    arg = compute_arg(state, params, m, g, variant)
    p = sigmoid(arg)

    if math.isinf(params.nu):
        p_day = p
    else:
        alpha = p * params.nu
        beta = (1.0 - p) * params.nu
        p_day = rng.beta(alpha, beta)

    M = int(rng.binomial(params.N, p_day))
    return M, p, arg


def update(state: MoodState, params: PersonaParams, score: float) -> MoodState:
    """End of day: μ' = ρ·μ + k·(score − score_neutral). Returns a NEW state.

    Closed form under constant score s: μ∞ = k·(s − neutral)/(1−ρ).
    """
    mu_new = params.rho * state.mu + params.k * (score - params.score_neutral)
    return MoodState(mu=mu_new, eta=state.eta)


def step_endogenous(
    state: MoodState, params: PersonaParams, rng: np.random.Generator
) -> MoodState:
    """End of day: η' = ρ_e·η + Normal(0, σ_e). Returns a NEW state.

    Stationary sd: σ_e/√(1−ρ_e²).
    """
    innovation = rng.normal(0.0, params.sigma_e)
    eta_new = params.rho_e * state.eta + innovation
    return MoodState(mu=state.mu, eta=eta_new)
