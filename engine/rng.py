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

#: Streams reservados por el harness (fuera del engine congelado; registro
#: completo de claves 0-7 — no añadir una nueva sin reservar aquí y en
#: engine/rng.py; la siguiente clave libre es 8).
#:   4 = LIFE        (harness/life.py)
#:   5 = PERSONA     (harness/persona.py)
#:   6 = CONVERSATION (harness/session.py)
#:   7 = DECISION    (harness/session.py)
#:   next free: 8 — reclamada por el diseño AFK (INACTIVITY_STREAM).
#: PROACTIVE_TIEBREAK_STREAM no es un stream real (ver nota en proactive.py).


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
