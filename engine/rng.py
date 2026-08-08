"""RNG reproducible (Ola 0 — CONGELADO tras W0.1).

Spawn jerárquico por `numpy.random.SeedSequence`: una semilla maestra por
companion y generadores hijos por (stream, clave...). Replay determinista de
cualquier día: mismo `master_seed` + mismo día ⇒ mismo Generator.
"""
from __future__ import annotations

import numpy as np

#: Streams reservados (primer elemento del spawn_key).
DAILY_STREAM = 0  # motor lento: un generador por día (mood, cycle)
EVENTS_STREAM = 1  # temporización: stream continuo de eventos
EXPERIMENT_STREAM = 2  # usos auxiliares de experimentos (scores guionados, etc.)
INIT_STREAM = 3  # inicialización de estado (L_0 del ciclo, φ sorteada, ...)


def init_rng(master_seed: int) -> np.random.Generator:
    """Generator de inicialización (separado de los días para no colisionar)."""
    return np.random.default_rng(
        np.random.SeedSequence(master_seed, spawn_key=(INIT_STREAM,))
    )


def day_rng(master_seed: int, day: int) -> np.random.Generator:
    """Generator del día `day` del companion `master_seed`.

    Equivale a SeedSequence(master_seed, spawn_key=(DAILY_STREAM, day)).
    Es la referencia de replay: DayRecord.seed + DayRecord.t ⇒ este Generator.
    """
    return np.random.default_rng(
        np.random.SeedSequence(master_seed, spawn_key=(DAILY_STREAM, day))
    )


def stream_rng(master_seed: int, *key: int) -> np.random.Generator:
    """Generator genérico por clave jerárquica.

    Ejemplos: stream_rng(seed, EVENTS_STREAM) para el stream de eventos de
    run_events; stream_rng(seed, EXPERIMENT_STREAM, i) para réplicas de un
    experimento.
    """
    return np.random.default_rng(np.random.SeedSequence(master_seed, spawn_key=key))
