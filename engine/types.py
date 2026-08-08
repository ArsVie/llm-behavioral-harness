"""Contrato congelado de la Fase 1 (Ola 0 — W0.1).

Este archivo es de SOLO LECTURA tras la Ola 0. Define los tipos, los
parámetros por defecto (tabla de DESIGN.md §"Parámetros iniciales") y las
decisiones abiertas que W0.1 fijó (marcadas "Decisión W0.1" en cada campo).
Especificación matemática completa: DESIGN.md §"Motor estocástico" y
research/05-reevaluacion-diseno.md §2–§3.

Convención de tiempo (transversal, congelada):
  - Escala lenta: días enteros t = 0, 1, 2, ... (un paso de motor por día).
  - Escala rápida: horas absolutas desde el inicio de la simulación
    (t_h = 0.0 es el día 0 a las 00:00). Hora local del día = t_h % 24.
  - El día del evento t_h es int(t_h // 24).

Convención de RNG (congelada, ver engine/rng.py):
  - `seed` maestro por companion; generadores hijos por SeedSequence con
    spawn_key jerárquico (stream, día). Replay de un día = (seed, t).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import numpy as np

# ---------------------------------------------------------------------------
# Alias de tipos

#: Modulador multiplicativo del hazard: recibe tiempo absoluto en horas
#: (t_h, ver convención arriba) y devuelve un factor >= 0. Los drivers de la
#: Ola 2 componen aquí envolvente circadiana × multiplicador de fase × adj.
Modulator = Callable[[float], float]


# ---------------------------------------------------------------------------
# Variantes del modelo de ánimo (research/05 §2.2)


class MoodVariant(Enum):
    """Fórmula del argumento logit del ánimo diario.

    ORIGINAL:           arg = (logit λ + μ) · g(t)
        Plan original §3: la ganancia multiplica también el temperamento;
        estructuralmente sin m(t) ni η(t) (equivale a B≡0, η≡0).
    DECOUPLED:          arg = logit λ + g(t)·(μ + η)
        Ganancia solo sobre desviaciones; sin desplazamiento de media (B≡0,
        m(t) se ignora).
    DECOUPLED_OFFSETS:  arg = logit λ + m(t) + g(t)·(μ + η)
        Modelo completo de DESIGN.md.

    Las tres variantes coinciden exactamente cuando B=0, η≡0 (sigma_e=0 y
    η₀=0), ν=∞ y g≡1 (A=0, sigma_eps=0). El muestreo M ~ BetaBinomial(N, p, ν)
    es común a las tres (ν=∞ ⇒ binomial pura).
    """

    ORIGINAL = "original"
    DECOUPLED = "decoupled"
    DECOUPLED_OFFSETS = "decoupled_offsets"


# ---------------------------------------------------------------------------
# Fases del ciclo (Decisión W0.1 — rangos exactos no fijados en DESIGN)

#: Etiquetas de fase, en orden dentro del ciclo.
PHASE_MENSTRUAL = "menstrual"
PHASE_FOLLICULAR = "follicular"
PHASE_OVULATORY = "ovulatory"
PHASE_LUTEAL_EARLY = "luteal_early"
PHASE_LUTEAL_LATE = "luteal_late"

#: Decisión W0.1 — fronteras de fase como FRACCIONES de la longitud del ciclo
#: L, para que escalen con el redraw de L por ciclo. Con L=28 los rangos en
#: días (semiabiertos [ini, fin)) son: menstrual [0,5), follicular [5,12),
#: ovulatory [12,16), luteal_early [16,23), luteal_late [23,28).
#: (label, frac_inicio, frac_fin); la última fase cierra en 1.0.
PHASE_FRACTIONS: tuple[tuple[str, float, float], ...] = (
    (PHASE_MENSTRUAL, 0.0, 5.0 / 28.0),
    (PHASE_FOLLICULAR, 5.0 / 28.0, 12.0 / 28.0),
    (PHASE_OVULATORY, 12.0 / 28.0, 16.0 / 28.0),
    (PHASE_LUTEAL_EARLY, 16.0 / 28.0, 23.0 / 28.0),
    (PHASE_LUTEAL_LATE, 23.0 / 28.0, 1.0),
)

#: Decisión W0.1 — multiplicadores de tasa de mensajes por fase, dentro del
#: rango 0.6–1.4 de DESIGN, ordenados según las anclas de valencia de
#: research/02 (menstrual −0.3 · folicular +0.1 · ovulatoria +0.4 ·
#: luteal-temprana +0.1 · luteal-tardía −0.2).
DEFAULT_PHASE_MULTIPLIERS: dict[str, float] = {
    PHASE_MENSTRUAL: 0.7,
    PHASE_FOLLICULAR: 1.1,
    PHASE_OVULATORY: 1.4,
    PHASE_LUTEAL_EARLY: 1.1,
    PHASE_LUTEAL_LATE: 0.8,
}

#: Decisión W0.1 — desplazamiento del canal de energía por fase (aditivo
#: sobre la base 0.6 ± diurnal_amp del coseno circadiano; ver
#: engine/circadian.py). Valores sutiles: la energía vive en [0, 1].
ENERGY_PHASE_OFFSETS: dict[str, float] = {
    PHASE_MENSTRUAL: -0.15,
    PHASE_FOLLICULAR: +0.05,
    PHASE_OVULATORY: +0.10,
    PHASE_LUTEAL_EARLY: 0.0,
    PHASE_LUTEAL_LATE: -0.10,
}

#: Decisión W0.1 — nivel base del canal de energía (antes de coseno y fase).
ENERGY_BASE = 0.6

#: Decisión W0.1 — ancho (horas) de la rampa coseno suave con la que la
#: envolvente circadiana entra/sale de quiet hours (continuidad, sin saltos).
ENVELOPE_RAMP_H = 1.0

#: Decisión W0.1 — ajuste por score del día anterior para la tasa de mensajes:
#: adj(s) = clip(1 + ADJ_SLOPE·s, *adj_bounds). Lineal, centrado en 1.
ADJ_SLOPE = 0.3


# ---------------------------------------------------------------------------
# Parámetros (congelados por persona / por deployment)


@dataclass(frozen=True)
class PersonaParams:
    """Parámetros del motor lento (ánimo + ciclo). Defaults = DESIGN.md.

    La cota de estabilidad del lazo juez→μ es k < 2(1−ρ)/g_max con
    g_max = 1 + A + 3·sigma_eps (engine/validation.py).
    """

    N: int = 10  # pasos de la escala de ánimo (M ∈ 0..N)
    lam: float = 0.60  # λ — temperamento base (media sigmoide sin ciclo)
    nu: float = math.inf  # ν — volatilidad beta-binomial; inf ⇒ binomial pura
    k: float = 0.18  # aprendizaje de μ desde el score del juez;
    #   afinado post-Fase 1 (era 0.15) — junto con rho=0.85 sube el techo del
    #   trato a μ∞=k/(1−ρ)=±1.2: un mes perfecto vive en ~7–10 y uno horrible
    #   en ~0–4 (engine_simulation/15_mes_perfecto_horrible.png)
    rho: float = 0.85  # ρ — decaimiento diario de μ (half-life ~4.3 d);
    #   afinado post-Fase 1 (era 0.70): memoria a escala de mes — los días
    #   sueltos pesan poco, las rachas sostenidas se acumulan
    rho_e: float = 0.7  # ρ_e — autocorr. del ánimo endógeno η (AR(1));
    #   afinado en Fase 1 (era 0.5) — ver results/fase-1-informe.md
    sigma_e: float = 0.45  # σ_e — sd de la innovación de η;
    #   afinado en Fase 1 (era 0.2) — ver results/fase-1-informe.md
    B: float = 0.5  # amplitud del desplazamiento de media del ciclo m(t);
    #   afinado post-Fase 1 (era 0.15) — barrido promediado de 30 semillas
    #   (engine_simulation/12_barrido_B_30seeds.png): mínimo para que el ritmo
    #   mensual sea legible en el comportamiento observable
    A: float = 0.25  # amplitud de la ganancia de reactividad del ciclo g(t)
    sigma_eps: float = 0.03  # σ_ε — ruido diario de g(t)
    L_mean: float = 28.0  # media de la longitud del ciclo (días)
    L_sd: float = 1.5  # sd de la longitud del ciclo (redraw por ciclo)
    phi: float = 0.0  # φ — fase inicial: cycle_day inicial en días.
    #   "Aleatoria por persona" en DESIGN: el driver puede sortearla
    #   (uniforme en [0, L)); default 0.0 por reproducibilidad.
    score_neutral: float = 0.0  # neutro del juez (se calibra en Fase 3)


@dataclass(frozen=True)
class TimingParams:
    """Parámetros de temporización de mensajes espontáneos. Defaults = DESIGN.md.

    Convención: `theta_h` y todos los tiempos en HORAS (ver convención de
    tiempo en el docstring del módulo). `quiet_hours=(ini, fin)` en hora local
    [0, 24); si ini > fin la ventana cruza medianoche (caso por defecto).
    """

    k_w: float = 2.0  # forma Weibull; >1 ⇒ hazard creciente; 1 ⇒ exponencial
    theta_h: float = 13.5  # escala Weibull (horas); media base ≈ θ·Γ(1+1/k_w)
    peak_hour: float = 14.0  # pico del coseno circadiano (valencia y energía)
    diurnal_amp: float = 0.25  # amplitud circadiana (valencia logit y energía)
    quiet_hours: tuple[float, float] = (23.0, 8.0)  # Decisión W0.1
    phase_multipliers: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PHASE_MULTIPLIERS)
    )
    adj_bounds: tuple[float, float] = (0.7, 1.3)  # cota del ajuste por score
    min_gap_min: float = 15.0  # guard de cola: gap mínimo entre mensajes (min)
    daily_cap: int = 3  # Decisión W0.1 — guard de cola: máx. proactivos/día
    max_gap_h: float = 48.0  # guard de cola: silencio máximo (DESIGN 48 h)


# ---------------------------------------------------------------------------
# Estado mutable del motor (evoluciona día a día)


@dataclass
class CycleState:
    """Reloj del ciclo hormonal.

    `cycle_day` ∈ [0, L_current) avanza 1.0 por día; al completar el ciclo
    (cycle_day >= L_current) se resta L_current y se redibuja
    L_current ~ Normal(L_mean, L_sd) (truncada a >= 1). φ entra como
    cycle_day inicial.
    """

    cycle_day: float
    L_current: float


@dataclass
class MoodState:
    """Estado lento del ánimo: memoria de eventos μ y ánimo endógeno η."""

    mu: float = 0.0
    eta: float = 0.0


# ---------------------------------------------------------------------------
# Registros de simulación


@dataclass(frozen=True)
class DayRecord:
    """Snapshot de un día simulado.

    Semántica: `m, g, arg, p, M` son los del día `t`; `mu, eta` son los
    valores USADOS ese día (estado al inicio del día, antes de
    mood.update / mood.step_endogenous de fin de día); `score` es el score
    (sintético) recibido ese día; `cycle_day`/`phase_label` los del día;
    `seed` es la semilla maestra del companion — el RNG del día se
    reconstruye con engine.rng.day_rng(seed, t).
    """

    t: int
    m: float
    g: float
    arg: float
    p: float
    M: int
    score: float
    mu: float
    eta: float
    cycle_day: float
    phase_label: str
    seed: int


@dataclass
class SimResult:
    """Resultado de una corrida día-a-día (driver W2.1)."""

    params: PersonaParams
    variant: MoodVariant
    records: list[DayRecord]

    # -- propiedades como arrays (derivadas de records) --------------------

    def _arr(self, name: str) -> np.ndarray:
        return np.asarray([getattr(r, name) for r in self.records])

    @property
    def t(self) -> np.ndarray:
        return self._arr("t")

    @property
    def M(self) -> np.ndarray:
        return self._arr("M")

    @property
    def m(self) -> np.ndarray:
        return self._arr("m")

    @property
    def g(self) -> np.ndarray:
        return self._arr("g")

    @property
    def arg(self) -> np.ndarray:
        return self._arr("arg")

    @property
    def p(self) -> np.ndarray:
        return self._arr("p")

    @property
    def mu(self) -> np.ndarray:
        return self._arr("mu")

    @property
    def eta(self) -> np.ndarray:
        return self._arr("eta")

    @property
    def score(self) -> np.ndarray:
        return self._arr("score")

    @property
    def cycle_day(self) -> np.ndarray:
        return self._arr("cycle_day")

    @property
    def phase_label(self) -> list[str]:
        return [r.phase_label for r in self.records]

    @property
    def seed(self) -> int:
        """Semilla maestra de la corrida (constante en todos los records)."""
        return self.records[0].seed if self.records else -1
