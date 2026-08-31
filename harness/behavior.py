"""Traducción del estado latente a comportamiento observable y sutil.

El motor decide el estado; este módulo decide cómo puede notarse. La fase del
ciclo se conserva solo en la traza de depuración. El brief describe la
experiencia resultante y nunca pide al modelo anunciar números, hormonas ni una
etiqueta de ánimo.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.circadian import energy as circadian_energy
from engine.types import DayRecord, TimingParams


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class BehaviorTrace:
    """Causas auditables que no se exponen al prompt conversacional."""

    phase_label: str
    hormonal_gain: float
    event_memory: float
    endogenous_tone: float
    mood_delta: float


@dataclass(frozen=True)
class BehaviorDirective:
    """Controles continuos que un canal o ensamblador puede aplicar."""

    valence: float
    energy: float
    momentum: float
    reactivity: float
    warmth: float
    expressiveness: float
    playfulness: float
    reflectiveness: float
    initiative: float
    response_length_scale: float
    response_delay_s: float
    closing_tendency: float
    prompt_brief: str
    trace: BehaviorTrace


def _render_brief(
    *,
    valence: float,
    energy: float,
    momentum: float,
    warmth: float,
    playfulness: float,
    reflectiveness: float,
) -> str:
    if valence > 0.9:
        bearing = "radiant"
    elif valence > 0.65:
        bearing = "buoyant"
    elif valence > 0.35:
        bearing = "bright"
    elif valence > 0.05:
        bearing = "even"
    elif valence > -0.25:
        bearing = "quiet"
    elif valence > -0.55:
        bearing = "tender"
    elif valence > -0.85:
        bearing = "somber"
    else:
        bearing = "heavy"

    if energy > 0.83:
        pace = "vibrant"
    elif energy > 0.67:
        pace = "energized"
    elif energy > 0.5:
        pace = "lively"
    elif energy > 0.33:
        pace = "calm"
    elif energy > 0.17:
        pace = "subdued"
    else:
        pace = "drained"

    if momentum > 0.2:
        continuity = "There is a gentle sense of opening up compared with recently."
    elif momentum < -0.2:
        continuity = "Let the recent dip show as restraint, without withdrawing affection."
    else:
        continuity = "Carry a sense of emotional continuity from the recent past."

    # These lines render verbatim into the system prompt; the forbidden-
    # token battery scans for raw substrings of engine internals.
    if playfulness > reflectiveness + 0.12:
        texture = "Favor light wit and small spontaneous touches over big declarations."
    elif reflectiveness > playfulness + 0.12:
        texture = "Favor thoughtful pauses, precise words, and one sincere touch."
    else:
        texture = "Balance lightness with one grounded, personal touch."

    care = (
        "Keep care intact; warmth should remain visible even when the mood is subdued."
        if warmth < 0.62
        else "Keep the affection natural, specific, and free of exaggerated sweetness."
    )
    return " ".join(
        (
            f"Current bearing: {bearing}, {pace}.",
            continuity,
            texture,
            care,
            "Do not name or explain the internal state; show it through cadence, word choice, initiative, and conversational length.",
        )
    )


def derive_behavior(
    record: DayRecord,
    timing: TimingParams,
    *,
    hour: float,
    mood_scale: int = 10,
    previous: DayRecord | None = None,
) -> BehaviorDirective:
    """Deriva una directiva determinista desde estado diario, hora e historia.

    Valencia y energía son ortogonales. ``g`` modula la sensibilidad al cambio,
    no el afecto base; así una fase reactiva se nota en expresividad sin convertir
    una etiqueta hormonal en un estereotipo de personalidad.
    """

    if mood_scale <= 0:
        raise ValueError("mood_scale debe ser positivo")

    valence = _clip(2.0 * (record.M / mood_scale) - 1.0, -1.0, 1.0)
    energy = circadian_energy(hour, record.phase_label, timing)
    mood_delta = 0.0 if previous is None else (record.M - previous.M) / mood_scale
    momentum = _clip(2.0 * mood_delta, -1.0, 1.0)

    normalized_gain = _clip((record.g - 0.7) / 0.6)
    reactivity = _clip(0.35 + 0.42 * normalized_gain + 0.20 * abs(momentum))

    warmth = _clip(0.58 + 0.20 * valence + 0.06 * energy + 0.05 * record.mu, 0.35, 0.92)
    expressiveness = _clip(
        0.24 + 0.34 * energy + 0.20 * momentum + 0.14 * reactivity,
        0.12,
        0.95,
    )
    playfulness = _clip(
        0.12 + 0.34 * max(valence, 0.0) + 0.28 * energy - 0.10 * max(-momentum, 0.0)
    )
    reflectiveness = _clip(
        0.24 + 0.34 * (1.0 - energy) + 0.20 * max(-valence, 0.0) + 0.12 * abs(momentum)
    )
    initiative = _clip(
        0.14 + 0.38 * energy + 0.16 * max(valence, 0.0) + 0.08 * record.mu - 0.08 * max(-momentum, 0.0)
    )
    response_length_scale = _clip(
        0.05 + 0.90 * energy + 0.45 * expressiveness,
        0.22,
        1.30,
    )
    response_delay_s = 0.8 + 38.0 * (1.0 - energy) + 5.0 * max(-momentum, 0.0)
    closing_tendency = _clip(
        0.04 + 1.00 * (1.0 - energy) + 0.22 * max(-valence, 0.0),
        0.04,
        0.85,
    )

    trace = BehaviorTrace(
        phase_label=record.phase_label,
        hormonal_gain=record.g,
        event_memory=record.mu,
        endogenous_tone=record.eta,
        mood_delta=mood_delta,
    )
    brief = _render_brief(
        valence=valence,
        energy=energy,
        momentum=momentum,
        warmth=warmth,
        playfulness=playfulness,
        reflectiveness=reflectiveness,
    )
    return BehaviorDirective(
        valence=valence,
        energy=energy,
        momentum=momentum,
        reactivity=reactivity,
        warmth=warmth,
        expressiveness=expressiveness,
        playfulness=playfulness,
        reflectiveness=reflectiveness,
        initiative=initiative,
        response_length_scale=response_length_scale,
        response_delay_s=response_delay_s,
        closing_tendency=closing_tendency,
        prompt_brief=brief,
        trace=trace,
    )

