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
import json
import os
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
    render_day_block,
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
from harness.life import LIFE_STREAM, transition_past_windows
from harness.memory import MemoryAgent
from harness.negotiation_contract import (
    DEFER_TURNS_KEY,
    PULL_PER_DELAY,
    SHORT_AFK_H,
    NegotiationEpisode,
    NegotiationPhase,
    is_skippable,
)
from harness.negotiation_state import (
    NegotiationState,
    decide_status_at,
    map_defer_n,
    next_trigger_t_h,
    pull_toward_go,
    rearm_after_delay,
    state_from_dict,
    state_to_dict,
    window_ending_at,
)
try:  # A3's episode hook (harness/negotiation_episodes.py). Checkouts
    # without it degrade to NO episode emission — the negotiation itself
    # is unaffected (the hook is an A3 landing pad, never a requirement).
    from harness.negotiation_episodes import emit_negotiation_episode
except ImportError:  # pragma: no cover — A3 not merged in this checkout
    emit_negotiation_episode = None
from harness.scheduler import VALID_REASONS
from harness.score import synthetic_score as run_daily_synthetic_score
from harness.steering import (
    BOUNDARY_IDLE,
    KIND_EVENT_POPUP,
    KIND_USER_MESSAGE,
    Steer,
    SteeringQueue,
    render_steer_block,
    wrap_steer_marker,
)
from harness.store import SQLiteStore
from harness.tools import (
    Capabilities,
    DecisionConfig,
    DecisionRequeue,
    DecisionResult,
    DecisionRunner,
    PopupRequest,
    RawReply,
    load_decision_config,
)

#: Steer-application outcomes (``_apply_steer`` return codes).
_STEER_INJECT = "inject"        #: render the block into the next LLM call
_STEER_CONSUMED = "consumed"    #: handled (decision executed / consumed)
_STEER_SUPPRESS = "suppress"    #: no-reply verdict — suppress the reply

#: Closing-tendency draw stream (it3 B2): a dedicated engine.rng stream.
#: Streams 0..5 are reserved by other lanes (0=DAILY, 1=EVENTS, 2=EXPERIMENT,
#: 3=INIT, 4=LIFE, 5=PERSONA); 6 is this lane's. Each draw is keyed by
#: (conversation sequence, companion turn's 0-based index within the
#: conversation), so every conversation draws its OWN deterministic
#: sequence — the k-th draw of conversation n is always
#: ``stream_rng(seed, 6, n, k)``, and a resumed conversation continues
#: exactly where it left off (no re-draws).
CONVERSATION_STREAM = 6

# Conversation-lifecycle tunables live in ONE place (harness/tunables.py) so
# code and tests read the same source — no drift. Re-exported here so existing
# ``from harness.session import USER_LEFT_THRESHOLD_H`` / ``MAX_TURNS`` callers
# and tests keep working.
from harness.tunables import (  # noqa: E402
    CLOSING_TENDENCY_ENABLED,
    MAX_TURNS,
    USER_LEFT_THRESHOLD_H,
    WIND_DOWN_GRACE_H,
)

#: Two-phase close wind-down guidance rendered through the assembler's
#: ``closing_guidance`` channel into the next companion turn's state card.
WIND_DOWN_GUIDANCE = "You're wrapping up, say a natural goodbye."

#: Namespace base for per-conversation MEMORY SESSION ids. The MemoryAgent
#: seam (harness/memory.py — must-not-touch) parses session ids with
#: ``re.fullmatch(r"day-(\d+)", ...)`` (``_day_of``: judgement lookup and
#: the eager ``started`` default in ``close_session``), so conversation-
#: scoped session ids must stay day-shaped. They are namespaced ABOVE any
#: real day count: conversation ``conv-<n>`` maps to memory session
#: ``day-<OFFSET+n>``. One memory session per conversation still holds —
#: each conversation gets a unique id; the seam itself is used as-is (the
#: brief: "you change which ids the session passes, not the seam"). A
#: one-line lazy-default fix in memory.py would let honest ids through —
#: reported to the orchestrator.
CONVERSATION_SESSION_OFFSET = 1000

#: Decision-draw stream (WS4, runtime redesign): server_draw verdicts
#: (``HARNESS_DECISION_SOURCE=server_draw``) draw from a DEDICATED engine.rng
#: stream — never the day_rng draw order, so the engine replay contract is
#: untouched. Streams 0..6 are reserved (0=DAILY, 1=EVENTS, 2=EXPERIMENT,
#: 3=INIT, 4=LIFE, 5=PERSONA, 6=CONVERSATION); 7 is this lane's.
DECISION_STREAM = 7

#: Env vars that enable the decision/steering layer (WS2/WS3/WS4). With none
#: of them set the harness behaves exactly as before the runtime redesign:
#: no steering queue activity, no pop-up calls, no reasoning effort.
_DECISION_ENV_VARS = (
    "HARNESS_VERBOSE",
    "HARNESS_BUDGET",
    "HARNESS_DECISION_SOURCE",
    "HARNESS_DECISION_PARSE_FAILURE",
    "HARNESS_TOOL_MODE",
    "HARNESS_NAME",
    "HARNESS_THINKING_EFFORT",
)


def _decision_env_set() -> bool:
    """True when any HARNESS_* decision/steering variable is set (non-empty)."""
    return any(os.environ.get(name) not in (None, "") for name in _DECISION_ENV_VARS)


def _two_phase_close_env_set() -> bool:
    """True when ``HARNESS_TWO_PHASE_CLOSE`` is set (any non-empty value).

    Two-phase close (seam S1) is OFF by default: with the variable unset the
    session behaves exactly as before — the closing draw closes the
    conversation at the drawn turn (byte parity).
    """
    return os.environ.get("HARNESS_TWO_PHASE_CLOSE") not in (None, "")


def _load_thinking_effort() -> str | None:
    """HARNESS_THINKING_EFFORT: none|low|medium|high; unset = no emission.

    The value is passed through to the client as ``reasoning_effort`` when
    set. Per the repo pitfall (3af0a5a) a reasoning model must NEVER receive
    a capped ``max_tokens`` — the session drops the cap whenever an effort
    is configured.
    """
    raw = os.environ.get("HARNESS_THINKING_EFFORT")
    if raw is None or raw.strip() == "":
        return None
    value = raw.strip().lower()
    if value not in ("none", "low", "medium", "high"):
        raise ValueError(
            f"HARNESS_THINKING_EFFORT must be one of none|low|medium|high, "
            f"got {raw!r}"
        )
    return value


@dataclass
class TurnResult:
    """What one on_message()/fire_proactive() produced: the reply + the
    observable state + the mechanical delivery controls.

    Wave 2: ``controls`` (GenerationControls) is what the runtime's delivery
    path reads for ``response_delay_s``; ``directive`` remains for legacy
    callers (runtime falls back to it when controls is None).

    WS4 (runtime redesign): the decision layer's channel outputs ride along
    so the runtime can send them through the channel without the session
    ever touching it:

    - ``notices`` — server notices for no-reply verdicts
      (``tool_decide_reply``, user L361). When a notice is present the
      ordinary reply is suppressed (SINGLE REPLY-PATH invariant: one reply
      per user message — never an ordinary reply AND a decision notice).
    - ``proactive_out`` — ``(reason, text)`` pairs for ``initiate`` verdicts
      (``tool_decide_event``): messages the companion sends through the
      channel as proactive outbound.
    """

    reply: str
    directive: BehaviorDirective
    day: int
    hour: float
    controls: GenerationControls | None = None
    notices: tuple[str, ...] = ()
    proactive_out: tuple[tuple[str, str], ...] = ()


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
        decision_config: DecisionConfig | None = None,
        two_phase_close: bool = False,
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
        # WS-D spend accounting: only stores whose log_llm_call accepts the
        # usage/lane/raw_cost kwargs receive them (legacy store stubs stay
        # untouched and the new llm_calls columns stay NULL for them).
        self._accepts_usage = all(
            k in _llm_params for k in ("usage", "lane", "raw_cost")
        )

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

        # W-close (seam S1): two-phase close flag — OFF by default (env
        # HARNESS_TWO_PHASE_CLOSE overrides). When ON, the closing draw sets
        # a persisted ``closing_pending_t_h`` instead of closing, and the
        # conversation closes after the user's next reply (or by the grace
        # deadline when the user never replies). The resumed wind-down state
        # rides along with the reopened conversation.
        self.two_phase_close = two_phase_close or _two_phase_close_env_set()
        self._closing_pending_t_h: float | None = None
        self._sync_closing_pending()

        # G0 A1: availability negotiations — one NegotiationState per
        # AgendaItem that hit its start boundary while a conversation was
        # open. Rebuilt from persisted ``negotiation_state`` snapshots, so
        # a restart resumes the loop exactly (Inform stays fired, the
        # decide index continues — deterministic replay by decision id).
        self._negotiations: dict[str, NegotiationState] = (
            self._restore_negotiations()
        )

        # WS4 (runtime redesign): steering + decision layer.
        #
        # - SteeringQueue: wired whenever the store exposes the v5 backend
        #   seam (enqueue_steer/pending_steers/mark_steer_delivered/
        #   requeue_steer). With no steers enqueued it is inert — the
        #   pre-redesign behavior is byte-identical.
        # - DecisionRunner: built only when the decision layer is ENABLED —
        #   either explicitly injected (tests) or via any HARNESS_* env var.
        #   With defaults (no env) no pop-up call ever fires.
        self._steering: SteeringQueue | None = None
        self._decision: DecisionRunner | None = None
        self._decision_enabled = decision_config is not None or _decision_env_set()
        self._decision_cfg = (
            decision_config if decision_config is not None else load_decision_config()
        )
        self._thinking_effort = _load_thinking_effort()
        self._day_block: str | None = None
        self._day_block_day: int | None = None
        #: System prompt of the turn in progress — shared with pop-up calls.
        self._last_system_prompt: str = ""
        #: Steers drained for the turn currently being generated — requeued
        #: if the turn is interrupted (the LLM call is abandoned).
        self._turn_drained: list[int] = []
        if all(
            hasattr(store, name)
            for name in (
                "enqueue_steer", "pending_steers",
                "mark_steer_delivered", "requeue_steer",
            )
        ):
            self._steering = SteeringQueue(store)
        if self._decision_enabled and all(
            hasattr(store, name)
            for name in ("record_decision", "decision_for_replay", "decisions_for_day")
        ):
            self._decision = DecisionRunner(
                store,
                verbose=self._decision_cfg.verbose,
                budget=self._decision_cfg.budget,
                decision_source=self._decision_cfg.decision_source,
                parse_failure_mode=self._decision_cfg.parse_failure_mode,
                tool_mode=self._decision_cfg.tool_mode,
                name=self._decision_cfg.name,
                # Dedicated stream: server_draw never touches the day_rng
                # draw order (engine replay contract untouched).
                rng=stream_rng(self.seed, DECISION_STREAM),
            )

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

        Exactly three boundary closes live here (the other two — the
        ``closing_tendency`` draw and ``max_turns`` — fire at companion
        turns inside ``_chat``):

        * ``quiet_hours`` — the conversation's last turn preceded the start
          of the current quiet window (the conversation crossed the 23:00
          boundary; a conversation that OPENED inside quiet hours has no
          crossed boundary and keeps running).
        * ``closing_tendency`` — wind-down expiry (two-phase close, seam
          S1): the closing draw fired at ``closing_pending_t_h`` and the
          user never replied within ``WIND_DOWN_GRACE_H``. The draw already
          decided the close; the grace is delivery, not a new decision.
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
        if (
            self._closing_pending_t_h is not None
            and t_h - self._closing_pending_t_h >= WIND_DOWN_GRACE_H
        ):
            self._close_conversation(conv, t_h, "closing_tendency")
            return "closing_tendency"
        anchor = self._last_user_turn_t_h(conv)
        if anchor is None:
            anchor = conv.opened_t_h
        if t_h - anchor >= USER_LEFT_THRESHOLD_H:
            self._close_conversation(conv, t_h, "user_left")
            return "user_left"
        return None

    def next_conversation_close_t_h(self, now: float) -> float | None:
        """Next strictly-future close instant for the open conversation.

        The earliest of the next quiet-hours boundary (when the
        conversation's last turn precedes it), the ``user_left`` deadline
        and — under two-phase close (seam S1) — the wind-down grace
        deadline ``closing_pending_t_h + WIND_DOWN_GRACE_H``; None when no
        conversation is open or no close is pending. The runtime parks the
        rollover at this instant so the close is recorded at the boundary,
        not lazily at the next turn.
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
        if self._closing_pending_t_h is not None:
            grace_deadline = self._closing_pending_t_h + WIND_DOWN_GRACE_H
            if grace_deadline > now + 1e-12:
                candidates.append(grace_deadline)
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
                self._sync_closing_pending()
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

        TWO-PHASE CLOSE (seam S1, flag ``two_phase_close``): the draw keys
        and consumption are UNCHANGED — a fired draw persists
        ``closing_pending_t_h`` instead of closing (the conversation enters
        its wind-down grace window and the next companion turn's state card
        renders ``WIND_DOWN_GUIDANCE`` through the existing
        ``closing_guidance`` channel). While a wind-down is pending, the
        NEXT companion turn closes deterministically with reason
        ``closing_tendency`` — no second draw. A silent user's conversation
        is closed by the grace deadline in ``check_conversation_lifecycle``
        (``WIND_DOWN_GRACE_H``), with ``user_left`` remaining the outer
        backstop.
        """
        if self._closing_pending_t_h is not None:
            # Wind-down pending: the draw already decided the close; the
            # goodbye turn delivers it (deterministic, no second draw).
            self._close_conversation(conv, t_h, "closing_tendency")
            return
        if not conv.turns:
            return
        # G0 A1: while an availability negotiation is pending, the
        # negotiation owns the conversation end — neither the
        # closing_tendency draw nor the max_turns cap may yank her out
        # mid-negotiation (go closes gracefully via ``followed_event``;
        # skip / forced leave the conversation open). The checks resume
        # once no negotiation is pending.
        if any(not st.resolved for st in self._negotiations.values()):
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
        # closing_tendency draw is feature-flagged OFF (harness/tunables.py).
        # The stream RNG is keyed (not sequential), so skipping the draw stays
        # replay-safe — no consumption to desync. Re-enable via the tunable when
        # the closing model is redefined (flat vs fatigue curve; see BACKLOG).
        if CLOSING_TENDENCY_ENABLED:
            conv_seq = int(conv.id.split("-", 1)[1])
            rng = stream_rng(
                self.seed, CONVERSATION_STREAM, conv_seq, last_turn.turn_index
            )
            if rng.uniform() < float(closing_tendency):
                if self.two_phase_close:
                    self._begin_wind_down(conv, t_h)
                else:
                    self._close_conversation(conv, t_h, "closing_tendency")
                return
        # MAX_TURNS cap is OFF (None): conversations are not capped by turn
        # count — "running out of room" is a compaction concern (see BACKLOG).
        if MAX_TURNS is not None and len(conv.turns) >= MAX_TURNS:
            self._close_conversation(conv, t_h, "max_turns")

    def _begin_wind_down(self, conv: Conversation, t_h: float) -> None:
        """Two-phase close (seam S1): persist the wind-down marker instead
        of closing. The conversation stays open — the next companion turn
        renders the wind-down guidance and closes deterministically, or the
        grace deadline in ``check_conversation_lifecycle`` closes it.
        """
        self._closing_pending_t_h = t_h
        if hasattr(self.store, "set_conversation_closing_pending"):
            self.store.set_conversation_closing_pending(conv.id, t_h)
        self.store.log_event(
            int(t_h // 24.0), t_h, "wind_down_started", f"id={conv.id}"
        )

    def _sync_closing_pending(self) -> None:
        """Restore ``_closing_pending_t_h`` from the store's open
        conversation (resume). No-op without the v6 seam or without an open
        conversation."""
        if self._conversation is None:
            self._closing_pending_t_h = None
            return
        if hasattr(self.store, "conversation_closing_pending"):
            self._closing_pending_t_h = self.store.conversation_closing_pending(
                self._conversation.id
            )

    def _close_conversation(
        self, conv: Conversation, closed_t_h: float, reason: str
    ) -> None:
        """Persist the close (``close_reason``) and drive the per-
        conversation memory tail (L1->L2->L3->L4) at the conversation
        boundary. Idempotent: the store close is an UPDATE and the memory
        tail is summary-guarded. Any pending wind-down marker (two-phase
        close, seam S1) is cleared — a closed conversation has no wind-down
        state."""
        if conv.close_reason is not None:
            return
        self._conversation = None
        if self._closing_pending_t_h is not None:
            self._closing_pending_t_h = None
            if hasattr(self.store, "set_conversation_closing_pending"):
                self.store.set_conversation_closing_pending(conv.id, None)
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

    def _real_time_anchor(self):
        """The store's attached RealTimeAnchor (W1/S1), or None when the run
        is unanchored (replay / legacy fakes). The store exposes no public
        getter (store.py is frozen this wave), so this reads the private
        slot defensively — fakes simply lack it and yield None, which the
        assembler treats as "no temporal section" (G2: never fall back to
        rendering t_h)."""
        return getattr(self.store, "_anchor", None)

    def _transition_agenda_windows(self, day: int, t_h: float) -> None:
        """W2/S2: persist planned→completed transitions for today's items
        whose window has fully passed, keyed off the current ``t_h`` (pure
        deterministic transition — no wall clock). The state card's agenda
        partition renders from the same window comparison, so the render
        and the persisted status agree. Fakes without the store seams are
        skipped."""
        if not hasattr(self.store, "load_agenda") or not hasattr(
            self.store, "update_agenda_item_status"
        ):
            return
        agenda = self.store.load_agenda(day)
        if agenda is None:
            return
        for item in transition_past_windows(agenda, t_h, day):
            self.store.update_agenda_item_status(item.id, item.status)

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

        WS4 (runtime redesign): this method is the IDLE boundary of the
        steering queue — pending steers (event pop-ups, mid-turn user
        messages) are drained here, delivered at turn start, and applied:
        decision pop-ups run through the DecisionRunner (native or textual
        transport), no-reply verdicts suppress the ordinary reply (single
        reply-path invariant), and non-decision steers are rendered into the
        next LLM call's messages wrapped in the steer trust marker. The
        three-tier context assembly (WS1) is unchanged; the day-start block
        is rendered once per day and cached.
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
        if self.two_phase_close and self._closing_pending_t_h is not None:
            # Two-phase close (seam S1): a wind-down is pending — render the
            # wind-down guidance through the assembler's EXISTING
            # ``closing_guidance`` channel (the state card carries it into
            # this companion turn; the turn then closes deterministically in
            # ``_maybe_close_conversation``).
            controls = replace(controls, closing_guidance=WIND_DOWN_GUIDANCE)
        brief = to_brief(directive)

        # it3 B2: one conversation per exchange run — opened by the first
        # message of either party; the memory session id IS the conversation
        # id (one memory session per conversation).
        conv = self._ensure_conversation(
            t_h, opened_by="user" if user_text is not None else "companion"
        )
        conv_id = conv.id
        session_id = self._memory_session_id(conv_id)
        turn_id = self._turn_id(conv)

        query = user_text
        if user_text is None and intent is not None:
            query = intent.hook

        snapshot = self._build_snapshot(day, t_h, brief=brief, intent=intent, query=query)
        # v2 unified brief renderer: the state card's mood line consumes
        # BehaviorDirective.prompt_brief VERBATIM (single source of the
        # 'Current bearing' prose; the assembler never re-renders it).
        # The tier-2 DAY-START block is rendered once per day and cached, so
        # it stays stable within the day (design §2.1: personality + today's
        # agenda at day start; the state card refreshes every turn).
        if self._day_block is None or self._day_block_day != day:
            self._day_block = render_day_block(snapshot)
            self._day_block_day = day
        system = assemble_snapshot(
            snapshot, controls=controls, prompt_brief=directive.prompt_brief,
            day_block=self._day_block,
            # W2: the temporal section (current-time/day line + agenda
            # partition) renders only when the run is anchored; unanchored
            # runs (replay) omit it entirely (G2: never render raw t_h).
            t_h=t_h, anchor=self._real_time_anchor(),
        )
        if user_text is None and intent is None:
            # Legacy ungrounded proactive call (pre-slice callers/tests):
            # generic opening without any invented source claim.
            system += "\n\n" + proactive_block()

        # WS4: pop-up calls (decision layer) share the turn's system prompt.
        self._last_system_prompt = system

        recent = self.store.recent_messages()
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
            # WS-E: never pass None content into the client — a stored
            # reasoning-only turn (content NULL) would serialize as
            # content:null and 400 the request; "" is the safe form.
            messages = [
                {"role": m["role"], "content": m["content"] or ""}
                for m in recent
            ]

        # -- WS4: idle-boundary steering ----------------------------------- #
        # Detect crossed agenda boundaries (event pop-ups) and drain every
        # pending steer into this turn. A no-reply verdict suppresses the
        # ordinary reply; the steer block for non-decision kinds is appended
        # to the messages below. If anything raises, the steers delivered to
        # this turn are re-queued (interrupted turn — WS3 contract).
        notices: list[str] = []
        proactive_out: list[tuple[str, str]] = []
        suppress_reply = False
        injections: list[str] = []
        self._turn_drained = []
        active_before: set[str] = set()
        if self._steering is not None:
            if self._decision_enabled:
                self._enqueue_event_popups(day, t_h)
            # G0 A1: negotiations already in DECIDE BEFORE this turn are
            # due for the companion-turn decide trigger; the Inform turn
            # itself never decides (the loop fires from the next turn on).
            active_before = {
                iid for iid, st in self._negotiations.items()
                if st.phase == NegotiationPhase.DECIDE.value
                and not st.resolved
            }
            drained = self._steering.drain_pending(BOUNDARY_IDLE, turn_id, t_h)
            self._turn_drained = [s.steer_id for s in drained]
            try:
                for steer in drained:
                    outcome = self._apply_steer(
                        steer, day=day, t_h=t_h,
                        notices=notices, proactive_out=proactive_out,
                    )
                    if outcome == _STEER_SUPPRESS:
                        suppress_reply = True
                    if outcome == _STEER_INJECT:
                        injections.append(
                            wrap_steer_marker(render_steer_block(steer))
                        )
                    else:
                        self._turn_drained.remove(steer.steer_id)
                # G0 A1: the availability-negotiation decide loop fires at
                # this companion turn for every negotiation that was
                # already deciding. A go verdict suppresses the ordinary
                # reply — her natural close (proactive_out) is the only
                # message (single reply-path invariant).
                if self._run_turn_decides(
                    day, t_h, proactive_out, active_before=active_before
                ):
                    suppress_reply = True
            except BaseException:
                for steer_id in self._turn_drained:
                    self._steering.requeue(steer_id)
                self._turn_drained = []
                raise

        # W2 (S2): agenda status transitions as windows pass — keyed off the
        # current t_h, persisted via the store, so the persisted status and
        # the state card's temporal partition agree. Runs AFTER the steering
        # drain: the decision layer must still see planned items whose
        # window just ended (END pop-ups / abandon), and any steer outcome
        # (completed/skipped) is never re-drawn by the transition.
        self._transition_agenda_windows(day, t_h)

        if suppress_reply:
            # SINGLE REPLY-PATH invariant: a no-reply verdict means NO
            # ordinary reply for this user message — the server notice goes
            # out instead (the user message above is already persisted).
            self._turn_drained = []
            self.store.log_event(
                day, t_h, "decision_no_reply",
                f"turn={turn_id} notices={len(notices)}",
            )
            return TurnResult(
                reply="", directive=directive, day=day,
                hour=self.clock.local_hour(), controls=controls,
                notices=tuple(notices), proactive_out=tuple(proactive_out),
            )

        if injections:
            messages.append({"role": "user", "content": "\n".join(injections)})

        # -- WS4: thinking ------------------------------------------------ #
        # reasoning_effort passes through HARNESS_THINKING_EFFORT when set;
        # per the repo pitfall (3af0a5a) a reasoning model never receives a
        # capped max_tokens, so the cap is dropped when an effort is set.
        max_tokens = (
            None if self._thinking_effort is not None else controls.max_tokens
        )
        reasoning: str | None = None
        usage = None
        raw_cost = None
        chat_with_meta = getattr(self.client, "chat_with_meta", None)
        if chat_with_meta is None:
            reply = self.client.chat(messages, system=system, max_tokens=max_tokens)
        else:
            result = chat_with_meta(
                messages, system=system, max_tokens=max_tokens,
                reasoning_effort=self._thinking_effort,
            )
            reply = result.content
            reasoning = result.reasoning
            # WS-D spend accounting: the parsed usage + gateway-reported
            # cost ride on the ChatResult; they are persisted (when the
            # store accepts them) alongside the lane attribution.
            usage = getattr(result, "usage", None)
            raw_cost = getattr(result, "raw_cost", None)
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
                "max_tokens": max_tokens,
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
        # WS4: reasoning persists in the call's meta (audit.py renders it
        # under #Thinking; non-reasoning runs store nothing).
        meta = {"reasoning": reasoning} if reasoning else None
        usage_kwargs: dict = {}
        if self._accepts_usage:
            # WS-D spend accounting: parsed usage + WS-C lane attribution
            # + gateway-reported cost (G-cost cross-check). The lane is
            # stamped at client construction; un-laned clients persist
            # lane=NULL (no attribution, row still counted).
            usage_kwargs["usage"] = usage
            usage_kwargs["lane"] = getattr(self.client, "lane", None)
            usage_kwargs["raw_cost"] = raw_cost
        self.store.log_llm_call(
            day,
            t_h,
            "chat",
            system + "\n" + repr(messages),
            reply,
            getattr(self.client, "model", None),
            meta,
            **repro_kwargs,
            **usage_kwargs,
        )
        self.store.log_event(day, t_h, "assistant_reply", f"len={len(reply)}")
        self._turn_drained = []
        return TurnResult(
            reply=reply,
            directive=directive,
            day=day,
            hour=self.clock.local_hour(),
            controls=controls,
            notices=tuple(notices),
            proactive_out=tuple(proactive_out),
        )

    # ------------------------------------------------------------------ #
    # WS4: steering + decision layer (idle boundary, pop-up execution)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _turn_id(conv: Conversation) -> str:
        """Stable id of the turn being generated (the steering seen marker).

        ``conv-<n>#<k>`` where k is the index the next recorded turn will
        get. Deterministic across restarts: a replayed turn computes the
        same id, so the steering queue's persisted seen marker keeps working
        (WS3 replay guard — a steer a turn already saw is never injected
        into it again).
        """
        return f"{conv.id}#{len(conv.turns)}"

    def steering_enabled(self) -> bool:
        """True when the decision/steering layer is active (v5 store seam +
        enabled via env or an injected DecisionConfig)."""
        return self._decision is not None

    def enqueue_user_message_steer(self, text: str, t_h: float) -> int | None:
        """Queue a user message arriving at ``t_h`` for the next boundary.

        The runtime calls this from its inbound path (WS4): when a turn is
        in flight the message is steered into the next safe boundary instead
        of being lost. At the boundary it becomes a ``tool_decide_reply``
        pop-up when an event is in progress (user L356); otherwise it is
        consumed silently (the message is already in the transcript).
        Returns the steer id, or None when the layer is off.
        """
        if self._steering is None or not self._decision_enabled:
            return None
        day = int(t_h // 24.0)
        activity = self._current_activity(day, t_h)
        event = (
            activity.item.activity
            if activity is not None and activity.item is not None
            else "?"
        )
        return self._steering.enqueue(
            KIND_USER_MESSAGE,
            {"message": text, "event": event, "state": "in_progress", "time": t_h},
            day,
            t_h,
        )

    def _enqueue_event_popups(self, day: int, t_h: float) -> None:
        """Detect crossed agenda-item boundaries since the last check and
        queue event pop-ups for them (start and end), delivered at this
        boundary.

        Lazy detection: runs at each turn while the decision layer is
        enabled. The last-check marker is persisted as a
        ``popup_boundary_check`` state event, so restarts never re-enqueue
        the same boundary (the queued steers themselves survive restart in
        the steering_queue table). On a fresh store the first check covers
        the whole current day — a resume mid-day notices items that started
        (or ended) earlier, which is the restart-recovery intent.
        """
        if self._steering is None or not hasattr(self.store, "events_since"):
            return
        prev: float | None = None
        for event in self.store.events_since(0):
            if event.get("event") == "popup_boundary_check":
                prev = float(event["t_h"])
        items = (
            self.store.list_agenda_items(day=day)
            if hasattr(self.store, "list_agenda_items")
            else ()
        )
        now = t_h
        for it in items:
            if it.status != "planned":
                continue
            if (prev is None or it.start_t_h > prev) and it.start_t_h <= now:
                self._steering.enqueue(
                    KIND_EVENT_POPUP,
                    {"event_id": it.id, "event": it.activity, "state": "start",
                     "time": it.start_t_h, "item_id": it.id},
                    day,
                    now,
                )
            if (prev is None or it.end_t_h > prev) and it.end_t_h <= now:
                self._steering.enqueue(
                    KIND_EVENT_POPUP,
                    {"event_id": it.id, "event": it.activity, "state": "end",
                     "time": it.end_t_h, "item_id": it.id},
                    day,
                    now,
                )
        self.store.log_event(day, now, "popup_boundary_check", f"items={len(items)}")

    def _apply_steer(
        self,
        steer: Steer,
        *,
        day: int,
        t_h: float,
        notices: list[str],
        proactive_out: list[tuple[str, str]],
    ) -> str:
        """Apply one delivered steer at a boundary.

        Returns one of the ``_STEER_*`` outcomes:

        - ``_STEER_INJECT`` — no decision attached (or the decision layer is
          off): the caller renders the steer block into the next LLM call's
          messages;
        - ``_STEER_CONSUMED`` — handled: decision executed (or the steer was
          re-queued for the next boundary);
        - ``_STEER_SUPPRESS`` — a no-reply verdict: the ordinary reply for
          this user message must be suppressed (single reply-path
          invariant); the notice is appended to ``notices``.
        """
        if self._decision is None:
            return _STEER_INJECT
        payload = steer.payload or {}
        kind = steer.kind
        if kind == KIND_USER_MESSAGE:
            activity = self._current_activity(day, t_h)
            if activity is None or activity.item is None:
                # No event in progress: the message is already part of the
                # transcript — consume the steer without a pop-up.
                return _STEER_CONSUMED
            result = self._execute_decision(
                decision_id=f"steer-{steer.steer_id}",
                popup_kind="tool_decide_reply",
                inputs={
                    "event_id": activity.item.id,
                    "event_label": activity.item.activity,
                    "state_label": "in_progress",
                    "time": str(t_h),
                    "latest_user_message": str(payload.get("message", "")),
                    "conversation_context": self._conversation_context(),
                },
                steer=steer,
                day=day,
                t_h=t_h,
            )
            if result is None:
                return _STEER_CONSUMED  # re-queued: next boundary
            if result.verdict.get("reply") is False:
                notices.append(result.notice or "")
                return _STEER_SUPPRESS
            if result.verdict.get("terminate_event"):
                self._mark_event_closed(activity.item.id)
            return _STEER_CONSUMED
        if kind == KIND_EVENT_POPUP:
            state = str(payload.get("state", "start"))
            item_id = str(
                payload.get("item_id") or payload.get("event_id") or ""
            )
            if item_id:
                if state == "start" and self._maybe_start_negotiation(
                    item_id, day, t_h, steer, proactive_out
                ):
                    # The availability negotiation owns the START pop-up:
                    # Inform-once (or a re-delivery of a negotiation whose
                    # responded-bool marker is not True) then the Decide
                    # loop. Consumed — the plain start semantics do not run.
                    return _STEER_CONSUMED
                if state == "end" and item_id in self._negotiations:
                    # The negotiation owns the item's lifecycle: the END
                    # pop-up is consumed without a model call (the item was
                    # resolved at its decide instants — go completes it,
                    # skip/forced mark it skipped).
                    return _STEER_CONSUMED
            result = self._execute_decision(
                decision_id=f"steer-{steer.steer_id}",
                popup_kind="tool_decide_event",
                inputs={
                    "event_id": str(
                        payload.get("item_id") or payload.get("event_id") or ""
                    ),
                    "event_label": str(payload.get("event") or "?"),
                    "state_label": state,
                    "time": str(payload.get("time", t_h)),
                },
                steer=steer,
                day=day,
                t_h=t_h,
            )
            if result is None:
                return _STEER_CONSUMED  # re-queued: next boundary
            verdict = result.verdict
            if state == "start" and verdict.get("initiate"):
                # Initiate: she engages the event — a proactive message goes
                # out through the channel (her own reason when present).
                label = str(payload.get("event") or "?")
                text = str(verdict.get("reason") or "").strip() or f"Starting {label}."
                proactive_out.append(("event_popup", text))
            elif state == "end" and verdict.get("action") == "abandon":
                self._mark_event_closed(
                    str(payload.get("item_id") or payload.get("event_id") or "")
                )
            return _STEER_CONSUMED
        # schedule_fire / day_rollover (or unknown kinds): the harness's own
        # paths own those flows; the block is still rendered as context.
        return _STEER_INJECT

    def _execute_decision(
        self,
        decision_id: str,
        popup_kind: str,
        inputs: dict,
        *,
        steer: Steer,
        day: int,
        t_h: float,
    ) -> DecisionResult | None:
        """Run one pop-up decision through the DecisionRunner.

        Returns the DecisionResult, or None when the pop-up was re-queued
        (parse-failure policy ``requeue`` — the raw reply stays persisted,
        the verdict does not; the steer is delivered again at the next
        boundary). The decision_id is the steer id — stable across restarts
        — so a re-drained steer REPLAYS its recorded verdict instead of
        re-rolling (deterministic replay).
        """
        assert self._decision is not None
        try:
            return self._decision.execute(
                decision_id,
                popup_kind,
                inputs,
                Capabilities(
                    has_native_tools=bool(
                        getattr(self.client, "supports_tools", False)
                    )
                ),
                lambda request: self._popup_request_call(request),
                day=day,
                t_h=t_h,
                delivered_t_h=steer.delivered_t_h,
            )
        except DecisionRequeue:
            if self._steering is not None:
                self._steering.requeue(steer.steer_id)
            return None

    def _popup_request_call(self, request: PopupRequest) -> RawReply:
        """One pop-up model call (the callable injected into the runner).

        Builds the real request from the ``PopupRequest``: the current
        three-tier system prompt (stable core + day-start block + state
        card, as assembled for the turn in progress), the recent transcript,
        and the pop-up block wrapped in the steer trust marker as the final
        user message. Native transport offers the tool schemas
        (``tool_choice=auto``); textual transport relies on the model's
        ``tool_decide_*: {...}`` marker reply. The returned ``RawReply``
        carries the model's raw output for the runner to parse and persist
        (dual persistence). ``max_tokens`` stays None on pop-up calls (they
        are short verdicts and a cap must never starve a reasoning model —
        repo pitfall 3af0a5a).
        """
        recent = self.store.recent_messages()
        # WS-E: never pass None content into the client (a stored
        # reasoning-only turn must serialize as "", never null).
        messages = [
            {"role": m["role"], "content": m["content"] or ""} for m in recent
        ]
        messages.append(
            {"role": "user", "content": wrap_steer_marker(request.popup)}
        )
        # Native transport: the runner's schemas are Hermes-style
        # {name, description, parameters}; OpenAI-compatible endpoints
        # require the {"type": "function", "function": ...} wrapper (the
        # decision-probe callable wraps its own copy the same way — this
        # is the ONLY consumer of request.tools, so the wrap belongs
        # here, at the transport boundary).
        native_tools = None
        if request.native and request.tools:
            native_tools = [
                {"type": "function", "function": t} for t in request.tools
            ]
        result = self.client.chat_with_meta(
            messages,
            system=self._last_system_prompt,
            temperature=0.8,
            max_tokens=None,
            tools=native_tools,
            tool_choice="auto" if request.native else None,
            reasoning_effort=self._thinking_effort,
        )
        raw_tool_calls = None
        if result.tool_calls:
            # ChatResult tool calls are {id, name, arguments_json}; the
            # runner's parser expects the OpenAI shape.
            raw_tool_calls = [
                {
                    "id": tc.get("id"),
                    "type": "function",
                    "function": {
                        "name": tc.get("name"),
                        "arguments": tc.get("arguments_json"),
                    },
                }
                for tc in result.tool_calls
            ]
        return RawReply(text=result.content or None, tool_calls=raw_tool_calls)

    def _conversation_context(self, limit: int = 4) -> str:
        """Condensed recent transcript for decide_reply pop-up inputs."""
        recent = self.store.recent_messages(limit=limit)
        return "\n".join(
            f"{m['role']}: {str(m['content'])[:200]}" for m in recent
        )

    def _mark_event_closed(self, item_id: str) -> None:
        """Server-side event close (``terminate_event`` verdict / ``abandon``
        action): the agenda item is no longer in progress — marked skipped so
        the NOW-semantics state card stops showing it."""
        if not item_id or not hasattr(self.store, "update_agenda_item_status"):
            return
        self.store.update_agenda_item_status(item_id, "skipped")

    # ------------------------------------------------------------------ #
    # G0 A1: availability negotiation (Inform-once -> Decide loop)
    # ------------------------------------------------------------------ #

    def _restore_negotiations(self) -> dict[str, NegotiationState]:
        """Rebuild active negotiations from persisted ``negotiation_state``
        state-event snapshots (latest per item wins). Runs once at session
        init, so a restart resumes the loop without re-Informing."""
        out: dict[str, NegotiationState] = {}
        if not hasattr(self.store, "events_since"):
            return out
        for event in self.store.events_since(0):
            if event.get("event") != "negotiation_state":
                continue
            st = state_from_dict(json.loads(event.get("detail") or "{}"))
            if st is not None:
                out[st.item_id] = st
        return out

    def _persist_negotiation(self, st: NegotiationState, t_h: float) -> None:
        """Persist one negotiation as a full JSON snapshot (state event).
        Every mutation writes a snapshot, so restart recovery is exact."""
        if not hasattr(self.store, "log_event"):
            return
        self.store.log_event(
            int(t_h // 24.0), t_h, "negotiation_state",
            json.dumps(state_to_dict(st), sort_keys=True),
        )

    def _find_agenda_item(self, item_id: str, day: int):
        """Today's AgendaItem by id (the popup payload carries the id; the
        item's source_type/salience/end_t_h drive the negotiation)."""
        items = (
            self.store.list_agenda_items(day=day)
            if hasattr(self.store, "list_agenda_items")
            else ()
        )
        for it in items:
            if it.id == item_id:
                return it
        return None

    def _afk_anchor(self) -> float | None:
        """The AFK bomb's anchor: the conversation's last USER turn (or its
        opening when the companion opened and the user never replied —
        same fallback as the ``user_left`` close). ``None`` when no
        conversation is open (a negotiation only exists while one is)."""
        conv = self._conversation
        if conv is None:
            return None
        anchor = self._last_user_turn_t_h(conv)
        if anchor is None:
            anchor = conv.opened_t_h
        return anchor

    def next_negotiation_trigger_t_h(self, now: float) -> float | None:
        """Next strictly-future negotiation wake instant for the runtime's
        rollover park: the earlier of the AFK-bomb decide instant and the
        window-close backstop instant of the earliest active negotiation.
        None when no negotiation is pending. Mirrors
        :meth:`next_conversation_close_t_h` exactly (future instants only;
        a past deadline fires at the next wake of any kind)."""
        candidates: list[float] = []
        for st in self._negotiations.values():
            nxt = next_trigger_t_h(st, now)
            if nxt is not None:
                candidates.append(nxt)
        return min(candidates) if candidates else None

    def check_negotiation(self, now: float) -> tuple[tuple[str, str], ...]:
        """Runtime wake hook (the parallel of ``check_conversation_lifecycle``).

        Runs lazy event-boundary detection (so start/end pop-ups enqueue
        even between turns) and then every due decide leg of the active
        negotiations: the AFK bomb fired (silence > SHORT_AFK) or the
        window closed (backstop — forced skip, no model call). Returns the
        proactive ``(reason, text)`` messages the decide legs produced
        (her natural close on ``go``) for the runtime to send through the
        channel. Idempotent per virtual instant: a decide leg executes at
        most once per instant per item, so repeated wakes can never
        double-fire or hot-loop the model."""
        outs: list[tuple[str, str]] = []
        if self._decision is None or self._steering is None:
            return ()
        day = int(now // 24.0)
        if self._decision_enabled:
            self._enqueue_event_popups(day, now)
        for item_id in list(self._negotiations):
            st = self._negotiations[item_id]
            status = decide_status_at(st, now=now, companion_turn=False)
            if status == "forced":
                self._resolve_forced(st, now)
            elif status == "due":
                self._run_decide_leg(st, day, now, outs, afk_path=True)
        return tuple(outs)

    def _maybe_start_negotiation(
        self,
        item_id: str,
        day: int,
        t_h: float,
        steer: Steer,
        proactive_out: list[tuple[str, str]],
    ) -> bool:
        """Route a START event pop-up into the availability negotiation.

        Returns True when the pop-up belongs to the negotiation machine
        (consumed without the plain start-popup semantics); False lets the
        caller keep the EXACT existing tool_decide_event semantics.

        The negotiation activates only when a conversation was OPEN at the
        item's start boundary (``conv.opened_t_h <= start_t_h``): an item
        whose boundary landed with NO open conversation skips Inform and
        keeps the plain start popup as its Decide (G0 floor) — the pop-up
        is then delivered at the first later turn unchanged. A negotiation
        that already exists owns the pop-up: a re-delivered start pop-up
        (interrupted-turn requeue) re-runs Inform only while the responded-
        bool marker ``informed`` is not True; a resolved negotiation just
        consumes it.
        """
        if self._decision is None or self._steering is None:
            return False
        conv = self._conversation
        if conv is None:
            return False  # no open conversation: plain semantics
        st = self._negotiations.get(item_id)
        if st is not None:
            if (
                st.phase == NegotiationPhase.INFORM.value
                and st.informed is not True
            ):
                self._run_inform(st, day, t_h, steer, proactive_out)
            return True
        item = self._find_agenda_item(item_id, day)
        if item is None:
            return False
        if (
            item.status != "planned"
            or t_h < item.start_t_h - 1e-12
            or t_h >= item.end_t_h - 1e-12
        ):
            return False  # dead / not-yet / closed window: plain semantics
        if conv.opened_t_h > item.start_t_h + 1e-12:
            # The conversation OPENED after the boundary landed — at the
            # boundary no conversation was open, so no negotiation (the
            # plain start pop-up IS the Decide for that case).
            return False
        st = NegotiationState(
            item_id=item.id,
            activity=item.activity,
            source_type=item.source_type,
            start_t_h=item.start_t_h,
            end_t_h=item.end_t_h,
            salience=item.salience,
        )
        self._negotiations[item.id] = st
        self._persist_negotiation(st, t_h)
        self._run_inform(st, day, t_h, steer, proactive_out)
        return True

    def _run_inform(
        self,
        st: NegotiationState,
        day: int,
        t_h: float,
        steer: Steer,
        proactive_out: list[tuple[str, str]],
    ) -> None:
        """INFORM leg: the model mentions the event naturally (the reason
        text rides out through ``proactive_out`` as a channel message). NO
        verdict is executed — she does not leave, nothing is resolved. The
        responded-bool marker ``informed`` flips to True exactly once; the
        decision id is deterministic (``neg-<item_id>-inform``), so
        DecisionRunner's replay-by-decision_id makes a restart replay the
        recorded mention instead of re-rolling.
        """
        assert self._decision is not None
        decision_id = f"neg-{st.item_id}-inform"
        inputs = {
            "event_id": st.item_id,
            "event_label": st.activity,
            "state_label": "inform",
            "time": str(t_h),
            "phase": NegotiationPhase.INFORM.value,
            "skippable": is_skippable(st.source_type),
            "conversation_context": self._conversation_context(),
        }
        result = self._execute_decision(
            decision_id, "tool_decide_event", inputs, steer=steer,
            day=day, t_h=t_h,
        )
        if result is None:
            # Parse failure (requeue policy): the steer is back in the
            # queue; the state stays INFORM (informed not True) and the
            # re-delivered pop-up re-runs this same decision id.
            return
        mention = str(
            (result.verdict or {}).get("message")
            or (result.verdict or {}).get("reason")
            or ""
        ).strip()
        if not mention:
            mention = f"I've got {st.activity} coming up soon."
        proactive_out.append(("event_popup", mention))
        # Responded-bool idempotency marker: checked as VALUE True
        # (``informed is True``), never key presence.
        st.informed = True
        st.phase = NegotiationPhase.DECIDE.value
        st.turns_to_decide = 0            # the NEXT companion turn decides
        st.afk_deadline_t_h = None
        anchor = self._afk_anchor()
        if anchor is not None:
            st.afk_deadline_t_h = anchor + SHORT_AFK_H
        st.last_decide_at_t_h = t_h       # this turn must not decide
        self._persist_negotiation(st, t_h)
        self.store.log_event(
            int(t_h // 24.0), t_h, "negotiation_inform",
            f"item={st.item_id}",
        )

    def _run_turn_decides(
        self,
        day: int,
        t_h: float,
        proactive_out: list[tuple[str, str]],
        *,
        active_before: set[str],
    ) -> bool:
        """Companion-turn decide trigger: run the due decide leg of every
        negotiation that was ALREADY in DECIDE before this turn (the
        Inform turn itself never decides — the loop fires from the NEXT
        companion turn on). Returns True when a ``go`` resolved this turn:
        the ordinary reply is suppressed (her natural close is the only
        message — single reply-path invariant)."""
        suppress = False
        for item_id in active_before:
            st = self._negotiations.get(item_id)
            if st is None or st.resolved:
                continue
            status = decide_status_at(st, now=t_h, companion_turn=True)
            if status == "forced":
                self._resolve_forced(st, t_h)
            elif status == "due":
                self._run_decide_leg(st, day, t_h, proactive_out)
                if st.phase == NegotiationPhase.RESOLVED_GO.value:
                    suppress = True
        return suppress

    def _run_decide_leg(
        self,
        st: NegotiationState,
        day: int,
        t_h: float,
        proactive_out: list[tuple[str, str]],
        *,
        afk_path: bool = False,
    ) -> None:
        """One DECIDE leg. The decision id is deterministic per
        (item, delay index): ``neg-<item_id>-decide-<delay_count>``, so a
        restart replays the recorded verdict instead of re-rolling. The
        request carries the A2 schema keys (phase/skippable/delay_count/
        window_ending) plus the converging-pull context (delay_count,
        pull, remaining window) the MODEL sees — the server never overrides
        the verdict. State mutation is synchronous, so a same-instant
        double fire (turn + runtime wake) is a no-op."""
        assert self._decision is not None
        decision_id = f"neg-{st.item_id}-decide-{st.delay_count}"
        remaining = max(0.0, st.end_t_h - t_h)
        inputs = {
            "event_id": st.item_id,
            "event_label": st.activity,
            "state_label": "decide",
            "time": str(t_h),
            "phase": NegotiationPhase.DECIDE.value,
            "skippable": is_skippable(st.source_type),
            "delay_count": st.delay_count,
            "window_ending": window_ending_at(st, t_h),
            "pull": round(pull_toward_go(st), 4),
            "remaining_h": round(remaining, 4),
            "conversation_context": self._conversation_context(),
        }
        steer = Steer(
            steer_id=-1,  # synthetic: clock/turn driven, not a queued steer
            day=day,
            t_h=(
                st.afk_deadline_t_h
                if afk_path and st.afk_deadline_t_h is not None
                else t_h
            ),
            kind=KIND_EVENT_POPUP,
            payload={
                "item_id": st.item_id, "event": st.activity,
                "state": "decide", "time": t_h,
            },
            delivered_t_h=t_h,
        )
        result = self._execute_decision(
            decision_id, "tool_decide_event", inputs, steer=steer,
            day=day, t_h=t_h,
        )
        st.last_decide_at_t_h = t_h
        self._persist_negotiation(st, t_h)
        if result is None:
            # Parse failure (requeue policy): the synthetic steer has no
            # queue row to requeue (the SQLite backend no-ops on id -1);
            # the state stays in DECIDE and the next decide instant retries
            # the SAME decision id (a parse failure records no decision
            # row, so the retry re-calls the model — no false replay).
            return
        verdict = result.verdict or {}
        reason = str(verdict.get("reason") or "")
        action = verdict.get("action")
        if action not in ("follow", "abandon", "defer"):
            # Pre-A2 L369 verdicts carry no action: initiate is the
            # fallback (True -> go, False -> skip). A2's schema ships the
            # action as the primary signal.
            action = (
                "follow" if verdict.get("initiate") is True
                else "abandon" if verdict.get("initiate") is False
                else None
            )
        if action == "follow":
            self._resolve_go(st, t_h, reason, proactive_out)
        elif action == "abandon":
            self._resolve_skip(st, t_h, reason)
        elif action == "defer":
            self._resolve_delay(st, t_h, verdict)
        else:  # pragma: no cover — defensive: terminal and bounded
            self._resolve_skip(st, t_h, reason or "no actionable verdict")

    def _resolve_go(
        self,
        st: NegotiationState,
        t_h: float,
        reason: str,
        proactive_out: list[tuple[str, str]],
    ) -> None:
        """go (follow): her natural close rides out through the channel,
        the conversation closes gracefully (close_reason
        ``followed_event``), the agenda item completes, and the episode
        hook fires (A3's module; no emission when it has not landed)."""
        text = (reason or "").strip() or f"Time to go to {st.activity}."
        proactive_out.append(("event_popup", text))
        conv = self._conversation
        source_session_id = ""
        if conv is not None:
            source_session_id = self._memory_session_id(conv.id)
            self._close_conversation(conv, t_h, "followed_event")
        if hasattr(self.store, "update_agenda_item_status"):
            self.store.update_agenda_item_status(st.item_id, "completed")
        st.phase = NegotiationPhase.RESOLVED_GO.value
        st.resolved_action = "follow"
        st.resolved_t_h = t_h
        self._persist_negotiation(st, t_h)
        self._log_resolution(st, t_h, "go")
        self._emit_episode(st, "GO", t_h, source_session_id)

    def _resolve_skip(self, st: NegotiationState, t_h: float, reason: str) -> None:
        """skip (abandon): the activity is dropped (status ``skipped``,
        recorded), the conversation continues. Terminal."""
        if hasattr(self.store, "update_agenda_item_status"):
            self.store.update_agenda_item_status(st.item_id, "skipped")
        st.phase = NegotiationPhase.RESOLVED_SKIP.value
        st.resolved_action = "abandon"
        st.resolved_t_h = t_h
        self._persist_negotiation(st, t_h)
        self._log_resolution(st, t_h, "skip")
        self._emit_episode(st, "SKIP", t_h, self._memory_session_id(
            self._conversation.id
        ) if self._conversation is not None else "")

    def _resolve_forced(self, st: NegotiationState, t_h: float,
                        reason: str | None = None) -> None:
        """BACKSTOP: ``now >= end_t_h`` at a decide instant (or a delay
        whose re-arm would land at/after the window close) — forced skip
        ("missed it entirely"), NO model call. Recorded as a decision row
        (source ``backstop``) so the trace is complete and a restart
        replays the forced outcome instead of re-asking. Terminal."""
        if st.resolved:
            return
        if hasattr(self.store, "update_agenda_item_status"):
            self.store.update_agenda_item_status(st.item_id, "skipped")
        if hasattr(self.store, "record_decision"):
            self.store.record_decision(
                int(t_h // 24.0), t_h, "tool_decide_event",
                st.item_id, st.activity, "decide", str(t_h), None, None,
                json.dumps({
                    "initiate": False,
                    "reason": reason or "missed it entirely — window closed",
                    "action": "abandon",
                    "forced_skip": True,
                }, sort_keys=True),
                "backstop", "server_draw", t_h, 0,
                replay_id=f"neg-{st.item_id}-decide-{st.delay_count}",
            )
        st.phase = NegotiationPhase.RESOLVED_FORCED.value
        st.resolved_action = "forced"
        st.resolved_t_h = t_h
        self._persist_negotiation(st, t_h)
        self._log_resolution(st, t_h, "forced")
        self._emit_episode(st, "FORCED", t_h, self._memory_session_id(
            self._conversation.id
        ) if self._conversation is not None else "")

    def _resolve_delay(self, st: NegotiationState, t_h: float,
                       verdict: dict) -> None:
        """delay (defer): the server maps the reason text to N
        (``DEFER_N_PATTERNS``, clamped) and re-arms BOTH triggers — the
        companion-turn counter (N turns) and the AFK bomb (last user turn
        + SHORT_AFK). A re-arm that would land at/after ``end_t_h``
        resolves immediately as a forced skip instead (the backstop clamp:
        defer never re-arms past the window)."""
        # Prefer the runner's SERVER-FILLED defer_turns (A2 fills the
        # recorded verdict deterministically from the reason); fall back to
        # the identical mapping for runners that predate the fill — the
        # re-armed N always equals the recorded defer_turns by
        # construction. The model never emits N.
        n = verdict.get(DEFER_TURNS_KEY)
        if not isinstance(n, int):
            n = map_defer_n(str(verdict.get("reason") or ""))
        anchor = self._afk_anchor()
        if not rearm_after_delay(
            st, now=t_h, last_user_turn_t_h=anchor, n=n
        ):
            # The AFK bomb would land at/after the window close: the delay
            # resolves immediately (forced skip) — never a re-arm past
            # end_t_h (G0 floor: termination is guaranteed).
            self._resolve_forced(st, t_h, "window closed before the next decide")
            return
        self._persist_negotiation(st, t_h)
        self.store.log_event(
            int(t_h // 24.0), t_h, "negotiation_delay",
            f"item={st.item_id} n={n} delays={st.delay_count}",
        )

    def _log_resolution(self, st: NegotiationState, t_h: float,
                        outcome: str) -> None:
        if hasattr(self.store, "log_event"):
            self.store.log_event(
                int(t_h // 24.0), t_h, "negotiation_resolved",
                f"item={st.item_id} outcome={outcome} "
                f"delays={st.delay_count}",
            )

    def _emit_episode(self, st: NegotiationState, outcome: str, t_h: float,
                      source_session_id: str) -> None:
        """Call the A3 episode hook (imported defensively — checkouts
        without ``harness/negotiation_episodes.py`` emit nothing). The
        salience gate (a plain zero-delay go does not emit) lives in A3's
        module; the hook is replay-idempotent (deterministic id upsert)."""
        if emit_negotiation_episode is None or not hasattr(
            self.store, "insert_episode"
        ):
            return
        try:
            emit_negotiation_episode(self.store, NegotiationEpisode(
                item_id=st.item_id,
                activity=st.activity,
                outcome=outcome,
                delay_count=st.delay_count,
                salience=st.salience,
                occurred_at_t_h=t_h,
                summary="",
                source_session_id=source_session_id,
                tags=(),
            ))
        except Exception:  # pragma: no cover — the hook must never break
            # the negotiation's own resolution (A3 seam, best-effort).
            self.store.log_event(
                int(t_h // 24.0), t_h, "negotiation_episode_error",
                f"item={st.item_id} outcome={outcome}",
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
