"""Session — the e2e daily loop (W-E1) + Wave 2 central integration (A1).

Wires engine → behavior → life → memory → assembler → client → judge → store
under a virtual clock. Engine replay contract (frozen in sim/run_daily): per
day t, the FIRST consumer of day_rng(seed, t) is cycle.step, then mood.step;
mood.step_endogenous consumes the SAME generator at day end. This module
preserves that order so a session's mood sequence replays exactly like
`sim.run_daily` for the same seed.

Wave 2 (vertical slice): the system prompt is assembled from a
``CompanionSnapshot`` — the single place the lanes meet before composition.
Reactive turn: user message → persist → DayRecord → BehaviorDirective →
GenerationControls + BehaviorBrief → CurrentActivity → MemoryContext →
active life arcs → CompanionSnapshot → assembler → client (max_tokens from
controls) → TurnResult(controls) for the runtime's delivery path.
Proactive turn: grounded ProactiveIntent fetched by its EXACT id
(``fire_proactive(intent_id)`` — never a reason-type lookup; two intents
with the same reason are never interchangeable) → CompanionSnapshot
(proactive_intent=...) → assembler (renders the intent's concrete hook
verbatim) → the outgoing message persists ``message.intent_id``.

Lane rule: this session COMPOSES the snapshot; memory/life/persona never
mutate each other. Memory writes happen ONLY via MemoryAgent calls at session
boundaries (close_session/promote/update_user_model at day finalize); life
writes happen ONLY through harness.life entry points at day boundaries
(generate_agenda at rollover, step_life at finalize). Both use the reserved
LIFE stream (stream_rng(seed, 4, day)) — NEVER day_rng — so the engine
replay contract is untouched.

Day lifecycle:
  - rollover (start of day): sample m/g/phase/M for the day, persist
    daily_state, hold the day's RNG generator for the end-of-day update,
    register the memory session, plan the day's life agenda.
  - during the day: on_message() derives the behavior directive + controls,
    composes the snapshot, assembles the prompt, calls the client, persists
    messages + trace.
  - finalize (when the clock moves past the day): judge the day's exchange
    (shadow by default — recorded, does NOT touch mu), close the memory
    session (L2 summary + L3 promotion + L4 consolidation), step the life
    lane, then apply the end-of-day engine update (mu ← score in feedback
    mode, eta AR(1) always).

Resume: with the same seed + store, the latest daily_state row restores
mu/eta (values "used" that day = state at start); the cycle clock is
reconstructed by replaying init_rng + cycle.step from day 0. Life and memory
state are restored from the store (arcs, agenda, summaries, episodes). A
driver clock that restarts BEHIND the store's progressed day is fast-
forwarded to the store's day (resume must never rewind — Iteration-2 A5
routed defect).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from typing import Literal

import engine.rng as rng_mod
from engine import cycle, mood
from engine.rng import stream_rng
from engine.types import (
    CycleState,
    DayRecord,
    MoodState,
    MoodVariant,
    PersonaParams,
    TimingParams,
)
from harness import life
from harness.actuation import controls_from_directive, to_brief
from harness.assembler import (
    DEFAULT_PERSONA_CORE,
    RECENT_TURNS,
    assemble_snapshot,
    build_messages,
    proactive_block,
)
from harness.behavior import BehaviorDirective, derive_behavior
from harness.clock import VirtualClock
from harness.client import LLMClient
from harness.domain import (
    BehaviorBrief,
    CompanionSnapshot,
    Conversation,
    ConversationTurn,
    CurrentActivity,
    DailyAgenda,
    GenerationControls,
    LifeArc,
    MemoryContext,
    PersonaProfile,
    ProactiveIntent,
    Turn,
)
from harness.judge import JudgeResult, judge_day
from harness.life import LIFE_STREAM
from harness.memory import MemoryAgent
from harness.scheduler import VALID_REASONS
from harness.score import synthetic_score as run_daily_synthetic_score
from harness.store import SQLiteStore

#: Closing-tendency draw stream (it3 B2): a dedicated engine.rng stream.
#: Streams 0..5 are reserved by other lanes (0=DAILY, 1=EVENTS, 2=EXPERIMENT,
#: 3=INIT, 4=LIFE, 5=PERSONA); 6 is this lane's. Each draw is keyed by
#: (conversation sequence, companion turn's 0-based index within the
#: conversation), so every conversation draws its OWN deterministic
#: sequence — the k-th draw of conversation n is always
#: ``stream_rng(seed, 6, n, k)``, and a resumed conversation continues
#: exactly where it left off (no re-draws).
CONVERSATION_STREAM = 6

#: ``close_reason == "user_left"`` threshold: virtual hours of user silence
#: (measured from the conversation's last USER turn — or its opening when
#: the companion opened and the user never replied) after which the
#: conversation is closed.
USER_LEFT_THRESHOLD_H = 12.0

#: Hard cap on conversation length in total turns (user + companion): no
#: conversation runs forever (``close_reason == "max_turns"``).
MAX_TURNS = 12

#: Namespace base for per-conversation MEMORY SESSION ids. The MemoryAgent
#: seam (harness/memory.py — must-not-touch) parses session ids with
#: ``re.fullmatch(r"day-(\\d+)", ...)`` (``_day_of``: judgement lookup and
#: the eager ``started`` default in ``close_session``), so conversation-
#: scoped session ids must stay day-shaped. They are namespaced ABOVE any
#: real day count: conversation ``conv-<n>`` maps to memory session
#: ``day-<OFFSET+n>``. One memory session per conversation still holds —
#: each conversation gets a unique id; the seam itself is used as-is (the
#: brief: "you change which ids the session passes, not the seam"). A
#: one-line lazy-default fix in memory.py would let honest ids through —
#: reported to the orchestrator.
CONVERSATION_SESSION_OFFSET = 1000


@dataclass
class TurnResult:
    """What one on_message()/fire_proactive() produced: the reply + the
    observable state + the mechanical delivery controls.

    Wave 2: ``controls`` (GenerationControls) is what the runtime's delivery
    path reads for ``response_delay_s``; ``directive`` remains for legacy
    callers (runtime falls back to it when controls is None).
    """

    reply: str
    directive: BehaviorDirective
    day: int
    hour: float
    controls: GenerationControls | None = None


class _NoopMemory:
    """Memory-seam fallback for stores without the A5 tiers (legacy fakes).

    ``retrieve`` returns an empty ``MemoryContext``; ``close_session``
    returns None (the session then skips promote/update — no provenance, no
    truth). Used only when the injected store lacks the A5 memory methods;
    the real ``MemoryAgent`` is used whenever the seam is present.
    """

    def retrieve(self, query: str, *, context: dict | None = None, limit: int = 8) -> MemoryContext:
        return MemoryContext(
            recent_turns=(), session_context=(), episodes=(),
            user_model=None, evidence_anchors=(),
        )

    def close_session(self, session_id: str, *, ended_at_t_h: float) -> None:
        return None

    def promote(self, summary) -> list:
        return []

    def update_user_model(self, summary) -> list:
        return []


class Session:
    """One companion run: engine state + store + client under a virtual clock."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        persona: PersonaParams,
        timing: TimingParams,
        variant: MoodVariant,
        seed: int,
        client: LLMClient,
        clock: VirtualClock,
        judge=judge_day,
        feedback: bool = False,
        persona_core: str | None = None,
        judge_model: str | None = None,
        synthetic_score: bool = False,
        persona_profile: PersonaProfile | None = None,
        memory: MemoryAgent | None = None,
    ):
        self.store = store
        self.persona = persona
        self.timing = timing
        self.variant = variant
        self.seed = seed
        self.client = client
        self.clock = clock
        self.judge = judge
        self.feedback = feedback
        self.persona_core = persona_core
        self.judge_model = judge_model
        # synthetic_score=True replicates sim.run_daily's score source
        # (clip(2(M/N-0.5)+Normal(0,0.2))) INCLUDING its RNG draw, so the
        # session's mood sequence is byte-identical to run_daily for the same
        # seed. The judge path consumes no RNG (score is external).
        self.synthetic_score = synthetic_score

        # Wave 2 lanes: persona / life / memory are store-backed; the session
        # only COMPOSES them into CompanionSnapshots (lane rule). MemoryAgent
        # is injectable (tests may substitute a recording agent).
        self._profile: PersonaProfile | None = (
            persona_profile
            if persona_profile is not None
            else (store.load_persona() if hasattr(store, "load_persona") else None)
        )
        memory_seam = all(
            hasattr(store, name)
            for name in (
                "load_embeddings", "load_user_model", "load_session_summary",
                "touch_episode", "save_session_summary",
            )
        )
        self._memory = memory if memory is not None else (
            MemoryAgent(store) if memory_seam else _NoopMemory()
        )
        self._life_arcs: list[LifeArc] = (
            store.list_life_arcs() if hasattr(store, "list_life_arcs") else []
        )
        try:
            _params = inspect.signature(store.add_message).parameters
        except (TypeError, ValueError):
            _params = {}
        self._accepts_session_id = "session_id" in _params
        self._accepts_intent_id = "intent_id" in _params
        self._accepts_conversation_id = "conversation_id" in _params
        try:
            _llm_params = inspect.signature(store.log_llm_call).parameters
        except (TypeError, ValueError):
            _llm_params = {}
        # Eval-mode call reproducibility (it3 B7): only stores that accept
        # the ``repro`` keyword receive the exact request payload (the store
        # itself drops it unless audit_mode=True — production privacy
        # default). Legacy store stubs without the kwarg stay untouched.
        self._accepts_repro = "repro" in _llm_params

        self.cycle_state: CycleState = cycle.init_state(persona, rng_mod.init_rng(seed))
        self.mood_state = MoodState()
        self.current_day: int | None = None
        self.current_record: DayRecord | None = None
        self._day_rng = None
        self._records: dict[int, DayRecord] = {}

        # Resume: latest persisted day restores mu/eta; cycle is replayed.
        latest = store.latest_daily_state()
        if latest is not None:
            self._resume_from(latest)

        # it3 B2: the open conversation (if any) is reopened at conversation
        # granularity — its turns continue and turn_index keeps counting.
        # Stores without the conversation seam (legacy fakes) start with no
        # open conversation, exactly as before.
        self._conversation: Conversation | None = None
        if hasattr(self.store, "load_open_conversation"):
            self._conversation = self.store.load_open_conversation()

    # ------------------------------------------------------------------ #
    # resume / replay
    # ------------------------------------------------------------------ #

    def _resume_from(self, latest: dict) -> None:
        day = int(latest["day"])
        # ROUTED DEFECT (A1b): restarting on an already-progressed store with
        # a driver clock that starts at day 0 crashed with "cannot rewind
        # session from day N to 0" the moment a day-0 event fired on resume
        # (session.ensure_day vs a clock always starting at day 0). A session
        # must never rewind: initialize the clock AT the store's day so the
        # resumed conversation continues from where the store is. A clock
        # already at/past the store's day is never moved (resume tests pin
        # that behavior).
        if self.clock.day() < day and hasattr(self.clock, "advance_to_day"):
            self.clock.advance_to_day(day)
        self.mood_state = MoodState(mu=float(latest["mu"]), eta=float(latest["eta"]))
        # Replay cycle state from day 0 up to `day` (deterministic, cheap).
        state = self.cycle_state
        for t in range(day):
            state = cycle.step(state, self.persona, rng_mod.day_rng(self.seed, t))[3]
        self.cycle_state = state
        self.current_day = day
        self.current_record = self._record_from_row(latest)
        self._records[day] = self.current_record
        # Reconstruct the day's RNG generator at the post-rollover position:
        # consume the same draws the original rollover consumed (cycle.step
        # then mood.step) so the end-of-day update continues the stream.
        rng_t = rng_mod.day_rng(self.seed, day)
        m, g, _phase, _next = cycle.step(self.cycle_state, self.persona, rng_t)
        mood.step(self.mood_state, self.persona, m, g, self.variant, rng_t)
        self._day_rng = rng_t
        # Review fix #1: if the latest day was ALREADY finalized (judgement
        # exists, no rollover beyond it — e.g. clean shutdown after
        # finalize_current), re-apply the end-of-day update the original run
        # performed, so resume matches a fresh run byte-for-byte.
        judgement = self.store.load_judgement(day)
        if judgement is not None and not self.synthetic_score:
            if self.feedback:
                self.mood_state = mood.update(
                    self.mood_state, self.persona, float(judgement["score"])
                )
            self.mood_state = mood.step_endogenous(self.mood_state, self.persona, rng_t)
        elif judgement is not None and self.synthetic_score:
            # Synthetic mode: the original finalize consumed the score draw
            # BEFORE the endogenous update.
            run_daily_synthetic_score(self._records[day].M, self.persona.N, rng_t)
            if self.feedback:
                self.mood_state = mood.update(
                    self.mood_state, self.persona, float(judgement["score"])
                )
            self.mood_state = mood.step_endogenous(self.mood_state, self.persona, rng_t)

        # Wave 2 lanes: restore life state from the store (arcs + today's
        # agenda); lazily seed arcs when a persona exists but none persisted,
        # and regenerate today's agenda only if the original rollover never
        # persisted it (deterministic per (seed, day) — identical draws).
        self._ensure_life()
        if (
            self._profile is not None
            and hasattr(self.store, "load_agenda")
            and self.store.load_agenda(day) is None
        ):
            self._generate_agenda(day)

        # A1 finding 1 (finalize crash window): the judgement was persisted
        # but the memory/life tail of finalize_day never completed (process
        # death between save_judgement and update_daily_score). A completed
        # finalize always persists the day's score, so judgement + NULL
        # score is the in-between marker — complete the missing steps
        # exactly once. Each step is individually idempotent (persistence
        # markers), so a CLEAN finalize followed by resume re-runs nothing
        # and the no-double-advance guard keeps holding.
        if judgement is not None and latest.get("score") is None:
            self._complete_pending_finalize(day, judgement)

    @staticmethod
    def _record_from_row(row: dict) -> DayRecord:
        return DayRecord(
            t=int(row["day"]),
            m=float(row["m"]),
            g=float(row["g"]),
            arg=float(row["arg"]),
            p=float(row["p"]),
            M=int(row["M"]),
            score=float(row["score"] or 0.0),
            mu=float(row["mu"]),
            eta=float(row["eta"]),
            cycle_day=float(row["cycle_day"]),
            phase_label=row["phase_label"],
            seed=int(row["seed"]),
        )

    # ------------------------------------------------------------------ #
    # day lifecycle
    # ------------------------------------------------------------------ #

    def ensure_day(self, day: int) -> None:
        """Roll the session forward so `current_day == day` (no rewind)."""
        if self.current_day is not None and day < self.current_day:
            raise ValueError(f"cannot rewind session from day {self.current_day} to {day}")
        if self.current_day is None:
            self._rollover(0)
        assert self.current_day is not None
        while self.current_day < day:
            self.finalize_day(self.current_day)
            self._rollover(self.current_day + 1)

    def _rollover(self, day: int) -> None:
        rng_t = rng_mod.day_rng(self.seed, day)
        m, g, phase_label, cycle_next = cycle.step(self.cycle_state, self.persona, rng_t)
        M, p, arg = mood.step(
            self.mood_state, self.persona, m, g, self.variant, rng_t
        )
        record = DayRecord(
            t=day,
            m=m,
            g=g,
            arg=arg,
            p=p,
            M=M,
            score=0.0,
            mu=self.mood_state.mu,
            eta=self.mood_state.eta,
            cycle_day=self.cycle_state.cycle_day,
            phase_label=phase_label,
            seed=self.seed,
        )
        self.store.save_daily_state(
            day,
            {
                "day": day,
                "M": M,
                "m": m,
                "g": g,
                "p": p,
                "arg": arg,
                "mu": record.mu,
                "eta": record.eta,
                "cycle_day": record.cycle_day,
                "phase_label": phase_label,
                "seed": self.seed,
                "score": None,
            },
        )
        self.store.log_event(day, self.clock.now_h(), "day_rollover", f"M={M} phase={phase_label}")
        self.cycle_state = cycle_next
        self.current_day = day
        self.current_record = record
        self._day_rng = rng_t
        self._records[day] = record

        # Wave 2 lanes: plan today's life agenda. All draws come from the
        # reserved LIFE stream (never day_rng) so the engine replay order
        # above stands byte-for-byte. (Memory sessions are no longer opened
        # here — it3 B2: one memory session per CONVERSATION, opened at
        # conversation open and closed at conversation close.)
        self._ensure_life()
        if self._profile is not None:
            self._generate_agenda(day)

    def finalize_day(self, day: int) -> None:
        """Judge the day (shadow or feedback), close the memory session, step
        the life lane, then run the engine's end-of-day update with the day's
        own RNG generator (replay-compatible)."""
        if day != self.current_day:
            raise ValueError(
                f"finalize_day({day}) while current day is {self.current_day} — "
                "only the current day can be finalized (review fix #6/#7)"
            )
        if self.store.load_judgement(day) is not None:
            # Already finalized (resume case) — the state snapshot was
            # restored by _resume_from instead, so nothing to do here.
            return
        transcript = self._transcript_for(day)
        if self.synthetic_score:
            # Replicate run_daily's synthetic score INCLUDING its RNG draw
            # (consumption order: cycle.step, mood.step, score, endogenous).
            assert self.current_record is not None
            assert self._day_rng is not None
            score = run_daily_synthetic_score(
                self.current_record.M, self.persona.N, self._day_rng
            )
            result = JudgeResult(score=score, justification="synthetic")
        elif transcript:
            # The judge is a noisy sensor — a failed call must not kill the
            # day (review fix #3): degrade to a logged neutral score.
            try:
                result = self.judge(transcript, self.client, model=self.judge_model)
            except Exception as exc:  # noqa: BLE001 - sensor degradation
                self.store.log_event(
                    day, self.clock.now_h(), "judge_failed", str(exc)[:200]
                )
                result = JudgeResult(score=0.0, justification=f"judge failed: {exc}")
        else:
            result = JudgeResult(score=0.0, justification="no interaction that day")
        score = result.score
        self.store.save_judgement(
            day, score, result.justification, self.judge_model, shadow=not self.feedback
        )

        # Wave 2 lanes at the session boundary: the life step for the day
        # just ended. Memory is NOT closed here — it3 B2: L1->L2->L3->L4
        # formation keys off the CONVERSATION boundary (one memory session
        # per conversation), and conversation closes happen at their own
        # times; a conversation still open at the day boundary stays open.
        # Conversations that closed during the day ALREADY ran their memory
        # tail at close time; this sweep only completes stragglers (crash
        # between close_conversation and the tail). Neither consumes day_rng
        # (life uses the LIFE stream), so the engine replay order below is
        # untouched.
        self._step_life(day)
        self._recover_conversation_memory_tails(day)

        if self.feedback:
            self.mood_state = mood.update(self.mood_state, self.persona, score)
        assert self._day_rng is not None
        self.mood_state = mood.step_endogenous(self.mood_state, self.persona, self._day_rng)
        self.store.update_daily_score(day, score)
        self.store.log_event(
            day, self.clock.now_h(), "day_finalized",
            f"score={score:.3f} shadow={not self.feedback}",
        )

    def finalize_current(self) -> None:
        """Finalize the current day if it has not been finalized yet.

        Intended for clean shutdown paths (CLI quit) — creates the
        finalized-latest-day state that `_resume_from` now handles.
        """
        if self.current_day is None:
            return
        if self.store.load_judgement(self.current_day) is not None:
            return
        self.finalize_day(self.current_day)

    def _transcript_for(self, day: int) -> str:
        msgs = self.store.messages_for_day(day)
        if not msgs:
            return ""
        return "\n".join(f"{m['role']}: {m['content']}" for m in msgs)

    # ------------------------------------------------------------------ #
    # Wave 2 lanes: life + memory drivers (session COMPOSES; lanes persist)
    # ------------------------------------------------------------------ #

    def _ensure_life(self) -> None:
        """Seed persistent life arcs once per life epoch (store-backed,
        deterministic).

        The epoch (number of prior seeding generations persisted in this
        store) is passed to ``init_life`` so arc ids are never reused from a
        wiped generation (A1 finding 2: a life-arc wipe followed by restart
        re-seeds under a FRESH id namespace instead of silently pretending
        the wiped days happened). Normal restarts keep the persisted arcs
        untouched and log nothing.
        """
        if self._life_arcs or self._profile is None or not self._profile.interests:
            return
        epoch = self._life_epoch()
        self._life_arcs = life.init_life(self.seed, self._profile, self.store, epoch=epoch)
        if self._life_arcs and hasattr(self.store, "log_event"):
            self.store.log_event(
                self.current_day if self.current_day is not None else 0,
                self.clock.now_h(),
                "life_init",
                f"epoch={epoch} arcs={len(self._life_arcs)}",
            )

    def _life_epoch(self) -> int:
        """Number of prior life-arc seeding generations persisted in this
        store (each seeding logs a ``life_init`` event), i.e. the epoch
        counter for the next seeding. Derived from the store's persisted
        state (the audit log), so it survives arc wipes and is deterministic
        across restarts.

        A ``life_wipe`` event (the NO_LIFE goldfish day-boundary wipe) also
        counts as a generation boundary: the next seeding must be a FRESH id
        namespace, never a reuse of the wiped generation's ids (A1 finding 2
        mechanism, extended to per-day wipes). FULL never wipes, so its
        epoch is unaffected.
        """
        if not hasattr(self.store, "events_since"):
            return 0
        return sum(
            1 for e in self.store.events_since(0)
            if e.get("event") in ("life_init", "life_wipe")
        )

    def _generate_agenda(self, day: int) -> None:
        """Plan + persist today's agenda via the life lane (LIFE stream)."""
        if self._profile is None:
            return
        rng = stream_rng(self.seed, LIFE_STREAM, day)
        life.generate_agenda(day, self._profile, self._life_arcs, self.store, rng)

    def _step_life(self, day: int) -> None:
        """Advance the life lane for the day just ended (LIFE stream)."""
        if self._profile is None or not hasattr(self.store, "load_agenda"):
            return
        agenda = self.store.load_agenda(day)
        if agenda is None:
            return
        rng = stream_rng(self.seed, LIFE_STREAM, day)
        result = life.step_life(day, self._profile, self._life_arcs, agenda, self.store, rng)
        self._life_arcs = result.updated_arcs
        self.store.log_event(
            day, self.clock.now_h(), "life_step",
            f"arcs={len(result.updated_arcs)} items={len(agenda.items)}",
        )

    def _life_step_done(self, day: int) -> bool:
        """True when the life step for ``day`` already ran.

        Persistence markers: a ``life_step`` event for the day, or no agenda
        to step (``_step_life`` no-ops without an agenda). Used to make the
        crash-window completion idempotent."""
        if not hasattr(self.store, "load_agenda") or self.store.load_agenda(day) is None:
            return True
        if not hasattr(self.store, "events_since"):
            return False
        return any(
            e.get("event") == "life_step" and e.get("day") == day
            for e in self.store.events_since(day)
        )

    def _complete_pending_finalize(self, day: int, judgement: dict) -> None:
        """A1 finding 1: finish the memory/life tail of a finalize_day that
        died after ``save_judgement`` (the crash window), exactly once.

        Runs on resume when the day's judgement exists but its score was
        never persisted (a completed finalize always persists it). Each step
        is guarded by its own persistence marker (L2 summary, ``life_step``
        event), so a clean finalize followed by resume re-runs nothing; a
        crash inside this recovery is itself recoverable on the next resume.
        The engine's end-of-day mood update is NOT re-applied here —
        ``_resume_from`` already re-applied it (judgement present), so the
        replay contract is preserved.
        """
        # it3 B2: memory formation is CONVERSATION-boundary driven, so the
        # crash window's memory tail is "conversations that closed during
        # this day but whose L2 summary never persisted" (process death
        # between close_conversation and the memory tail). Each close is
        # idempotent (summary-exists guard), so a clean finalize re-runs
        # nothing.
        self._recover_conversation_memory_tails(day)
        if self._profile is not None and not self._life_step_done(day):
            self._step_life(day)
        if hasattr(self.store, "update_daily_score"):
            self.store.update_daily_score(day, float(judgement["score"]))
        self.store.log_event(
            day, self.clock.now_h(), "day_finalized",
            f"score={float(judgement['score']):.3f} shadow={not self.feedback}",
        )

    def _recover_conversation_memory_tails(self, day: int) -> None:
        """Complete the per-conversation memory tail for conversations that
        closed during ``day`` but whose L2 summary never persisted.

        Idempotent (summary-exists guard): the normal path closes memory at
        conversation-close time, so a clean day re-runs nothing; a crash
        between ``close_conversation`` and the memory tail (mid-_chat or in
        the finalize window) is recovered here, at the day boundary and on
        resume.
        """
        if not hasattr(self.store, "list_conversations"):
            return
        for conv in self.store.list_conversations():
            if conv.close_reason is None or conv.closed_t_h is None:
                continue
            if not (day * 24.0 <= conv.closed_t_h < (day + 1) * 24.0):
                continue
            if (
                hasattr(self.store, "load_session_summary")
                and self.store.load_session_summary(
                    self._memory_session_id(conv.id)
                ) is None
            ):
                self._close_conversation_memory(conv)

    # ------------------------------------------------------------------ #
    # it3 B2: conversation lifecycle (module invariant 8)
    # ------------------------------------------------------------------ #

    def open_conversation_id(self) -> str | None:
        """Id of the currently open conversation, or None.

        Read-only accessor for the runtime's lifecycle pacing (parking the
        rollover at conversation close instants).
        """
        return self._conversation.id if self._conversation is not None else None

    def check_conversation_lifecycle(self, t_h: float) -> str | None:
        """Close the open conversation if a boundary close is due at ``t_h``.

        Exactly two boundary closes live here (the other two — the
        ``closing_tendency`` draw and ``max_turns`` — fire at companion
        turns inside ``_chat``):

        * ``quiet_hours`` — the conversation's last turn preceded the start
          of the current quiet window (the conversation crossed the 23:00
          boundary; a conversation that OPENED inside quiet hours has no
          crossed boundary and keeps running).
        * ``user_left`` — user silence since the conversation's last user
          turn (or its opening, when the companion opened and the user
          never replied) reached ``USER_LEFT_THRESHOLD_H``.

        Idempotent and cheap: the runtime calls it at every wake (rollover
        parks, firing wakes, inbound turns) and the session calls it before
        every turn, so the close is recorded at its boundary instant rather
        than lazily. Returns the close_reason, or None when the
        conversation stays open.
        """
        conv = self._conversation
        if conv is None:
            return None
        last = self._last_turn_t_h(conv)
        if last is None:
            last = conv.opened_t_h
        boundary = self._quiet_start_at_or_before(t_h, self.timing.quiet_hours)
        if last < boundary:
            self._close_conversation(conv, t_h, "quiet_hours")
            return "quiet_hours"
        anchor = self._last_user_turn_t_h(conv)
        if anchor is None:
            anchor = conv.opened_t_h
        if t_h - anchor >= USER_LEFT_THRESHOLD_H:
            self._close_conversation(conv, t_h, "user_left")
            return "user_left"
        return None

    def next_conversation_close_t_h(self, now: float) -> float | None:
        """Next strictly-future close instant for the open conversation.

        The earlier of the next quiet-hours boundary (when the
        conversation's last turn precedes it) and the ``user_left``
        deadline; None when no conversation is open or no close is pending.
        The runtime parks the rollover at this instant so the close is
        recorded at the boundary, not lazily at the next turn.
        """
        conv = self._conversation
        if conv is None:
            return None
        last = self._last_turn_t_h(conv)
        if last is None:
            last = conv.opened_t_h
        _quiet_ini, _quiet_fin = self.timing.quiet_hours
        day = int(now // 24.0)
        qstart = day * 24.0 + _quiet_ini
        if qstart <= now + 1e-12:
            qstart += 24.0
        candidates: list[float] = []
        if last < qstart:
            candidates.append(qstart)
        anchor = self._last_user_turn_t_h(conv)
        if anchor is None:
            anchor = conv.opened_t_h
        deadline = anchor + USER_LEFT_THRESHOLD_H
        if deadline > now + 1e-12:
            candidates.append(deadline)
        return min(candidates) if candidates else None

    @staticmethod
    def _last_turn_t_h(conv: Conversation) -> float | None:
        if not conv.turns:
            return None
        return conv.turns[-1].t_h

    @staticmethod
    def _last_user_turn_t_h(conv: Conversation) -> float | None:
        for t in reversed(conv.turns):
            if t.speaker == "user":
                return t.t_h
        return None

    @staticmethod
    def _quiet_start_at_or_before(t_h: float, quiet_hours) -> float:
        """Start hour of the quiet window containing (or ending at) t_h."""
        _quiet_ini, _quiet_fin = quiet_hours
        boundary = int(t_h // 24.0) * 24.0 + _quiet_ini
        if boundary > t_h:
            boundary -= 24.0
        return boundary

    def _next_conversation_id(self) -> str:
        """Deterministic conversation id: ``conv-<n>`` where n is the number
        of conversations already persisted (0-based). Restarts never reuse
        or collide with an existing id; stores without the seam fall back
        to a session-local counter (no persistence, no resume concern)."""
        if hasattr(self.store, "list_conversations"):
            return f"conv-{len(self.store.list_conversations())}"
        n = getattr(self, "_conv_seq", 0)
        self._conv_seq = n + 1
        return f"conv-{n}"

    @staticmethod
    def _memory_session_id(conv_id: str) -> str:
        """Memory session id for a conversation (see
        ``CONVERSATION_SESSION_OFFSET``): ``conv-<n>`` -> ``day-<OFFSET+n>``."""
        n = int(conv_id.split("-", 1)[1])
        return f"day-{CONVERSATION_SESSION_OFFSET + n}"

    def _ensure_conversation(
        self, t_h: float, *, opened_by: Literal["user", "companion"]
    ) -> Conversation:
        """Return the active conversation, opening a new one when none is.

        A conversation opens on the first message of either party
        (``opened_by`` records who). On a restart mid-conversation the
        store's OPEN conversation is reopened (turns continue, turn_index
        continues — no rewind); a closed one stays closed.
        """
        conv = self._conversation
        if conv is None and hasattr(self.store, "load_open_conversation"):
            conv = self.store.load_open_conversation()
            if conv is not None:
                self._conversation = conv
        if conv is not None:
            return conv
        conv_id = self._next_conversation_id()
        if hasattr(self.store, "open_conversation"):
            self.store.open_conversation(conv_id, t_h, opened_by)
        if hasattr(self.store, "open_session"):
            # One memory session per conversation: L1->L2->L3->L4 formation
            # keys off the conversation boundary. The session id is derived
            # from the conversation id (day-namespaced — see
            # CONVERSATION_SESSION_OFFSET); the MemoryAgent seam is used
            # as-is, only the ids the session passes changed.
            self.store.open_session(self._memory_session_id(conv_id), t_h)
        conv = Conversation(
            id=conv_id, opened_t_h=t_h, closed_t_h=None,
            opened_by=opened_by, close_reason=None, turns=(),
        )
        self._conversation = conv
        self.store.log_event(
            int(t_h // 24.0), t_h, "conversation_opened",
            f"id={conv_id} opened_by={opened_by}",
        )
        return conv

    def _record_turn(
        self,
        conv: Conversation,
        speaker: Literal["user", "companion"],
        text: str,
        t_h: float,
        *,
        message_id: int | None = None,
    ) -> Conversation:
        """Persist one ConversationTurn row and return the updated
        in-memory Conversation (turn_index = len(conv.turns), so a resumed
        conversation keeps counting from the persisted turns)."""
        turn = ConversationTurn(
            speaker=speaker, text=text, t_h=t_h,
            turn_index=len(conv.turns), conversation_id=conv.id,
        )
        if hasattr(self.store, "add_conversation_turn"):
            self.store.add_conversation_turn(
                conv.id, speaker, text, t_h, turn.turn_index,
                message_id=message_id,
            )
        updated = replace(conv, turns=conv.turns + (turn,))
        self._conversation = updated
        return updated

    def _maybe_close_conversation(
        self, conv: Conversation, t_h: float, closing_tendency: float
    ) -> None:
        """The companion-turn close checks: the closing_tendency draw and
        the max_turns cap.

        DRAW DISCIPLINE: at each companion turn EXCEPT the first companion
        turn of the conversation, draw ``uniform()`` from
        ``stream_rng(seed, CONVERSATION_STREAM, conv_seq, turn_index)``
        (stream 6, keyed by the conversation's sequence number AND the
        turn's 0-based index — deterministic, resume-safe, independent of
        call order) and close with ``closing_tendency`` when the draw is
        below ``controls.closing_tendency``. Keying by the conversation
        sequence keeps every conversation on its OWN draw sequence (a
        turn-index-only key would give every conversation the identical
        draw — a degenerate distribution). The first companion turn is
        exempt by design: the companion always completes at least one full
        exchange before any taper decision — without this floor a high
        closing tendency would degenerate the turn-count distribution (and
        B3's mean-turns>=4 becomes unreachable). A conversation that
        survives the draw closes with ``max_turns`` once it reaches
        ``MAX_TURNS`` total turns.
        """
        if not conv.turns:
            return
        first_companion = next(
            (t for t in conv.turns if t.speaker == "companion"), None
        )
        if first_companion is None:
            return
        last_turn = conv.turns[-1]
        if last_turn.speaker != "companion":
            return
        if last_turn.turn_index == first_companion.turn_index:
            return  # first companion turn: the no-taper floor
        conv_seq = int(conv.id.split("-", 1)[1])
        rng = stream_rng(
            self.seed, CONVERSATION_STREAM, conv_seq, last_turn.turn_index
        )
        if rng.uniform() < float(closing_tendency):
            self._close_conversation(conv, t_h, "closing_tendency")
            return
        if len(conv.turns) >= MAX_TURNS:
            self._close_conversation(conv, t_h, "max_turns")

    def _close_conversation(
        self, conv: Conversation, closed_t_h: float, reason: str
    ) -> None:
        """Persist the close (``close_reason``) and drive the per-
        conversation memory tail (L1->L2->L3->L4) at the conversation
        boundary. Idempotent: the store close is an UPDATE and the memory
        tail is summary-guarded."""
        if conv.close_reason is not None:
            return
        self._conversation = None
        closed = replace(conv, closed_t_h=closed_t_h, close_reason=reason)
        if hasattr(self.store, "close_conversation"):
            self.store.close_conversation(conv.id, closed_t_h, reason)
        self.store.log_event(
            int(closed_t_h // 24.0), closed_t_h, "conversation_closed",
            f"id={conv.id} reason={reason} turns={len(conv.turns)}",
        )
        self._close_conversation_memory(closed)

    def _close_conversation_memory(self, conv: Conversation) -> None:
        """L1 -> L2 -> L3 -> L4 at the CONVERSATION boundary, via the
        MemoryAgent seam only (the ids the session passes changed to
        conversation ids; the seam itself is untouched).

        Runs once per conversation (summary-exists guard). Silent
        conversations are skipped — an empty session has no provenance to
        promote (the existing memory guards). ``conv`` must be closed
        (``closed_t_h`` set) — both call sites guarantee it.
        """
        session_id = self._memory_session_id(conv.id)
        if not hasattr(self.store, "messages_for_session"):
            return
        if not self.store.messages_for_session(session_id):
            return
        if (
            hasattr(self.store, "load_session_summary")
            and self.store.load_session_summary(session_id) is not None
        ):
            return  # already closed (e.g. crash-window recovery ran it)
        assert conv.closed_t_h is not None, "memory close requires a closed conversation"
        closed_t_h = conv.closed_t_h
        summary = self._memory.close_session(session_id, ended_at_t_h=closed_t_h)
        if summary is not None:
            self._memory.promote(summary)
            self._memory.update_user_model(summary)
        if hasattr(self.store, "close_session"):
            self.store.close_session(session_id, closed_t_h)
        self.store.log_event(
            int(closed_t_h // 24.0), closed_t_h,
            "memory_session_closed", session_id,
        )

    def _current_activity(self, day: int, t_h: float) -> CurrentActivity | None:
        """NOW semantics (plan §5-A2 T2, orchestrator invariant 8).

        Read-only view of today's agenda from the persisted store, resolved
        through ``life.current_activity_now``: only an item actually in
        progress at ``t_h`` (``start_t_h <= t_h < end_t_h`` and not
        skipped/shifted — those are not happening at their planned slot)
        can be current, choosing the highest salience when several overlap;
        ``None`` when nothing is active. A 7 PM plan is never what she is
        doing at 10 AM, and a day with nothing in progress reports None
        instead of the day's highest-salience item (the documented 53-56%
        error this replaces).
        """
        items = (
            self.store.list_agenda_items(day=day)
            if hasattr(self.store, "list_agenda_items")
            else ()
        )
        if not items:
            return None
        return life.current_activity_now(DailyAgenda(day=day, items=tuple(items)), t_h)

    def _resolve_intent(self, reason: str | None) -> ProactiveIntent | None:
        """LEGACY reason-path fallback: most recently created stored intent
        for `reason`, or None.

        Only pre-slice callers that pass a REASON string (never an intent id)
        reach this — the Iteration-2 seam is ``fire_proactive(intent_id)``,
        which fetches the EXACT intent and never downgrades identity to a
        reason-type lookup (invariant 7). None ⇒ legacy ungrounded call: the
        session degrades to a generic opening (no invented source claim).
        """
        if reason is None:
            return None
        if not hasattr(self.store, "list_proactive_intents"):
            return None
        for intent in self.store.list_proactive_intents():
            if intent.reason == reason:
                return intent
        return None

    def _lookup_intent(self, intent_id: str) -> ProactiveIntent | None:
        """EXACT-id lookup (never a reason lookup): the stored intent whose
        id equals ``intent_id``, or None. Two intents with the same reason
        are never interchangeable — only the id identifies the intent."""
        if hasattr(self.store, "load_proactive_intent"):
            return self.store.load_proactive_intent(intent_id)
        if hasattr(self.store, "list_proactive_intents"):
            return next(
                (i for i in self.store.list_proactive_intents() if i.id == intent_id),
                None,
            )
        return None

    def _build_snapshot(
        self,
        day: int,
        t_h: float,
        *,
        brief: BehaviorBrief,
        intent: ProactiveIntent | None,
        query: str | None,
    ) -> CompanionSnapshot:
        """Compose ALL lanes into ONE CompanionSnapshot (lane rule: nothing
        is mutated here — life/memory/persona state is only read)."""
        profile = self._profile
        if profile is None:
            profile = PersonaProfile(
                name="Nova",
                core=self.persona_core or DEFAULT_PERSONA_CORE,
                interests=(),
                routines=(),
            )
        memory_ctx = self._memory.retrieve(query or "", context={"t_h": t_h}, limit=8)
        recent = tuple(
            Turn(role=m["role"], text=m["content"], t_h=float(m["t_h"]))
            for m in self.store.recent_messages(limit=RECENT_TURNS)
        )
        return CompanionSnapshot(
            persona=profile,
            current_behavior=brief,
            current_activity=self._current_activity(day, t_h),
            agenda=tuple(
                self.store.list_agenda_items(day=day)
                if hasattr(self.store, "list_agenda_items")
                else ()
            ),
            life_arcs=tuple(self._life_arcs),
            memory_context=memory_ctx,
            recent_conversation=recent,
            proactive_intent=intent,
        )

    # ------------------------------------------------------------------ #
    # conversation
    # ------------------------------------------------------------------ #

    def _persist_message(
        self,
        role: str,
        content: str,
        t_h: float,
        day: int,
        *,
        proactive: bool,
        session_id: str,
        intent_id: str | None = None,
        conversation_id: str | None = None,
    ) -> int:
        """Persist one message, passing only the kwargs the store accepts
        (legacy fakes predate session_id/intent_id/conversation_id;
        SQLiteStore takes all three). ``intent_id`` carries the EXACT
        validated intent on outgoing messages (invariant 6); reactive
        messages keep it None. ``conversation_id`` links the message to its
        conversation (module invariant 8). Returns the message row id."""
        kwargs: dict = {"proactive": proactive}
        if self._accepts_session_id:
            kwargs["session_id"] = session_id
        if self._accepts_intent_id:
            kwargs["intent_id"] = intent_id
        if self._accepts_conversation_id:
            kwargs["conversation_id"] = conversation_id
        return self.store.add_message(role, content, t_h, day, **kwargs)

    def _chat(
        self,
        user_text: str | None,
        *,
        proactive: bool,
        intent: ProactiveIntent | None = None,
    ) -> TurnResult:
        """Shared path for reactive and proactive messages.

        `user_text=None` means the companion initiates: the transcript has no
        trailing user request and the system prompt renders the grounded
        intent's CONCRETE HOOK (never "Contact reason: schedule"). The
        intent is the EXACT validated ``ProactiveIntent`` (resolved by id in
        ``fire_proactive``); when generation completes its id is persisted on
        the outgoing message (``message.intent_id``, invariant 6).
        """
        t_h = self.clock.now_h()
        day = self.clock.day()
        self.ensure_day(day)
        assert self.current_record is not None
        # it3 B2: close a stale open conversation BEFORE this turn (quiet
        # boundary crossed / user silence past USER_LEFT_THRESHOLD_H); the
        # current message then opens a fresh conversation.
        self.check_conversation_lifecycle(t_h)

        previous = self._records.get(day - 1)
        directive = derive_behavior(
            self.current_record, self.timing, hour=self.clock.local_hour(), previous=previous
        )
        controls = controls_from_directive(directive)
        brief = to_brief(directive)

        query = user_text
        if user_text is None and intent is not None:
            query = intent.hook

        snapshot = self._build_snapshot(day, t_h, brief=brief, intent=intent, query=query)
        # v2 unified brief renderer: the state card's mood line consumes
        # BehaviorDirective.prompt_brief VERBATIM (single source of the
        # 'Current bearing' prose; the assembler never re-renders it).
        system = assemble_snapshot(
            snapshot, controls=controls, prompt_brief=directive.prompt_brief
        )
        if user_text is None and intent is None:
            # Legacy ungrounded proactive call (pre-slice callers/tests):
            # generic opening without any invented source claim.
            system += "\n\n" + proactive_block()

        recent = self.store.recent_messages()
        # it3 B2: one conversation per exchange run — opened by the first
        # message of either party; the memory session id IS the conversation
        # id (one memory session per conversation).
        conv = self._ensure_conversation(
            t_h, opened_by="user" if user_text is not None else "companion"
        )
        conv_id = conv.id
        session_id = self._memory_session_id(conv_id)
        if user_text is not None:
            messages = build_messages(recent, user_text)
            mid = self._persist_message(
                "user", user_text, t_h, day,
                proactive=False, session_id=session_id, conversation_id=conv_id,
            )
            conv = self._record_turn(
                conv, "user", user_text, t_h, message_id=mid
            )
        else:
            messages = [
                {"role": m["role"], "content": m["content"]}
                for m in recent
            ]
        reply = self.client.chat(messages, system=system, max_tokens=controls.max_tokens)
        if not reply.strip():
            # Generation integrity (it3 B1): an empty/whitespace reply is
            # NEVER persisted. The client retries empties with bounded
            # backoff first; this guard is the invariant that a blank
            # assistant row cannot enter the store, whatever the client did.
            raise RuntimeError(
                "refusing to persist empty assistant reply (client returned "
                "empty/whitespace-only content)"
            )
        mid = self._persist_message(
            "assistant", reply, t_h, day,
            proactive=proactive, session_id=session_id, conversation_id=conv_id,
            intent_id=intent.id if intent is not None else None,
        )
        conv = self._record_turn(
            conv, "companion", reply, t_h, message_id=mid
        )
        # it3 B2: the companion-turn close checks — the closing_tendency
        # draw and the max_turns cap. The close (when it fires) persists
        # close_reason and drives the per-conversation memory tail.
        self._maybe_close_conversation(conv, t_h, controls.closing_tendency)
        repro_kwargs: dict = {}
        if self._accepts_repro:
            # Eval-mode call reproducibility (it3 B7): persist the EXACT
            # assembled system prompt + message payload + sampling params so
            # repro_json alone can reconstruct this call (invariant 19).
            # temperature/json_mode are the LLMClient protocol defaults —
            # this call site never overrides them. The store drops the
            # payload unless audit_mode=True (production privacy default).
            repro_kwargs["repro"] = {
                "model": getattr(self.client, "model", None),
                "system": system,
                "messages": messages,
                "max_tokens": controls.max_tokens,
                "temperature": 0.8,
                "json_mode": False,
                "controls": {
                    "response_delay_s": controls.response_delay_s,
                    "closing_tendency": controls.closing_tendency,
                    "initiative_factor": controls.initiative_factor,
                    "closing_guidance": controls.closing_guidance,
                },
                "intent_id": intent.id if intent is not None else None,
                "timestamp": {"day": day, "t_h": t_h},
            }
        self.store.log_llm_call(
            day,
            t_h,
            "chat",
            system + "\n" + repr(messages),
            reply,
            getattr(self.client, "model", None),
            **repro_kwargs,
        )
        self.store.log_event(day, t_h, "assistant_reply", f"len={len(reply)}")
        return TurnResult(
            reply=reply,
            directive=directive,
            day=day,
            hour=self.clock.local_hour(),
            controls=controls,
        )

    def on_message(self, user_text: str) -> TurnResult:
        """Process one user message: directive → snapshot → assemble → LLM."""
        return self._chat(user_text, proactive=False)

    def fire_proactive(
        self,
        intent_id: str | None = None,
        *,
        reason: str | None = None,
    ) -> TurnResult:
        """Assistant-initiated message (Iteration-2 A5 T3 seam contract).

        ``intent_id`` is the EXACT id of a validated, stored
        ``ProactiveIntent`` — A3's runtime passes ``intent.id``, never a
        reason. The snapshot is constructed from that exact intent: two
        intents with the same reason are never interchangeable (invariant
        7). The intent must exist and be inside its validity window; the
        outgoing message persists its id (invariant 6). Deep source/hook
        validation is the content gate's job (A3), not re-derived here.

        ``reason`` is a DEPRECATED keyword alias for pre-slice callers that
        fire by reason type (no callers may mix the two). It resolves to the
        most recently created stored intent for that reason, or a generic
        opening when none exists; an unknown reason raises ``ValueError``.
        An argument that is neither a known intent id nor a valid reason
        also raises ``ValueError``.
        """
        if reason is not None:
            if intent_id is not None:
                raise ValueError("pass either intent_id or reason, not both")
            if reason not in VALID_REASONS:
                raise ValueError(f"unknown proactive reason: {reason!r}")
            return self._chat(None, proactive=True, intent=self._resolve_intent(reason))
        intent: ProactiveIntent | None = None
        if intent_id is not None:
            found = self._lookup_intent(intent_id)
            if found is not None:
                if found.valid_until_t_h < self.clock.now_h():
                    raise ValueError(
                        f"proactive intent {intent_id!r} expired at "
                        f"t_h={found.valid_until_t_h:.2f} "
                        f"(now {self.clock.now_h():.2f})"
                    )
                intent = found
            elif intent_id in VALID_REASONS:
                intent = self._resolve_intent(intent_id)
            else:
                raise ValueError(
                    f"no proactive intent or valid reason matches {intent_id!r}"
                )
        return self._chat(None, proactive=True, intent=intent)

    def state_summary(self) -> dict:
        """Dev-facing snapshot of the current latent + observable state."""
        r = self.current_record
        return {
            "day": self.current_day,
            "M": r.M if r else None,
            "m": r.m if r else None,
            "g": r.g if r else None,
            "mu": r.mu if r else None,
            "eta": r.eta if r else None,
            "phase": r.phase_label if r else None,
            "cycle_day": r.cycle_day if r else None,
            "hour": self.clock.local_hour(),
            "feedback": self.feedback,
        }

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        """Release store and client resources (review fix #10)."""
        self.store.close()
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
