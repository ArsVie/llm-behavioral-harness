"""Timing of spontaneous messages — Weibull hazard + thinning.

Does not import engine.circadian: the modulation arrives as `modulator`
already composed (envelope × phase × adj, composed by the caller).

Specification (DESIGN.md §"Spontaneous message timing", research/05 §3.3):
    h(τ, t) = (k_w/θ)·(τ/θ)^(k_w−1) · modulator(t)
    τ = t − t_last_interaction (hours);  k_w > 1 ⇒ hazard increasing in τ
    ("the longer without talking, the more likely"); k_w = 1 ⇒ exponential/NHPP.

Sampling by THINNING over windows: in [u, u+Δ] the baseline hazard is
bounded by its value at τ(u+Δ) (it is monotone in τ for k_w >= 1), and the
modulator by `mod_ub`; candidates are proposed with a dominating rate and
accepted with prob h(τ,t)/dominating rate. Invariants that the tests verify:
  - modulator ≡ 1 ⇒ gaps ~ Weibull(k_w, θ) (KS);
  - k_w = 1 ⇒ exponential (memoryless);
  - k_w > 1 ⇒ gap distribution with mode > 0;
  - 0/1 step modulator ⇒ zero events where it is 0;
  - modulator ≡ c ⇒ the rate scales ~c (exactly for k_w=1).
"""
from __future__ import annotations

import math

import numpy as np

from engine.types import Modulator, TimingParams

#: Window width (hours) for the thinning dominating bound; the
#: baseline is monotonically increasing, so the right end bounds it.
_WINDOW_H = 1.0


def _baseline_hazard(tau_h: float, params: TimingParams) -> float:
    """(k_w/θ)·(τ/θ)^(k_w−1), without the `modulator` factor. τ_h >= 0."""
    if tau_h <= 0.0:
        # k_w > 1 ⇒ 0 at τ=0; k_w == 1 ⇒ k/θ; k_w < 1 is not supported.
        if params.k_w == 1.0:
            return params.k_w / params.theta_h
        return 0.0
    return (params.k_w / params.theta_h) * (tau_h / params.theta_h) ** (
        params.k_w - 1.0
    )


def hazard(
    tau_h: float, t_h: float, modulator: Modulator, params: TimingParams
) -> float:
    """h(τ, t) of the model. τ_h >= 0 in hours; t_h absolute time in hours."""
    return _baseline_hazard(tau_h, params) * modulator(t_h)


def next_event(
    t_now_h: float,
    t_last_interaction_h: float,
    modulator: Modulator,
    params: TimingParams,
    rng: np.random.Generator,
    *,
    mod_ub: float = 2.0,
    max_horizon_h: float = 720.0,
) -> float:
    """ABSOLUTE time of the next candidate event (> t_now_h), via thinning.

    `mod_ub` must upper-bound `modulator` for all t (responsibility
    of the caller; the 2.0 default covers envelope<=1 × phase<=1.4 × adj<=1.3).
    If no candidate is accepted before t_now_h + max_horizon_h,
    it returns math.inf (the caller decides — the tail guards have their
    own maximum of 48 h). Deterministic given (rng, arguments).

    Algorithm: thinning with a dominating bound over windows of width `_WINDOW_H`. In
    each window [u, u_end) the dominating rate is baseline_hazard(τ at the end
    of the window) · mod_ub (the baseline is monotonically increasing in τ for
    k_w >= 1, so its maximum over the window is at the right end).
    An exponential jump of rate lam_star is proposed; if the candidate falls
    outside the current window, it is discarded WITHOUT evaluating acceptance and
    the search jumps to the start of the next window (where the bound is recomputed).
    If it falls inside, it is accepted with prob hazard(cand)/lam_star; if rejected,
    the search continues from the candidate (not from the window start).
    """
    horizon_end_h = t_now_h + max_horizon_h
    u = t_now_h
    while u < horizon_end_h:
        window_end_h = min(u + _WINDOW_H, horizon_end_h)
        tau_end = window_end_h - t_last_interaction_h
        lam_star = _baseline_hazard(tau_end, params) * mod_ub
        if lam_star <= 0.0:
            u = window_end_h
            continue
        w = rng.exponential(1.0 / lam_star)
        if w > window_end_h - u:
            # The candidate falls outside the window: the bound is recomputed in the
            # next window without sampling acceptance.
            u = window_end_h
            continue
        cand = u + w
        tau_cand = cand - t_last_interaction_h
        h_cand = hazard(tau_cand, cand, modulator, params)
        if rng.uniform() <= h_cand / lam_star:
            return cand
        u = cand  # rejected: the search resumes from the candidate itself
    return math.inf
