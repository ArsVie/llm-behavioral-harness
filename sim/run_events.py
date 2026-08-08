"""Driver de eventos: timing + envolvente + fase + adj, con guards (W2.2 — Ola 2).

PROPIEDAD: tarea W2.2 (este archivo + tests/test_run_events.py). Se implementa
DESPUÉS de la Ola 1; puede importar engine.timing, engine.circadian,
engine.cycle, engine.rng (composición permitida solo en drivers).

Composición del modulador (congelada):
    modulator(t_h) = circadian.envelope(t_h % 24, timing)
                   × timing.phase_multipliers[phase_label(día de t_h)]
                   × adj_from_score(score del día ANTERIOR al de t_h)
    adj_from_score(s) = clip(1 + types.ADJ_SLOPE·s, *timing.adj_bounds);
    s = None (sin score, p. ej. día 0 o scores no dados) ⇒ 1.0.

Fases por día: instancia propia de cycle con el MISMO contrato de replay que
run_daily — cycle.init_state(persona, engine.rng.init_rng(seed)) y, por día,
cycle.step como PRIMER consumo de day_rng(seed, t) ⇒ misma secuencia de fases
que run_daily con la misma semilla.

Muestreo: engine.timing.next_event con rng propio del stream
engine.rng.stream_rng(seed, engine.rng.EVENTS_STREAM), avanzando t_now al
candidato tras cada aceptación/descarte.

GUARDS DE COLA (fuera del proceso, congelados):
  - envolvente < 1e-9 en el candidato ⇒ descartar (cinturón: quiet hours ya
    son 0 por construcción);
  - gap < timing.min_gap_min/60 desde el último ACEPTADO ⇒ descartar;
  - día del candidato ya con timing.daily_cap aceptados ⇒ descartar;
  - silencio > timing.max_gap_h ⇒ forzar un contacto en el primer instante
    posterior a (t_last + max_gap_h) con envolvente >= 0.5 que respete
    min_gap y daily_cap.
  τ (reloj del hazard) se resetea SOLO en eventos aceptados (incluidos los
  forzados por max_gap). En Fase 1 no hay mensajes del usuario.

Determinismo: misma semilla ⇒ mismo array de tiempos.
"""
from __future__ import annotations

import argparse

import numpy as np

import engine.rng as rng_mod
from engine import circadian, cycle, timing as timing_mod
from engine.types import ADJ_SLOPE, PersonaParams, TimingParams

#: Paso de búsqueda (horas) para el instante forzado por el guard max-gap.
_FORCE_STEP_H = 0.25

#: Envolvente mínima exigida al instante forzado por max-gap (ventana despierta
#: "de verdad", no un instante marginal de la rampa).
_FORCE_MIN_ENVELOPE = 0.5


def adj_from_score(score: float | None, timing: TimingParams) -> float:
    """clip(1 + ADJ_SLOPE·score, *adj_bounds); None ⇒ 1.0."""
    if score is None:
        return 1.0
    lo, hi = timing.adj_bounds
    return float(np.clip(1.0 + ADJ_SLOPE * score, lo, hi))


def _precompute_phase_labels(
    days: int, seed: int, persona: PersonaParams
) -> list[str]:
    """Fases día a día con el mismo contrato de replay que run_daily.

    cycle.init_state consume engine.rng.init_rng(seed); por cada día t,
    day_rng(seed, t) es el PRIMER (y único, aquí) consumo del día, alimentado
    a cycle.step ⇒ misma secuencia de fases que produciría run_daily con la
    misma semilla.
    """
    state = cycle.init_state(persona, rng_mod.init_rng(seed))
    labels: list[str] = []
    for t in range(days):
        day_generator = rng_mod.day_rng(seed, t)
        _m, _g, phase_label, state = cycle.step(state, persona, day_generator)
        labels.append(phase_label)
    return labels


def _make_modulator(
    days: int,
    phase_labels: list[str],
    timing: TimingParams,
    scores: np.ndarray | None,
):
    """Compone envelope × phase_multiplier × adj_from_score(score del día previo)."""
    last_day_idx = days - 1

    def modulator(t_h: float) -> float:
        day = int(t_h // 24.0)
        if day > last_day_idx:
            day = last_day_idx
        elif day < 0:
            day = 0
        env = circadian.envelope(t_h % 24.0, timing)
        phase_mult = timing.phase_multipliers[phase_labels[day]]
        if scores is not None and day >= 1:
            score = float(scores[day - 1])
        else:
            score = None
        adj = adj_from_score(score, timing)
        return env * phase_mult * adj

    return modulator


def _mod_ub(timing: TimingParams) -> float:
    """Cota superior del modulador: 1.0 × max(phase_multipliers) × adj_bounds[1]."""
    return 1.0 * max(timing.phase_multipliers.values()) * timing.adj_bounds[1]


def _find_forced_time(
    t_last_h: float,
    horizon_h: float,
    modulator,
    timing: TimingParams,
    day_counts: dict[int, int],
) -> float | None:
    """Primer instante u > t_last_h + max_gap_h con envelope(u % 24) >= 0.5 que
    respete min_gap (trivial: u > t_last_h + max_gap_h >= min_gap) y daily_cap
    (si el día está lleno, sigue avanzando). None si se sale del horizonte.
    """
    min_gap_h = timing.min_gap_min / 60.0
    u = t_last_h + timing.max_gap_h
    # Asegura respetar min_gap incluso si max_gap_h fuese menor que min_gap_h
    # (no ocurre con los defaults, pero es una cota barata de mantener).
    if u - t_last_h < min_gap_h:
        u = t_last_h + min_gap_h
    while u < horizon_h:
        env = circadian.envelope(u % 24.0, timing)
        day = int(u // 24.0)
        if env >= _FORCE_MIN_ENVELOPE and day_counts.get(day, 0) < timing.daily_cap:
            return u
        u += _FORCE_STEP_H
    return None


def run(
    days: int,
    seed: int,
    persona: PersonaParams | None = None,
    timing: TimingParams | None = None,
    scores: np.ndarray | None = None,
) -> np.ndarray:
    """Stream de `days` días de eventos proactivos ACEPTADOS.

    `scores`: opcional, shape (days,), score sintético de cada día (alimenta
    adj del día siguiente); None ⇒ adj ≡ 1. Devuelve horas absolutas
    ordenadas (np.ndarray float, en [0, days·24)).
    """
    if persona is None:
        persona = PersonaParams()
    if timing is None:
        timing = TimingParams()

    horizon_h = days * 24.0
    phase_labels = _precompute_phase_labels(days, seed, persona)
    modulator = _make_modulator(days, phase_labels, timing, scores)
    mod_ub = _mod_ub(timing)
    min_gap_h = timing.min_gap_min / 60.0

    ev_rng = rng_mod.stream_rng(seed, rng_mod.EVENTS_STREAM)

    accepted: list[float] = []
    day_counts: dict[int, int] = {}
    t_now = 0.0
    t_last = 0.0  # convención: "última interacción" al inicio de la simulación

    def _accept(t_ev: float) -> None:
        accepted.append(t_ev)
        day_counts[int(t_ev // 24.0)] = day_counts.get(int(t_ev // 24.0), 0) + 1

    while True:
        cand = timing_mod.next_event(
            t_now,
            t_last,
            modulator,
            timing,
            ev_rng,
            mod_ub=mod_ub,
            max_horizon_h=horizon_h - t_now if horizon_h > t_now else 1.0,
        )

        # MAX-GAP: silencio > max_gap_h implicado por el candidato (o inf).
        # (cand - t_last es +inf cuando cand es inf, así que la comparación
        # basta sin distinguir el caso explícitamente.)
        if (cand - t_last) > timing.max_gap_h:
            forced = _find_forced_time(
                t_last, horizon_h, modulator, timing, day_counts
            )
            if forced is not None:
                _accept(forced)
                t_last = forced
                t_now = forced
                continue
            # No hay instante forzado dentro del horizonte: termina.
            break

        if cand >= horizon_h:
            break

        # Guard b) envolvente ~0 en el candidato.
        if circadian.envelope(cand % 24.0, timing) < 1e-9:
            t_now = cand
            continue

        # Guard c) gap mínimo desde el último aceptado.
        if (cand - t_last) < min_gap_h:
            t_now = cand
            continue

        # Guard d) tope diario ya alcanzado.
        cand_day = int(cand // 24.0)
        if day_counts.get(cand_day, 0) >= timing.daily_cap:
            t_now = cand
            continue

        # Aceptado.
        _accept(cand)
        t_last = cand
        t_now = cand

    return np.asarray(sorted(accepted), dtype=float)


def main(argv: list[str] | None = None) -> int:
    """CLI mínima: --days --seed; imprime nº de eventos y tasa diaria."""
    parser = argparse.ArgumentParser(description="Simula eventos proactivos.")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--seed", type=int, default=12345)
    args = parser.parse_args(argv)

    events = run(args.days, args.seed)
    n = len(events)
    rate = n / args.days if args.days > 0 else 0.0
    print(f"eventos: {n}")
    print(f"tasa diaria: {rate:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
