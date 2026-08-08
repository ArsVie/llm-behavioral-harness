"""Driver día-a-día: cycle + mood + score sintético (W2.1 — Ola 2).

PROPIEDAD: tarea W2.1 (este archivo + tests/test_run_daily.py). Se implementa
DESPUÉS de la Ola 1; puede importar engine.cycle, engine.mood, engine.rng,
engine.circadian (composición permitida solo en drivers).

Score sintético (congelado en el plan):
    score = clip(2·(M/N − 0.5) + Normal(0, SCORE_NOISE_SD), −1, 1)
    Con override por guion: shocks[t] fuerza el score del día t (clipped a
    [−1, 1], sin ruido).

Orden de composición POR DÍA t (congelado — contrato de replay compartido
con run_events; cycle.step debe ser el PRIMER consumo de day_rng(seed, t)):
    1. rng_t = engine.rng.day_rng(seed, t)
    2. m, g, phase_label, cycle_next = cycle.step(cycle_state, persona, rng_t)
    3. M, p, arg = mood.step(mood_state, persona, m, g, variant, rng_t)
    4. score = shocks[t] si t está guionado; si no, synthetic_score(M, N, rng_t)
    5. DayRecord(t, m, g, arg, p, M, score, mu=μ usado, eta=η usado,
                 cycle_day del día, phase_label, seed)
    6. mood_state = mood.update(...); mood_state = mood.step_endogenous(...)
    7. cycle_state = cycle_next
Inicialización: cycle_state = cycle.init_state(persona, engine.rng.init_rng(seed));
mood_state = MoodState() (μ=η=0).

CLI mínima:
    python -m sim.run_daily --days 90 --seed 12345 \
        --variant decoupled_offsets --params params.yaml
    --params: YAML con overrides de PersonaParams (mapping plano, opcional).
    Imprime resumen (media/sd de M, autocorr lag-1) a stdout.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys

import numpy as np
import yaml

from engine import cycle, mood, rng as rng_mod
from engine.types import MoodState, MoodVariant, PersonaParams, DayRecord, SimResult
from sim.metrics import autocorr_lag1, mean_sd

#: sd del ruido del score sintético (congelado en el plan de Fase 1).
SCORE_NOISE_SD = 0.2


def synthetic_score(
    M: int, N: int, rng: np.random.Generator, override: float | None = None
) -> float:
    """Score sintético del día; con `override` devuelve ese valor clipped."""
    if override is not None:
        return float(np.clip(override, -1.0, 1.0))
    raw = 2.0 * (M / N - 0.5) + rng.normal(0.0, SCORE_NOISE_SD)
    return float(np.clip(raw, -1.0, 1.0))


def run(
    days: int,
    seed: int,
    variant: MoodVariant,
    persona: PersonaParams | None = None,
    shocks: dict[int, float] | None = None,
) -> SimResult:
    """Corre `days` días y devuelve SimResult (determinista dado seed)."""
    if persona is None:
        persona = PersonaParams()
    if shocks is None:
        shocks = {}

    cycle_state = cycle.init_state(persona, rng_mod.init_rng(seed))
    mood_state = MoodState()

    records: list[DayRecord] = []
    for t in range(days):
        rng_t = rng_mod.day_rng(seed, t)

        cycle_day_today = cycle_state.cycle_day
        m, g, phase_label, cycle_next = cycle.step(cycle_state, persona, rng_t)
        M, p, arg = mood.step(mood_state, persona, m, g, variant, rng_t)

        if t in shocks:
            score = synthetic_score(M, persona.N, rng_t, override=shocks[t])
        else:
            score = synthetic_score(M, persona.N, rng_t)

        records.append(
            DayRecord(
                t=t,
                m=m,
                g=g,
                arg=arg,
                p=p,
                M=M,
                score=score,
                mu=mood_state.mu,
                eta=mood_state.eta,
                cycle_day=cycle_day_today,
                phase_label=phase_label,
                seed=seed,
            )
        )

        mood_state = mood.update(mood_state, persona, score)
        mood_state = mood.step_endogenous(mood_state, persona, rng_t)
        cycle_state = cycle_next

    return SimResult(params=persona, variant=variant, records=records)


def _load_persona_overrides(path: str) -> PersonaParams:
    """Carga overrides de PersonaParams desde YAML (mapping plano).

    Lanza ValueError con mensaje claro si el YAML contiene una llave que no
    es un campo de PersonaParams (para que main() la traduzca a exit code 2).
    """
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    valid_fields = {f.name for f in dataclasses.fields(PersonaParams)}
    unknown = set(raw.keys()) - valid_fields
    if unknown:
        raise ValueError(
            f"Llave(s) desconocida(s) en --params: {sorted(unknown)}. "
            f"Campos válidos de PersonaParams: {sorted(valid_fields)}"
        )

    return dataclasses.replace(PersonaParams(), **raw)


def main(argv: list[str] | None = None) -> int:
    """CLI (ver docstring del módulo). Devuelve exit code."""
    parser = argparse.ArgumentParser(
        prog="run_daily", description="Driver día-a-día (cycle + mood + score sintético)."
    )
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument(
        "--variant",
        type=str,
        default=MoodVariant.DECOUPLED_OFFSETS.value,
        choices=[v.value for v in MoodVariant],
    )
    parser.add_argument("--params", type=str, default=None, help="Ruta YAML con overrides de PersonaParams.")

    args = parser.parse_args(argv)

    if args.params is not None:
        try:
            persona = _load_persona_overrides(args.params)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        persona = PersonaParams()

    variant = MoodVariant(args.variant)

    result = run(days=args.days, seed=args.seed, variant=variant, persona=persona)

    mean_M, sd_M = mean_sd(result.M)
    ac1 = autocorr_lag1(result.M)

    print(f"días: {args.days}")
    print(f"variante: {variant.value}")
    print(f"semilla: {args.seed}")
    print(f"M: media={mean_M:.4f} sd={sd_M:.4f}")
    print(f"autocorr lag-1: {ac1:.4f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
