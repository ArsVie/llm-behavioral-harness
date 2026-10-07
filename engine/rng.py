"""Reproducible RNG (Wave 0 — FROZEN after W0.1).

Hierarchical spawn via `numpy.random.SeedSequence`: one master seed per
companion and child generators per (stream, key...). Deterministic replay of
any day: same `master_seed` + same day ⇒ same Generator.
"""
from __future__ import annotations

import numpy as np

#: Reserved streams (first element of the spawn_key).
DAILY_STREAM = 0  # slow engine: one generator per day (mood, cycle)
EVENTS_STREAM = 1  # timing: continuous event stream
EXPERIMENT_STREAM = 2  # auxiliary experiment uses (scripted scores, etc.)
INIT_STREAM = 3  # state initialization (cycle L_0, drawn φ, ...)

#: Harness stream keys (outside the engine): 4 life, 5 persona, 6 conversation,
#: 7 decision, 8 habits; experiments/decision_probe uses 9. Next free: 10.


def init_rng(master_seed: int) -> np.random.Generator:
    """Initialization Generator (separate from the days so they do not collide)."""
    return np.random.default_rng(
        np.random.SeedSequence(master_seed, spawn_key=(INIT_STREAM,))
    )


def day_rng(master_seed: int, day: int) -> np.random.Generator:
    """Generator for day `day` of companion `master_seed`.

    Equivalent to SeedSequence(master_seed, spawn_key=(DAILY_STREAM, day)).
    It is the replay reference: DayRecord.seed + DayRecord.t ⇒ this Generator.
    """
    return np.random.default_rng(
        np.random.SeedSequence(master_seed, spawn_key=(DAILY_STREAM, day))
    )


def stream_rng(master_seed: int, *key: int) -> np.random.Generator:
    """Generic Generator by hierarchical key.

    Examples: stream_rng(seed, EVENTS_STREAM) for the event stream of
    run_events; stream_rng(seed, EXPERIMENT_STREAM, i) for replicas of an
    experiment.
    """
    return np.random.default_rng(np.random.SeedSequence(master_seed, spawn_key=key))
