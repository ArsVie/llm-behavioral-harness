"""Ánimo diario — beta-binomial en espacio logit (W1.1).

PROPIEDAD: tarea W1.1 (este archivo + tests/test_mood.py). Implementar contra
engine/types.py (CONGELADO). No importar engine.cycle ni engine.circadian:
`m` y `g` llegan como floats (regla de no-importación entre módulos del motor).

Ecuaciones (DESIGN.md §"Ánimo diario"):
    arg  según MoodVariant (ver types.MoodVariant — las 3 fórmulas)
    p    = sigmoid(arg)
    M    ~ BetaBinomial(N, p, ν)     ν=∞ ⇒ Binomial(N, p) EXACTA (sin muestrear Beta)
           implementación ν finita: p_day ~ Beta(p·ν, (1−p)·ν); M ~ Binomial(N, p_day)
    μ'   = ρ·μ + k·(score − score_neutral)
    η'   = ρ_e·η + Normal(0, σ_e)

Los pasos NO mutan el estado recibido: devuelven un MoodState nuevo.
"""
from __future__ import annotations

import math

import numpy as np

from engine.types import MoodState, MoodVariant, PersonaParams


def logit(x: float) -> float:
    """log(x/(1−x)). Dominio (0,1)."""
    return math.log(x / (1.0 - x))


def sigmoid(x: float) -> float:
    """1/(1+exp(−x)), numéricamente estable para |x| grande.

    Forma condicional estándar: evita overflow de exp() para x muy negativo
    o muy positivo evaluando siempre exp() sobre un argumento <= 0.
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
    """Argumento logit del día según la variante (ver types.MoodVariant).

    ORIGINAL:           (logit λ + μ) · g       — ignora m (offset de ciclo)
    DECOUPLED:          logit λ + g·(μ + η)     — ignora m
    DECOUPLED_OFFSETS:  logit λ + m + g·(μ + η) — modelo completo

    Las tres coinciden exactamente cuando m=0, η=0, g=1 (ver docstring de
    MoodVariant en types.py).
    """
    lam_logit = logit(params.lam)
    if variant is MoodVariant.ORIGINAL:
        return (lam_logit + state.mu) * g
    if variant is MoodVariant.DECOUPLED:
        return lam_logit + g * (state.mu + state.eta)
    if variant is MoodVariant.DECOUPLED_OFFSETS:
        return lam_logit + m + g * (state.mu + state.eta)
    raise ValueError(f"MoodVariant desconocida: {variant!r}")


def step(
    state: MoodState,
    params: PersonaParams,
    m: float,
    g: float,
    variant: MoodVariant,
    rng: np.random.Generator,
) -> tuple[int, float, float]:
    """Muestrea el ánimo del día. Devuelve (M, p, arg).

    M ∈ 0..N. Con params.nu == math.inf usa Binomial(N, p) exacta (ninguna
    llamada a Beta — el caso especial es estructural, no un límite numérico).
    No modifica `state` (solo lee μ y η).
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
    """Fin de día: μ' = ρ·μ + k·(score − score_neutral). Devuelve estado NUEVO.

    Forma cerrada bajo score constante s: μ∞ = k·(s − neutral)/(1−ρ).
    """
    mu_new = params.rho * state.mu + params.k * (score - params.score_neutral)
    return MoodState(mu=mu_new, eta=state.eta)


def step_endogenous(
    state: MoodState, params: PersonaParams, rng: np.random.Generator
) -> MoodState:
    """Fin de día: η' = ρ_e·η + Normal(0, σ_e). Devuelve estado NUEVO.

    sd estacionaria: σ_e/√(1−ρ_e²).
    """
    innovation = rng.normal(0.0, params.sigma_e)
    eta_new = params.rho_e * state.eta + innovation
    return MoodState(mu=state.mu, eta=eta_new)
