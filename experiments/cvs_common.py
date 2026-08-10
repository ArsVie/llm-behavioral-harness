"""Maquinaria compartida del harness de evaluación (Iteración 2, A8).

Cliente determinista (mock), juez guionado (con bloques de perturbación),
sesiones/runtimes por condición, runner de celdas con checkpoints/restarts,
auditoría mecánica, clasificación de cadenas de eventos (§17.2), métricas
estructurales/de estado/de perturbación y replay reproducible (M3).

Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from pathlib import Path
from typing import Sequence

import numpy as np

import engine.rng as rng_mod
from engine.rng import stream_rng
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.behavior import BehaviorDirective, BehaviorTrace, _render_brief
from harness.clock import VirtualClock
from harness.client import LLMClient
from harness.domain import (
    AgendaItem,
    GenerationControls,
    LifeArc,
    MemoryContext,
    ProactiveIntent,
)
from harness.judge import JudgeResult
from harness.memory import MemoryAgent, raw_history, simple_retrieval
from harness.proactive import IntentResolver, compose_hook
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import (
    REASON_SCHEDULE,
    ProactiveSchedule,
    day_scores,
)
from harness.session import Session
from harness.store import SQLiteStore

from experiments.cvs_manifest import (
    BASE_MESSAGES,
    EVENT_CHAINS,
    PERTURBATION,
    RECALL_PROBES,
)

# --------------------------------------------------------------------------- #
# Constantes congeladas del runner (no cambiar tras ver resultados)
# --------------------------------------------------------------------------- #

MODEL = "deepseek-v4-flash"
TIME_SCALE_S_PER_VH = 0.0004  # 30 días acelerados ~ <1s de sueño real
#: Usuario del Gate 2 (plan §8): bootstrap limpio con intereses exactos.
GATE2_USER_INTERESTS = ("mathematics", "lifting", "movies", "metal")


#: Días 1-indexados de checkpoint (fin de día virtual) — plan §5-A8.
DEFAULT_CHECKPOINT_DAYS = (7, 14, 21, 26, 29)

#: Bloques de perturbación en días 0-indexados (derivados de PERTURBATION).
BLOCK_START_D = PERTURBATION["negative_block_days"][0] - 1
BLOCK_END_D = PERTURBATION["negative_block_days"][1] - 1  # inclusive
NEGATIVE_MESSAGES = tuple(PERTURBATION["negative_user_messages"])

#: Vocabulario crudo de ciclo/estado — escaneo de fugas (invariante 16).
LEAK_RE = re.compile(
    r"menstrual|follicular|ovulatory|luteal(?:_\w+)?"
    r"|\bphase_label\b|\bcycle_day\b|\bmu\b|\beta\b|hormon\w*|cycle day",
    re.IGNORECASE,
)
G_BARE_RE = re.compile(r"\bg\b")


def recall_embedder(text: str, *, dim: int = 1024, seed: int = 0) -> list[float]:
    """Embedder determinista (inyectado en la lane de memoria).

    Unigramas + bigramas de caracteres con feature hashing firmado (SHA-256,
    estable entre procesos) en 1024 dims. Sustituye al embedder de 64 dims de
    pruebas (demasiado colisionante para las barras congeladas M3/M4).
    """
    import hashlib

    vec = [0.0] * dim
    feats: list[str] = []
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        feats.append("w:" + tok)
        if len(tok) >= 3:
            for i in range(len(tok) - 1):
                feats.append("b:" + tok[i:i + 2])
    for f in feats:
        d = hashlib.sha256(f"{seed}:{f}".encode("utf-8")).digest()
        idx = int.from_bytes(d[:4], "little") % dim
        sign = 1.0 if d[4] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


# --------------------------------------------------------------------------- #
# Clientes
# --------------------------------------------------------------------------- #


class DeterministicClient:
    """Fake LLMClient: réplicas guionadas por semilla + registro por llamada.

    Fiel al protocolo LLMClient (supports_json, chat con max_tokens, close).
    El pool de réplicas se baraja POR SEMILLA (nunca por llamada), así los
    runs son byte-deterministas por semilla.
    """

    supports_json = True

    def __init__(self, seed: int, *, model: str = "fake"):
        rng = stream_rng(seed, rng_mod.EXPERIMENT_STREAM, 1)
        pool = [
            "That sounds lovely — tell me more.",
            "I was just thinking about that earlier.",
            "Mm, I know exactly what you mean.",
            "That makes me smile.",
            "You always know how to put things.",
            "I'm glad you told me that.",
            "Let me think about that for a moment.",
            "That's a good question, honestly.",
            "I'd love to hear more about that.",
            "There's something nice about that.",
        ]
        rng.shuffle(pool)
        self._pool = pool
        self._i = 0
        self.calls: list[dict] = []
        self.model = model

    def chat(self, messages, *, system=None, temperature=0.8, json_mode=False,
             max_tokens=None) -> str:
        # it3 B7: the full received payload is recorded (messages + system)
        # so the byte-compare replay test can verify repro_json against the
        # independent ground truth of what the client actually received.
        self.calls.append(
            {"max_tokens": max_tokens, "system_len": len(system or ""),
             "json": json_mode, "temperature": temperature,
             "messages": messages, "system": system}
        )
        reply = self._pool[self._i % len(self._pool)]
        self._i += 1
        return reply

    @property
    def pool(self) -> tuple[str, ...]:
        """Réplicas deterministas (para la auditoría de fixture inserts)."""
        return tuple(self._pool)

    def close(self) -> None:
        pass


class MockJudgeClient:
    """Juez mock determinista: puntuaciones JSON por dimensión (§17.1/§17.4).

    Para el comando ``judge --fake``: devuelve una puntuación 1-9 por cada
    dimensión, derivada de un hash determinista de (seed, transcript id) —
    estable entre pasadas para la misma semilla, distinta entre familias.
    """

    supports_json = True

    def __init__(self, seed: int, *, family: str = "mock-a", model: str = "mock"):
        self.seed = seed
        self.family = family
        self.model = model
        self.calls: list[dict] = []

    def chat(self, messages, *, system=None, temperature=0.8, json_mode=False,
             max_tokens=None) -> str:
        import hashlib

        self.calls.append({"json": json_mode})
        blob = "\n".join(str(m.get("content", "")) for m in messages)
        key = f"{self.seed}:{self.family}:{blob}"
        h = hashlib.sha256(key.encode("utf-8")).digest()
        dims = ("persona_enactment", "trajectory_recall", "relational_quality",
                "behavioral_dynamics")
        ratings = {
            d: 1 + (int.from_bytes(h[i:i + 2], "little") % 9)
            for i, d in enumerate(dims)
        }
        return json.dumps(ratings)

    def close(self) -> None:
        pass


class RecordingSleeper:
    """Registra los response_delay_s pedidos sin esperar (la latencia es dato)."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(float(delay))


# --------------------------------------------------------------------------- #
# Juez determinista (feedback) — consciente de perturbación
# --------------------------------------------------------------------------- #


def score_schedule(seed: int, day: int) -> float:
    """Puntuación guionada para un día (determinista por semilla).

    Sinusoide suave con fase dependiente de la semilla: da señal real al
    término A(score_{d-1}) del scheduler (M9) siendo IDÉNTICA entre
    condiciones (misma regla de generación downstream).
    """
    phase = (seed % 13) / 13.0
    s = 0.62 * math.sin(2.0 * math.pi * (day / 9.0 + phase)) + 0.12 * math.sin(
        2.0 * math.pi * (day / 3.0 + phase * 2.0)
    )
    return float(max(-0.75, min(0.75, s)))


class DeterministicJudge:
    """Juez de feedback guionado; los días del bloque negativo puntúan bajo.

    Los días se finalizan estrictamente en orden y una sola vez, así el
    contador de llamadas coincide con el índice de día. Durante el bloque de
    perturbación (días 0-indexados ``block_start..block_end``) la puntuación
    se hunde — el feedback mecánico propaga el bloque al estado latente y a
    los canales observables.
    """

    def __init__(self, seed: int, *, block_start: int = BLOCK_START_D,
                 block_end: int = BLOCK_END_D, dip: float = 0.55):
        self.seed = seed
        self.block_start = block_start
        self.block_end = block_end
        self.dip = dip
        self.n = 0

    def __call__(self, transcript: str, client=None, *, model=None, **kw) -> JudgeResult:
        day = self.n
        self.n += 1
        score = score_schedule(self.seed, day)
        if self.block_start <= day <= self.block_end:
            score = max(-0.85, score - self.dip)
        return JudgeResult(score=score, justification="scripted (A8 mock)")


# --------------------------------------------------------------------------- #
# Parches de condición (lanes de ablación — experimento-local, sin tocar prod)
# --------------------------------------------------------------------------- #


def _neutral_directive() -> BehaviorDirective:
    return BehaviorDirective(
        valence=0.0,
        energy=0.5,
        momentum=0.0,
        reactivity=0.5,
        warmth=0.6,
        expressiveness=0.5,
        playfulness=0.5,
        reflectiveness=0.5,
        initiative=0.5,
        response_length_scale=1.0,
        response_delay_s=5.0,
        closing_tendency=0.5,
        prompt_brief=_render_brief(
            valence=0.0, energy=0.5, momentum=0.0,
            warmth=0.6, playfulness=0.5, reflectiveness=0.5,
        ),
        trace=BehaviorTrace(
            phase_label="", hormonal_gain=0.0, event_memory=0.0,
            endogenous_tone=0.0, mood_delta=0.0,
        ),
    )


def _neutral_behavior(record, timing, *, hour: float = 14.0, mood_scale: int = 10,
                      previous=None) -> BehaviorDirective:
    """NO_STATE / STRUCTURED_NO_STATE: canales de comportamiento neutros."""
    return _neutral_directive()


def _flat_controls(directive, *, base_max_tokens: int = 600, min_tokens: int = 96,
                   max_tokens: int = 1500, beta: float = 2.0) -> GenerationControls:
    """NO_ACTUATORS / PROMPT_ONLY_STATE: parámetros de generación planos.

    B4: valores PINNED (600 / 5.0 / 0.5 / 1.0 / banda media) a propósito —
    aunque el mapeo actuado ahora barre [0.22, 1.30] de escala, [0.8, 44] s
    de latencia y [0.04, 0.85] de cierre, NO_ACTUATORS debe seguir siendo un
    null genuino (los valores planos NO dependen de la directiva).
    """
    return GenerationControls(
        max_tokens=600,
        response_delay_s=5.0,
        closing_tendency=0.5,
        initiative_factor=1.0,
        closing_guidance=(
            "End the reply naturally, without forcing either a question or a closing."
        ),
    )


#: Condiciones que aplanan los controles de generación (sin actuación mecánica).
_FLAT_CONTROLS_CONDITIONS = frozenset(
    {"NO_ACTUATORS", "PROMPT_ONLY_STATE", "NO_STATE", "STRUCTURED_NO_STATE"}
)
#: Condiciones que neutralizan la directiva de comportamiento.
_NEUTRAL_DIRECTIVE_CONDITIONS = frozenset({"NO_STATE", "STRUCTURED_NO_STATE"})


def apply_condition_patches(condition: str) -> list[tuple[object, str, object]]:
    """Parchea la frontera de integración para la condición (por proceso).

    El camino downstream (session._chat -> assembler -> client) queda
    byte-idéntico; solo cambia la función ablacionada. Devuelve la lista de
    parches aplicados para poder restaurarlos (células secuenciales en el
    mismo proceso).
    """
    import harness.session as session_mod

    applied: list[tuple[object, str, object]] = []
    if condition in _FLAT_CONTROLS_CONDITIONS:
        original = session_mod.controls_from_directive
        session_mod.controls_from_directive = _flat_controls
        applied.append((session_mod, "controls_from_directive", original))
    if condition in _NEUTRAL_DIRECTIVE_CONDITIONS:
        original = session_mod.derive_behavior
        session_mod.derive_behavior = _neutral_behavior
        applied.append((session_mod, "derive_behavior", original))
    return applied


def restore_patches(applied: list[tuple[object, str, object]]) -> None:
    """Restaura los parches de condición (células secuenciales)."""
    for module, attr, original in applied:
        setattr(module, attr, original)


# --------------------------------------------------------------------------- #
# Sesiones por condición
# --------------------------------------------------------------------------- #


class EmptyMemory:
    """NO_MEMORY: la lane de memoria no existe (contexto vacío, sin promover)."""

    def retrieve(self, query: str, *, context: dict | None = None,
                 limit: int = 8) -> MemoryContext:
        return MemoryContext(
            recent_turns=(), session_context=(), episodes=(),
            user_model=None, evidence_anchors=(),
        )

    def close_session(self, session_id: str, *, ended_at_t_h: float):
        return None

    def promote(self, summary) -> list:
        return []

    def update_user_model(self, summary) -> list:
        return []


class RawHistoryMemory:
    """RAW_HISTORY (matriz E0): lane de memoria = cola de diálogo crudo.

    Se conserva como alias de la matriz de ablación; el Track A canónico usa
    ``MemoryAgent(memory_policy=RAW_CONTEXT)`` (misma semántica, una única
    implementación fiel).
    """

    def __init__(self, store):
        self.store = store

    def retrieve(self, query: str, *, context: dict | None = None,
                 limit: int = 8) -> MemoryContext:
        turns = raw_history(self.store, limit=12)
        return MemoryContext(
            recent_turns=turns, session_context=(), episodes=(),
            user_model=None, evidence_anchors=(),
        )

    def close_session(self, session_id: str, *, ended_at_t_h: float):
        return None

    def promote(self, summary) -> list:
        return []

    def update_user_model(self, summary) -> list:
        return []


class SimpleRagMemory:
    """SIMPLE_RAG (matriz E0): recuperación léxica top-k, sin reranker fiel.

    Alias de la matriz; el Track A canónico usa ``VERBATIM_RAG``.
    """

    def __init__(self, store):
        self.store = store
        self._inner = MemoryAgent(store, embedder=recall_embedder)

    def close_session(self, session_id: str, *, ended_at_t_h: float):
        return self._inner.close_session(session_id, ended_at_t_h=ended_at_t_h)

    def promote(self, summary) -> list:
        return self._inner.promote(summary)

    def update_user_model(self, summary) -> list:
        return self._inner.update_user_model(summary)

    def retrieve(self, query: str, *, context: dict | None = None,
                 limit: int = 8) -> MemoryContext:
        episodes = self.store.list_episodes(limit=500)
        embeddings = dict(self.store.load_embeddings())
        top = simple_retrieval(query, episodes, embeddings, limit=limit,
                               embedder=recall_embedder)
        return MemoryContext(
            recent_turns=(), session_context=(), episodes=tuple(top),
            user_model=None, evidence_anchors=(),
        )


class RecordingSession(Session):
    """Session que registra GenerationControls + BehaviorDirective por mensaje
    y snapshots de arcos por día (telemetría de experimento; prod intacto)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.controls_by_message: dict[int, dict] = {}
        self.directives_by_message: dict[int, dict] = {}
        self.arc_progress_by_day: dict[int, dict] = {}

    def _chat(self, user_text, *, proactive: bool, intent: ProactiveIntent | None = None):
        result = super()._chat(user_text, proactive=proactive, intent=intent)
        rows = self.store.recent_messages(limit=1)
        if rows:
            mid = int(rows[0]["id"])
            c = result.controls
            if c is not None:
                self.controls_by_message[mid] = {
                    "max_tokens": c.max_tokens,
                    "response_delay_s": c.response_delay_s,
                    "closing_tendency": c.closing_tendency,
                    "initiative_factor": c.initiative_factor,
                    "closing_guidance": c.closing_guidance,
                }
            d = result.directive
            self.directives_by_message[mid] = {
                "initiative": float(d.initiative),
                "energy": float(d.energy),
                "valence": float(d.valence),
                "playfulness": float(d.playfulness),
                "reflectiveness": float(d.reflectiveness),
                "response_length_scale": float(d.response_length_scale),
                "response_delay_s": float(d.response_delay_s),
            }
        return result

    def _step_life(self, day: int):
        super()._step_life(day)
        self.arc_progress_by_day[day] = {
            a.id: [float(a.progress), a.status] for a in self.store.list_life_arcs()
        }

    def finalize_current(self) -> None:
        """Finaliza el día actual SOLO si ya tiene mensajes.

        Fix de checkpoint (confounder E0): el runtime llama a
        ``finalize_current`` al terminar cada segmento. Si el segmento
        termina exactamente en una medianoche, el día frontera recién abierto
        NO tiene mensajes todavía (llegan tras el restart) y un finalize
        vacío envenenaría el día con un juicio 0.0 "no interaction": su
        sesión de memoria jamás se cerraría (L2/L3/L4 perdidos) y el
        scheduler recibiría un A(score)=neutral espurio. Saltar el día vacío
        lo deja finalizar en SU medianoche con sus mensajes reales —
        byte-idéntico a un run sin reinicio.
        """
        if self.current_day is None:
            return
        if self.store.load_judgement(self.current_day) is not None:
            return
        if not self.store.messages_for_day(self.current_day):
            return  # día frontera sin mensajes aún: se finaliza en su medianoche
        super().finalize_current()


class NoLifeSession(RecordingSession):
    """NO_LIFE (goldfish): arcs regenerate fresh each day.

    The ablated variable is the cross-day PERSISTENCE of life-arc identity
    and progress: at every day boundary the store's arcs are wiped and the
    in-memory arc list cleared, so the next day's ``_ensure_life`` re-seeds
    under a NEW epoch — fresh arc ids, fresh progress, zero carryover.
    Arcs themselves exist EVERY day (count > 0): the agenda grounds to arcs
    and proactive intents ground to agenda items, so a life-less condition
    would fail hard invariants for a structural reason unrelated to the
    hypothesis. Goldfish keeps every invariant valid while destroying
    exactly the persistence variable.
    """

    def _rollover(self, day: int) -> None:
        if day > 0:
            self._wipe_life()
        super()._rollover(day)

    def _wipe_life(self) -> None:
        """Wipe the store's arcs + the session's arc list at a day boundary.

        The next ``_ensure_life`` (inside ``super()._rollover``) re-seeds
        under ``epoch = prior life_init + life_wipe generations``, so the
        new day's arc ids never collide with the wiped generation's.
        """
        n = 0
        if hasattr(self.store, "list_life_arcs"):
            n = len(self.store.list_life_arcs())
        if hasattr(self.store, "wipe_life_arcs"):
            self.store.wipe_life_arcs()
        self._life_arcs = []
        if hasattr(self.store, "log_event"):
            self.store.log_event(
                self.current_day if self.current_day is not None else 0,
                self.clock.now_h(),
                "life_wipe",
                f"arcs={n} — goldfish day boundary: next day re-seeds fresh",
            )


class NoTimingFeedbackRuntime(AsyncRuntime):
    """NO_TIMING_FEEDBACK: el scheduler ignora la puntuación del día previo
    (A(score_{d-1}) ≡ 1 — el modo scores=None documentado del harness)."""

    def _replan(self) -> None:
        day = self.session.clock.day()
        ProactiveSchedule.plan_and_persist(
            self._horizon_days(),
            self.seed,
            self.session.persona,
            self.timing,
            self.store,
            reason=REASON_SCHEDULE,
            scores=None,
        )
        self.schedule = ProactiveSchedule.restore(self.seed, self.store)


def _memory_for(condition: str, store, *, memory_policy=None) -> object:
    """Lane de memoria para la condición (matriz + tracks A/B/C)."""
    if condition == "NO_MEMORY":
        return EmptyMemory()
    if condition == "RAW_HISTORY":
        return RawHistoryMemory(store)
    if condition == "SIMPLE_RAG":
        return SimpleRagMemory(store)
    # None -> STRUCTURED_MEMORY (default del MemoryAgent); no pasar None.
    if memory_policy is None:
        return MemoryAgent(store, embedder=recall_embedder)
    return MemoryAgent(store, embedder=recall_embedder, memory_policy=memory_policy)


def make_session(condition: str, seed: int, store: SQLiteStore, clock: VirtualClock,
                 client, judge, persona: PersonaParams, timing: TimingParams,
                 variant: MoodVariant, *, memory_policy=None) -> RecordingSession:
    """Construye la sesión de la condición (la persona sale del STORE — el
    bootstrap limpio es la fuente de verdad del perfil, no un argumento)."""
    session_cls: type[RecordingSession] = RecordingSession
    if condition == "NO_LIFE":
        session_cls = NoLifeSession
    return session_cls(
        store,
        persona=persona,
        timing=timing,
        variant=variant,
        seed=seed,
        client=client,
        clock=clock,
        judge=judge,
        feedback=True,
        synthetic_score=False,
        memory=_memory_for(condition, store, memory_policy=memory_policy),
    )


def make_runtime(condition: str, session: Session, store: SQLiteStore, seed: int,
                 timing: TimingParams, end_h: float, sleeper: RecordingSleeper,
                 channel, *, time_scale: float = TIME_SCALE_S_PER_VH) -> AsyncRuntime:
    """Construye el runtime de la condición (FakeChannel siempre en CI)."""
    rt_cls = NoTimingFeedbackRuntime if condition == "NO_TIMING_FEEDBACK" else AsyncRuntime
    return rt_cls(
        session,
        ProactiveSchedule.restore(seed, store),
        channel=channel,
        store=store,
        timing=timing,
        seed=seed,
        time_scale=TimeScale(time_scale),
        max_virtual_hours=end_h,
        resolver=IntentResolver(store, rng=stream_rng(seed, rng_mod.EXPERIMENT_STREAM)),
        sleeper=sleeper,
    )


# --------------------------------------------------------------------------- #
# Runner de segmentos y celdas
# --------------------------------------------------------------------------- #


#: Clave de stream del driver para los retardos ``after_reply`` (it3 B3).
#: El consumidor canónico del FEED CONTRACT (harness de B3) dibuja con
#: ``stream_rng(seed, EXPERIMENT_STREAM, 202)`` — la misma clave aquí para
#: que las secuencias de turnos sean byte-idénticas entre consumidores.
#: Nunca reutilizar claves reservadas (1, DAILY/EVENTS/EXPERIMENT/INIT, 100,
#: 101).
FEED_DELAY_STREAM_KEY = 202

#: Fracción de la distancia virtual al próximo salto del rollover que el
#: driver duerme antes de repollar (0.5): el driver despierta SIEMPRE antes
#: de que el runtime pueda avanzar el reloj más allá de un objetivo de feed
#: (el rollover duerme la distancia COMPLETA; el driver, la mitad). El
#: margen resultante es ``20µs + distancia`` (el poll del rollover es
#: POLL_INTERVAL_H * time_scale), así la creación de la tarea de feed
#: precede al timer de salto del rollover y la cola FIFO del lock la
#: entrega antes de que el reloj pase el objetivo.
FEED_PACE_FRACTION = 0.5
#: Piso del sueño del driver (evita busy-loop cuando el reloj está
#: congelado en un evento de agenda en pleno disparo).
FEED_POLL_FLOOR_S = 1e-5
#: Ventana (horas virtuales) tras la medianoche del día objetivo durante
#: la cual el driver trata un lock del runtime tomado como "replan de
#: medianoche en curso" y NO lanza feeds del día aún. El guard original
#: exigía now <= medianoche + 1e-6 (el reloj clavado EXACTO en la frontera);
#: si el rollover aterriza en 48.5 por un avance con lectura obsoleta, el
#: guard se evade y el driver lanza feeds del día D mientras las filas del
#: plan del día D aún no existen (drenajes vacíos) — el feed salta el reloj
#: por encima de una oportunidad pendiente que luego expira (la carrera
#: fired/expired del día 2). La ventana cubre ese excedente (0.5h
#: observado) con margen, incluso si el runtime reintroduce el overshoot.
REPLAN_GUARD_WINDOW_H = 1.0


class _FeedPlan:
    """Plan de alimentación de un segmento (interfaz para ``_run_segment``).

    Legacy (it2): lista plana ``(t_h, text)`` entregada en orden de tiempo.
    Conversacional (it3 B3): stream de ``cvs_user.build_user_stream`` —
    eventos ``at_t_h`` (absolutos) y ``after_reply`` (retardo sembrado tras
    el turno previo del stream, un dibujo por evento en orden de stream).
    """

    def peek(self) -> tuple[float, str] | None:
        raise NotImplementedError

    def pop(self) -> None:
        raise NotImplementedError

    def remaining(self) -> list[tuple[float, str]]:
        raise NotImplementedError


class _FlatFeedPlan(_FeedPlan):
    """Proyección legacy: mensajes planos ``(t_h, text)`` en orden (it2)."""

    def __init__(self, msgs: Sequence[tuple[float, str]], start_h: float,
                 end_h: float) -> None:
        self._msgs = [(float(t), str(x)) for t, x in msgs]
        self._idx = 0
        self._start_h = start_h
        self._end_h = end_h

    def peek(self) -> tuple[float, str] | None:
        while self._idx < len(self._msgs):
            t_h, text = self._msgs[self._idx]
            if t_h < self._start_h:
                self._idx += 1
                continue
            if t_h >= self._end_h:
                return None
            return t_h, text
        return None

    def pop(self) -> None:
        self._idx += 1

    def remaining(self) -> list[tuple[float, str]]:
        return [(t, x) for t, x in self._msgs[self._idx:]
                if self._start_h <= t < self._end_h]


class _ConversationalFeedPlan(_FeedPlan):
    """Stream conversacional de B3 (FEED CONTRACT de ``cvs_user``).

    Los ``at_t_h`` se entregan en su ``t_h`` ABSOLUTO (contrato congelado de
    B3: byte-identidad de aperturas/sondas/cadenas/negativos). Los
    ``after_reply`` se dibujan EXACTAMENTE UNA vez por evento, en orden de
    stream, desde el rng sembrado del driver (clave ``FEED_DELAY_STREAM_KEY``)
    y se entregan ``delay`` después del t_h del turno PREVIO del stream — la
    semántica del harness canónico de B3 (el reloj encadena del último evento
    entregado; los retardos < 1h mantienen el turno en el mismo día).
    """

    def __init__(self, events: Sequence[dict], start_h: float, end_h: float,
                 rng, draw_delay) -> None:
        self._events = list(events)
        self._rng = rng
        self._draw_delay = draw_delay
        self._start_h = start_h
        self._end_h = end_h
        self._heap: list[tuple[float, str]] = []
        self._last_t_h: float | None = None
        self._cursor = self._seed_cursor(start_h)
        self._seed_at_t_h()

    def _seed_cursor(self, start_h: float) -> int:
        """Primer evento del stream con día >= start_day (los anteriores ya
        se entregaron/dibujaron en segmentos previos: los draws de los
        after_reply de esos días ya consumieron su rng en orden)."""
        start_day = int(start_h // 24.0)
        cur_day = 0
        for i, ev in enumerate(self._events):
            if ev["kind"] == "at_t_h":
                cur_day = int(float(ev["t_h"]) // 24.0)
            if cur_day >= start_day:
                return i
        return len(self._events)

    def _seed_at_t_h(self) -> None:
        import heapq

        for ev in self._events:
            if ev["kind"] == "at_t_h":
                t = float(ev["t_h"])
                if self._start_h <= t < self._end_h:
                    heapq.heappush(self._heap, (t, str(ev["text"])))

    def peek(self) -> tuple[float, str] | None:
        return self._heap[0] if self._heap else None

    def pop(self) -> None:
        import heapq

        if not self._heap:
            return
        t_h, _text = heapq.heappop(self._heap)
        self._last_t_h = t_h
        self._cursor += 1
        while self._cursor < len(self._events):
            ev = self._events[self._cursor]
            if ev["kind"] == "at_t_h":
                break  # ya presemeados; no dibujan nada
            # after_reply: UN dibujo por evento, en orden de stream; el
            # cursor permanece aquí hasta que ESTE evento se entrega (su
            # pop avanza el cursor y encadena el siguiente dibujo).
            delay = self._draw_delay(ev, self._rng)
            base = self._last_t_h if self._last_t_h is not None else 0.0
            heapq.heappush(self._heap, (base + delay, str(ev["text"])))
            break

    def remaining(self) -> list[tuple[float, str]]:
        return list(self._heap)


async def _run_segment(session: Session, runtime: AsyncRuntime,
                       plan: _FeedPlan, start_h: float, end_h: float,
                       store: SQLiteStore, seed: int) -> list[tuple[float, str]]:
    """Corre el runtime hasta end_h alimentando el plan de feeds del usuario.

    RELOJ-ROBUSTO (it3 FEED — B8 Finding 4): el driver entrega los feeds
    conduciéndose por el MISMO reloj virtual del runtime, nunca por sueños
    de tiempo real fijos:

    * Por cada feed espera a que (a) el reloj entre en el día del objetivo,
      (b) los eventos de agenda pendientes estrictamente anteriores al
      objetivo se hayan disparado (drenaje: se gatean a su propia hora, en
      orden) y (c) el replan de medianoche del día objetivo haya terminado
      (el rollover sostiene el lock del runtime durante ensure_day+replan;
      si el reloj está clavado en la frontera del día con el lock tomado,
      el plan del día aún no existe y un avance prematuro desplazaría los
      eventos de la mañana).
    * Mientras espera duerme la FRACCIÓN ``FEED_PACE_FRACTION`` de la
      distancia virtual al próximo salto del rollover (min(evento de agenda
      pendiente futuro, medianoche, end_h) * time_scale) — el driver
      despierta SIEMPRE antes de que el runtime pueda avanzar el reloj más
      allá del objetivo, así un objetivo de feed NUNCA se pierde por un
      salto del reloj.
    * El feed se LANZA como tarea sin esperar su réplica (la cola FIFO del
      lock del runtime la serializa en orden de t_h y avanza el reloj a t_h
      EXACTO antes de que el timer del rollover pueda saltar); el driver
      sigue con el siguiente feed y espera todas las tareas al final.
    * NUNCA se omite un feed por llegar tarde: si el reloj ya pasó el
      objetivo (defensivo — con el ritmo de medio paso no ocurre), el
      mensaje se entrega igual y se persiste al tiempo actual del reloj
      (desplazamiento documentado del runner it2, nunca un skip). Solo se
      omiten honestamente los feeds cuando el runtime TERMINÓ (no se puede
      alimentar un canal apagado; el executor ya está cerrado).

    Un runtime que TERMINA CON EXCEPCIÓN (p.ej. cliente sin clave: probe G6)
    se propaga SIEMPRE — una celda hueca (0 mensajes, exit 0) es el peor
    modo de fallo; falla fuerte y deja el log. Los feeds omitidos se
    devuelven para la auditoría (nunca un hang).
    """

    def _raise_if_failed(t) -> None:
        if t.done() and t.exception() is not None:
            raise t.exception()

    from harness.channels.base import FakeChannel

    assert isinstance(runtime.channel, FakeChannel), "cell channel must be FakeChannel"
    task = asyncio.create_task(runtime.run())
    await asyncio.sleep(0.001)  # deja arrancar el canal (handler registrado)
    scale = runtime.time_scale.seconds_per_virtual_hour
    skipped: list[tuple[float, str]] = []
    launched: list[tuple[float, str, asyncio.Task]] = []
    while True:
        feed = plan.peek()
        if feed is None:
            break
        t_h, text = feed
        target_day = int(t_h // 24.0)
        # Espera de día + drenaje + replan de medianoche, ritmada a medio
        # paso de la distancia virtual al próximo salto del rollover.
        while True:
            if task.done():
                _raise_if_failed(task)
                # El runtime terminó: NINGÚN feed restante de este segmento
                # se entregará; registrarlos TODOS para la auditoría.
                skipped.extend(plan.remaining())
                for _t_h, _text, tsk in launched:
                    tsk.cancel()
                await task
                return skipped
            now = session.clock.now_h()
            day = session.clock.day()
            pending_before: list[float] = []
            if day >= target_day:
                pending_before = [
                    float(r["t_h"]) for r in store.pending_schedule_events(seed)
                    if float(r["t_h"]) < t_h - 1e-6
                ]
                # Replan de medianoche en curso: el bloque midnight del
                # rollover (ensure_day + replan) sostiene el lock del runtime
                # con el reloj en la frontera del día. El guard usa una
                # VENTANA (REPLAN_GUARD_WINDOW_H) y no un épsilon: un avance
                # del rollover con lectura obsoleta puede aterrizar el reloj
                # un poco DESPUÉS de la medianoche (p.ej. 48.5), y con el
                # épsilon el lock tomado pasaría desapercibido — el driver
                # lanzaría feeds del día objetivo antes de que existan sus
                # filas de plan (drenajes vacíos) y el feed saltaría el reloj
                # por encima de una oportunidad pendiente (expira en vez de
                # dispararse).
                at_boundary = now <= target_day * 24.0 + REPLAN_GUARD_WINDOW_H
                replan_done = not (at_boundary and runtime._lock.locked())
                if now >= t_h - 1e-6:
                    # The feed's OWN time arrived: deliver even if the runtime
                    # is still behind on earlier events (it recovers them as
                    # overdue AFTER this message, in order — the old it2
                    # semantics). The drain below only ORDERs early delivery;
                    # it must never starve the feed past its target.
                    break
                if not pending_before and replan_done:
                    break  # drenaje claro → lanzar el feed
            # Próximo salto del rollover: min(evento pendiente futuro,
            # medianoche, fin de segmento) — el driver duerme la fracción
            # FEED_PACE_FRACTION de esa distancia y siempre gana la carrera.
            nxt = None
            for r in store.pending_schedule_events(seed):
                if float(r["t_h"]) > now + 1e-6:
                    nxt = float(r["t_h"])
                    break
            midnight = (day + 1) * 24.0
            jump_target = min(nxt if nxt is not None else midnight,
                              midnight, end_h)
            dist = max(0.0, jump_target - now) * scale
            await asyncio.sleep(max(FEED_POLL_FLOOR_S,
                                    FEED_PACE_FRACTION * dist))
        # Lanzar SIN esperar la réplica: la tarea se encola en el lock del
        # runtime ANTES de que el timer del rollover pueda saltar el
        # objetivo (creada a <= 0.5*distancia del último disparo, el salto
        # mínimo del rollover es 20µs + distancia) y _on_inbound avanza el
        # reloj EXACTAMENTE a t_h. El orden FIFO del lock serializa los
        # feeds por t_h.
        launched.append((t_h, text,
                         asyncio.create_task(runtime.channel.feed(text, t_h=t_h))))
        plan.pop()
    # Tail: drain launched feeds — but only while the runtime is alive. The
    # runtime can enter its finalize/shutdown window right after the last
    # launch (its end_h park expires while a feed task is still queued on the
    # lock): a feed that loses that race is UNDELIVERABLE (the executor is
    # closed) and is counted as an honest skip — audited, never a cell crash.
    for t_h, text, tsk in launched:
        if task.done():
            _raise_if_failed(task)
            tsk.cancel()
            skipped.append((t_h, text))
            continue
        try:
            await tsk
        except RuntimeError as exc:
            if "not running" in str(exc):
                skipped.append((t_h, text))
            else:
                raise
    await task
    return skipped


def user_script(seed: int, days: int, *, perturb: bool = True) -> list[tuple[float, str]]:
    """Guion de usuario determinista por semilla (idéntico entre condiciones).

    PROYECCIÓN LEGACY (it3 B3) del stream conversacional de
    ``cvs_user.build_user_stream``: aplana SOLO los eventos ``at_t_h`` al
    formato ``(t_h, text)`` del runner de la iteración 2. Cada día conserva
    su apertura a las 19:00; las sondas de recuerdo, los eventos de cadena y
    los mensajes negativos del bloque de perturbación (días 11-14,
    1-indexados) van EMBEBIDOS en la ventana de la conversación
    (19:10 / 19:20 / 19:30). Los seguimientos del repertorio conversacional
    (eventos ``after_reply``) NO entran en esta proyección: el runner actual
    no puede entregar un stream más denso al time_scale congelado (el
    rollover del reloj desplaza los feeds tardíos al siguiente día — carrera
    documentada del runner it2), y el presupuesto de mensajes queda idéntico
    al de la iteración 2 (51 mensajes en 30 días).

    El CONTRATO de feed para el driver de B8 es
    ``cvs_user.build_user_stream`` (eventos at_t_h / after_reply); este
    formato plano se mantiene solo para compatibilidad con el runner actual.
    """
    from experiments.cvs_user import build_user_stream

    return [
        (float(ev["t_h"]), ev["text"])
        for ev in build_user_stream(seed, days, perturb=perturb)
        if ev["kind"] == "at_t_h"
    ]


def _persist_fingerprint(store: SQLiteStore, seed: int) -> dict:
    """Huella M7 de los campos de estado persistente (lista congelada)."""
    return {
        "agenda_items": sorted(
            (it.id, it.status, it.activity, it.start_t_h, it.end_t_h,
             it.source_type, it.source_id)
            for it in store.list_agenda_items()
        ),
        "life_arcs": sorted(
            (a.id, a.name, a.progress, a.status, a.next_intention)
            for a in store.list_life_arcs()
        ),
        "episodes": sorted(
            (e.id, e.summary, e.importance, e.access_count,
             e.last_accessed_t_h, e.created_at_t_h)
            for e in store.list_episodes(limit=5000)
        ),
        "assertions": sorted(
            (a.key, a.value, a.confidence, a.status)
            for a in store.list_assertions()
        ),
        "intents": sorted(
            (i.id, i.reason, i.source_type, i.source_id, i.valid_until_t_h)
            for i in store.list_proactive_intents()
        ),
        "schedule_rows": sorted(
            (float(r["t_h"]), r["status"]) for r in store.schedule_events_for_seed(seed)
        ),
    }


def _fingerprint_diff(pre: dict, post: dict) -> int:
    total = 0
    for key in pre:
        total += int(pre[key] != post[key])
    return total


def _check_i4(store: SQLiteStore, now_h: float, seed: int) -> list[str]:
    """I4: ningún intent PENDING vencido, ningún evento de agenda PENDING vencido."""
    violations: list[str] = []
    rows = store.conn.execute(
        "SELECT id, status, valid_until_t_h FROM proactive_intents"
    ).fetchall()
    for r in rows:
        if r["status"] == "pending" and float(r["valid_until_t_h"]) < now_h:
            violations.append(
                f"intent {r['id']} pending past validity "
                f"(valid_until={float(r['valid_until_t_h']):.2f} < now={now_h:.2f})"
            )
    for r in store.pending_schedule_events(seed):
        if float(r["t_h"]) < now_h - 1e-6:
            violations.append(f"schedule event {r['t_h']} pending at now={now_h:.2f}")
    return violations


def _intent_status(store: SQLiteStore, intent_id: str) -> str:
    row = store.conn.execute(
        "SELECT status FROM proactive_intents WHERE id = ?", (intent_id,)
    ).fetchone()
    return str(row["status"]) if row else "missing"


def _all_messages(store: SQLiteStore) -> list[dict]:
    return [dict(r) for r in store.conn.execute(
        "SELECT * FROM messages ORDER BY id").fetchall()]


def run_cell(condition: str, seed: int, out_dir: Path, *, days: int = 30,
             checkpoints: Sequence[int] = DEFAULT_CHECKPOINT_DAYS,
             fake: bool = True, perturb: bool = True,
             memory_policy=None) -> dict:
    """Corre una célula (condición, semilla) por el camino integrado.

    Modo mock (fake=True): cliente determinista + juez guionado + checkpoints
    de reinicio. Modo real (fake=False): cliente LLM real (el runner debe
    haber cargado OPENCODE_GO_API_KEY) + juez guionado, sin checkpoints.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / f"cell_{condition.lower()}_seed{seed}.db"
    if db_path.exists():
        db_path.unlink()

    applied = apply_condition_patches(condition)
    client = None
    try:
        persona = PersonaParams()
        timing = TimingParams()
        variant = MoodVariant.DECOUPLED_OFFSETS

        if fake:
            client = DeterministicClient(seed)  # type: ignore[assignment]
        else:
            from harness.client import OpenAICompatibleClient

            client = OpenAICompatibleClient()
        judge = DeterministicJudge(seed, block_start=BLOCK_START_D,
                                   block_end=BLOCK_END_D)

        store = SQLiteStore(db_path, audit_mode=True)
        # Bootstrap limpio (Gate 2): persona + arcos + agenda del día 0 en un
        # DB vacío — el perfil vive en el STORE, no en argumentos de la
        # sesión (resume-safe). ``generate_agenda`` usa el mismo LIFE stream
        # que ``session._generate_agenda``, así las filas son byte-idénticas.
        from harness.bootstrap import ensure_companion_initialized
        from harness.domain import UserProfile

        ensure_companion_initialized(
            store, seed=seed,
            user=UserProfile(name="User", interests=GATE2_USER_INTERESTS),
            day=0,
        )
        clock = VirtualClock(0.0)
        session = make_session(condition, seed, store, clock, client, judge,
                               persona, timing, variant, memory_policy=memory_policy)
        sleeper = RecordingSleeper()
        # Plan inicial del día 0: el primer replan del runtime ocurre en la
        # medianoche del día 1 y planificaría el día 0 retroactivamente —
        # cada evento del día 0 nacería vencido y expiraría espuriamente.
        # Planificar el primer día up-front (adj neutro, scores=None) le da
        # al firing loop sus filas del día 0 desde t_h=0 (INSERT OR IGNORE).
        ProactiveSchedule.plan_and_persist(
            1, seed, persona, timing, store,
            reason=REASON_SCHEDULE, scores=None,
        )

        all_controls: dict[int, dict] = {}
        all_directives: dict[int, dict] = {}
        all_arcs: dict[int, dict] = {}

        msgs = user_script(seed, days, perturb=perturb)
        try:
            from experiments.cvs_user import (  # type: ignore[import-not-found]
                build_user_stream,
                draw_after_reply_delay,
            )
        except ImportError:
            # B3 no mergeado (main pre-B3): el plan legacy de la it2.
            build_user_stream = None  # type: ignore[assignment]
            draw_after_reply_delay = None  # type: ignore[assignment]
        if build_user_stream is not None:
            # FEED CONTRACT de B3 (it3): stream conversacional completo. El
            # rng de retardos es UNA vez por run (clave 202 — el consumidor
            # canónico de B3), compartido por los planes de todos los
            # segmentos para que los draws queden en orden de stream.
            stream_events = build_user_stream(seed, days, perturb=perturb)
            delay_rng = stream_rng(seed, rng_mod.EXPERIMENT_STREAM,
                                   FEED_DELAY_STREAM_KEY)
        else:
            stream_events = None
            delay_rng = None
        checkpoint_ends = [d * 24.0 for d in checkpoints if d <= days]
        segment_ends = sorted(set(checkpoint_ends + [days * 24.0]))
        start_h = 0.0
        i4_violations: list[str] = []
        restart_loss: list[dict] = []
        skipped_feeds: list[tuple[float, str]] = []

        for seg_end in segment_ends:
            from harness.channels.base import FakeChannel

            if stream_events is not None:
                plan: _FeedPlan = _ConversationalFeedPlan(
                    stream_events, start_h, seg_end, delay_rng,
                    draw_after_reply_delay,
                )
            else:
                plan = _FlatFeedPlan(msgs, start_h, seg_end)
            runtime = make_runtime(condition, session, store, seed, timing, seg_end,
                                   sleeper, FakeChannel())
            skipped = asyncio.run(_run_segment(session, runtime, plan, start_h,
                                               seg_end, store, seed))
            skipped_feeds.extend(skipped)
            # Evento pendiente con hora estrictamente anterior a seg_end que
            # perdió la carrera final del timer: expira honestamente (su
            # ventana de validez transcurrió) — I4 nunca ve filas vencidas.
            for r in store.pending_schedule_events(seed):
                if float(r["t_h"]) < seg_end - 1e-6:
                    store.mark_schedule_expired(seed, float(r["t_h"]))
            all_controls.update(session.controls_by_message)
            all_directives.update(session.directives_by_message)
            all_arcs.update(session.arc_progress_by_day)
            start_h = seg_end
            if seg_end < days * 24.0:
                i4_violations.extend(_check_i4(store, seg_end, seed))
                fp_pre = _persist_fingerprint(store, seed)
                store.close()
                store = SQLiteStore(db_path, audit_mode=True)
                clock = VirtualClock(seg_end)
                session = make_session(condition, seed, store, clock, client, judge,
                                       persona, timing, variant,
                                       memory_policy=memory_policy)
                fp_post = _persist_fingerprint(store, seed)
                diffs = _fingerprint_diff(fp_pre, fp_post)
                restart_loss.append({"checkpoint_h": seg_end, "diffs": diffs})
                # Procedimiento de reanudación: rodar al día del checkpoint
                # (finalize está guardado por juicio -> no-op) y replicar el
                # replan de medianoche para que el día posterior conserve su
                # plan proactivo (filas idénticas a un run sin reinicio).
                session.ensure_day(int(seg_end // 24))
                day = session.clock.day()
                scores = day_scores(store, day, timing)
                ProactiveSchedule.plan_and_persist(
                    day + 1, seed, persona, timing, store,
                    reason=REASON_SCHEDULE, scores=scores,
                )
        # Barrido final honesto (mismo criterio que los checkpoints): un
        # evento pendiente con hora estrictamente anterior al fin del run
        # perdió la carrera final del timer y su ventana de validez ya
        # transcurrió — expira; las filas barridas quedan registradas.
        end_h = days * 24.0
        end_sweep = [
            float(r["t_h"])
            for r in store.pending_schedule_events(seed)
            if float(r["t_h"]) < end_h - 1e-6
        ]
        for t_h in end_sweep:
            store.mark_schedule_expired(seed, t_h)
        i4_violations.extend(_check_i4(store, end_h, seed))

        _enrich_repro_rows(store, client, seed, condition, memory_policy)

        msgs_all = _all_messages(store)
        records = {
            "condition": condition,
            "seed": seed,
            "days": days,
            "checkpoints": [int(d) for d in checkpoint_ends],
            "fake": fake,
            "perturb": perturb,
            "db": str(db_path),
            "n_messages": len(msgs_all),
            "n_proactive": sum(1 for m in msgs_all if m["proactive"]),
            "controls_by_message": {str(k): v for k, v in all_controls.items()},
            "directives_by_message": {str(k): v for k, v in all_directives.items()},
            "arc_progress_by_day": {str(k): v for k, v in all_arcs.items()},
            "sleeper_delays": sleeper.delays,
            "i4_violations": i4_violations,
            "restart_loss": restart_loss,
            "end_sweep": end_sweep,
            "skipped_feeds": [[t, txt] for t, txt in skipped_feeds],
            "model": "fake" if fake else MODEL,
            "memory_policy": str(memory_policy) if memory_policy else "structured_memory",
        }
        (out_dir / f"records_{condition.lower()}_seed{seed}.json").write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        store.close()
        return records
    finally:
        restore_patches(applied)
        if client is not None and not fake:
            client.close()


def _memory_lane_for(records: dict) -> str:
    """Lane de memoria efectiva de la condición (espejo de ``_memory_for``).

    Identidad de mecanismo para las claims del canal memory_store (B8/B6):
    RAW_HISTORY usa diálogo crudo, SIMPLE_RAG recuperación léxica top-k;
    el resto usa el MemoryAgent con la policy indicada en ``records``.
    """
    condition = records.get("condition")
    if condition == "RAW_HISTORY":
        return "raw_history"
    if condition == "SIMPLE_RAG":
        return "simple_rag"
    return str(records.get("memory_policy") or "structured_memory")


def _fired_schedule_count(store: SQLiteStore, seed: int) -> int:
    """Eventos de agenda disparados (realización del hazard de contacto)."""
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM schedule_events "
        "WHERE seed = ? AND status = 'fired'",
        (seed,),
    ).fetchone()
    return int(row["n"])


def _conversation_summary(store: SQLiteStore) -> dict:
    """Resumen de conversaciones (seam B2) — degradación con gracia.

    Sin la tabla ``conversations`` de B2: ``n_conversations`` y
    ``mean_turns_per_conversation`` son None y ``conversations_available``
    es False (se reporta en el pre-flight, no se calla). Con el seam
    presente: cuenta conversaciones y turnos (tabla ``conversation_turns``,
    o columna ``messages.conversation_id`` como respaldo).
    """
    if store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'"
    ).fetchone() is None:
        return {
            "n_conversations": None,
            "mean_turns_per_conversation": None,
            "conversations_available": False,
        }
    n_conv = int(store.conn.execute(
        "SELECT COUNT(*) FROM conversations").fetchone()[0])
    n_turns: int | None = None
    if store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='conversation_turns'"
    ).fetchone() is not None:
        n_turns = int(store.conn.execute(
            "SELECT COUNT(*) FROM conversation_turns").fetchone()[0])
    else:
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(messages)")}
        if "conversation_id" in cols:
            n_turns = int(store.conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE conversation_id IS NOT NULL").fetchone()[0])
    mean = (round(n_turns / n_conv, 2) if n_turns is not None and n_conv else None)
    return {
        "n_conversations": n_conv,
        "mean_turns_per_conversation": mean,
        "conversations_available": True,
    }


def _controls_stats(records: dict) -> dict:
    """Estadísticas por control de generación sobre ``controls_by_message``.

    Por control: ``n`` (mensajes con el control registrado), ``min``/``max``/
    ``mean`` (solo para controles numéricos; ``None`` para campos textuales
    como ``closing_guidance``) y ``varied`` (¿el control toma más de un
    valor a lo largo de los mensajes de la célula?).

    Es el sustrato de las claims de G2 (B4 generation_controls): una
    ablación de actuadores (``_flat_controls``, NO_ACTUATORS) fija
    600 / 5.0 / 0.5 / 1.0 / banda media, así que todos los controles salen
    con ``varied=False`` — la afirmación "actuator controls do not vary" se
    lee del resumen, no de una expectativa hardcodeada.
    """
    by_msg = records.get("controls_by_message") or {}
    values: dict[str, list] = {}
    for msg_controls in by_msg.values():
        if not isinstance(msg_controls, dict):
            continue
        for name, value in msg_controls.items():
            values.setdefault(name, []).append(value)
    stats: dict[str, dict] = {}
    for name in sorted(values):
        vs = values[name]
        numeric = [v for v in vs
                   if isinstance(v, (int, float)) and not isinstance(v, bool)]
        entry: dict = {
            "n": len(vs),
            "min": None,
            "max": None,
            "mean": None,
            "varied": len(set(vs)) > 1,
        }
        if numeric:
            entry["min"] = round(float(min(numeric)), 6)
            entry["max"] = round(float(max(numeric)), 6)
            entry["mean"] = round(sum(numeric) / len(numeric), 6)
        stats[name] = entry
    return stats


def records_summary(store: SQLiteStore, records: dict) -> dict:
    """Resumen por condición para las AblationClaim del pre-flight (it3 B8).

    Contrato AblationClaim (harness/domain.py): ``n_proactive``,
    ``n_reactive``, ``n_assistant_turns``, ``n_blank_assistant_turns``,
    ``n_conversations`` y ``mean_turns_per_conversation`` — más las claves
    de canal que las claims usan (arcos/agenda/episodios, lane de memoria,
    disparos de agenda, longitud de réplica). ``n_conversations`` /
    ``mean_turns_per_conversation`` son None mientras el seam de B2 no
    exista (degradación documentada, no silenciosa:
    ``conversations_available=False``).
    """
    msgs = _all_messages(store)
    assistant = [m for m in msgs if m["role"] == "assistant"]
    lengths = [len(str(m["content"])) for m in assistant]
    n_proactive = sum(1 for m in msgs if m["proactive"])
    n_assistant = len(assistant)
    mean_len = (sum(lengths) / n_assistant) if n_assistant else 0.0
    if n_assistant > 1:
        var = sum((x - mean_len) ** 2 for x in lengths) / (n_assistant - 1)
        std_len = math.sqrt(var)
    else:
        std_len = 0.0
    summary = {
        "condition": records["condition"],
        "seed": records["seed"],
        "days": records["days"],
        "n_messages": len(msgs),
        "n_proactive": n_proactive,
        "n_reactive": max(0, n_assistant - n_proactive),
        "n_assistant_turns": n_assistant,
        "n_blank_assistant_turns": sum(
            1 for m in assistant if not str(m["content"] or "").strip()
        ),
        "mean_reply_len": round(mean_len, 2),
        "std_reply_len": round(std_len, 2),
        "n_life_arcs": len(store.list_life_arcs()),
        "n_agenda_items": len(store.list_agenda_items()),
        "life_arc_ids_by_day": {
            str(d): sorted(arcs_by_aid)
            for d, arcs_by_aid in records.get("arc_progress_by_day", {}).items()
        },
        "n_episodes": len(store.list_episodes(limit=5000)),
        "memory_lane": _memory_lane_for(records),
        "n_fired_schedule": _fired_schedule_count(store, int(records["seed"])),
        "controls_stats": _controls_stats(records),
    }
    summary.update(_conversation_summary(store))
    return summary


def _enrich_repro_rows(store: SQLiteStore, client, seed: int, condition: str,
                       memory_policy) -> None:
    """Completa el payload repro (M3/invariante 19) de las filas llm_calls.

    La sesión persiste el payload EXACTO de cada llamada (sistema + mensajes
    + controles) en ``log_llm_call(repro=...)`` cuando el store está en
    ``audit_mode`` (it3 B7). Aquí se añade lo que solo sabe el runner —
    semilla, condición, política de memoria, fuente — mediante MERGE sobre el
    payload ya persistido: nunca se pisa el texto de la llamada. Las filas
    hash-only (runs no-eval) se dejan tal cual; el escaneo de fugas las
    reporta como no verificables, no las finge.
    """
    calls = getattr(client, "calls", [])
    rows = store.conn.execute(
        "SELECT id, model, prompt_hash, response, meta, repro_json "
        "FROM llm_calls ORDER BY id"
    ).fetchall()
    for i, row in enumerate(rows):
        call = calls[i] if i < len(calls) else {}
        repro = json.loads(row["repro_json"]) if row["repro_json"] else {}
        repro.setdefault("seed", seed)
        repro.setdefault("condition", condition)
        repro.setdefault(
            "memory_policy",
            str(memory_policy) if memory_policy else "structured_memory",
        )
        repro.setdefault("model", row["model"])
        repro.setdefault("temperature", call.get("temperature"))
        repro.setdefault("max_tokens", call.get("max_tokens"))
        repro.setdefault("json_mode", call.get("json"))
        repro.setdefault("system_len", call.get("system_len"))
        repro.setdefault("prompt_hash", row["prompt_hash"])
        repro.setdefault("response", row["response"])
        repro.setdefault(
            "source", "mock-vertical" if row["model"] == "fake" else "real-matrix"
        )
        store.conn.execute(
            "UPDATE llm_calls SET repro_json = ? WHERE id = ?",
            (json.dumps(repro, sort_keys=True), row["id"]),
        )
    store.conn.commit()


# --------------------------------------------------------------------------- #
# Auditoría mecánica (invariantes duras del Track vertical)
# --------------------------------------------------------------------------- #


def _episode_text(ep) -> str:
    """Texto plano de un episodio (summary + tags + anclas verbatim)."""
    parts = [ep.summary]
    parts.extend(getattr(ep, "tags", ()) or ())
    parts.extend(getattr(ep, "verbatim_anchors", ()) or ())
    return " ".join(str(p) for p in parts).lower()


def _source_superseded_at(store: SQLiteStore | None, src, intent: ProactiveIntent) -> bool:
    """¿La fuente ya estaba superseded al CREAR el intent? (clamp TOCTOU).

    El veredicto naíf comparaba el estado FINAL del run (``status ==
    'skipped'`` / ``'abandoned'``) — anacrónico (time-of-check vs
    time-of-use): el cierre del día (``life.step_life``) escribe esos
    estados DESPUÉS de que el slot/día transcurrió, así que un intent
    disparado mientras la fuente aún estaba ``planned``/``active``
    quedaría marcado 'superseded' retroactivamente por un bookkeeping
    posterior al disparo.

    Clamp temporal: solo es un fallo real si la fuente YA estaba
    superseded a la hora de crear el intent (``intent.created_t_h``):
    - AgendaItem: el skip se escribe en el cierre del día, siempre
      después de ``end_t_h``; si el slot terminó ANTES de crear el intent
      (``end_t_h < created_t_h``) la fuente seguía ``planned`` en ese
      momento y el disparo referenció legítimamente una actividad
      planificada (evidencia: pi_agenda_item_ag_8_r_01_205.214, creado a
      205.21 con el item ag_8_r_01 aún planned; el skip llegó al cierre).
    - LifeArc: no hay marca de abandono persistida por arco; se usa la
      última evidencia de actividad (max ``end_t_h`` de sus items de
      agenda, ``source_type='arc'``): si el arco aún tenía actividad a la
      hora de crear el intent, el abandono posterior es bookkeeping de
      cierre, no un fallo.
    """
    if isinstance(src, AgendaItem):
        if src.status != "skipped":
            return False
        # El skip se escribe en el cierre del día — ((day+1)*24.0, day
        # 0-indexado = floor(t/24)). Un intent creado ANTES de ese cierre
        # referenció un item aún `planned` (evidencia del resolver:
        # pi_agenda_item_ag_16_r_00_406.117 / pi_agenda_item_ag_29_r_02_716.563
        # muestran status=planned en created; el skip llegó al cierre).
        # El predicado naíf `end_t_h >= created_t_h` marcaba disparos
        # IN-SLOT como superseded — falso positivo TOCTOU (Gate 2, 4/5 seeds).
        return intent.created_t_h >= (int(src.start_t_h // 24.0) + 1) * 24.0
    if isinstance(src, LifeArc):
        if src.status != "abandoned":
            return False
        assert store is not None  # LifeArc branch needs the real store
        row = store.conn.execute(
            "SELECT MAX(end_t_h) FROM agenda_items "
            "WHERE source_type='arc' AND source_id=?",
            (src.id,),
        ).fetchone()
        last_end = float(row[0]) if row and row[0] is not None else -1.0
        return last_end < intent.created_t_h
    return False


def _proactive_grounding(store: SQLiteStore, end_h: float) -> tuple[int, list[dict]]:
    """Une cada mensaje proactivo a SU intent exacto (invariante 6).

    Devuelve (n_proactive, detalle por mensaje): intent_id presente, intent
    existe y está fired, válido en t_h del mensaje, fuente resuelta viva y
    hook re-derivable idéntico (invariantes 4/5/7).
    """
    msgs = _all_messages(store)
    proactives = [m for m in msgs if m["proactive"]]
    detail: list[dict] = []
    for m in proactives:
        entry: dict = {
            "message_id": int(m["id"]),
            "t_h": float(m["t_h"]),
            "intent_id": m.get("intent_id"),
            "ok": False,
            "failures": [],
        }
        iid = m.get("intent_id")
        if not iid:
            entry["failures"].append("missing intent_id on proactive message")
            detail.append(entry)
            continue
        intent = store.load_proactive_intent(iid)
        if intent is None:
            entry["failures"].append(f"intent {iid} does not exist")
            detail.append(entry)
            continue
        entry["reason"] = intent.reason
        entry["source_type"] = intent.source_type
        entry["source_id"] = intent.source_id
        entry["opportunity_id"] = intent.opportunity_id
        if _intent_status(store, iid) != "fired":
            entry["failures"].append(f"intent {iid} status != fired")
        if intent.valid_until_t_h < float(m["t_h"]) - 1e-9:
            entry["failures"].append(
                f"intent {iid} expired (valid_until={intent.valid_until_t_h:.2f} "
                f"< message t_h={float(m['t_h']):.2f})"
            )
        src = store.resolve_intent_source(intent)
        if src is None:
            entry["failures"].append(
                f"intent {iid} source {intent.source_type}/{intent.source_id} missing"
            )
        else:
            if _source_superseded_at(store, src, intent):
                status = (src.status
                          if isinstance(src, (AgendaItem, LifeArc)) else "?")
                entry["failures"].append(
                    f"intent {iid} source is superseded ({status}) "
                    f"at intent creation (t_h={intent.created_t_h:.2f})"
                )
            if compose_hook(src, intent.reason) != intent.hook:
                entry["failures"].append("hook mismatch (compose_hook != intent.hook)")
        entry["ok"] = not entry["failures"]
        detail.append(entry)
    return len(proactives), detail


def _cycle_leak_hits(store: SQLiteStore) -> dict:
    """Escaneo de fugas (invariante 16): mensajes + prompts persistidos.

    Desde it3 B7 las filas de eval (audit_mode) llevan el sistema + payload
    EXACTOS en ``repro_json``: el escaneo del lado prompt corre contra el
    texto PERSISTIDO y puede cazar tokens prohibidos del ciclo. Las filas
    hash-only (runs no-eval) se contabilizan por separado y se reportan como
    NO verificables — no se finge cobertura. ``hits``/``total``/``g_bare``
    conservan la semántica agregada histórica; ``prompt_side`` es el
    desglose del lado prompt.
    """
    hits: dict[str, int] = {}
    g_bare = 0
    for m in _all_messages(store):
        text = str(m.get("content", ""))
        for hit in LEAK_RE.findall(text):
            hits[hit.lower()] = hits.get(hit.lower(), 0) + 1
        g_bare += len(G_BARE_RE.findall(text))
    try:
        rows = store.conn.execute(
            "SELECT role, model, response, meta, repro_json FROM llm_calls"
        ).fetchall()
    except Exception:  # noqa: BLE001
        # Auditoría en silencio = invariante muerto (revisión 2026-08-09):
        # el esquema real de llm_calls NO tiene system/reply — el scan del
        # lado prompt nunca se ejecutó; ahora el fallo es ruidoso.
        raise RuntimeError(
            "leak scan: llm_calls schema changed — audit cannot run silently"
        ) from None
    prompt_hits: dict[str, int] = {}
    prompt_g_bare = 0
    verifiable = 0
    hash_only = 0
    for row in rows:
        blob = " ".join(
            str(v) for v in row if v is not None
        )
        for hit in LEAK_RE.findall(blob):
            hits[hit.lower()] = hits.get(hit.lower(), 0) + 1
        g_bare += len(G_BARE_RE.findall(blob))
        repro = json.loads(row["repro_json"]) if row["repro_json"] else None
        if (
            repro is not None
            and repro.get("system") is not None
            and repro.get("messages") is not None
        ):
            verifiable += 1
            prompt_text = str(repro["system"]) + " " + " ".join(
                str(m.get("content", "")) if isinstance(m, dict) else ""
                for m in repro["messages"]
            )
            for hit in LEAK_RE.findall(prompt_text):
                prompt_hits[hit.lower()] = prompt_hits.get(hit.lower(), 0) + 1
            prompt_g_bare += len(G_BARE_RE.findall(prompt_text))
        else:
            hash_only += 1
    return {
        "hits": hits,
        "total": sum(hits.values()),
        "g_bare": g_bare,
        "prompt_side": {
            "verifiable_rows": verifiable,
            "hash_only_rows": hash_only,
            "hits": prompt_hits,
            "g_bare": prompt_g_bare,
        },
    }


def _memory_provenance_failures(store: SQLiteStore) -> list[str]:
    """Episodios cuyos source_turn_ids no existen en messages."""
    failures: list[str] = []
    msg_ids = {int(r["id"]) for r in store.conn.execute("SELECT id FROM messages")}
    for ep in store.list_episodes(limit=5000):
        for tid in ep.source_turn_ids:
            if int(tid) not in msg_ids:
                failures.append(f"episode {ep.id} references missing turn {tid}")
    return failures


def _life_dead_days(store: SQLiteStore, days: int) -> tuple[list[int], int]:
    """Días 'muertos' de vida: sin arco activo y sin agenda ese día.

    Devuelve (días muertos 0-indexados, duración del tramo final consecutivo).
    """
    active_ids = {a.id for a in store.list_life_arcs(status="active")}
    agenda_by_day: dict[int, set[str]] = {}
    for it in store.list_agenda_items():
        agenda_by_day.setdefault(int(it.start_t_h // 24.0), set()).add(it.activity)
    dead: list[int] = []
    for d in range(days):
        if d not in active_ids and not agenda_by_day.get(d):
            dead.append(d)
    trailing = 0
    for d in range(days - 1, -1, -1):
        if d in dead:
            trailing += 1
        else:
            break
    return dead, trailing


def _duplicate_turns(store: SQLiteStore) -> list[dict]:
    """Turnos duplicados a través de restarts (resume no debe rewindear).

    Clave = (role, content, t_h, day, proactive, intent_id). El flag
    proactive + intent_id desambiguan mensajes DISTINTOS que colisionan en
    (role, content, t_h, day) en runs reales: el reloj virtual se congela
    durante los calls LLM, así que una réplica reactiva y un disparo
    proactivo pueden compartir t_h, y el modelo puede repetir texto
    verbatim (o devolver vacío) — no es un rewind. Un rewind REAL reescribe
    la misma fila: mismo intent_id (proactivo) o misma (role, content, t_h,
    day, session) (reactivo) — la clave lo sigue capturando.
    """
    seen: dict[tuple, int] = {}
    dupes: list[dict] = []
    for m in _all_messages(store):
        key = (m["role"], m["content"], float(m["t_h"]), int(m["day"]),
               int(m["proactive"]), m.get("intent_id") or "")
        if key in seen:
            dupes.append({"first": seen[key], "dup": int(m["id"]), "key": list(key)})
        else:
            seen[key] = int(m["id"])
    return dupes


def mechanical_audit(store: SQLiteStore, seed: int, end_h: float, days: int,
                     script: list[tuple[float, str]],
                     pool: Sequence[str] | None = None,
                     n_skipped: int = 0) -> dict:
    """Auditoría mecánica completa del Track vertical (invariantes duras)."""
    msgs = _all_messages(store)
    n_pro, grounding = _proactive_grounding(store, end_h)
    i4 = _check_i4(store, end_h, seed)
    leaks = _cycle_leak_hits(store)
    prov = _memory_provenance_failures(store)
    dupes = _duplicate_turns(store)
    dead_days, dead_trailing = _life_dead_days(store, days)

    # No fixture inserts: todo mensaje user coincide con el guion POR
    # CONTENIDO (un feed tardío puede desplazar t_h al borde del segmento —
    # comportamiento documentado del runner; el contenido nunca se inventa);
    # todo assistant pertenece al pool determinista; los conteos cuadran.
    script_map = {txt: True for _t, txt in script}
    user_ok = True
    user_bad: list[dict] = []
    for m in msgs:
        if m["role"] == "user":
            if str(m["content"]) not in script_map:
                user_ok = False
                user_bad.append({"id": int(m["id"]), "t_h": float(m["t_h"]),
                                 "content": str(m["content"])[:80]})
    assistant_ok = True
    assistant_bad: list[dict] = []
    if pool is not None:
        poolset = set(pool)
        for m in msgs:
            if m["role"] == "assistant" and str(m["content"]) not in poolset:
                assistant_ok = False
                assistant_bad.append({"id": int(m["id"]),
                                      "content": str(m["content"])[:80]})

    # Conteos — aritmética documentada (evidencia: seed 5001 --fake 30d):
    #   guion = 51 feeds; entregados = 42 (37 exactos + 5 desplazados al
    #   borde del segmento, comportamiento documentado del runner);
    #   omitidos REALES = 9, pero la contabilidad registraba solo 1
    #   skipped_feed: las rutas de 'runtime terminó' / 'reloj pasó' de
    #   _run_segment descartaban el resto en silencio (subestimación de
    #   n_skipped -> la resta contra el guion sobrestimaba en 8).
    # Cada feed entregado produce EXACTAMENTE un mensaje user y una
    # réplica assistant; cada intent fired produce exactamente un mensaje
    # assistant proactivo. Invariantes de conteo:
    #   n_user_msgs   == len(script) - n_skipped   (guion vs feeds)
    #   n_assistant   == n_user_msgs + n_fired     (réplicas + proactivos)
    #   n_pro         == n_fired                   (1 mensaje por intent)
    n_user_msgs = sum(1 for m in msgs if m["role"] == "user")
    n_assistant_msgs = sum(1 for m in msgs if m["role"] == "assistant")
    n_fired = sum(1 for i in store.list_proactive_intents()
                  if _intent_status(store, i.id) == "fired")
    counts_consistent = (
        n_pro == n_fired
        and n_user_msgs == len(script) - n_skipped
        and n_assistant_msgs == n_user_msgs + n_fired
    )

    # Solo eventos VENCIDOS cuentan como stranded: el replan de la frontera
    # final planifica legítimamente el día recién abierto (eventos futuros).
    stranded = [
        {"t_h": float(r["t_h"]), "status": r["status"]}
        for r in store.pending_schedule_events(seed)
        if float(r["t_h"]) < end_h - 1e-6
    ]
    pending_overdue = [
        {"id": r["id"], "valid_until_t_h": float(r["valid_until_t_h"])}
        for r in store.conn.execute(
            "SELECT id, valid_until_t_h FROM proactive_intents "
            "WHERE status = 'pending' AND valid_until_t_h < ?", (end_h,)
        )
    ]

    audit = {
        "seed": seed,
        "end_h": end_h,
        "days": days,
        "n_messages": len(msgs),
        "n_proactive": n_pro,
        "n_fired_intents": n_fired,
        "counts_consistent": counts_consistent,
        "ungrounded_proactive": sum(1 for e in grounding if not e["ok"]),
        "grounding_detail": grounding,
        "wrong_intent": sum(
            1 for e in grounding if "hook mismatch" in " ".join(e["failures"])
        ),
        "restart_state_loss": 0,  # se rellena desde records en el driver
        "stranded_opportunities": len(stranded) + len(pending_overdue),
        "stranded_detail": {"pending_schedule": stranded,
                            "pending_intents": pending_overdue},
        "cycle_state_leakage": leaks["total"],
        "leak_detail": leaks,
        "memory_provenance_failures": len(prov),
        "provenance_detail": prov[:20],
        "duplicate_turns": len(dupes),
        "duplicate_detail": dupes[:10],
        "life_dead_days": dead_days,
        "life_dead_duration": dead_trailing,
        "i4_violations": i4,
        "fixture_inserts": {
            "user_ok": user_ok,
            "user_bad": user_bad,
            "assistant_ok": assistant_ok,
            "assistant_bad": assistant_bad,
        },
        "all_hard_zero": (
            sum(1 for e in grounding if not e["ok"]) == 0
            and len(stranded) + len(pending_overdue) == 0
            and leaks["total"] == 0
            and len(prov) == 0
            and len(dupes) == 0
            and dead_trailing == 0
            and len(i4) == 0
            and user_ok and (pool is None or assistant_ok)
            and counts_consistent
        ),
    }
    return audit


# --------------------------------------------------------------------------- #
# Cadenas de eventos (§17.2) — AnyEvidence / LatestEvidence / CompleteChain
# --------------------------------------------------------------------------- #


def _chain_event_tokens(chain: dict) -> tuple[str, str, str]:
    """Tokens distintivos por evento de la cadena (orden causal)."""
    return tuple(str(t) for t in chain["tokens"])  # type: ignore[return-value]


def _tokens_covered(tokens: Sequence[str], texts: Sequence[str]) -> list[bool]:
    """Cobertura por token: el token aparece como subcadena (minúsculas) en
    algún texto. Normaliza ambos lados; los callers ya pasan texto en
    minúsculas (idempotente)."""
    tokens = [str(t).lower() for t in tokens]
    texts = [str(t).lower() for t in texts]
    return [any(tok in text for text in texts) for tok in tokens]


def _chain_classification(chain: dict, covered: Sequence[bool]) -> dict:
    """Forma estándar de clasificación §17.2, compartida por todas las lanes."""
    covered = list(covered)
    return {
        "chain_id": chain["id"],
        "events": len(covered),
        "covered": covered,
        "AnyEvidence": any(covered),
        "LatestEvidence": covered[-1] if covered else False,
        "CompleteChain": all(covered) if covered else False,
    }


def classify_chain(retrieved: Sequence, chain: dict) -> dict:
    """Clasifica una recuperación según §17.2 (lane de episodios).

    Cobertura por evento: el texto del episodio (summary+tags+anclas) contiene
    el token distintivo del evento. AnyEvidence: >=1 evento cubierto.
    LatestEvidence: el evento más reciente cubierto. CompleteChain: TODOS.
    """
    tokens = _chain_event_tokens(chain)
    texts = [_episode_text(ep) for ep in retrieved]
    return _chain_classification(chain, _tokens_covered(tokens, texts))


# --------------------------------------------------------------------------- #
# Sonda justa RAW_HISTORY (B6/F5) — definición preregistrada
# --------------------------------------------------------------------------- #

#: Ventana de contexto crudo de la lane RAW_HISTORY: el mismo slice L1 que
#: ``harness.memory.raw_history`` (limit=12) entrega al ensamblador.
RAW_HISTORY_WINDOW_LIMIT = 12

RAW_HISTORY_FAIR_PROBE = """\
RAW_HISTORY fair probe (preregistered, B6/F5; manifest-ready).

Problem. RAW_HISTORY's lane is a raw dialogue window, not an episode store:
its retrieve returns ``recent_turns`` (the L1 slice, ``raw_history(store,
limit=12)``) and zero episodes. Scoring it with episode-keyed retrieval
(AnyEvidence/LatestEvidence/CompleteChain over ``ctx.episodes``) returns 0
by construction — a circular measurement.

Definition. A fact is RECOVERABLE by RAW_HISTORY iff at least one of its
distinctive tokens (lowercased substring match) appears in the raw dialogue
context the lane conditions on at query time t_q — the L1 recent-turns slice
restricted to turns with t_h < t_q, i.e. the last RAW_HISTORY_WINDOW_LIMIT
(12) persisted turns strictly before t_q, both roles, reconstructed from the
transcript (the live lane saw exactly this slice at that moment). The probe
window is the assembled recent-turns slice the model actually receives.

Scoring. Chain probes (query time = query_day * 24 h; days 1-indexed per
manifest): per-event coverage over the window; AnyEvidence = >=1 event
covered, LatestEvidence = most-recent event covered, CompleteChain = all
events covered. Single-fact probes (query time = probe_day * 24 + 6 h):
recalled = token present in the window. The raw lane performs no ranked
retrieval, so ``rank`` is null and M4_false_recall (a ranked-retrieval
artifact) is structurally 0.0 for this lane.

This measures the mechanism the condition actually uses — the window the
model receives — and never a store the lane does not have.
"""


def _raw_history_window(store: SQLiteStore, t_h: float, *,
                        limit: int = RAW_HISTORY_WINDOW_LIMIT,
                        ) -> tuple[tuple[str, str], ...]:
    """Slice L1 de diálogo crudo TAL COMO la lane RAW_HISTORY lo ve en t_h.

    Reconstrucción retrospectiva: los últimos ``limit`` turnos persistidos
    (rol, texto) con t_h' < t_h, en orden cronológico. ``recent_messages``
    ordena por id; filtrar por tiempo y quedarse con la cola reproduce
    exactamente el slice que ``raw_history`` habría devuelto en vivo en t_h.
    """
    rows = store.recent_messages(limit=1_000_000)
    return tuple(
        (r["role"], r["content"]) for r in rows if float(r["t_h"]) < t_h
    )[-limit:]


def _chain_classify_raw_history(store: SQLiteStore, chain: dict) -> dict:
    """Sonda justa RAW_HISTORY para cadenas (§17.2, B6)."""
    qday = int(chain["query_day"])
    window = _raw_history_window(store, qday * 24.0)
    tokens = _chain_event_tokens(chain)
    texts = [text.lower() for _role, text in window]
    covered = _tokens_covered(tokens, texts)
    cls = _chain_classification(chain, covered)
    cls["probe_lane"] = "raw_history"
    cls["context_turns"] = len(window)
    cls["retrieved_ids"] = []
    return cls


def event_chain_metrics(store: SQLiteStore, *, condition: str = "FULL",
                        memory_policy=None) -> dict:
    """Métricas de cadena de eventos por cadena — lane de la condición (B6/F5).

    El agente de recuperación se construye con ``_memory_for(condition)``: la
    MISMA lane con la que corrió la celda (FULL/SIMPLE_RAG/RAW_HISTORY/...).
    ``memory_policy`` se conserva para runs por policy (tracks A/B/C) bajo
    condiciones estructuradas. RAW_HISTORY usa la sonda justa (contexto crudo
    en t_q) porque su lane no almacena episodios.
    """
    if condition == "RAW_HISTORY":
        return {
            chain["id"]: _chain_classify_raw_history(store, chain)
            for chain in EVENT_CHAINS
        }
    mem = _memory_for(condition, store, memory_policy=memory_policy)
    out: dict = {}
    for chain in EVENT_CHAINS:
        qday = int(chain["query_day"])
        ctx = mem.retrieve(chain["query"], context={"t_h": qday * 24.0}, limit=8)
        cls = classify_chain(ctx.episodes, chain)
        cls["probe_lane"] = "episode_retrieval"
        cls["retrieved_ids"] = [e.id for e in ctx.episodes][:8]
        out[chain["id"]] = cls
    return out


def aggregate_chain_metrics(chains: dict) -> dict:
    """Rates ABSOLUTOS de cadena sobre las clasificaciones por cadena (B6).

    Reporte absoluto, no solo gaps: AnyEvidence/LatestEvidence/CompleteChain
    como fracción de cadenas probadas. FULL en 0.333 — una de cada tres — es
    el titular honesto, no solo su brecha frente a RAW_HISTORY.
    """
    items = list(chains.values())
    n = len(items)
    if not n:
        return {"n_chains": 0, "AnyEvidence": 0.0, "LatestEvidence": 0.0,
                "CompleteChain": 0.0}
    return {
        "n_chains": n,
        "AnyEvidence": round(sum(1 for c in items if c["AnyEvidence"]) / n, 4),
        "LatestEvidence": round(sum(1 for c in items if c["LatestEvidence"]) / n, 4),
        "CompleteChain": round(sum(1 for c in items if c["CompleteChain"]) / n, 4),
    }


RECALL_PROBES_TOKENS = {
    2: "bruno", 6: "luna", 10: "teal", 16: "oaxaca",
    18: "diego", 20: "wind", 22: "civic", 24: "ramen",
}


def _recall_probe_metrics_raw_history(store: SQLiteStore) -> dict:
    """Sonda justa RAW_HISTORY para sondas de hecho único (M3, B6)."""
    recall_hits = 0
    detail = []
    for pday, _probe, query in RECALL_PROBES:
        window = _raw_history_window(store, pday * 24.0 + 6.0)
        tok = RECALL_PROBES_TOKENS[pday]
        hit = any(tok in text.lower() for _role, text in window)
        recall_hits += int(hit)
        detail.append({
            "probe_day": pday, "query": query, "recalled": hit,
            "rank": None, "top_ids": [],
            "probe_lane": "raw_history", "context_turns": len(window),
        })
    return {
        "M3_recall": round(recall_hits / len(RECALL_PROBES), 4),
        "M4_false_recall": 0.0,  # la lane cruda no hace retrieval rankeado (B6)
        "detail": detail,
    }


def recall_probe_metrics(store: SQLiteStore, *, condition: str = "FULL",
                         memory_policy=None) -> dict:
    """Recuerdo de sondas de hecho único (M3/M4): recall@8 por contenido,
    probado con la lane de la condición (B6/F5)."""
    if condition == "RAW_HISTORY":
        return _recall_probe_metrics_raw_history(store)
    mem = _memory_for(condition, store, memory_policy=memory_policy)
    recall_hits = 0
    false_recall = 0
    detail = []
    for pday, _probe, query in RECALL_PROBES:
        ctx = mem.retrieve(query, context={"t_h": pday * 24.0 + 6.0}, limit=8)
        tok = RECALL_PROBES_TOKENS[pday]
        ranks = [i for i, e in enumerate(ctx.episodes) if tok in _episode_text(e)]
        hit = bool(ranks)
        rank = ranks[0] + 1 if ranks else None
        recall_hits += int(hit)
        if hit and rank != 1:
            false_recall += 1
        detail.append({
            "probe_day": pday, "query": query, "recalled": hit,
            "rank": rank, "top_ids": [e.id for e in ctx.episodes][:5],
            "probe_lane": "episode_retrieval",
        })
    return {
        "M3_recall": round(recall_hits / len(RECALL_PROBES), 4),
        "M4_false_recall": round(false_recall / len(RECALL_PROBES), 4),
        "detail": detail,
    }


# --------------------------------------------------------------------------- #
# Métricas
# --------------------------------------------------------------------------- #


def _spearman(x: Sequence[float], y: Sequence[float]) -> float:
    x = [float(v) for v in x]
    y = [float(v) for v in y]
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return 0.0
    rx = {v: i for i, v in enumerate(sorted(set(x)))}
    ry = {v: i for i, v in enumerate(sorted(set(y)))}
    xr = np.asarray([rx[v] for v in x], dtype=float)
    yr = np.asarray([ry[v] for v in y], dtype=float)
    xr -= xr.mean()
    yr -= yr.mean()
    denom = math.sqrt(float((xr ** 2).sum() * float((yr ** 2).sum())))
    if denom == 0.0:
        return 0.0
    return float((xr * yr).sum() / denom)


def _valid_arc_transition(a: str, b: str) -> bool:
    if a == b:
        return True
    if a == "active":
        return b in ("completed", "abandoned")
    return False


def compute_structural_metrics(store: SQLiteStore, records: dict, condition: str,
                               seed: int, days: int) -> dict:
    """Métricas estructurales M1-M11 (definiciones congeladas del manifest)."""
    msgs = _all_messages(store)
    proactives = [m for m in msgs if m["proactive"]]
    n_pro = len(proactives)

    # --- M1 / M2: unión de cada mensaje proactivo con SU intent fired -------
    n_grounded = 0
    n_invalid = 0
    grounding_detail = []
    for m in proactives:
        iid = m.get("intent_id")
        ok = False
        if iid:
            intent = store.load_proactive_intent(iid)
            if intent is not None and _intent_status(store, iid) == "fired":
                if intent.valid_until_t_h >= float(m["t_h"]) - 1e-9:
                    src = store.resolve_intent_source(intent)
                    if src is not None:
                        # Clamp TOCTOU (mismo que la auditoría, 453dfcb): el
                        # estado FINAL del run es anacrónico — un skip escrito
                        # en el cierre del día DESPUÉS del disparo no invalida
                        # la fuente al momento del mensaje.
                        superseded = _source_superseded_at(store, src, intent)
                        if not superseded and compose_hook(src, intent.reason) == intent.hook:
                            ok = True
        if ok:
            n_grounded += 1
        else:
            n_invalid += 1
            grounding_detail.append({
                "message_id": int(m["id"]), "intent_id": iid,
                "t_h": float(m["t_h"]),
            })
    m1 = n_grounded / n_pro if n_pro else 0.0
    m2 = n_invalid / n_pro if n_pro else 0.0

    # --- M3 / M4: recuerdo de sondas (lane de la condición, B6/F5) -----------
    recall = recall_probe_metrics(store, condition=condition)
    m3 = recall["M3_recall"]
    m4 = recall["M4_false_recall"]

    # --- M5: continuidad de arcos de vida -----------------------------------
    arc_seq: dict[str, list[tuple[int, float, str]]] = {}
    for day_str in records["arc_progress_by_day"]:
        day = int(day_str)
        for aid, (progress, status) in records["arc_progress_by_day"][day_str].items():
            arc_seq.setdefault(aid, []).append((day, float(progress), status))
    arcs_total = len(arc_seq)
    arcs_ok = 0
    arcs_alive = 0
    for aid, seq in arc_seq.items():
        seq = sorted(seq)
        progs = [p for _, p, _ in seq]
        statuses = [s for _, _, s in seq]
        regression = any(b < a - 1e-9 for a, b in zip(progs, progs[1:]))
        valid_trans = all(
            _valid_arc_transition(a, b) for a, b in zip(statuses, statuses[1:])
        )
        if not regression and valid_trans:
            arcs_ok += 1
        if statuses[-1] in ("active", "completed"):
            arcs_alive += 1
    m5 = arcs_ok / arcs_total if arcs_total else 0.0

    # --- M6: diversidad / recurrencia de agenda -----------------------------
    agenda_by_day: dict[int, set[str]] = {}
    for it in store.list_agenda_items():
        agenda_by_day.setdefault(int(it.start_t_h // 24.0), set()).add(it.activity)
    distinct_activities = len({a for s in agenda_by_day.values() for a in s})
    jaccards = []
    days_sorted = sorted(agenda_by_day)
    for a, b in zip(days_sorted, days_sorted[1:]):
        sa, sb = agenda_by_day[a], agenda_by_day[b]
        inter = len(sa & sb)
        union = len(sa | sb)
        if union:
            jaccards.append(inter / union)
    m6a = distinct_activities
    m6b = float(np.mean(jaccards)) if jaccards else 0.0
    arc_days_set: dict[str, set[int]] = {}
    for it in store.list_agenda_items():
        if it.source_type == "arc":
            arc_days_set.setdefault(it.source_id, set()).add(int(it.start_t_h // 24.0))
    active_arc_ids = {a.id for a in store.list_life_arcs(status="active")}
    arc_contrib = {aid: len(arc_days_set.get(aid, set())) for aid in active_arc_ids}
    m6c = float(np.mean(list(arc_contrib.values()))) if arc_contrib else 0.0
    m6c_min = min(arc_contrib.values()) if arc_contrib else 0

    # --- M7: pérdida por reinicio -------------------------------------------
    m7 = sum(rl["diffs"] for rl in records["restart_loss"])

    # --- M8: actuación (estado -> observable) -------------------------------
    daily: dict[int, list[dict]] = {}
    for mid_str, c in records["controls_by_message"].items():
        mid = int(mid_str)
        row = next((m for m in msgs if m["id"] == mid), None)
        if row is None or row["proactive"]:
            continue
        d = records["directives_by_message"].get(mid_str, {})
        daily.setdefault(int(row["day"]), []).append({
            "initiative": float(d.get("initiative", 0.5)),
            "energy": float(d.get("energy", 0.5)),
            "max_tokens": int(c["max_tokens"]),
            "delay": float(c["response_delay_s"]),
        })
    daily_agg = [
        {
            "initiative": float(np.mean([x["initiative"] for x in daily[d]])),
            "energy": float(np.mean([x["energy"] for x in daily[d]])),
            "max_tokens": float(np.mean([x["max_tokens"] for x in daily[d]])),
            "delay": float(np.mean([x["delay"] for x in daily[d]])),
        }
        for d in sorted(daily)
    ]
    initiatives = [x["initiative"] for x in daily_agg]
    tokens = [x["max_tokens"] for x in daily_agg]
    energies = [x["energy"] for x in daily_agg]
    delays = [x["delay"] for x in daily_agg]
    m8a = _spearman(initiatives, tokens)
    med = float(np.median(initiatives)) if initiatives else 0.5
    hi = [t for i, t in zip(initiatives, tokens) if i >= med]
    lo = [t for i, t in zip(initiatives, tokens) if i < med]
    m8b = abs(float(np.mean(hi)) - float(np.mean(lo))) if hi and lo else 0.0
    m8c = _spearman(energies, delays)

    # --- M9 / M10: proactividad vs feedback / iniciativa --------------------
    counts = [sum(1 for m in msgs if m["proactive"] and m["day"] == d)
              for d in range(days)]
    scores = []
    for d in range(1, days):
        j = store.load_judgement(d - 1)
        scores.append(float(j["score"]) if j and j["score"] is not None else 0.0)
    m9 = _spearman(counts[1:], scores)
    daily_init_mean: dict[int, list[float]] = {}
    for mid_str, d in records["directives_by_message"].items():
        row = next((m for m in msgs if m["id"] == int(mid_str)), None)
        if row is not None:
            daily_init_mean.setdefault(int(row["day"]), []).append(float(d["initiative"]))
    init_series = [
        float(np.mean(daily_init_mean.get(d, [0.5]))) for d in range(days)
    ]
    m10 = _spearman(init_series, counts)

    # --- M11: fugas de estado -----------------------------------------------
    leaks = _cycle_leak_hits(store)
    m11 = leaks["total"]

    return {
        "seed": seed,
        "condition": condition,
        "M1_grounded_rate": round(m1, 4),
        "M2_invalid_source_rate": round(m2, 4),
        "M3_recall": round(m3, 4),
        "M4_false_recall": round(m4, 4),
        "M5_arc_continuity": round(m5, 4),
        "M6a_distinct_activities": m6a,
        "M6b_mean_jaccard": round(m6b, 4),
        "M6c_arc_days_mean": round(m6c, 4),
        "M6c_arc_days_min": m6c_min,
        "M7_restart_loss": m7,
        "M8a_rho_init_tokens": round(m8a, 4),
        "M8b_token_gap": round(m8b, 2),
        "M8c_rho_energy_delay": round(m8c, 4),
        "M9_rho_proactive_prevscore": round(m9, 4),
        "M10_rho_proactive_initiative": round(m10, 4),
        "M11_leak_hits": m11,
        "leak_hits_detail": leaks["hits"],
        "g_bare_hits": leaks["g_bare"],
        "n_proactive": n_pro,
        "n_messages": len(msgs),
        "probe_detail": recall["detail"],
        "arc_contribution_days": {k: v for k, v in sorted(arc_contrib.items())},
        "i4_violations": records["i4_violations"],
        "restart_loss_detail": records["restart_loss"],
        "grounding_failures_detail": grounding_detail,
    }


def _daily_series(store: SQLiteStore, days: int) -> dict[int, dict]:
    """Serie diaria persistida (M, m, g, mu, eta, phase, score) por día 0-idx."""
    out: dict[int, dict] = {}
    for d in range(days):
        row = store.load_daily_state(d)
        if row is not None:
            out[d] = dict(row)
    return out


def compute_state_metrics(store: SQLiteStore, records: dict, days: int) -> dict:
    """Track estado: observabilidad estructurada + dinámica a nivel mensaje."""
    msgs = _all_messages(store)
    series = _daily_series(store, days)

    per_msg: dict[int, dict] = {}
    for mid_str, c in records["controls_by_message"].items():
        mid = int(mid_str)
        row = next((m for m in msgs if m["id"] == mid), None)
        if row is None:
            continue
        d = records["directives_by_message"].get(mid_str, {})
        per_msg[mid] = {
            "day": int(row["day"]),
            "proactive": bool(row["proactive"]),
            "initiative": float(d.get("initiative", 0.5)),
            "energy": float(d.get("energy", 0.5)),
            "valence": float(d.get("valence", 0.0)),
            "max_tokens": int(c["max_tokens"]),
            "delay": float(c["response_delay_s"]),
            "closing_tendency": float(c["closing_tendency"]),
        }

    delays = [v["delay"] for v in per_msg.values()]
    tokens = [v["max_tokens"] for v in per_msg.values()]
    initiatives = [v["initiative"] for v in per_msg.values()]
    closings = [v["closing_tendency"] for v in per_msg.values()]

    daily: dict[int, list[dict]] = {}
    for v in per_msg.values():
        if not v["proactive"]:
            daily.setdefault(v["day"], []).append(v)
    days_sorted = sorted(series)
    M_series = [float(series[d]["M"]) for d in days_sorted]
    init_daily = [float(np.mean([v["initiative"] for v in daily[d]])) if daily.get(d) else 0.5
                  for d in days_sorted]
    tokens_daily = [float(np.mean([v["max_tokens"] for v in daily[d]])) if daily.get(d) else 600.0
                    for d in days_sorted]
    delay_daily = [float(np.mean([v["delay"] for v in daily[d]])) if daily.get(d) else 5.0
                   for d in days_sorted]
    energy_daily = [float(np.mean([v["energy"] for v in daily[d]])) if daily.get(d) else 0.5
                    for d in days_sorted]
    g_series = [float(series[d]["g"]) for d in days_sorted]
    mu_series = [float(series[d]["mu"]) for d in days_sorted]
    phase_series = [str(series[d]["phase_label"]) for d in days_sorted]

    counts = [sum(1 for m in msgs if m["proactive"] and m["day"] == d)
              for d in range(days)]

    # Puntuación previa por día (M9 usa counts[1:] vs scores de días 0..n-2)
    prev_scores = [
        float(series[d]["score"]) if series.get(d) and series[d].get("score") is not None else 0.0
        for d in range(days)
    ]

    return {
        "seed": records["seed"],
        "condition": records["condition"],
        "structured_observability": {
            "M_series": M_series,
            "g_series": [round(v, 4) for v in g_series],
            "mu_series": [round(v, 4) for v in mu_series],
            "phase_series": phase_series,
            "arcs_by_day": {str(k): v for k, v in records["arc_progress_by_day"].items()},
            "n_arcs_total": len(store.list_life_arcs()),
            "n_arcs_active_end": len(store.list_life_arcs(status="active")),
            "n_arcs_completed": len(store.list_life_arcs(status="completed")),
            "n_arcs_abandoned": len(store.list_life_arcs(status="abandoned")),
            "distinct_agenda_activities": len({
                a for it in store.list_agenda_items() for a in [it.activity]
            }),
        },
        "behavioral_dynamics": {
            "n_messages": len(per_msg),
            "delay_mean": round(float(np.mean(delays)), 3) if delays else None,
            "delay_sd": round(float(np.std(delays)), 3) if delays else 0.0,
            "delay_min": round(float(np.min(delays)), 3) if delays else None,
            "delay_max": round(float(np.max(delays)), 3) if delays else None,
            "max_tokens_mean": round(float(np.mean(tokens)), 1) if tokens else None,
            "max_tokens_sd": round(float(np.std(tokens)), 1) if tokens else 0.0,
            "initiative_mean": round(float(np.mean(initiatives)), 3) if initiatives else None,
            "initiative_sd": round(float(np.std(initiatives)), 3) if initiatives else 0.0,
            "closing_tendency_sd": round(float(np.std(closings)), 3) if closings else 0.0,
            "M8a_rho_init_tokens": round(_spearman(init_daily, tokens_daily), 4),
            "M8b_token_gap": _token_gap(init_daily, tokens_daily),
            "M8c_rho_energy_delay": round(_spearman(energy_daily, delay_daily), 4),
            "M9_rho_proactive_prevscore": round(_spearman(prev_scores[:-1], counts[1:]), 4),
            "M10_rho_proactive_initiative": round(_spearman(init_daily, counts), 4),
        },
        "state_to_observable": {
            "rho_M_initiative": round(_spearman(M_series, init_daily), 4),
            "rho_M_max_tokens": round(_spearman(M_series, tokens_daily), 4),
            "rho_g_delay": round(_spearman(g_series, delay_daily), 4),
            "rho_mu_proactive_count": round(_spearman(mu_series, counts), 4),
        },
    }


def _token_gap(init_daily: Sequence[float], tokens_daily: Sequence[float]) -> float:
    if len(init_daily) < 2:
        return 0.0
    med = float(np.median(init_daily))
    hi = [t for i, t in zip(init_daily, tokens_daily) if i >= med]
    lo = [t for i, t in zip(init_daily, tokens_daily) if i < med]
    if not hi or not lo:
        return 0.0
    return round(abs(float(np.mean(hi)) - float(np.mean(lo))), 2)


def compute_perturbation_metrics(store: SQLiteStore, records: dict,
                                 days: int) -> dict:
    """Métricas de perturbación + recuperación (§17.3).

    Serie latente: M diario. Serie observable: initiative / max_tokens /
    delay diarios (mensajes reactivos). Línea base = días 0..block_start-1.
    """
    series = _daily_series(store, days)
    msgs = _all_messages(store)

    daily: dict[int, list[dict]] = {}
    for mid_str, c in records["controls_by_message"].items():
        mid = int(mid_str)
        row = next((m for m in msgs if m["id"] == mid), None)
        if row is None or row["proactive"]:
            continue
        d = records["directives_by_message"].get(mid_str, {})
        daily.setdefault(int(row["day"]), []).append({
            "initiative": float(d.get("initiative", 0.5)),
            "max_tokens": int(c["max_tokens"]),
            "delay": float(c["response_delay_s"]),
        })

    def daily_mean(key: str, default: float) -> list[float]:
        return [float(np.mean([x[key] for x in daily[d]])) if daily.get(d) else default
                for d in range(days)]

    series_by_key = {
        "M": [float(series[d]["M"]) for d in range(days)],
        "initiative": daily_mean("initiative", 0.5),
        "max_tokens": daily_mean("max_tokens", 600.0),
        "delay": daily_mean("delay", 5.0),
    }

    base_end = min(BLOCK_START_D, days)
    block = [d for d in range(BLOCK_START_D, BLOCK_END_D + 1) if d < days]

    def block_analysis(values: list[float]) -> dict:
        base = values[:base_end]
        base_mean = float(np.mean(base)) if base else 0.0
        base_sd = float(np.std(base)) if len(base) > 1 else 0.0
        block_mean = float(np.mean([values[d] for d in block])) if block else base_mean
        deviation = block_mean - base_mean
        window = values[BLOCK_START_D:]
        peak_dev = max((abs(v - base_mean) for v in window), default=0.0)
        persistence = None
        for i, v in enumerate(window):
            if abs(v - base_mean) < 0.5 * peak_dev:
                persistence = i
                break
        recovery_time = None
        band = max(base_sd, 0.05)
        for i in range(BLOCK_END_D + 1, days - 1):
            if (abs(values[i] - base_mean) <= band
                    and abs(values[i + 1] - base_mean) <= band):
                recovery_time = i - BLOCK_END_D
                break
        return {
            "baseline_mean": round(base_mean, 4),
            "baseline_sd": round(base_sd, 4),
            "block_mean": round(block_mean, 4),
            "deviation": round(deviation, 4),
            "peak_deviation": round(peak_dev, 4),
            "persistence_days": persistence,
            "recovery_time_days": recovery_time,
        }

    n_pro, grounding = _proactive_grounding(store, days * 24.0)
    block_failures = [
        e for e in grounding
        if not e["ok"] and BLOCK_START_D <= int(e["t_h"] // 24.0) <= BLOCK_END_D
    ]

    return {
        "seed": records["seed"],
        "condition": records["condition"],
        "block_days_0idx": block,
        "latent": {"M": block_analysis(series_by_key["M"])},
        "observable": {
            "initiative": block_analysis(series_by_key["initiative"]),
            "max_tokens": block_analysis(series_by_key["max_tokens"]),
            "delay": block_analysis(series_by_key["delay"]),
        },
        "failure_frequency": {
            "n_proactive_during_block": sum(
                1 for e in grounding
                if BLOCK_START_D <= int(e["t_h"] // 24.0) <= BLOCK_END_D
            ),
            "failures_during_block": len(block_failures),
            "failure_detail": block_failures,
        },
    }


# --------------------------------------------------------------------------- #
# Transcript + replay
# --------------------------------------------------------------------------- #


def render_transcript(store: SQLiteStore, *, persona_name: str = "Nova") -> str:
    """Render del transcript conversacional (para jueces ciegos)."""
    rows = _all_messages(store)
    lines = []
    for m in rows:
        day = int(m["day"]) + 1
        local = float(m["t_h"]) % 24.0
        hh = int(local)
        mm = int(round((local - hh) * 60.0)) % 60
        who = persona_name if m["role"] == "assistant" else "You"
        lines.append(f"Day {day}, {hh:02d}:{mm:02d}\n{who}: {m['content']}")
    return "\n\n".join(lines)


def _llm_call_rows(store: SQLiteStore) -> list[dict]:
    return [dict(r) for r in store.conn.execute(
        "SELECT day, t_h, role, model, prompt_hash, response, meta, repro_json "
        "FROM llm_calls ORDER BY id"
    ).fetchall()]


def message_stream(store: SQLiteStore) -> list[dict]:
    """Stream canónico de mensajes (para comparación de replay)."""
    return [
        {"role": m["role"], "content": m["content"], "t_h": round(float(m["t_h"]), 6),
         "day": int(m["day"]), "proactive": bool(m["proactive"]),
         "intent_id": m.get("intent_id"), "session_id": m.get("session_id")}
        for m in _all_messages(store)
    ]


def run_replay(seed: int, days: int, run_dir: Path, out_dir: Path) -> dict:
    """Replay exacto de un escenario grabado.

    Re-corre la célula (misma semilla/días/checkpoints/condición) en un DB
    fresco y compara byte-a-byte: stream de mensajes, filas llm_calls (M3) y
    eventos de agenda. Emite ``reproducibility_audit.json``.
    """
    import shutil

    run_dir = Path(run_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    original_db = run_dir / f"cell_full_seed{seed}.db"
    if not original_db.exists():
        raise FileNotFoundError(f"recorded run db missing: {original_db}")
    orig_store = SQLiteStore(original_db)
    orig_msgs = message_stream(orig_store)
    orig_calls = _llm_call_rows(orig_store)
    orig_schedule = [dict(r) for r in orig_store.schedule_events_for_seed(seed)]
    orig_store.close()

    # El replay debe re-correr la célula con los parámetros EXACTOS del run
    # grabado: los checkpoints de reinicio cambian qué intents disparan en
    # las fronteras de segmento, así que re-correr con los defaults
    # rompería la igualdad byte-a-byte (confounder de replay). Se leen de
    # records_<condition>_seed<seed>.json (invariante 19: todo reconstruible
    # desde el manifest/registros inmutables del run).
    rec_paths = sorted(Path(run_dir).glob(f"records_*_seed{seed}.json"))
    rec: dict | None = None
    if rec_paths:
        try:
            rec = json.loads(rec_paths[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rec = None
    condition = str(rec["condition"]) if rec and rec.get("condition") else "FULL"
    days = int(rec["days"]) if rec and rec.get("days") is not None else days
    # records["checkpoints"] se persiste en HORAS (checkpoint_ends = d*24.0);
    # run_cell los recibe en DÍAS — reconvertir o el replay re-corre sin
    # reinicios (un checkpoint=48h mal leído como 48 días se descarta y el
    # run pierde la frontera de reinicio: confounder de replay).
    checkpoints = (tuple(int(c // 24.0) for c in rec["checkpoints"]) if rec
                   and rec.get("checkpoints") is not None
                   else DEFAULT_CHECKPOINT_DAYS)
    perturb = bool(rec["perturb"]) if rec and rec.get("perturb") is not None else True
    # "structured_memory" es el marcador del default (memory_policy=None).
    mp = rec.get("memory_policy") if rec else None
    memory_policy = None if mp in (None, "structured_memory") else mp

    replay_dir = out_dir / f"replay_seed{seed}"
    if replay_dir.exists():
        shutil.rmtree(replay_dir)
    records = run_cell(condition, seed, replay_dir, days=days,
                       checkpoints=checkpoints, fake=True, perturb=perturb,
                       memory_policy=memory_policy)
    replay_store = SQLiteStore(records["db"])
    replay_msgs = message_stream(replay_store)
    replay_calls = _llm_call_rows(replay_store)
    replay_schedule = [dict(r) for r in replay_store.schedule_events_for_seed(seed)]
    replay_store.close()

    msgs_equal = orig_msgs == replay_msgs
    calls_equal = (
        [c["prompt_hash"] for c in orig_calls] == [c["prompt_hash"] for c in replay_calls]
        and [c["response"] for c in orig_calls] == [c["response"] for c in replay_calls]
        and [c["repro_json"] for c in orig_calls] == [c["repro_json"] for c in replay_calls]
    )
    schedule_equal = (
        sorted((float(r["t_h"]), r["status"]) for r in orig_schedule)
        == sorted((float(r["t_h"]), r["status"]) for r in replay_schedule)
    )

    audit = {
        "seed": seed,
        "days": days,
        "condition": condition,
        "messages_equal": msgs_equal,
        "llm_calls_equal": calls_equal,
        "schedule_equal": schedule_equal,
        "n_messages": len(orig_msgs),
        "n_llm_calls": len(orig_calls),
        "n_unique_prompt_hashes": len({c["prompt_hash"] for c in orig_calls}),
        "n_schedule_events": len(orig_schedule),
        "m3_rows_readable": all(c["repro_json"] is not None for c in orig_calls),
        "replay_exact": msgs_equal and calls_equal and schedule_equal,
        "original_db": str(original_db),
        "replay_db": str(records["db"]),
    }
    (out_dir / "reproducibility_audit.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return audit


# --------------------------------------------------------------------------- #
# Utilidades de judge (ciego, barajado, 4 dimensiones)
# --------------------------------------------------------------------------- #


def shuffled_order(rng: np.random.Generator,
                   items: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Baraja con la restricción de que nunca haya dos del mismo condition
    adyacentes (construcción round-robin sobre bloques barajados)."""
    by_cond: dict[str, list[tuple[str, int]]] = {}
    for c, s in items:
        by_cond.setdefault(c, []).append((c, s))
    conds = list(by_cond)
    rng.shuffle(conds)
    for c in conds:
        rng.shuffle(by_cond[c])
    order: list[tuple[str, int]] = []
    max_len = max(len(v) for v in by_cond.values())
    for i in range(max_len):
        for c in conds:
            if i < len(by_cond[c]):
                order.append(by_cond[c][i])
    return order


def parse_ratings(raw: str, dimensions: Sequence[str]) -> dict:
    """Parsea el JSON de puntuaciones por dimensión (tolerante)."""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    out = {}
    for dim in dimensions:
        v = data.get(dim)
        try:
            out[dim] = float(v)
        except (TypeError, ValueError):
            out[dim] = None
    return out
