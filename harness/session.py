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
Proactive turn: grounded ProactiveIntent (store-backed, most recently
created for the fired reason) → CompanionSnapshot(proactive_intent=...) →
assembler (renders the intent's concrete hook verbatim).

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
state are restored from the store (arcs, agenda, summaries, episodes).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

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
    CurrentActivity,
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
            self._add_message_accepts_session = (
                "session_id" in inspect.signature(store.add_message).parameters
            )
        except (TypeError, AttributeError):
            self._add_message_accepts_session = False

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

    # ------------------------------------------------------------------ #
    # resume / replay
    # ------------------------------------------------------------------ #

    def _resume_from(self, latest: dict) -> None:
        day = int(latest["day"])
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

        # Wave 2 lanes: register the memory session and plan today's life
        # agenda. All draws come from the reserved LIFE stream (never day_rng)
        # so the engine replay order above stands byte-for-byte.
        if hasattr(self.store, "open_session"):
            self.store.open_session(f"day-{day}", day * 24.0)
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

        # Wave 2 lanes at the session boundary: memory L1->L2->L3->L4 and the
        # life step for the day just ended. Neither consumes day_rng (memory
        # is deterministic; life uses the LIFE stream), so the engine replay
        # order below is untouched.
        self._close_memory_session(day)
        self._step_life(day)

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
        """Seed persistent life arcs once (store-backed, deterministic)."""
        if self._life_arcs or self._profile is None or not self._profile.interests:
            return
        self._life_arcs = life.init_life(self.seed, self._profile, self.store)

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

    def _close_memory_session(self, day: int) -> None:
        """L1 -> L2 -> L3 -> L4 at the day boundary, via MemoryAgent only.

        Runs once per day (finalize is judgement-guarded). Silent days are
        skipped — an empty session has no provenance to promote.
        """
        if not hasattr(self.store, "messages_for_day") or not self.store.messages_for_day(day):
            return
        session_id = f"day-{day}"
        ended = (day + 1) * 24.0
        summary = self._memory.close_session(session_id, ended_at_t_h=ended)
        if summary is not None:
            self._memory.promote(summary)
            self._memory.update_user_model(summary)
        if hasattr(self.store, "close_session"):
            self.store.close_session(session_id, ended)
        self.store.log_event(day, self.clock.now_h(), "memory_session_closed", session_id)

    def _current_activity(self, day: int, t_h: float) -> CurrentActivity | None:
        """Read-only view of today's main activity from the persisted agenda.

        The life lane owns the agenda; the session only composes the view —
        in-progress item first, else today's highest-salience item.
        """
        items = (
            self.store.list_agenda_items(day=day)
            if hasattr(self.store, "list_agenda_items")
            else []
        )
        if not items:
            return None
        in_progress = [it for it in items if it.start_t_h <= t_h < it.end_t_h]
        candidates = in_progress or items
        main = max(candidates, key=lambda it: (it.salience, it.start_t_h))
        return CurrentActivity(t_h=t_h, item=main, description=main.activity)

    def _resolve_intent(self, reason: str | None) -> ProactiveIntent | None:
        """Most recently created stored intent for `reason`, or None.

        The runtime persists the grounded intent (IntentResolver →
        content gate) BEFORE calling fire_proactive, so the store is the
        session's only source of the concrete hook. None ⇒ legacy
        ungrounded call: the session degrades to a generic opening (no
        invented source claim).
        """
        if reason is None:
            return None
        if not hasattr(self.store, "list_proactive_intents"):
            return None
        for intent in self.store.list_proactive_intents():
            if intent.reason == reason:
                return intent
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

    def _chat(
        self,
        user_text: str | None,
        *,
        proactive: bool,
        reason: str | None = None,
    ) -> TurnResult:
        """Shared path for reactive and proactive messages.

        `user_text=None` means the companion initiates: the transcript has no
        trailing user request and the system prompt renders the grounded
        intent's CONCRETE HOOK (never "Contact reason: schedule").
        """
        t_h = self.clock.now_h()
        day = self.clock.day()
        self.ensure_day(day)
        assert self.current_record is not None

        previous = self._records.get(day - 1)
        directive = derive_behavior(
            self.current_record, self.timing, hour=self.clock.local_hour(), previous=previous
        )
        controls = controls_from_directive(directive)
        brief = to_brief(directive)

        intent: ProactiveIntent | None = None
        query = user_text
        if user_text is None:
            intent = self._resolve_intent(reason)
            query = intent.hook if intent is not None else None

        snapshot = self._build_snapshot(day, t_h, brief=brief, intent=intent, query=query)
        system = assemble_snapshot(snapshot, controls=controls)
        if user_text is None and intent is None:
            # Legacy ungrounded proactive call (pre-slice callers/tests):
            # generic opening without any invented source claim.
            system += "\n\n" + proactive_block()

        recent = self.store.recent_messages()
        session_id = f"day-{day}"
        if user_text is not None:
            messages = build_messages(recent, user_text)
            if self._add_message_accepts_session:
                self.store.add_message(
                    "user", user_text, t_h, day, proactive=False, session_id=session_id
                )
            else:
                self.store.add_message("user", user_text, t_h, day, proactive=False)
        else:
            messages = [
                {"role": m["role"], "content": m["content"]}
                for m in recent
            ]
        reply = self.client.chat(messages, system=system, max_tokens=controls.max_tokens)
        if self._add_message_accepts_session:
            self.store.add_message(
                "assistant", reply, t_h, day, proactive=proactive, session_id=session_id
            )
        else:
            self.store.add_message(
                "assistant", reply, t_h, day, proactive=proactive
            )
        self.store.log_llm_call(
            day,
            t_h,
            "chat",
            system + "\n" + repr(messages),
            reply,
            getattr(self.client, "model", None),
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

    def fire_proactive(self, reason: str = "schedule") -> TurnResult:
        """Assistant-initiated message with a contact reason (no user input).

        Grounded path (runtime): the store holds the persisted ProactiveIntent
        for `reason`; the snapshot carries it and the prompt renders its hook
        verbatim. Legacy direct calls without a stored intent degrade to a
        generic opening (no fabricated source).
        """
        if reason not in VALID_REASONS:
            raise ValueError(f"unknown proactive reason: {reason!r}")
        return self._chat(None, proactive=True, reason=reason)

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
