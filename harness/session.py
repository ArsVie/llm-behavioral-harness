"""Session — the e2e daily loop (W-E1).

Wires engine → behavior → assembler → client → judge → store under a virtual
clock. Engine replay contract (frozen in sim/run_daily): per day t, the FIRST
consumer of day_rng(seed, t) is cycle.step, then mood.step; mood.step_endogenous
consumes the SAME generator at day end. This module preserves that order so a
session's mood sequence replays exactly like `sim.run_daily` for the same seed.

Day lifecycle:
  - rollover (start of day): sample m/g/phase/M for the day, persist
    daily_state, hold the day's RNG generator for the end-of-day update.
  - during the day: on_message() derives the behavior directive, assembles
    the prompt, calls the client, persists messages + trace.
  - finalize (when the clock moves past the day): judge the day's exchange
    (shadow by default — recorded, does NOT touch mu), then apply the
    end-of-day engine update (mu ← score in feedback mode, eta AR(1) always).

Resume: with the same seed + store, the latest daily_state row restores
mu/eta (values "used" that day = state at start); the cycle clock is
reconstructed by replaying init_rng + cycle.step from day 0.
"""

from __future__ import annotations

from dataclasses import dataclass

import engine.rng as rng_mod
from engine import cycle, mood
from engine.types import (
    CycleState,
    DayRecord,
    MoodState,
    MoodVariant,
    PersonaParams,
    TimingParams,
)
from harness.assembler import build_messages, build_system_prompt
from harness.behavior import BehaviorDirective, derive_behavior
from harness.clock import VirtualClock
from harness.client import LLMClient
from harness.judge import JudgeResult, judge_day
from harness.scheduler import VALID_REASONS
from harness.score import synthetic_score as run_daily_synthetic_score
from harness.store import SQLiteStore


@dataclass
class TurnResult:
    """What one on_message() produced: the reply + the observable state."""

    reply: str
    directive: BehaviorDirective
    day: int
    hour: float


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
        self._records[day] = self._record_from_row(latest)
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

    def finalize_day(self, day: int) -> None:
        """Judge the day (shadow or feedback), then run the engine's end-of-day
        update with the day's own RNG generator (replay-compatible)."""
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
        trailing user request and the system prompt states the contact reason.
        """
        t_h = self.clock.now_h()
        day = self.clock.day()
        self.ensure_day(day)
        assert self.current_record is not None

        previous = self._records.get(day - 1)
        directive = derive_behavior(
            self.current_record, self.timing, hour=self.clock.local_hour(), previous=previous
        )
        system = build_system_prompt(self.persona_core, directive)
        recent = self.store.recent_messages()
        messages = [
            {"role": m["role"], "content": m["content"]}
            for m in recent
        ]
        if user_text is not None:
            messages = build_messages(recent, user_text)
            self.store.add_message("user", user_text, t_h, day, proactive=False)
        else:
            system += (
                f"\nYou are reaching out first. Contact reason: {reason or 'schedule'}.\n"
                "State this reason naturally in your FIRST sentence, then open with a "
                "concrete, verifiable hook. Never guilt-trip, nag, or imply the user "
                "owes you contact."
            )
        reply = self.client.chat(messages, system=system)
        self.store.add_message("assistant", reply, t_h, day, proactive=proactive)
        self.store.log_llm_call(
            day,
            t_h,
            "chat",
            system + "\n" + repr(messages),
            reply,
            getattr(self.client, "model", None),
        )
        self.store.log_event(day, t_h, "assistant_reply", f"len={len(reply)}")
        return TurnResult(reply=reply, directive=directive, day=day, hour=self.clock.local_hour())

    def on_message(self, user_text: str) -> TurnResult:
        """Process one user message: directive → assemble → LLM → persist."""
        return self._chat(user_text, proactive=False)

    def fire_proactive(self, reason: str = "schedule") -> TurnResult:
        """Assistant-initiated message with a contact reason (no user input)."""
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
