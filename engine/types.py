"""Frozen Phase 1 contract (Wave 0 — W0.1).

This file is READ-ONLY after Wave 0. It defines the types, the default
parameters (table in DESIGN.md §"Initial parameters") and the open
decisions that W0.1 fixed (marked "W0.1 decision" on each field).
Full mathematical specification: DESIGN.md §"Stochastic engine" and
research/05-reevaluacion-diseno.md §2–§3.

Time convention (cross-cutting, frozen):
  - Slow scale: whole days t = 0, 1, 2, ... (one engine step per day).
  - Fast scale: absolute hours since the start of the simulation
    (t_h = 0.0 is day 0 at 00:00). Local hour of day = t_h % 24.
  - The day of event t_h is int(t_h // 24).

RNG convention (frozen, see engine/rng.py):
  - Master `seed` per companion; child generators via SeedSequence with a
    hierarchical spawn_key (stream, day). Replay of one day = (seed, t).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import numpy as np

# ---------------------------------------------------------------------------
# Type aliases

#: Multiplicative hazard modulator: factor >= 0 as a function of t_h.
Modulator = Callable[[float], float]


# ---------------------------------------------------------------------------
# Mood model variants (research/05 §2.2)


class MoodVariant(Enum):
    """Formula of the logit argument of the daily mood.

    ORIGINAL:           arg = (logit λ + μ) · g(t)
        Original plan §3: the gain also multiplies the temperament;
        structurally without m(t) or η(t) (equivalent to B≡0, η≡0).
    DECOUPLED:          arg = logit λ + g(t)·(μ + η)
        Gain on deviations only; no mean shift (B≡0,
        m(t) is ignored).
    DECOUPLED_OFFSETS:  arg = logit λ + m(t) + g(t)·(μ + η)
        Full DESIGN.md model.

    The three variants coincide exactly when B=0, η≡0 (sigma_e=0 and
    η₀=0), ν=∞ and g≡1 (A=0, sigma_eps=0). Sampling M ~ BetaBinomial(N, p, ν)
    is common to all three (ν=∞ ⇒ pure binomial).
    """

    ORIGINAL = "original"
    DECOUPLED = "decoupled"
    DECOUPLED_OFFSETS = "decoupled_offsets"


# ---------------------------------------------------------------------------
# Cycle phases (W0.1 decision — exact ranges not fixed in DESIGN)

#: Phase labels, in order within the cycle.
PHASE_MENSTRUAL = "menstrual"
PHASE_FOLLICULAR = "follicular"
PHASE_OVULATORY = "ovulatory"
PHASE_LUTEAL_EARLY = "luteal_early"
PHASE_LUTEAL_LATE = "luteal_late"

#: Phase boundaries as fractions of L (label, start, end); the last
#: phase closes at 1.0.
PHASE_FRACTIONS: tuple[tuple[str, float, float], ...] = (
    (PHASE_MENSTRUAL, 0.0, 5.0 / 28.0),
    (PHASE_FOLLICULAR, 5.0 / 28.0, 12.0 / 28.0),
    (PHASE_OVULATORY, 12.0 / 28.0, 16.0 / 28.0),
    (PHASE_LUTEAL_EARLY, 16.0 / 28.0, 23.0 / 28.0),
    (PHASE_LUTEAL_LATE, 23.0 / 28.0, 1.0),
)

#: Per-phase message rate multipliers, ordered by valence
#: anchor (menstrual → late luteal).
DEFAULT_PHASE_MULTIPLIERS: dict[str, float] = {
    PHASE_MENSTRUAL: 0.7,
    PHASE_FOLLICULAR: 1.1,
    PHASE_OVULATORY: 1.4,
    PHASE_LUTEAL_EARLY: 1.1,
    PHASE_LUTEAL_LATE: 0.8,
}

#: Per-phase energy channel offset (additive, energy in
#: [0, 1]).
ENERGY_PHASE_OFFSETS: dict[str, float] = {
    PHASE_MENSTRUAL: -0.15,
    PHASE_FOLLICULAR: +0.05,
    PHASE_OVULATORY: +0.10,
    PHASE_LUTEAL_EARLY: 0.0,
    PHASE_LUTEAL_LATE: -0.10,
}

#: W0.1 decision — base level of the energy channel (before cosine and phase).
ENERGY_BASE = 0.6

#: W0.1 decision — width (hours) of the smooth cosine ramp with which the
#: circadian envelope enters/leaves quiet hours (continuity, no jumps).
ENVELOPE_RAMP_H = 1.0

#: W0.1 decision — previous-day score adjustment for the message rate:
#: adj(s) = clip(1 + ADJ_SLOPE·s, *adj_bounds). Linear, centered at 1.
ADJ_SLOPE = 0.3


# ---------------------------------------------------------------------------
# Parameters (frozen per persona / per deployment)


@dataclass(frozen=True)
class PersonaParams:
    """Slow-engine parameters (mood + cycle). Defaults = DESIGN.md.

    The stability bound of the judge→μ loop is k < 2(1−ρ)/g_max with
    g_max = 1 + A + 3·sigma_eps (engine/validation.py).
    """

    N: int = 10  # mood scale steps (M ∈ 0..N)
    lam: float = 0.60  # λ — base temperament (sigmoid mean without cycle)
    nu: float = math.inf  # ν — beta-binomial volatility; inf ⇒ pure binomial
    k: float = 0.18  # learning of μ from the judge score;
    #   tuned post-Phase 1 (was 0.15)
    rho: float = 0.85  # ρ — daily decay of μ (half-life ~4.3 d);
    #   tuned post-Phase 1 (was 0.70): month-scale memory — isolated
    #   days weigh little, sustained streaks accumulate
    rho_e: float = 0.7  # ρ_e — autocorr. of the endogenous mood η (AR(1));
    #   tuned in Phase 1 (was 0.5) — see results/phase-1-report.md
    sigma_e: float = 0.45  # σ_e — sd of the η innovation;
    #   tuned in Phase 1 (was 0.2) — see results/phase-1-report.md
    B: float = 0.5  # amplitude of the cycle mean shift m(t);
    #   tuned post-Phase 1 (was 0.15), 30-seed sweep
    A: float = 0.25  # amplitude of the cycle reactivity gain g(t)
    sigma_eps: float = 0.03  # σ_ε — daily noise of g(t)
    L_mean: float = 28.0  # mean cycle length (days)
    L_sd: float = 1.5  # sd of the cycle length (redrawn per cycle)
    phi: float = 0.0  # φ — initial phase: initial cycle_day in days.
    #   "random per persona" in DESIGN: the driver may draw it
    #   (uniform in [0, L)); default 0.0 for reproducibility.
    score_neutral: float = 0.0  # judge neutral (calibrated in Phase 3)


@dataclass(frozen=True)
class TimingParams:
    """Timing parameters for spontaneous messages. Defaults = DESIGN.md.

    Convention: `theta_h` and all times in HOURS (see the time convention
    in the module docstring). `quiet_hours=(start, end)` in local time
    [0, 24); if start > end the window crosses midnight (the default case).
    """

    k_w: float = 2.0  # Weibull shape; >1 ⇒ increasing hazard; 1 ⇒ exponential
    theta_h: float = 13.5  # Weibull scale (hours); base mean ≈ θ·Γ(1+1/k_w)
    peak_hour: float = 14.0  # circadian cosine peak (valence and energy)
    diurnal_amp: float = 0.25  # circadian amplitude (logit valence and energy)
    quiet_hours: tuple[float, float] = (23.0, 8.0)  # W0.1 decision
    phase_multipliers: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PHASE_MULTIPLIERS)
    )
    adj_bounds: tuple[float, float] = (0.7, 1.3)  # bound of the score adjustment
    min_gap_min: float = 15.0  # queue guard: minimum gap between messages (min)
    daily_cap: int = 3  # W0.1 decision — queue guard: max proactive/day
    max_gap_h: float = 48.0  # queue guard: maximum silence (DESIGN 48 h)


# ---------------------------------------------------------------------------
# Mutable engine state (evolves day by day)


@dataclass
class CycleState:
    """Hormonal cycle clock.

    `cycle_day` ∈ [0, L_current) advances 1.0 per day; on completing the
    cycle (cycle_day >= L_current) L_current is subtracted and
    L_current ~ Normal(L_mean, L_sd) is redrawn (truncated to >= 1). φ
    enters as the initial cycle_day.
    """

    cycle_day: float
    L_current: float


@dataclass
class MoodState:
    """Slow mood state: event memory μ and endogenous mood η."""

    mu: float = 0.0
    eta: float = 0.0


# ---------------------------------------------------------------------------
# Simulation records


@dataclass(frozen=True)
class DayRecord:
    """Snapshot of one simulated day.

    Semantics: `m, g, arg, p, M` are those of day `t`; `mu, eta` are the
    values USED that day (state at the start of the day, before the
    end-of-day mood.update / mood.step_endogenous); `score` is the
    (synthetic) score received that day; `cycle_day`/`phase_label` the
    day's; `seed` is the companion's master seed — the day's RNG is
    rebuilt with engine.rng.day_rng(seed, t).
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
    """Result of a day-by-day run (driver W2.1)."""

    params: PersonaParams
    variant: MoodVariant
    records: list[DayRecord]

    # -- array properties (derived from records) --------------------

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
    def seed(self) -> int:
        """Master seed of the run (constant across all records)."""
        return self.records[0].seed if self.records else -1
