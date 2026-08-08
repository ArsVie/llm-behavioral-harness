"""Temporización de mensajes espontáneos — hazard Weibull + thinning (W1.4).

PROPIEDAD: tarea W1.4 (este archivo + tests/test_timing.py). Implementar
contra engine/types.py (CONGELADO). No importar engine.circadian: la
modulación llega como `modulator: Callable[[float], float]` ya compuesta
(envolvente × fase × adj — la composición es de los drivers de la Ola 2).

Especificación (DESIGN.md §"Temporización", research/05 §3.3):
    h(τ, t) = (k_w/θ)·(τ/θ)^(k_w−1) · modulator(t)
    τ = t − t_last_interaction (horas);  k_w > 1 ⇒ hazard creciente con τ
    ("cuanto más sin hablar, más probable"); k_w = 1 ⇒ exponencial/NHPP.

Muestreo por THINNING sobre ventanas: en [u, u+Δ] el hazard baseline está
acotado por su valor en τ(u+Δ) (es monótono en τ para k_w >= 1), y el
modulador por `mod_ub`; se proponen candidatos con tasa mayorante y se
aceptan con prob h(τ,t)/mayorante. Invariantes que los tests verifican:
  - modulator ≡ 1 ⇒ gaps ~ Weibull(k_w, θ) (KS);
  - k_w = 1 ⇒ exponencial (memoryless);
  - k_w > 1 ⇒ distribución de gaps con moda > 0;
  - modulator escalón 0/1 ⇒ cero eventos donde vale 0;
  - modulator ≡ c ⇒ la tasa escala ~c (para k_w=1 exactamente).
"""
from __future__ import annotations

import math

import numpy as np

from engine.types import Modulator, TimingParams

#: Ancho de ventana (horas) para la cota mayorante del thinning. El baseline
#: hazard es monótono creciente en τ (para k_w >= 1), así que su valor en el
#: EXTREMO DERECHO de la ventana (mayor τ) acota el resto de la ventana.
_WINDOW_H = 1.0


def _baseline_hazard(tau_h: float, params: TimingParams) -> float:
    """(k_w/θ)·(τ/θ)^(k_w−1), sin el factor `modulator`. τ_h >= 0."""
    if tau_h <= 0.0:
        # k_w > 1 ⇒ 0 en τ=0 (potencia positiva de 0); k_w == 1 ⇒ k/θ (finito,
        # el límite continuo del exponencial). k_w < 1 no es un caso soportado
        # (queda fuera de alcance de esta tarea, ver docstring de next_event).
        if params.k_w == 1.0:
            return params.k_w / params.theta_h
        return 0.0
    return (params.k_w / params.theta_h) * (tau_h / params.theta_h) ** (
        params.k_w - 1.0
    )


def hazard(
    tau_h: float, t_h: float, modulator: Modulator, params: TimingParams
) -> float:
    """h(τ, t) del modelo. τ_h >= 0 en horas; t_h tiempo absoluto en horas."""
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
    """Hora ABSOLUTA del siguiente evento candidato (> t_now_h), por thinning.

    `mod_ub` debe acotar superiormente a `modulator` en todo t (responsabilidad
    del llamador; el default 2.0 cubre envolvente<=1 × fase<=1.4 × adj<=1.3).
    Si no se acepta ningún candidato antes de t_now_h + max_horizon_h,
    devuelve math.inf (el llamador decide — los guards de cola tienen su
    propio máximo de 48 h). Determinista dado (rng, argumentos).

    Algoritmo: thinning con mayorante por ventanas de ancho `_WINDOW_H`. En
    cada ventana [u, u_end) la tasa mayorante es baseline_hazard(τ al final
    de la ventana) · mod_ub (el baseline es monótono creciente en τ para
    k_w >= 1, así que su máximo en la ventana está en el extremo derecho).
    Se propone un salto exponencial de tasa lam_star; si el candidato cae
    fuera de la ventana actual, se descarta SIN evaluar aceptación y se
    salta al inicio de la ventana siguiente (donde se recalcula la cota).
    Si cae dentro, se acepta con prob hazard(cand)/lam_star; si se rechaza,
    la búsqueda continúa desde el candidato (no desde el inicio de ventana).
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
            # El candidato cae más allá de esta ventana: se salta sin
            # muestrear aceptación (el mayorante de esta ventana ya no
            # aplica); se recalcula la cota en la ventana siguiente.
            u = window_end_h
            continue
        cand = u + w
        tau_cand = cand - t_last_interaction_h
        h_cand = hazard(tau_cand, cand, modulator, params)
        if rng.uniform() <= h_cand / lam_star:
            return cand
        u = cand  # rechazado: se retoma desde el propio candidato
    return math.inf
