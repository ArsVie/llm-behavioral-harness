"""Events driver: timing + envelope + phase + adj, with guards (W2.2 — Wave 2).

OWNERSHIP: task W2.2 (this file + tests/test_run_events.py). Implemented
AFTER Wave 1; may import engine.timing, engine.circadian,
engine.cycle, engine.rng (composition allowed only in drivers).

Modulator composition (frozen; B5 extends it ADDITIVELY as opt-in):
    modulator(t_h) = circadian.envelope(t_h % 24, timing)
                   × timing.phase_multipliers[phase_label(day of t_h)]
                   × adj_from_score(score of the day BEFORE t_h)
                   × state_factors[day of t_h]        (B5; None ⇒ 1.0)
    adj_from_score(s) = clip(1 + types.ADJ_SLOPE·s, *timing.adj_bounds);
    s = None (no score, e.g. day 0 or scores not given) ⇒ 1.0.
    state_factors (B5, orchestrator carve-out): per-day array (days,) with the
    state term exp(w·(x−x₀)) precomputed by the caller (the
    scheduler derives it from the day's directive). Default None ⇒
    composition byte-identical to pre-B5 for every caller that does not opt in.

Phases per day: its own cycle instance with the SAME replay contract as
run_daily — cycle.init_state(persona, engine.rng.init_rng(seed)) and, per day,
cycle.step as the FIRST consumption of day_rng(seed, t) ⇒ the same phase
sequence as run_daily with the same seed.

Sampling: engine.timing.next_event with the stream's own rng
engine.rng.stream_rng(seed, engine.rng.EVENTS_STREAM), advancing t_now to the
candidate after each acceptance/discard.

QUEUE GUARDS (out-of-process, frozen):
  - envelope < 1e-9 at the candidate ⇒ discard (belt: quiet hours are
    already 0 by construction);
  - gap < timing.min_gap_min/60 since the last ACCEPTED ⇒ discard;
  - the candidate's day already has timing.daily_cap accepted ⇒ discard;
  - silence > timing.max_gap_h ⇒ force a contact at the first instant
    after (t_last + max_gap_h) with envelope >= 0.5 that respects
    min_gap and daily_cap.
  τ (the hazard clock) is reset ONLY on accepted events (including those
  forced by max_gap). In Phase 1 there are no user messages.

Determinism: same seed ⇒ same array of times.
"""
from __future__ import annotations

import argparse

import numpy as np

import engine.rng as rng_mod
from engine import circadian, cycle, timing as timing_mod
from engine.types import ADJ_SLOPE, PersonaParams, TimingParams

#: Search step (hours) for the instant forced by the max-gap guard.
_FORCE_STEP_H = 0.25

#: Minimum envelope required at the instant forced by max-gap (an "actually"
#: awake window, not a marginal instant of the ramp).
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
    """Day-by-day phases with the same replay contract as run_daily.

    cycle.init_state consumes engine.rng.init_rng(seed); for each day t,
    day_rng(seed, t) is the FIRST (and only, here) consumption of the day,
    fed to cycle.step ⇒ the same phase sequence that run_daily would
    produce with the same seed.
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
    state_factors: np.ndarray | None = None,
):
    """Composes envelope × phase_multiplier × adj_from_score(previous day's score)
    × state_factors[day] (B5 — opt-in state term; None ⇒ 1.0)."""
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
        state = float(state_factors[day]) if state_factors is not None else 1.0
        return env * phase_mult * adj * state

    return modulator


def _mod_ub(timing: TimingParams, state_factors: np.ndarray | None = None) -> float:
    """Upper bound of the modulator: 1.0 × max(phase_multipliers) × adj_bounds[1]
    × max(state_factors) (B5; without state_factors ⇒ 1.0 — byte-identical).

    The thinning majorant must bound the modulator at ALL t: with
    coupling active it is multiplied by the per-day array's maximum (the
    scheduler clips it to STATE_FACTOR_BOUNDS; the defensive max keeps the
    thinning correct for any caller).
    """
    ub = 1.0 * max(timing.phase_multipliers.values()) * timing.adj_bounds[1]
    if state_factors is not None and state_factors.size:
        ub *= float(np.max(state_factors))
    return ub


def _find_forced_time(
    t_last_h: float,
    horizon_h: float,
    modulator,
    timing: TimingParams,
    day_counts: dict[int, int],
) -> float | None:
    """First instant u > t_last_h + max_gap_h with envelope(u % 24) >= 0.5 that
    respects min_gap (trivial: u > t_last_h + max_gap_h >= min_gap) and daily_cap
    (if the day is full, it keeps advancing). None if it goes past the horizon.
    """
    min_gap_h = timing.min_gap_min / 60.0
    u = t_last_h + timing.max_gap_h
    # Ensures min_gap is respected even if max_gap_h were smaller than min_gap_h
    # (does not happen with the defaults, but it is a cheap bound to keep).
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
    state_factors: np.ndarray | None = None,
) -> np.ndarray:
    """Stream of `days` days of ACCEPTED proactive events.

    `scores`: optional, shape (days,), synthetic score for each day (feeds the
    next day's adj); None ⇒ adj ≡ 1. `state_factors` (B5): optional,
    shape (days,), multiplicative per-day state term (exp(w·(x−x₀))
    precomputed by the caller); None ⇒ composition byte-identical to
    pre-B5. Returns sorted absolute hours (np.ndarray float, in
    [0, days·24)).
    """
    if persona is None:
        persona = PersonaParams()
    if timing is None:
        timing = TimingParams()

    if state_factors is not None:
        state_factors = np.asarray(state_factors, dtype=float)
        if state_factors.ndim != 1 or state_factors.shape[0] != days:
            raise ValueError(
                f"state_factors must be a 1-D array of length days={days}, "
                f"got shape {state_factors.shape}"
            )

    horizon_h = days * 24.0
    phase_labels = _precompute_phase_labels(days, seed, persona)
    modulator = _make_modulator(days, phase_labels, timing, scores, state_factors)
    mod_ub = _mod_ub(timing, state_factors)
    min_gap_h = timing.min_gap_min / 60.0

    ev_rng = rng_mod.stream_rng(seed, rng_mod.EVENTS_STREAM)

    accepted: list[float] = []
    day_counts: dict[int, int] = {}
    t_now = 0.0
    t_last = 0.0  # convention: "last interaction" at the start of the simulation

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

        # MAX-GAP: silence > max_gap_h implied by the candidate (or inf).
        if (cand - t_last) > timing.max_gap_h:
            forced = _find_forced_time(
                t_last, horizon_h, modulator, timing, day_counts
            )
            if forced is not None:
                _accept(forced)
                t_last = forced
                t_now = forced
                continue
            # No forced instant within the horizon: finish.
            break

        if cand >= horizon_h:
            break

        # Guard b) envelope ~0 at the candidate.
        if circadian.envelope(cand % 24.0, timing) < 1e-9:
            t_now = cand
            continue

        # Guard c) minimum gap since the last accepted.
        if (cand - t_last) < min_gap_h:
            t_now = cand
            continue

        # Guard d) daily cap already reached.
        cand_day = int(cand // 24.0)
        if day_counts.get(cand_day, 0) >= timing.daily_cap:
            t_now = cand
            continue

        # Accepted.
        _accept(cand)
        t_last = cand
        t_now = cand

    return np.asarray(sorted(accepted), dtype=float)


def main(argv: list[str] | None = None) -> int:
    """Minimal CLI: --days --seed; prints the number of events and the daily rate."""
    parser = argparse.ArgumentParser(description="Simulates proactive events.")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--seed", type=int, default=12345)
    args = parser.parse_args(argv)

    events = run(args.days, args.seed)
    n = len(events)
    rate = n / args.days if args.days > 0 else 0.0
    print(f"events: {n}")
    print(f"daily rate: {rate:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
