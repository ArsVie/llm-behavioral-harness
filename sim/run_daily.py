"""Day-by-day driver: cycle + mood + synthetic score (W2.1 — Wave 2).

OWNERSHIP: task W2.1 (this file + tests/test_run_daily.py). Implemented
AFTER Wave 1; may import engine.cycle, engine.mood, engine.rng,
engine.circadian (composition allowed only in drivers).

Synthetic score (frozen in the plan):
    score = clip(2·(M/N − 0.5) + Normal(0, SCORE_NOISE_SD), −1, 1)
    With a scripted override: shocks[t] forces the score for day t (clipped to
    [−1, 1], no noise).

Composition order PER DAY t (frozen — replay contract shared
with run_events; cycle.step must be the FIRST consumption of day_rng(seed, t)):
    1. rng_t = engine.rng.day_rng(seed, t)
    2. m, g, phase_label, cycle_next = cycle.step(cycle_state, persona, rng_t)
    3. M, p, arg = mood.step(mood_state, persona, m, g, variant, rng_t)
    4. score = shocks[t] if t is scripted; otherwise synthetic_score(M, N, rng_t)
    5. DayRecord(t, m, g, arg, p, M, score, mu=μ used, eta=η used,
                 cycle_day of the day, phase_label, seed)
    6. mood_state = mood.update(...); mood_state = mood.step_endogenous(...)
    7. cycle_state = cycle_next
Initialization: cycle_state = cycle.init_state(persona, engine.rng.init_rng(seed));
mood_state = MoodState() (μ=η=0).

Minimal CLI:
    python -m sim.run_daily --days 90 --seed 12345 \
        --variant decoupled_offsets --params params.yaml
    --params: YAML with PersonaParams overrides (flat mapping, optional).
    Prints a summary (mean/sd of M, lag-1 autocorr) to stdout.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys

import yaml

from engine import cycle, mood, rng as rng_mod
from engine.types import MoodState, MoodVariant, PersonaParams, DayRecord, SimResult
from sim.metrics import autocorr_lag1, mean_sd
from harness.score import SCORE_NOISE_SD, synthetic_score  # noqa: F401  (canonical product-layer source; re-exported for backward compat)


def run(
    days: int,
    seed: int,
    variant: MoodVariant,
    persona: PersonaParams | None = None,
    shocks: dict[int, float] | None = None,
) -> SimResult:
    """Run `days` days and return SimResult (deterministic given the seed)."""
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
    """Load PersonaParams overrides from YAML (flat mapping).

    Raises ValueError with a clear message if the YAML contains a key that is
    not a PersonaParams field (so main() can translate it to exit code 2).
    """
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    valid_fields = {f.name for f in dataclasses.fields(PersonaParams)}
    unknown = set(raw.keys()) - valid_fields
    if unknown:
        raise ValueError(
            f"Unknown key(s) in --params: {sorted(unknown)}. "
            f"Valid PersonaParams fields: {sorted(valid_fields)}"
        )

    return dataclasses.replace(PersonaParams(), **raw)


def main(argv: list[str] | None = None) -> int:
    """CLI (see the module docstring). Returns the exit code."""
    parser = argparse.ArgumentParser(
        prog="run_daily", description="Day-by-day driver (cycle + mood + synthetic score)."
    )
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument(
        "--variant",
        type=str,
        default=MoodVariant.DECOUPLED_OFFSETS.value,
        choices=[v.value for v in MoodVariant],
    )
    parser.add_argument("--params", type=str, default=None, help="YAML path with PersonaParams overrides.")

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

    print(f"days: {args.days}")
    print(f"variant: {variant.value}")
    print(f"seed: {args.seed}")
    print(f"M: mean={mean_M:.4f} sd={sd_M:.4f}")
    print(f"autocorr lag-1: {ac1:.4f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
