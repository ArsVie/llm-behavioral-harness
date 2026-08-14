"""Decision probe v2 — engine-driven mood brief sampler (A1).

Every ``MoodDose`` here comes from the REAL engine chain, never from
hand-set labels or prose:

    engine.cycle.step + engine.mood.step (seeded via engine.rng helpers)
        -> DayRecord
    harness.behavior.derive_behavior(record, timing, hour=..., mood_scale=10)
        -> BehaviorDirective
    dose.brief = directive.prompt_brief VERBATIM (the state-card mood line)
    dose.availability = harness.assembler._availability_line(
                            harness.actuation.to_brief(directive))
        (read-only call into the real state-card availability renderer —
         the BehaviorBrief is built from the directive's channels exactly
         as the runtime does; no template replication.)

``engine/`` and ``harness/behavior.py`` are READ-ONLY here: we only call
them. The day loop mirrors the frozen composition order of
``sim/run_daily.py`` (cycle.step must be the FIRST consumption of
``day_rng(seed, t)``; then mood.step; then the synthetic score; then
mood.update + mood.step_endogenous).

Sets (see sample_moods docstring):
1. ``natural``            — ~90 seeded days, one directive per day at the
                            canonical hour (14.0), real (valence, energy)
                            co-movement (momentum is real too: previous-day
                            record is threaded through derive_behavior).
2. ``orthogonal_valence`` — M sweep 0,2,4,6,8,10 at fixed phase/hour: one
                            seeded neutral run, first day whose drawn M
                            matches each target (bounded attempts).
3. ``orthogonal_energy``  — hour sweep 8,12,16,20,23 at fixed M/phase: one
                            real day (M == target, phase == target) rendered
                            at each hour (the hour lever varies energy).
4. ``extremes``           — engine-real high (M >= 9) and low (M <= 1) at
                            the SAME hour, for the pilot.

Hit policy ("bounded attempts, keep first hit per target"): a single
seeded neutral run (canonical synthetic score) of up to ``max_attempts``
days is scanned; the first record matching (target M, phase) per target is
kept. If a target is still missed, the phase constraint is relaxed (logged
in ``engineered["relaxed_phase"]``). If STILL missed, a guided pass runs a
FRESH seeded run with a constant synthetic score (``score=+1`` pushes the
draw envelope up, ``score=-1`` down — the real score channel, real draws)
on a sub-seed derived via ``engine.rng.stream_rng`` (EXPERIMENT_STREAM).
Every fallback is logged in ``engineered``; a missed target after all
passes raises a loud error (never a silently hand-set record).

CLI::

    .venv/bin/python -m experiments.probe_moods --set natural --seed 20260814
        --out results/decision-probe-v2-2026-08-14/mood_samples.json
    .venv/bin/python -m experiments.probe_moods --sets all --seed 20260814

The output file is a JSON array of MoodDose dicts; runs APPEND to it with
``set_kind`` tagged, and a rerun of the same (set_kind, dose_id) replaces
the previous dose (idempotent). The run ends with the printed summary
(per-set counts, valence/energy ranges, distinct briefs by brief_hash,
Pearson r of (valence, energy) in the natural set) — the steer signal for
the v2 grid orchestrator.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np

import engine.rng as rng_mod  # read-only: hierarchical SeedSequence helpers
from engine import cycle, mood  # read-only: engine steps
from engine.types import DayRecord, MoodState, MoodVariant, PersonaParams, TimingParams
from experiments.probe_schema import MOOD_SCALE, MoodDose
from harness.actuation import to_brief  # read-only: directive -> BehaviorBrief
from harness.assembler import _availability_line  # read-only: energy tier prose
from harness.behavior import BehaviorDirective, derive_behavior  # read-only
from sim.run_daily import synthetic_score  # read-only: canonical score draw

# --------------------------------------------------------------------------- #
# Set registry and canonical defaults
# --------------------------------------------------------------------------- #

SET_KINDS: tuple[str, ...] = (
    "natural",
    "orthogonal_valence",
    "orthogonal_energy",
    "extremes",
)

DEFAULT_OUT = Path("results/decision-probe-v2-2026-08-14/mood_samples.json")

#: Scalar fields of BehaviorDirective logged in MoodDose.vector (verbatim
#: order from probe_schema.py). prompt_brief is a string and lives in
#: MoodDose.brief; the trace lives in MoodDose.trace.
VECTOR_FIELDS: tuple[str, ...] = (
    "valence",
    "energy",
    "momentum",
    "reactivity",
    "warmth",
    "expressiveness",
    "playfulness",
    "reflectiveness",
    "initiative",
    "response_length_scale",
    "response_delay_s",
    "closing_tendency",
)


# --------------------------------------------------------------------------- #
# Engine runner (read-only composition, frozen order)
# --------------------------------------------------------------------------- #


def run_engine(
    seed: int,
    days: int,
    *,
    score_const: float | None = None,
) -> list[DayRecord]:
    """Run the real engine ``days`` days from ``seed``.

    Mirrors the frozen per-day composition of ``sim/run_daily.run``:
    ``cycle.step`` is the FIRST consumption of ``day_rng(seed, t)``, then
    ``mood.step``, then the synthetic score, then ``mood.update`` +
    ``mood.step_endogenous``. ``score_const`` overrides the score channel
    (real engine channel, used only by the guided fallback).
    """
    persona = PersonaParams()
    variant = MoodVariant.DECOUPLED_OFFSETS
    cycle_state = cycle.init_state(persona, rng_mod.init_rng(seed))
    mood_state = MoodState()
    records: list[DayRecord] = []
    for t in range(days):
        rng_t = rng_mod.day_rng(seed, t)
        cycle_day_today = cycle_state.cycle_day
        m, g, phase_label, cycle_next = cycle.step(cycle_state, persona, rng_t)
        M, p, arg = mood.step(mood_state, persona, m, g, variant, rng_t)
        if score_const is None:
            score = synthetic_score(M, persona.N, rng_t)
        else:
            score = synthetic_score(M, persona.N, rng_t, override=score_const)
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
    return records


# --------------------------------------------------------------------------- #
# Dose assembly
# --------------------------------------------------------------------------- #


def _availability(directive: BehaviorDirective) -> str | None:
    """State-card availability prose via the REAL renderer (read-only call):
    BehaviorBrief built from the directive's channels (harness.actuation.
    to_brief), then the 3-tier energy template selection
    (harness.assembler._availability_line: energy > 0.7 / < 0.35)."""
    return _availability_line(to_brief(directive))


def _make_dose(
    set_kind: str,
    dose_id: str,
    record: DayRecord,
    directive: BehaviorDirective,
    engineered: dict,
) -> MoodDose:
    return MoodDose(
        dose_id=dose_id,
        set_kind=set_kind,
        engineered=engineered,
        record=dataclasses.asdict(record),
        vector={name: getattr(directive, name) for name in VECTOR_FIELDS},
        trace=dataclasses.asdict(directive.trace),
        brief=directive.prompt_brief,
        availability=_availability(directive),
        brief_hash=hashlib.sha1(directive.prompt_brief.encode("utf-8")).hexdigest(),
    )


def _levers(
    *,
    M: int | None,
    hour: float | None,
    phase: str | None,
    run_seed: int,
    scanned_days: int,
    score: float | None = None,
    relaxed_phase: bool = False,
) -> dict:
    """Engineered levers dict (canonical five keys + additive run metadata)."""
    levers: dict = {
        "M": M,
        "hour": hour,
        "phase": phase,
        "mu": None,
        "eta": None,
        "run_seed": run_seed,
        "scanned_days": scanned_days,
        "relaxed_phase": relaxed_phase,
    }
    if score is not None:
        levers["score"] = score
    return levers


def _first_hits(
    records: list[DayRecord],
    targets: list[int],
    phase: str | None,
) -> tuple[dict[int, DayRecord], list[int], set[int]]:
    """First record per target matching (M, phase); phase-relaxed fallback
    candidates are remembered per target. Returns (hits, missed, relaxed)."""
    hits: dict[int, DayRecord | None] = {t: None for t in targets}
    relaxed_first: dict[int, DayRecord] = {}
    for r in records:
        if r.M in hits and hits[r.M] is None:
            if phase is None or r.phase_label == phase:
                hits[r.M] = r
            elif r.M not in relaxed_first:
                relaxed_first[r.M] = r
    missed = [t for t in targets if hits[t] is None]
    relaxed_used: set[int] = set()
    for t in missed:
        if t in relaxed_first:
            hits[t] = relaxed_first[t]
            relaxed_used.add(t)
    missed = [t for t in targets if hits[t] is None]
    result: dict[int, DayRecord] = {}
    for t in targets:
        r = hits[t]
        if r is not None:
            result[t] = r
    return result, missed, relaxed_used


def _guided_subseed(master_seed: int, set_index: int, target_index: int) -> int:
    """Derived master seed for guided fallback runs (engine.rng helper)."""
    return int(
        rng_mod.stream_rng(
            master_seed, rng_mod.EXPERIMENT_STREAM, set_index, target_index
        ).integers(0, 2**31)
    )


# --------------------------------------------------------------------------- #
# Sets
# --------------------------------------------------------------------------- #


def _sample_natural(seed: int, cfg: dict) -> list[MoodDose]:
    days = int(cfg.get("days", 90))
    hour = float(cfg.get("hour", 14.0))
    timing = TimingParams()
    records = run_engine(seed, days)
    doses: list[MoodDose] = []
    previous: DayRecord | None = None
    for r in records:
        directive = derive_behavior(
            r, timing, hour=hour, mood_scale=MOOD_SCALE, previous=previous
        )
        engineered = _levers(
            M=None, hour=hour, phase=None, run_seed=seed, scanned_days=days
        )
        doses.append(_make_dose("natural", f"nat-d{r.t}", r, directive, engineered))
        previous = r
    return doses


def _sample_orthogonal_valence(seed: int, cfg: dict) -> list[MoodDose]:
    targets = [int(t) for t in cfg.get("targets", [0, 2, 4, 6, 8, 10])]
    phase = cfg.get("phase", "follicular")
    hour = float(cfg.get("hour", 14.0))
    max_attempts = int(cfg.get("max_attempts", 3000))
    timing = TimingParams()

    neutral = run_engine(seed, max_attempts)
    hits, missed, relaxed_used = _first_hits(neutral, targets, phase)

    for ti, target in enumerate(missed):
        # Guided pass: fresh run, constant score pushes the draw envelope.
        run_seed = _guided_subseed(seed, 1, ti)
        score_const = 1.0 if target >= MOOD_SCALE // 2 else -1.0
        guided = run_engine(run_seed, max_attempts, score_const=score_const)
        g_hits, g_missed, g_relaxed = _first_hits(guided, [target], phase)
        if target in g_missed:
            raise RuntimeError(
                f"orthogonal_valence: no real DayRecord with M={target} "
                f"(phase={phase}) after neutral + guided (score={score_const}) "
                f"passes of {max_attempts} days each"
            )
        hits[target] = g_hits[target]
        if target in g_relaxed:
            relaxed_used.add(target)

    doses: list[MoodDose] = []
    for target in targets:
        r = hits[target]
        directive = derive_behavior(r, timing, hour=hour, mood_scale=MOOD_SCALE)
        engineered = _levers(
            M=target,
            hour=hour,
            phase=phase,
            run_seed=r.seed,
            scanned_days=max_attempts,
            relaxed_phase=target in relaxed_used,
        )
        doses.append(_make_dose("orthogonal_valence", f"val-M{target}", r, directive, engineered))
    return doses


def _sample_orthogonal_energy(seed: int, cfg: dict) -> list[MoodDose]:
    hours = [float(h) for h in cfg.get("hours", [8, 12, 16, 20, 23])]
    m_fixed = int(cfg.get("M", 5))
    phase = cfg.get("phase", "menstrual")
    max_attempts = int(cfg.get("max_attempts", 3000))
    timing = TimingParams()

    neutral = run_engine(seed, max_attempts)
    hits, missed, relaxed_used = _first_hits(neutral, [m_fixed], phase)
    if missed:
        run_seed = _guided_subseed(seed, 2, 0)
        guided = run_engine(run_seed, max_attempts, score_const=0.0)
        g_hits, g_missed, g_relaxed = _first_hits(guided, [m_fixed], phase)
        if g_missed:
            raise RuntimeError(
                f"orthogonal_energy: no real DayRecord with M={m_fixed} "
                f"(phase={phase}) after neutral + guided passes of "
                f"{max_attempts} days each"
            )
        hits[m_fixed] = g_hits[m_fixed]
        relaxed_used |= g_relaxed

    r = hits[m_fixed]
    doses: list[MoodDose] = []
    for hour in hours:
        directive = derive_behavior(r, timing, hour=hour, mood_scale=MOOD_SCALE)
        engineered = _levers(
            M=m_fixed,
            hour=hour,
            phase=phase,
            run_seed=r.seed,
            scanned_days=max_attempts,
            relaxed_phase=m_fixed in relaxed_used,
        )
        doses.append(_make_dose("orthogonal_energy", f"ene-h{int(hour)}", r, directive, engineered))
    return doses


def _sample_extremes(seed: int, cfg: dict) -> list[MoodDose]:
    hour = float(cfg.get("hour", 14.0))
    max_attempts = int(cfg.get("max_attempts", 3000))
    timing = TimingParams()
    # Preferred exact anchors for the pilot pair (valence +1.0 / -1.0);
    # relaxed bands are the fallback (logged via relaxed_phase).
    bands = {"high": (10, 10), "low": (0, 0)}
    relaxed_bands = {"high": (9, 10), "low": (0, 1)}

    neutral = run_engine(seed, max_attempts)
    picked: dict[str, DayRecord] = {}
    relaxed_used: set[str] = set()
    for name, (lo, hi) in bands.items():
        rec = next((r for r in neutral if lo <= r.M <= hi), None)
        if rec is None:
            rlo, rhi = relaxed_bands[name]
            rec = next((r for r in neutral if rlo <= r.M <= rhi), None)
            if rec is not None:
                relaxed_used.add(name)
        picked[name] = rec
    for name, (lo, hi) in bands.items():
        if picked[name] is not None:
            continue
        run_seed = _guided_subseed(seed, 3, 0 if name == "high" else 1)
        score_const = 1.0 if name == "high" else -1.0
        guided = run_engine(run_seed, max_attempts, score_const=score_const)
        rlo, rhi = relaxed_bands[name]
        rec = next((r for r in guided if lo <= r.M <= hi), None)
        if rec is None:
            rec = next((r for r in guided if rlo <= r.M <= rhi), None)
            if rec is not None:
                relaxed_used.add(name)
        if rec is None:
            raise RuntimeError(
                f"extremes: no real DayRecord with M in [{rlo}, {rhi}] after "
                f"neutral + guided (score={score_const}) passes of "
                f"{max_attempts} days each"
            )
        picked[name] = rec

    doses: list[MoodDose] = []
    for name in ("high", "low"):
        r = picked[name]
        directive = derive_behavior(r, timing, hour=hour, mood_scale=MOOD_SCALE)
        engineered = _levers(
            M=r.M,
            hour=hour,
            phase=None,
            run_seed=r.seed,
            scanned_days=max_attempts,
            relaxed_phase=name in relaxed_used,
        )
        engineered["band"] = bands[name]
        doses.append(_make_dose("extremes", f"ext-M{r.M}", r, directive, engineered))
    return doses


# --------------------------------------------------------------------------- #
# Frozen interface (probe_schema.py delegates here)
# --------------------------------------------------------------------------- #

_SAMPLERS: dict[str, Callable[[int, dict], list[MoodDose]]] = {
    "natural": _sample_natural,
    "orthogonal_valence": _sample_orthogonal_valence,
    "orthogonal_energy": _sample_orthogonal_energy,
    "extremes": _sample_extremes,
}


def sample_moods(set_kind: str, seed: int, **cfg: Any) -> list[MoodDose]:
    """Engine-driven brief sampler (frozen interface of probe_schema.py).

    ``set_kind``: natural | orthogonal_valence | orthogonal_energy |
    extremes. ``seed``: master engine seed. ``cfg`` knobs (all optional):
    ``days`` (natural, default 90), ``hour`` (canonical hour, default 14.0),
    ``targets`` (valence M sweep, default [0,2,4,6,8,10]), ``hours``
    (energy hour sweep, default [8,12,16,20,23]), ``M`` (energy-sweep fixed
    M, default 5), ``phase`` (default: follicular for the valence sweep,
    menstrual for the energy sweep), ``max_attempts`` (scan bound, default
    3000). Returns real engine-chain MoodDose objects only.
    """
    if set_kind not in _SAMPLERS:
        raise ValueError(
            f"unknown set_kind {set_kind!r}; expected one of {SET_KINDS}"
        )
    return _SAMPLERS[set_kind](seed, dict(cfg))


# --------------------------------------------------------------------------- #
# JSON persistence (append, replace same (set_kind, dose_id))
# --------------------------------------------------------------------------- #


def load_doses(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON array of MoodDose dicts")
    return data


def merge_doses(existing: list[dict], new: list[MoodDose]) -> list[dict]:
    out = list(existing)
    index = {(d["set_kind"], d["dose_id"]): i for i, d in enumerate(out)}
    for dose in new:
        d = dataclasses.asdict(dose)
        key = (d["set_kind"], d["dose_id"])
        if key in index:
            out[index[key]] = d
        else:
            index[key] = len(out)
            out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Summary (the steer signal)
# --------------------------------------------------------------------------- #


def _pearson(x: list[float], y: list[float]) -> float:
    if len(x) < 2 or len(set(x)) < 2 or len(set(y)) < 2:
        return math.nan
    mx, my = sum(x) / len(x), sum(y) / len(y)
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    var_x = sum((a - mx) ** 2 for a in x)
    var_y = sum((b - my) ** 2 for b in y)
    return cov / math.sqrt(var_x * var_y)


def summarize(doses: list[dict]) -> dict:
    by_kind: dict[str, list[dict]] = {}
    for d in doses:
        by_kind.setdefault(d["set_kind"], []).append(d)
    summary: dict = {"sets": {}}
    for kind in SET_KINDS:
        group = by_kind.get(kind, [])
        if not group:
            summary["sets"][kind] = {"count": 0}
            continue
        valences = [d["vector"]["valence"] for d in group]
        energies = [d["vector"]["energy"] for d in group]
        hashes = {d["brief_hash"] for d in group}
        entry: dict = {
            "count": len(group),
            "valence_range": [min(valences), max(valences)],
            "energy_range": [min(energies), max(energies)],
            "distinct_briefs": len(hashes),
        }
        if kind == "natural":
            entry["pearson_valence_energy"] = _pearson(valences, energies)
        summary["sets"][kind] = entry
    summary["total_doses"] = len(doses)
    summary["total_distinct_briefs"] = len({d["brief_hash"] for d in doses})
    return summary


def print_summary(summary: dict) -> None:
    print("\n=== probe_moods summary (steer signal) ===")
    for kind, entry in summary["sets"].items():
        if entry["count"] == 0:
            print(f"  {kind:20s} count=0")
            continue
        parts = [
            f"count={entry['count']}",
            f"valence=[{entry['valence_range'][0]:+.2f},{entry['valence_range'][1]:+.2f}]",
            f"energy=[{entry['energy_range'][0]:.3f},{entry['energy_range'][1]:.3f}]",
            f"distinct_briefs={entry['distinct_briefs']}",
        ]
        if "pearson_valence_energy" in entry:
            r = entry["pearson_valence_energy"]
            parts.append(f"pearson_r(valence,energy)={r:+.3f}" if not math.isnan(r)
                         else "pearson_r(valence,energy)=nan")
        print(f"  {kind:20s} {' | '.join(parts)}")
    print(f"  {'total':20s} doses={summary['total_doses']} "
          f"distinct_briefs={summary['total_distinct_briefs']}")
    print("============================================")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe_moods",
        description="Decision probe v2: engine-driven mood brief sampler (A1).",
    )
    parser.add_argument(
        "--set",
        choices=list(SET_KINDS),
        default=None,
        help="one set kind to sample",
    )
    parser.add_argument(
        "--sets",
        nargs="*",
        default=None,
        metavar="KIND",
        help="'all' or a list of set kinds (e.g. --sets natural extremes)",
    )
    parser.add_argument("--seed", type=int, default=20260814)
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="JSON array output (appends, set_kind-tagged)",
    )
    args = parser.parse_args(argv)

    if args.set and args.sets:
        parser.error("use either --set or --sets, not both")
    if args.sets:
        kinds = list(SET_KINDS) if "all" in args.sets else list(args.sets)
    else:
        kinds = [args.set] if args.set else list(SET_KINDS)

    unknown = [k for k in kinds if k not in SET_KINDS]
    if unknown:
        parser.error(f"unknown set kind(s): {unknown}")

    out_path = args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_doses(out_path)

    for kind in kinds:
        doses = sample_moods(kind, args.seed)
        existing = merge_doses(existing, doses)
        print(
            f"sampled {len(doses):3d} doses for {kind:20s} "
            f"(seed={args.seed}, run_seed per dose in engineered)"
        )

    out_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    summary = summarize(existing)
    summary_path = out_path.with_name("summary.json")
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out_path.resolve()}")
    print(f"wrote {summary_path.resolve()}")
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
