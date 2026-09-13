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
import re
from dataclasses import dataclass, field, replace
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
    append_system,
    assemble_snapshot,
    prefix_break,
    build_context_messages,
    harness_text_in_user_roles,
    proactive_block,
    render_day_block,
    render_day_start_block,
    wire_message,
)
from harness.behavior import BehaviorDirective, derive_behavior
from harness.clock import VirtualClock, hhmm
from harness.env import env_bool as _env_bool
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
    NegotiationPhase,
)
from harness.negotiation_state import (
    NegotiationState,
)
from harness.negotiation_contract import HEADS_UP_LEAD_H
from harness.negotiation_coordinator import START_NOTE, NegotiationMixin
from harness.scheduler import VALID_REASONS
from harness.score import synthetic_score as run_daily_synthetic_score
from harness.steering import (
    BOUNDARY_IDLE,
    KIND_EVENT_POPUP,
    KIND_PROACTIVE,
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
    offered_tools,
    tools_identity,
)

#: Steer-application outcomes (``_apply_steer`` return codes).
_STEER_INJECT = "inject"        #: render the block into the next LLM call
_STEER_CONSUMED = "consumed"    #: handled (decision executed / consumed)
_STEER_SUPPRESS = "suppress"    #: no-reply verdict — suppress the reply

#: Closing-tendency draw stream (engine.rng stream 6), keyed by
#: (conversation sequence, companion turn index) — deterministic per draw.
CONVERSATION_STREAM = 6

# Conversation-lifecycle tunables re-exported from harness/tunables.py.
from harness.tunables import (  # noqa: E402
    CLOSING_TENDENCY_ENABLED,
    MAX_TURNS,
    USER_AWAY_THRESHOLD_H,
    USER_LEFT_THRESHOLD_H,
    WIND_DOWN_GRACE_H,
)

#: Two-phase close wind-down guidance rendered through the assembler's
#: ``closing_guidance`` channel into the next companion turn's state card.
WIND_DOWN_GUIDANCE = "You're wrapping up, say a natural goodbye."

#: kv_store key holding the context compaction watermark (see
#: ``Session.context_epoch_id``).
CONTEXT_EPOCH_KEY = "context.epoch_id"

#: Retained-history size that triggers compaction at a day boundary.
CONTEXT_COMPACT_AFTER_MESSAGES = 400

#: Messages kept when compaction fires (the newest N).
CONTEXT_RETAIN_MESSAGES = 200

#: Memory-session ids stay day-shaped (day-<OFFSET+n> for conversation n);
#: the MemoryAgent seam parses ids as day-<n>.
CONVERSATION_SESSION_OFFSET = 1000

#: Decision-draw stream (engine.rng stream 7); server_draw verdicts draw
#: from this stream, never the day_rng draw order.
DECISION_STREAM = 7

#: Env vars enabling the decision/steering layer. Unset, the harness runs
#: without steering or pop-up calls.
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
    path reads for ``response_delay_s``.

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
    - ``bubbles`` — when HARNESS_BUBBLES is on and the model emitted blank
      lines, the split bubble texts (\\n or \\n\\n count — a run of newlines
      is one separator, WS-B ruling). The runtime sends them as a paced
      multi-send. When the flag is off or no split exists, None (parity).
    - ``streamed`` — True when ``bubbles`` were parsed INCREMENTALLY off the
      backend stream (HARNESS_BUBBLE_STREAM + a client with ``chat_stream``)
      instead of post-hoc at the end of a non-streaming reply. Delivery is
      unchanged — the runtime's paced multi-send does not care how the
      split was made (sequential send_message, no SSE/edit). Always False
      when ``bubbles`` is None, so ``streamed`` implies a paced bubble
      send.
    """

    reply: str
    directive: BehaviorDirective
    day: int
    hour: float
    controls: GenerationControls | None = None
    notices: tuple[str, ...] = ()
    proactive_out: tuple[tuple[str, str], ...] = ()
    bubbles: tuple[str, ...] | None = None
    #: True when this turn's bubbles arrived via backend streaming (see the
    #: docstring above). Marker only — the send path is byte-identical.
    streamed: bool = False


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


@dataclass
class _SteerDrain:
    """What draining this turn's steers produced.

    Mutable by design: ``_apply_steer`` appends to ``notices`` and
    ``proactive_out`` through the same lists the caller reads back.
    ``decided_intents`` carries grounded proactive intents whose
    initiate verdict cleared them for this turn's generation;
    ``proactive_declined`` marks a decline that suppresses the reply.
    """

    notices: list[str] = field(default_factory=list)
    proactive_out: list[tuple[str, str]] = field(default_factory=list)
    injections: list[str] = field(default_factory=list)
    suppress_reply: bool = False
    decided_intents: list = field(default_factory=list)
    proactive_declined: bool = False
    #: Notes about what she just decided, appended to THIS turn's tail so the
    #: turn generates her words for it. Replaces pasting the verdict's
    #: ``reason`` into the channel: the reason is machine-facing rationale
    #: ("I'll send a warm in-character send-off and keep cooking") and reads
    #: as third-person narration about herself. The reason is still recorded
    #: in ``decision_records`` -- it is the engine's audit trail -- it just
    #: no longer doubles as dialogue.
    decided_notes: list[str] = field(default_factory=list)
    #: Close reason to apply AFTER the reply is persisted, when a verdict
    #: ends the conversation. Closing before generation left the user's
    #: goodbye unanswered (live 2026-09-07).
    close_after: str | None = None


#: Tool-call markup a model may emit as reply CONTENT instead of as a
#: structured tool call.
#:
#: This is not hypothetical. On 2026-09-08 a conversational turn came back as
#: DeepSeek DSML markup and the harness persisted it as an assistant message
#: and sent it to the channel verbatim:
#:
#:     <｜｜DSML｜｜tool_calls>
#:     <｜｜DSML｜｜invoke name="tool_decide_reply">
#:     <｜｜DSML｜｜parameter name="reason" ...
#:
#: The decision lane is the mirror image of the same confusion — a pop-up
#: answered with prose instead of a tool call — and both got likelier once
#: decisions began replaying into main context AS native tool exchanges
#: (which is what taught the model that tool calls happen here at all).
#: Nothing downstream can catch this: it is neither empty nor malformed, just
#: machinery where her words should be.
#:
#: Fullwidth vertical bars (U+FF5C) are DeepSeek's; the ASCII forms cover the
#: other families. The harness's own textual-fallback marker is included
#: because a mainline reply that opens with ``tool_decide_reply: {...}`` is
#: the same mistake in the harness's own notation.
_TOOL_MARKUP_PATTERNS: tuple[str, ...] = (
    # <｜｜DSML｜｜tool_calls> AND its </｜｜DSML｜｜parameter> closers — the
    # optional slash matters: without it the closing tags survive stripping
    # and the parameter bodies between them read as salvaged "prose".
    r"</?[｜|]{0,2}\s*DSML\s*[｜|]{0,2}[^>]*>",
    r"</?[｜|]?\s*tool_calls?(?:_begin|_end)?\s*[｜|]?>",
    r"</?[｜|]?\s*function_calls?\s*[｜|]?>",
    r"</?invoke\b[^>]*>",
    r"</?parameter\b[^>]*>",
    r"</?antml:\w+\b[^>]*>",
    # The harness's own textual-fallback notation, marker and payload.
    r"^\s*tool_decide_(?:event|reply|proactive)\s*:\s*(?:\{.*?\})?",
)
_TOOL_MARKUP = re.compile("|".join(_TOOL_MARKUP_PATTERNS),
                          re.IGNORECASE | re.MULTILINE)

#: State event recorded whenever a mainline reply carried tool-call markup,
#: whether or not prose was salvaged from it.
EVENT_TOOL_MARKUP_LEAK = "reply_tool_markup"


def looks_like_tool_markup(reply: str) -> bool:
    """True when a mainline reply carries tool-call machinery."""
    return bool(_TOOL_MARKUP.search(reply or ""))


def strip_tool_markup(reply: str) -> str:
    """Remove tool-call markup, returning whatever prose is left.

    A model sometimes emits markup AND real prose in one reply; that prose is
    hers and worth keeping. When the reply is nothing but machinery this
    returns ``""`` and the caller treats the generation as failed — silence
    the runtime can retry beats markup the user has already read.
    """
    text = reply or ""
    # When markup OPENS the reply, the whole thing is a tool call and the
    # text between the tags is argument values — the model's own rationale,
    # not her words. Salvaging that would put machine reasoning in the
    # channel as dialogue, which is the exact failure the decided-notes work
    # removed. Markup that appears AFTER prose is an aside: strip it, keep
    # what she actually said.
    head = _TOOL_MARKUP.match(text.lstrip())
    if head is not None:
        return ""
    text = _TOOL_MARKUP.sub("", text)
    # Parameter/result bodies left behind by the stripped tags are machinery
    # too: drop any line that is only an attribute-ish fragment or a brace.
    kept = [
        line for line in text.splitlines()
        if line.strip() and not re.fullmatch(
            r"[\s{}\[\],]*|(?:name|string|type|value)\s*=.*|</?[^>]*>",
            line.strip(),
        )
    ]
    out = "\n".join(kept).strip()
    # Belt and braces: if any recognisable machinery token SURVIVED the
    # stripping, the reply is not something to hand a person. Salvaging half a
    # tool call is worse than salvaging nothing, so give up rather than guess.
    if re.search(r"DSML|tool_decide_|tool_call|<\s*/?\s*invoke", out,
                 re.IGNORECASE):
        return ""
    return out


def _with_bubble_instruction(system: str) -> str:
    """Append the bubble instruction when HARNESS_BUBBLES is on.

    Import and flag read are both inside the guard on purpose: bubbles are
    an optional layer, and a harness built without the module (or with a
    broken flag) must still produce an ordinary single-message turn.
    """
    try:
        from harness.bubbles import BUBBLE_INSTRUCTION, bubbles_enabled

        if bubbles_enabled():
            return system + "\n\n" + BUBBLE_INSTRUCTION
    except Exception:
        pass
    return system


def _split_into_bubbles(reply: str) -> tuple[str, ...] | None:
    """Split a reply on blank-line runs when HARNESS_BUBBLES is on.

    Both ``\n`` and ``\n\n`` count as one boundary. None means "send as one
    message" — which is also what a single part means, so the caller never
    has to distinguish "off" from "the model did not split".
    """
    try:
        from harness.bubbles import bubbles_enabled, parse_bubbles

        if bubbles_enabled():
            parts = parse_bubbles(reply)
            if len(parts) >= 2:
                return tuple(parts)
    except Exception:
        pass
    return None


def _bubble_stream_on() -> bool:
    """Whether backend bubble streaming is enabled for this turn.

    Guarded like _split_into_bubbles: the bubbles module is an optional
    layer, and a harness built without it must keep the canonical
    non-streaming path. Requires HARNESS_BUBBLE_STREAM AND HARNESS_BUBBLES
    (enforced inside bubble_stream_enabled).
    """
    try:
        from harness.bubbles import bubble_stream_enabled

        return bubble_stream_enabled()
    except Exception:
        return False


class Session(NegotiationMixin):
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
        judge_client: LLMClient | None = None,
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
        #: Judge-lane client; judge spend attributes to the research lane.
        #: Defaults to the product client for offline/fake runs.
        self.judge_client = judge_client if judge_client is not None else client
        # synthetic_score replicates run_daily's score source and its RNG
        # draw; the judge path consumes no RNG.
        self.synthetic_score = synthetic_score

        # Persona / life / memory are store-backed; the session composes
        # them into snapshots. MemoryAgent is injectable.
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
        # Eval-mode repro: stores accepting the repro kwarg receive the
        # exact request payload; others stay untouched.
        self._accepts_repro = "repro" in _llm_params
        # Spend accounting: stores accepting usage/lane/raw_cost kwargs
        # receive them; legacy stubs stay untouched.
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

        # Conversation granularity: the open conversation reopens at its
        # turn boundary; stores without the seam start with none open.
        self._conversation: Conversation | None = None
        if hasattr(self.store, "load_open_conversation"):
            self._conversation = self.store.load_open_conversation()

        # Two-phase close: the closing draw sets closing_pending_t_h;
        # the conversation closes on the next reply or by grace deadline.
        self.two_phase_close = two_phase_close or _two_phase_close_env_set()
        self._closing_pending_t_h: float | None = None
        self._sync_closing_pending()

        # Availability negotiations: one NegotiationState per
        # AgendaItem; rebuilt from persisted snapshots on resume.
        self._negotiations: dict[str, NegotiationState] = (
            self._restore_negotiations()
        )

        # Steering + decision layer: SteeringQueue wires on the v5
        # backend seam; DecisionRunner builds when enabled.
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
        # WS-D cache order: the pop-up aux call must be a byte-identical
        # EXTENSION of the mainline request, not a request with its own
        # prefix. These hold the two halves of the turn in progress: the
        # stable system (core + persona, byte-identical every turn) and the
        # volatile state card (the trailing system message).
        self._last_stable_system: str = ""
        #: Per-lane requests already sent this run: the prefix invariant
        #: compares within a lane (the decide leg legitimately EXTENDS the
        #: mainline array rather than matching it, so a global check would
        #: report a violation on every turn).
        self._last_request_pairs: dict[str, list] = {}
        self._last_state_card: str = ""
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

    # -- resume / replay ----------------------------------------------- #

    def _resume_from(self, latest: dict) -> None:
        day = int(latest["day"])
        # Resume: a session never rewinds — initialize the clock at the
        # store's day; a clock already at/past it is never moved.
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
        # Reconstruct the day RNG at the post-rollover position by
        # consuming the same draws the rollover consumed.
        rng_t = rng_mod.day_rng(self.seed, day)
        m, g, _phase, _next = cycle.step(self.cycle_state, self.persona, rng_t)
        mood.step(self.mood_state, self.persona, m, g, self.variant, rng_t)
        self._day_rng = rng_t
        # If the latest day was already finalized, re-apply its end-of-day
        # update so resume matches a fresh run.
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

        # Restore life state from the store; seed arcs lazily when a
        # persona exists but none persisted.
        self._ensure_life()
        if (
            self._profile is not None
            and hasattr(self.store, "load_agenda")
            and self.store.load_agenda(day) is None
        ):
            self._generate_agenda(day)

        # Finalize crash window: judgement + NULL score marks the
        # crashed tail; steps complete once and are idempotent.
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

    # -- day lifecycle -------------------------------------------------- #

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
        # Compaction is a BOUNDARY operation: the epoch may move here and
        # nowhere else, so within a day the context prefix only grows.
        self._maybe_compact_context(day, self.clock.now_h())
        self.cycle_state = cycle_next
        self.current_day = day
        self.current_record = record
        self._day_rng = rng_t
        self._records[day] = record

        # Plan today's life agenda; draws come from the reserved LIFE
        # stream, never day_rng.
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
                result = self.judge(transcript, self.judge_client, model=self.judge_model)
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

        # Life step for the day just ended; this sweep completes
        # memory-tail stragglers from a crash between close and tail.
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

    # -- life + memory drivers (session composes; lanes persist) ------- #

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
        """Plan + persist today's agenda via the life lane (LIFE stream).

        Once per day, at rollover, the arc and interest slots go to the day
        planner for concrete activity text (``HARNESS_DAY_PLANNER=0`` opts
        out). The engine keeps the seeded selection, windows and ids; the
        planner only names what she is actually doing. Replay never calls it:
        a day is only generated when the store has no agenda for it, and the
        planned text is persisted with the items.
        """
        if self._profile is None:
            return
        rng = stream_rng(self.seed, LIFE_STREAM, day)
        life.generate_agenda(
            day, self._profile, self._life_arcs, self.store, rng,
            planner_client=self._planner_client(),
            weekday=self._weekday_name(day),
            logger=lambda line: self.store.log_event(
                day, self.clock.now_h(), "day_planner", line
            ),
        )

    def _planner_client(self):
        """The client the day planner may use, or None to keep templates.

        OPT-IN via ``HARNESS_DAY_PLANNER``, like every other optional layer
        here (bubbles, two-phase close, the decision lane). Default OFF keeps
        byte parity: the planner shares the conversation client, so switching
        it on adds one call per day to that client — which is correct in
        production and would silently consume a scripted response in every
        offline run and replay. The live launcher turns it on.
        """
        if not _env_bool("HARNESS_DAY_PLANNER", False):
            return None
        return self.client

    def _weekday_name(self, day: int) -> str:
        """Weekday of ``day`` from the real anchor, else a neutral word.

        Whether it is a Tuesday or a Saturday is the single most useful thing
        a planner can know about a day, and the anchor already resolves it.
        """
        anchor = self._real_time_anchor()
        if anchor is None:
            return "today"
        try:
            return anchor.real_at(day * 24.0 + 12.0).strftime("%A")
        except Exception:
            return "today"

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
        # Memory formation is conversation-boundary driven; the crash
        # tail is closed conversations missing an L2 summary.
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

    # -- conversation lifecycle ----------------------------------------- #

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

    def is_user_away(self, t_h: float) -> bool:
        """Whether the user is currently 'away' (presence signal, WS-A).

        Derived from ``_last_user_turn_t_h`` + current clock — no new RNG,
        replay-safe by construction. True when silence since the last user
        turn (or the conversation's opening when the companion opened and
        the user never replied) has reached ``USER_AWAY_THRESHOLD_H`` (15 min)
        but the conversation has NOT yet hit the long ``USER_LEFT_THRESHOLD_H``
        backstop (6 h) that actually closes it. Returns False when no
        conversation is open.
        """
        conv = self._conversation
        if conv is None:
            return False
        anchor = self._last_user_turn_t_h(conv)
        if anchor is None:
            anchor = conv.opened_t_h
        elapsed = t_h - anchor
        return USER_AWAY_THRESHOLD_H <= elapsed < USER_LEFT_THRESHOLD_H

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
            # One memory session per conversation; the session id is
            # derived from the conversation id (day-namespaced).
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
        # While a negotiation is pending it owns the conversation end;
        # the closing draw and max_turns cap resume after it resolves.
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
        # The closing_tendency draw is feature-flagged off; skipping it
        # stays replay-safe because the stream RNG is keyed.
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

    # -- conversation --------------------------------------------------- #

    #: State event marking that a day's opening block already reached the
    #: stream. Read back on restart, so a resume mid-day never re-announces.
    DAY_START_EMITTED = "day_start_block"

    def _emit_day_start_block(self, snapshot, day: int, t_h: float) -> None:
        """Put the day's plan into the STREAM once, at the day's first turn.

        Day-scoped state (agenda, arcs, user model) used to ride in the
        per-turn state card, which re-sent ~270 unchanged characters every
        turn, and could not move into the system prefix because the agenda
        mutates as windows pass. The stream is the third option and the right
        one: appended once, it sits inside the cached prefix for every later
        turn of the day — sent once, read all day.

        Idempotent through a persisted marker rather than in-memory state, so
        a restart mid-day resumes without repeating the block. Best-effort: a
        store without the audit seam simply keeps the old behaviour of never
        emitting, which is a smaller prompt, not a broken turn.
        """
        if not hasattr(self.store, "log_event"):
            return
        try:
            already = any(
                ev.get("event") == self.DAY_START_EMITTED
                and int(ev.get("day", -1)) == day
                for ev in self.store.events_since(0)
            ) if hasattr(self.store, "events_since") else False
        except Exception:  # noqa: BLE001 - the audit lane never breaks a turn
            already = False
        if already:
            return
        block = render_day_start_block(
            snapshot, t_h=t_h, anchor=self._real_time_anchor()
        )
        if not block:
            return
        conv = self._conversation
        self._persist_message(
            "system", block, t_h, day,
            proactive=False,
            session_id=self._memory_session_id(conv.id) if conv else "",
            conversation_id=conv.id if conv else None,
        )
        self.store.log_event(day, t_h, self.DAY_START_EMITTED, f"{len(block)}chars")

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

    # -- model context projection (messages + decisions, in order) ----- #

    @staticmethod
    def _decision_context_messages(row: dict) -> list[dict] | None:
        """One past decision as a NATIVE tool exchange, or None if unusable.

        Returns the conventional pair: an assistant message carrying
        ``tool_calls``, then the matching ``role="tool"`` result. Previously
        this rendered a prose summary, which recorded the decision but taught
        the model nothing -- the history contained no evidence that tool calls
        happen here at all, so every pop-up arrived as a first-ever request
        appended after real dialogue, and the likeliest continuation was to
        answer the person rather than fill the form. Live 2026-09-07: it
        answered in prose on three of seven pop-ups and picked the wrong tool
        on the rest.

        Putting the exchange back in native form does two things at once: it
        is the append-only decision lane the context contract asks for, and
        it is the precedent that makes the next pop-up unambiguous.

        A malformed audit row returns None -- context assembly must never
        break on the audit lane.
        """
        try:
            verdict = json.loads(row.get("verdict_json") or "null")
        except (TypeError, ValueError):
            return None
        if not isinstance(verdict, dict):
            return None
        kind = str(row.get("popup_kind") or "")
        if not kind:
            return None
        call_id = Session._decision_call_id(row)
        arguments = json.dumps(verdict, ensure_ascii=False, sort_keys=True)
        assistant = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": kind, "arguments": arguments},
                }
            ],
        }
        result = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": Session._decision_tool_result(row, verdict),
        }
        return [assistant, result]

    @staticmethod
    def _decision_call_id(row: dict) -> str:
        """The tool_call_id for a recorded decision.

        Reuses the provider's own id from the raw reply when it is there, so
        the replayed exchange is the one that actually happened; falls back to
        a stable id derived from the row so textual-transport and
        server-drawn decisions still form a valid pair.
        """
        raw = row.get("raw_reply")
        if isinstance(raw, str) and raw.lstrip().startswith("["):
            try:
                calls = json.loads(raw)
            except ValueError:
                calls = None
            if isinstance(calls, list) and calls:
                first = calls[0]
                if isinstance(first, dict) and first.get("id"):
                    return str(first["id"])
        return f"call_decision_{row.get('id', 0)}"

    @staticmethod
    def _decision_tool_result(row: dict, verdict: dict) -> str:
        """What the SERVER did with the verdict — the tool's return value.

        Short and factual. The model's own ``reason`` is already in the call
        arguments; repeating it here would just spend tokens restating what
        it said. What it does not otherwise know is whether the verdict was
        applied, which is the whole point of a tool result.
        """
        label = str(
            row.get("event_label") or row.get("event_id") or "the event"
        )
        when = hhmm(float(row.get("t_h") or 0.0))
        if "reply" in verdict:
            outcome = "replied" if verdict.get("reply") else "stayed quiet"
            if verdict.get("terminate_event"):
                outcome += f"; left {label}"
        elif "initiate" in verdict:
            action = verdict.get("action")
            if action == "follow":
                outcome = f"went to {label}"
            elif action == "abandon":
                outcome = f"skipped {label}"
            elif action == "defer":
                outcome = f"stayed a while longer instead of {label}"
            elif verdict.get("initiate"):
                outcome = f"started {label}"
            else:
                outcome = f"let {label} pass"
        elif "message" in verdict:
            outcome = f"mentioned {label}"
        else:
            outcome = "recorded"
        return f"recorded at {when}: {outcome}."

    def context_epoch_id(self) -> int:
        """The compaction watermark: context starts at the first message
        AFTER this id. 0 means "the whole history".

        The watermark moves ONLY at an explicit compaction boundary (day
        rollover, :meth:`_maybe_compact_context`), never per turn. Between
        boundaries the model's message list can only grow, so request N+1 is
        request N plus what was appended -- the condition a prefix cache
        needs to hit past the system message.
        """
        if not hasattr(self.store, "get_kv"):
            return 0
        raw = self.store.get_kv(CONTEXT_EPOCH_KEY)
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    def _maybe_compact_context(self, day: int, t_h: float) -> None:
        """Move the context epoch forward, at a boundary, if history is long.

        Called from the day rollover only. Compaction is a boundary
        operation on purpose: dropping messages shifts every byte after the
        drop, so doing it per turn pays a full re-prefill every turn and buys
        nothing. Doing it once a day pays it once. (Kafka's log compaction
        makes the same trade, and never compacts the active segment -- here
        the open conversation is the active segment.)

        The retained span keeps the NEWEST ``CONTEXT_RETAIN_MESSAGES``, so the
        conversation continuity the user can see is preserved; what is dropped
        is older material that memory retrieval already covers. The move is
        recorded as a state event so replay and audit can explain a prefix
        break.
        """
        if not hasattr(self.store, "max_message_id") or not hasattr(self.store, "set_kv"):
            return
        epoch = self.context_epoch_id()
        newest = self.store.max_message_id()
        retained = newest - epoch
        if retained <= CONTEXT_COMPACT_AFTER_MESSAGES:
            return
        new_epoch = max(epoch, newest - CONTEXT_RETAIN_MESSAGES)
        if new_epoch <= epoch:
            return
        self.store.set_kv(CONTEXT_EPOCH_KEY, str(new_epoch))
        self.store.log_event(
            day, t_h, "context_compacted",
            f"epoch={epoch}->{new_epoch} retained={newest - new_epoch}",
        )

    def _context_turns(self, limit: int | None = None) -> list[dict]:
        """The model's context stream: transcript turns AND its own decisions.

        APPEND-ONLY by construction: the transcript is read from the stored
        compaction epoch (:meth:`context_epoch_id`), not as "the last N rows".
        The distinction is the whole cache story -- front-truncation moves the
        first byte of the payload every turn, so nothing after the system
        message can be reused; an epoch-anchored read only ever appends, so
        request N+1 is request N plus the new turns.

        Merged by ``(t_h, lane, id)`` so the stream reproduces the real order
        within a turn: the user's message, then the decisions taken at that
        boundary, then the reply. Decisions render as ``role="system"`` --
        internal material carries system authority through the CHANNEL, which
        is why the stable prefix no longer needs prose explaining it.

        ``limit`` is a safety cap for callers that want one (the legacy
        transcript peek); the mainline passes None and lets the epoch bound
        the span.
        """
        epoch = self.context_epoch_id()
        if hasattr(self.store, "messages_since"):
            messages = self.store.messages_since(epoch, limit=limit)
        else:  # minimal stores (tests/fakes) keep working
            messages = self.store.recent_messages(limit=limit or RECENT_TURNS)
        floor = float(messages[0].get("t_h", 0.0)) if messages else 0.0
        decisions: list[dict] = []
        try:
            if hasattr(self.store, "decisions_since"):
                decisions = self.store.decisions_since(floor)
            elif hasattr(self.store, "recent_decisions"):
                decisions = self.store.recent_decisions(limit=RECENT_TURNS)
        except Exception:  # the audit lane must never break a turn
            decisions = []
        if not decisions:
            return list(messages)
        rows: list[tuple[float, int, int, dict]] = []
        for m in messages:
            role = m.get("role")
            # Lanes order what happens AT one instant: stored context first,
            # then the user's message, then the decisions taken at that
            # boundary, then the reply.
            #
            # A stored system message (the day-start block) needs lane -1, not
            # the assistant's. It is persisted at the same t_h as the user
            # message that triggered it, and this sort only runs when
            # decisions exist — so sharing the assistant lane put it AFTER the
            # user message on turns with a decision and BEFORE it on turns
            # without, reordering the supposedly append-only stream between
            # consecutive calls and costing the whole cached prefix.
            lane = -1 if role == "system" else (0 if role == "user" else 2)
            rows.append((float(m.get("t_h", 0.0)), lane, int(m.get("id", 0)), m))
        for d in decisions:
            t = float(d.get("t_h") or 0.0)
            if t < floor:
                continue
            pair = self._decision_context_messages(d)
            if pair is None:
                continue
            # The pair must stay adjacent and in order: a provider rejects an
            # assistant tool_calls message that is not followed by its result.
            for offset, message in enumerate(pair):
                rows.append((t, 1, int(d.get("id", 0)) * 2 + offset, message))
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        return [r[3] for r in rows]

    def _drain_steers(self, day: int, t_h: float, turn_id: str) -> _SteerDrain:
        """Apply every steer pending at this turn's idle boundary.

        Three outcomes per steer: SUPPRESS kills the ordinary reply (a
        no-reply verdict), INJECT adds a marked block to the prompt, and
        anything else is handled entirely by ``_apply_steer``'s side effects.
        Only INJECT steers stay in ``_turn_drained`` — the requeue set — so a
        turn that dies mid-generation puts back exactly the steers whose
        delivery the model never actually saw.

        With no steering backend the result is empty and the turn proceeds
        unchanged.
        """
        drain = _SteerDrain()
        self._turn_drained = []
        if self._steering is None:
            return drain
        if self._decision_enabled:
            self._enqueue_event_popups(day, t_h)
        # Negotiations already deciding fire their decide trigger
        # this companion turn.
        active_before = {
            iid for iid, st in self._negotiations.items()
            if st.phase == NegotiationPhase.DECIDE.value
            and not st.resolved
        }
        drained = self._steering.drain_pending(BOUNDARY_IDLE, turn_id, t_h)
        self._turn_drained = [s.steer_id for s in drained]
        # A verdict resolving during this drain needs the turn to speak for
        # it (see NegotiationMixin._active_drain / GO_NOTE).
        self._active_drain = drain
        try:
            for steer in drained:
                outcome = self._apply_steer(
                    steer, day=day, t_h=t_h,
                    notices=drain.notices, proactive_out=drain.proactive_out,
                    drain=drain,
                )
                if outcome == _STEER_SUPPRESS:
                    drain.suppress_reply = True
                if outcome == _STEER_INJECT:
                    drain.injections.append(
                        wrap_steer_marker(render_steer_block(steer))
                    )
                else:
                    self._turn_drained.remove(steer.steer_id)
            # NOTE: proactive_declined does NOT promote to suppress_reply
            # here: _chat decides that with user_text in scope (a stray
            # decline never kills a reactive turn).
            # The decide loop fires for every already-deciding
            # negotiation; a go verdict suppresses the ordinary reply.
            if self._run_turn_decides(
                day, t_h, drain.proactive_out, active_before=active_before
            ):
                drain.suppress_reply = True
        except BaseException:
            for steer_id in self._turn_drained:
                self._steering.requeue(steer_id)
            self._turn_drained = []
            raise
        finally:
            self._active_drain = None
        return drain

    def _generate(self, messages: list, system: str,
                  max_tokens: int | None):
        """Run the turn's LLM call; return (reply, reasoning, usage, raw_cost).

        ``chat_with_meta`` is the richer surface (reasoning, parsed usage,
        gateway cost); a client without it is still supported and simply
        yields None for the three extras. An empty or whitespace-only reply
        raises rather than persisting: the client has already retried empties
        with bounded backoff, so one that reaches here is a real failure and
        a blank assistant message would corrupt the transcript.

        A reply carrying TOOL-CALL MARKUP is treated the same way. Any real
        prose alongside it is kept and the machinery stripped; a reply that is
        nothing but machinery raises, because the alternative is what happened
        live on 2026-09-08 — ``<｜｜DSML｜｜tool_calls>...`` persisted as her
        message and delivered to the user.
        """
        chat_with_meta = getattr(self.client, "chat_with_meta", None)
        if chat_with_meta is None:
            reply = self.client.chat(
                messages, system=system, max_tokens=max_tokens
            )
            reasoning = usage = raw_cost = None
        else:
            result = chat_with_meta(
                messages, system=system, max_tokens=max_tokens,
                reasoning_effort=self._thinking_effort,
            )
            reply = result.content
            reasoning = result.reasoning
            # Parsed usage and cost ride on the ChatResult and persist
            # with the lane attribution when the store accepts them.
            usage = getattr(result, "usage", None)
            raw_cost = getattr(result, "raw_cost", None)
        if not reply.strip():
            raise RuntimeError(
                "refusing to persist empty assistant reply (client returned "
                "empty/whitespace-only content)"
            )
        reply = self._reject_tool_markup(reply)
        return reply, reasoning, usage, raw_cost

    def _reject_tool_markup(self, reply: str) -> str:
        """Keep her prose, drop the machinery, refuse a reply that is only
        machinery. Recorded loudly either way — a leak the user never saw is
        still a leak worth counting."""
        if not looks_like_tool_markup(reply):
            return reply
        cleaned = strip_tool_markup(reply)
        if hasattr(self.store, "log_event"):
            self.store.log_event(
                self.current_day, self.clock.now_h(), EVENT_TOOL_MARKUP_LEAK,
                f"salvaged={len(cleaned)}chars raw={reply[:120]!r}",
            )
        if not cleaned:
            raise RuntimeError(
                "refusing to persist tool-call markup as an assistant reply "
                f"(model returned machinery, not prose): {reply[:200]!r}"
            )
        return cleaned

    def _generate_stream(self, messages: list, system: str,
                         max_tokens: int | None):
        """Streamed generation: (reply, reasoning, usage, raw_cost, bubbles).

        Used ONLY when HARNESS_BUBBLE_STREAM is on AND the client exposes
        ``chat_stream`` — the bubble backend-streaming path (2026-09-07):
        the client yields the reply text in wire-order chunks and each
        completed bubble is released by a :class:`BubbleStreamer` as soon
        as its boundary parses (sentence-complete pieces only; mid-sentence
        wraps are held). The joined reply is the canonical record —
        ``bubbles`` re-join to EXACTLY it, byte for byte.

        Streamed replies carry NO usage accounting: ``chat_stream`` yields
        text only, so the streamed path degrades to ``usage=None,
        raw_cost=None, reasoning=None`` — the same graceful degradation as
        gateways without a usage object — and the ledger row stores NULLs
        (spend reporting treats those rows as unpriced). ``meta`` therefore
        has no reasoning, exactly like a non-reasoning gateway. The joined
        reply is still the one persisted row; the single-persist invariant
        is unchanged.

        Returns ``(reply, reasoning, usage, raw_cost, streamed_bubbles)``
        where ``streamed_bubbles`` is a tuple when at least two bubbles
        parsed, else None (parity with ``_split_into_bubbles`` semantics:
        a single part is sent as one message either way).
        """
        from harness.bubbles import BubbleStreamer, bubble_stream_enabled

        if not bubble_stream_enabled():
            # Flags changed between _chat's gate and here (or a direct
            # caller): fall back to the canonical non-streaming path.
            reply, reasoning, usage, raw_cost = self._generate(
                messages, system, max_tokens
            )
            return reply, reasoning, usage, raw_cost, None
        chat_stream = getattr(self.client, "chat_stream", None)
        if chat_stream is None:
            # No streaming surface: same fallback, byte-identical reply.
            reply, reasoning, usage, raw_cost = self._generate(
                messages, system, max_tokens
            )
            return reply, reasoning, usage, raw_cost, None
        streamer = BubbleStreamer()
        parts: list[str] = []
        raw_chunks: list[str] = []
        # The generator is lazy: the request fires on the first next() and
        # must be drained to completion before the client makes another
        # call, so the turn always consumes the full stream.
        for piece in chat_stream(
            messages, system=system, max_tokens=max_tokens,
            reasoning_effort=self._thinking_effort,
        ):
            raw_chunks.append(piece)
            parts.extend(streamer.feed(piece))
        parts.extend(streamer.flush())
        # The canonical reply is the raw stream joined — byte-identical to
        # what a non-streaming call returns for the same wire content.
        # ``parts`` are the trimmed bubble texts (separators consumed), so
        # they never re-join to the reply with "".join; the single persisted
        # row keeps the joined text and parse_bubbles(reply) == parts.
        reply = "".join(raw_chunks)
        if not reply.strip():
            raise RuntimeError(
                "refusing to persist empty assistant reply (stream carried "
                "no content)"
            )
        if len(parts) >= 2:
            return reply, None, None, None, tuple(parts)
        return reply, None, None, None, None

    def _repro_kwargs(self, system: str, messages: list,
                      max_tokens: int | None, controls, intent,
                      day: int, t_h: float) -> dict:
        """The exact prompt/payload/params needed to reconstruct this call.

        Empty unless the store accepts ``repro`` — and the store drops it
        again unless audit_mode=True, so production logs only the hash.
        """
        if not self._accepts_repro:
            return {}
        return {"repro": {
            "model": getattr(self.client, "model", None),
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.8,
            "json_mode": False,
            # Wire identity of the request beyond the prompt text: without
            # these, a stored call and a replay of it were only assumed equal.
            "reasoning_effort": self._thinking_effort,
            "tools_hash": None,
            "tool_names": [],
            "tool_choice": None,
            "controls": {
                "response_delay_s": controls.response_delay_s,
                "closing_tendency": controls.closing_tendency,
                "initiative_factor": controls.initiative_factor,
                "closing_guidance": controls.closing_guidance,
            },
            "intent_id": intent.id if intent is not None else None,
            "timestamp": {"day": day, "t_h": t_h},
        }}

    def _usage_kwargs(self, usage, raw_cost) -> dict:
        """Token usage, lane attribution and gateway cost for the ledger.

        Empty on stores without the columns; un-laned clients record
        lane=NULL rather than guessing a lane.
        """
        if not self._accepts_usage:
            return {}
        return {
            "usage": usage,
            "lane": getattr(self.client, "lane", None),
            "raw_cost": raw_cost,
        }

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
        next LLM call's messages as ``role="system"`` blocks wrapped in the
        steer marker. The day-start block is rendered once per day and cached.

        Context read (2026-09-07): the transcript comes from
        ``_context_turns`` — the merged stream of messages AND past decisions
        — not from ``recent_messages`` alone, so the model reads verdicts it
        gave on earlier turns. The stream is anchored to the compaction epoch,
        so it only ever grows between boundaries.
        """
        t_h = self.clock.now_h()
        day = self.clock.day()
        self.ensure_day(day)
        assert self.current_record is not None
        # Close a stale open conversation before this turn (quiet
        # boundary crossed or user silence); this message opens a new one.
        self.check_conversation_lifecycle(t_h)

        previous = self._records.get(day - 1)
        directive = derive_behavior(
            self.current_record, self.timing, hour=self.clock.local_hour(), previous=previous
        )
        controls = controls_from_directive(directive)
        if self.two_phase_close and self._closing_pending_t_h is not None:
            # Wind-down pending: render the guidance through the existing
            # closing_guidance channel; the turn then closes.
            controls = replace(controls, closing_guidance=WIND_DOWN_GUIDANCE)
        brief = to_brief(directive)

        # One conversation per exchange run; the memory session id is
        # the conversation id.
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
        # The mood line consumes prompt_brief verbatim; the day-start
        # block renders once per day and is cached.
        if self._day_block is None or self._day_block_day != day:
            self._day_block = render_day_block(snapshot)
            self._day_block_day = day
        # Day-scoped state enters the STREAM once, before the context is read
        # below, so it rides inside the cached prefix for the rest of the day.
        self._emit_day_start_block(snapshot, day, t_h)
        system = assemble_snapshot(
            snapshot, controls=controls, prompt_brief=directive.prompt_brief,
            day_block=self._day_block,
            # The temporal section renders only when the run is anchored.
            t_h=t_h, anchor=self._real_time_anchor(),
        )
        system = _with_bubble_instruction(system)
        if user_text is None and intent is None:
            # Legacy ungrounded proactive call (pre-slice callers/tests):
            # generic opening without any invented source claim.
            system += "\n\n" + proactive_block()

        # WS4: pop-up calls (decision layer) share the turn's system prompt.
        self._last_system_prompt = system

        recent = self._context_turns()
        # Mainline: stable system + context stream + the volatile state card
        # as a TRAILING SYSTEM message (never user-role: user-role content is
        # always what the user said). The legacy full 3-tier `system` above
        # stays for `_last_system_prompt`, which aux callers that never ran a
        # mainline turn still fall back to.
        if user_text is not None:
            stable, messages = build_context_messages(
                snapshot, recent, user_text,
                controls=controls, prompt_brief=directive.prompt_brief,
                t_h=t_h, anchor=self._real_time_anchor(),
                day_block=self._day_block,
                # The epoch already bounds the span; a tail limit here would
                # re-impose the sliding window the epoch exists to remove.
                limit=None,
            )
            mid = self._persist_message(
                "user", user_text, t_h, day,
                proactive=False, session_id=session_id, conversation_id=conv_id,
            )
            conv = self._record_turn(
                conv, "user", user_text, t_h, message_id=mid
            )
        else:
            # Pass "" instead of None content; None serializes to
            # content:null and 400s the request.
            stable, messages = build_context_messages(
                snapshot,
                [wire_message(m) for m in recent],
                None,
                controls=controls, prompt_brief=directive.prompt_brief,
                t_h=t_h, anchor=self._real_time_anchor(),
                day_block=self._day_block,
                limit=None,
            )
        stable = _with_bubble_instruction(stable)
        if user_text is None and intent is None:
            # Legacy ungrounded proactive call (pre-slice callers/tests):
            # generic opening without any invented source claim.
            stable += "\n\n" + proactive_block()
        # Capture the two halves for the pop-up aux calls: same stable
        # prefix, same card, pop-up appended after it.
        self._last_stable_system = stable
        self._last_state_card = (
            messages[-1]["content"]
            if messages and messages[-1].get("role") == "system"
            else ""
        )

        # Drain pending steers into this turn; no-reply verdicts
        # suppress the reply, and delivered steers re-queue on error.
        drain = self._drain_steers(day, t_h, turn_id)
        notices = drain.notices
        proactive_out = drain.proactive_out
        injections = drain.injections
        suppress_reply = drain.suppress_reply

        # Agenda status transitions as windows pass, persisted via the
        # store; runs after the steering drain.
        self._transition_agenda_windows(day, t_h)

        if drain.proactive_declined and user_text is None:
            # A declined proactive fire: the decide verdict said no.
            # Nothing is generated or persisted — only the decision
            # record (written by the runner) and the suppressed intent
            # status remain. Reactive turns ignore the decline: a stray
            # proactive steer never kills a user's own message.
            self._turn_drained = []
            self.store.log_event(
                day, t_h, "proactive_declined_turn",
                f"turn={turn_id}",
            )
            return TurnResult(
                reply="", directive=directive, day=day,
                hour=self.clock.local_hour(), controls=controls,
                notices=tuple(notices), proactive_out=tuple(proactive_out),
            )

        if suppress_reply:
            # A no-reply verdict means no ordinary reply; the server
            # notice goes out instead.
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

        # Internal events are system-level context, never user messages
        # (design: events=system, decisions=tools). Both blocks FOLD into the
        # trailing state card rather than stacking behind it — see
        # assembler.append_system for why adjacency is the bug.
        if injections:
            messages = append_system(messages, "\n".join(injections))

        if drain.decided_notes:
            # Something she decided during this drain needs saying. The note
            # states WHAT she decided; the generation supplies her words.
            messages = append_system(messages, "\n".join(drain.decided_notes))

        # reasoning_effort passes through HARNESS_THINKING_EFFORT when
        # set; the max_tokens cap is dropped then.
        max_tokens = (
            None if self._thinking_effort is not None else controls.max_tokens
        )
        # Backend bubble streaming: when HARNESS_BUBBLE_STREAM is on AND
        # the client exposes chat_stream, the reply is consumed off the
        # wire and its bubbles parsed incrementally (_generate_stream).
        # Otherwise the EXACT canonical path runs (_generate +
        # _split_into_bubbles post-hoc) — byte parity, no prompt changes.
        # The TurnResult.streamed flag marks the streamed origin; the
        # runtime's paced multi-send is identical either way.
        stream_chat = getattr(self.client, "chat_stream", None)
        if stream_chat is not None and _bubble_stream_on():
            reply, reasoning, usage, raw_cost, streamed_bubbles = (
                self._generate_stream(messages, stable, max_tokens)
            )
        else:
            reply, reasoning, usage, raw_cost = self._generate(
                messages, stable, max_tokens
            )
            streamed_bubbles = None
        streamed = streamed_bubbles is not None
        self._note_request_prefix("chat", messages)
        mid = self._persist_message(
            "assistant", reply, t_h, day,
            proactive=proactive, session_id=session_id, conversation_id=conv_id,
            intent_id=intent.id if intent is not None else None,
        )
        conv = self._record_turn(
            conv, "companion", reply, t_h, message_id=mid
        )
        if drain.close_after is not None:
            # A verdict ended the conversation, but only AFTER she spoke:
            # closing first suppressed the reply and left a goodbye
            # unanswered (live 2026-09-07).
            self._close_conversation(conv, t_h, drain.close_after)
        else:
            # Companion-turn close checks: the closing_tendency draw and
            # the max_turns cap; a close persists close_reason.
            self._maybe_close_conversation(conv, t_h, controls.closing_tendency)
        repro_kwargs = self._repro_kwargs(
            stable, messages, max_tokens, controls, intent, day, t_h
        )
        # WS4: reasoning persists in the call's meta (audit.py renders it
        # under #Thinking; non-reasoning runs store nothing).
        meta = {"reasoning": reasoning} if reasoning else None
        usage_kwargs = self._usage_kwargs(usage, raw_cost)
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
        # Post-hoc split on the canonical path; the streamed path already
        # parsed incrementally (its bubbles came back from _generate_stream).
        if streamed_bubbles is None:
            bubbles = _split_into_bubbles(reply)
        else:
            bubbles = streamed_bubbles
        return TurnResult(
            reply=reply,
            directive=directive,
            day=day,
            hour=self.clock.local_hour(),
            controls=controls,
            notices=tuple(notices),
            proactive_out=tuple(proactive_out),
            bubbles=bubbles,
            streamed=streamed,
        )

    # -- steering + decision layer (idle boundary, pop-up execution) --- #

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
        pop-up when an event is in progress (user L356).

        With NOTHING in progress there is nothing to gate: the message is
        already in the transcript and ``_steer_user_message`` consumes the
        steer without a model call. So no steer is queued at all. Queuing
        one wrote a row whose event name was the literal ``"?"`` — a
        placeholder that reached the rendered steer block on every ordinary
        turn outside an activity (7 of them on the first live day).

        Returns the steer id, None when the layer is off or she is free.
        """
        if self._steering is None or not self._decision_enabled:
            return None
        day = int(t_h // 24.0)
        activity = self._current_activity(day, t_h)
        if activity is None or activity.item is None:
            return None
        return self._steering.enqueue(
            KIND_USER_MESSAGE,
            {"message": text, "event": activity.item.activity,
             "state": "in_progress", "time": t_h},
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

        REPLAY (2026-09-08): a boundary is enqueued AT ITS OWN INSTANT, not
        at ``now``, and the whole batch is sorted by that instant. Her day
        runs even when nobody watched it, so a mid-day resume replays the
        morning in the order the morning happened: each pop-up reaches the
        model carrying the time the event actually arrived (``Time:`` is
        already the boundary), and the pop-ups queue behind one another in
        that same order instead of landing as a pile of stale questions
        stamped with the current hour. The steer's own ``t_h`` is what
        ``_omit_backlog_send`` reads, so a reach-out about a window that
        closed hours ago is correctly dropped rather than sent late.
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
        crossed: list[tuple[float, dict]] = []
        for it in items:
            if it.status != "planned":
                continue
            boundaries = [("start", it.start_t_h), ("end", it.end_t_h)]
            if it.start_t_h > now:
                # The "incoming event" heads-up, HEADS_UP_LEAD_H ahead of
                # the window: she can get ready and say so. No verdict.
                # Only while the window is still genuinely ahead — a lead
                # instant for an event that already started is nothing to
                # warn about, so it is never queued (a replayed morning
                # gets its decisions, not stale warnings).
                boundaries.insert(0, (
                    "heads_up", it.start_t_h - HEADS_UP_LEAD_H,
                ))
            for state, at in boundaries:
                if (prev is None or at > prev) and at <= now:
                    crossed.append((at, {
                        "event_id": it.id, "event": it.activity,
                        "state": state, "time": at, "item_id": it.id,
                    }))
        # Chronological across items, not per-item: two overlapping windows
        # must not interleave as A-start, A-end, B-start.
        crossed.sort(key=lambda pair: pair[0])
        for at, payload in crossed:
            self._steering.enqueue(KIND_EVENT_POPUP, payload, day, at)
        self.store.log_event(day, now, "popup_boundary_check", f"items={len(items)}")

    def _steer_user_message(self, steer: Steer, payload: dict, *,
                            day: int, t_h: float,
                            notices: list[str]) -> str:
        """A user message that arrived while an event was in progress.

        With no event running the message is already in the transcript, so
        the steer is consumed without a model call. Otherwise the model
        decides whether to reply at all: ``reply: false`` suppresses the
        ordinary reply and sends the notice instead (the single reply-path
        invariant), and ``terminate_event`` ends the event she was in.
        """
        activity = self._current_activity(day, t_h)
        if activity is None or activity.item is None:
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
            self._mark_event_closed(
                activity.item.id, str(result.verdict.get("reason") or "")
            )
        return _STEER_CONSUMED

    def _omit_backlog_send(self, steer: Steer) -> bool:
        """True when an initiate send is backlog catch-up to omit.

        A steer enqueued BEFORE the current conversation opened is
        fast-forward material: the verdict is decided and persisted, but
        its reason never reaches the channel — no conversation was live
        when it arose, so the text would reply to nobody. Steers born
        inside the live conversation still send.
        """
        conv = self._conversation
        if conv is None:
            return True
        return steer.t_h < conv.opened_t_h

    def _steer_event_popup(self, steer: Steer, payload: dict, *,
                           day: int, t_h: float,
                           proactive_out: list[tuple[str, str]],
                           drain: "_SteerDrain | None" = None) -> str:
        """An agenda event starting or ending.

        Three boundaries, one decision. HEADS_UP (``HEADS_UP_LEAD_H`` ahead
        of the window) is the "incoming event" steer: she learns something
        is about to start, with no verdict attached, and only while a
        conversation is open. The decision is offered ONCE, at the START
        boundary (a negotiation re-offers it only when the model itself
        deferred, which is his window to talk her out of going). The END
        boundary
        is NOT a decision and never calls the model: a window that fully
        passes resolves server-side and deterministically in
        ``life.transition_past_windows`` (planned -> completed), while an
        explicit skip already closed the item at its start. Asking the model
        anything at the end produced a second verdict per event whose
        ``reason`` was rationale for a choice that had already been made.
        """
        state = str(payload.get("state", "start"))
        item_id = str(payload.get("item_id") or payload.get("event_id") or "")
        if state == "heads_up":
            if item_id:
                self._maybe_heads_up(item_id, day, t_h, steer, proactive_out)
            return _STEER_CONSUMED
        if state == "end":
            return _STEER_CONSUMED
        if item_id and self._maybe_start_negotiation(
            item_id, day, t_h, steer, proactive_out
        ):
            return _STEER_CONSUMED
        # The decision is stamped with the boundary, not the drain instant:
        # the pop-up shows the model `Time: 07:28` and the tool result says
        # "recorded at 07:28", so a replayed morning does not contradict
        # itself. The drain instant is kept as the steer's delivered_t_h.
        at = float(payload.get("time", t_h))
        result = self._execute_decision(
            decision_id=f"steer-{steer.steer_id}",
            popup_kind="tool_decide_event",
            inputs={
                "event_id": item_id,
                "event_label": str(payload.get("event") or "?"),
                "state_label": state,
                "time": str(at),
            },
            steer=steer,
            day=day,
            t_h=at,
        )
        if result is None:
            return _STEER_CONSUMED  # re-queued: next boundary
        verdict = result.verdict
        label = str(payload.get("event") or "?")
        if verdict.get("action") == "abandon" and item_id:
            # An explicit no resolves the item HERE, at the one decision
            # point; nothing asks again at the end boundary.
            self._mark_event_closed(item_id, "")
        if verdict.get("initiate"):
            if self._omit_backlog_send(steer):
                if hasattr(self.store, "log_event"):
                    self.store.log_event(
                        day, t_h, "decision_catchup_omit",
                        f"steer={steer.steer_id} event={label} "
                        f"enqueued={steer.t_h:.2f}",
                    )
            elif drain is not None:
                # The TURN speaks for it. This used to paste the verdict's
                # `reason` into the channel, which put machine rationale in
                # the conversation; the reason stays in decision_records.
                drain.decided_notes.append(START_NOTE.format(activity=label))
            elif hasattr(self.store, "log_event"):
                # No turn to speak for it, and nothing here may write the
                # channel: the verdict's `reason` is engine rationale, not
                # dialogue, and START_NOTE is an instruction to her, not a
                # message to him. The activity still starts; the reach-out
                # is dropped and recorded as dropped.
                self.store.log_event(
                    day, t_h, "decision_start_no_turn",
                    f"steer={steer.steer_id} event={label}",
                )
        return _STEER_CONSUMED

    def _apply_steer(
        self,
        steer: Steer,
        *,
        day: int,
        t_h: float,
        notices: list[str],
        proactive_out: list[tuple[str, str]],
        drain: _SteerDrain | None = None,
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

        ``drain`` is the turn's drain object: proactive initiate verdicts
        stash their resolved intent on ``drain.decided_intents`` and
        declines set ``drain.proactive_declined`` through it.
        """
        if self._decision is None:
            return _STEER_INJECT
        payload = steer.payload or {}
        kind = steer.kind
        if kind == KIND_USER_MESSAGE:
            return self._steer_user_message(
                steer, payload, day=day, t_h=t_h, notices=notices
            )
        if kind == KIND_EVENT_POPUP:
            return self._steer_event_popup(
                steer, payload, day=day, t_h=t_h,
                proactive_out=proactive_out, drain=drain,
            )
        if kind == KIND_PROACTIVE:
            return self._steer_proactive(
                steer, payload, day=day, t_h=t_h, drain=drain,
            )
        # schedule_fire / day_rollover (or unknown kinds): the harness's own
        # paths own those flows; the block is still rendered as context.
        return _STEER_INJECT

    def _latest_user_text(self) -> str:
        """Most recent user message text, or '' when none exists."""
        recent = self.store.recent_messages(limit=12)
        for m in reversed(recent):
            if m.get("role") == "user":
                return str(m.get("content") or "")
        return ""

    def _silence_hours(self, t_h: float) -> float | None:
        """Hours since the last user turn, or None when unknown."""
        conv = self._conversation
        anchor = self._last_user_turn_t_h(conv) if conv is not None else None
        if anchor is None:
            recent = self.store.recent_messages(limit=12)
            for m in reversed(recent):
                if m.get("role") == "user":
                    anchor = float(m.get("t_h", t_h))
                    break
        if anchor is None:
            return None
        return max(0.0, t_h - anchor)

    def _steer_proactive(
        self,
        steer: Steer,
        payload: dict,
        *,
        day: int,
        t_h: float,
        drain: _SteerDrain | None = None,
    ) -> str:
        """A grounded proactive intent awaiting its initiate/decline verdict.

        The model decides via ``tool_decide_proactive`` whether the fire
        goes out now. ``initiate=true`` stashes the resolved intent on the
        drain (the turn's own generation IS the proactive message — nothing
        is sent here). ``initiate=false`` declines quietly: the intent is
        marked suppressed and the turn's reply is suppressed. Either way
        the decision record persists (dual persistence + replay).
        """
        intent_id = payload.get("intent_id")
        intent = self._lookup_intent(intent_id) if intent_id else None
        if intent is None:
            if hasattr(self.store, "log_event"):
                self.store.log_event(
                    day, t_h, "proactive_no_intent",
                    f"steer={steer.steer_id} intent={intent_id!r}",
                )
            return _STEER_CONSUMED
        inputs = {
            "hook": intent.hook,
            "reason": intent.reason,
            "source_type": intent.source_type,
            "source_id": intent.source_id,
            "valid_until": intent.valid_until_t_h,
            "latest_user_message": self._latest_user_text(),
            "silence_h": self._silence_hours(t_h),
            "time": str(t_h),
        }
        result = self._execute_decision(
            decision_id=f"proactive-{steer.steer_id}",
            popup_kind="tool_decide_proactive",
            inputs=inputs,
            steer=steer,
            day=day,
            t_h=t_h,
        )
        if result is None:
            return _STEER_CONSUMED  # re-queued: next boundary
        if result.verdict.get("initiate"):
            if drain is not None:
                drain.decided_intents.append(intent)
            return _STEER_CONSUMED
        if hasattr(self.store, "update_proactive_intent_status"):
            self.store.update_proactive_intent_status(intent.id, "suppressed")
        if hasattr(self.store, "log_event"):
            self.store.log_event(
                day, t_h, "proactive_declined",
                f"steer={steer.steer_id} intent={intent.id}",
            )
        if drain is not None:
            drain.proactive_declined = True
        return _STEER_CONSUMED

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

    def _note_request_prefix(self, lane: str, messages: list) -> None:
        """Witness the append-only property while the bot runs.

        Off by default (``HARNESS_PREFIX_INVARIANT=1``); a violation is logged
        as a state event instead of raising, because a diagnostic sensor must
        never kill a live run. It is the runtime twin of the test that pins the
        pop-up extending the mainline request, and the reason to keep it is
        measured: a rewritten head costs the whole prefix.

        The role convention is checked on every call, flag or no flag: the scan
        is a substring pass over a handful of messages, and a leak is a bug
        worth knowing about immediately (see ``harness_text_in_user_roles``).
        """
        leaked = harness_text_in_user_roles(messages)
        if leaked:
            self.store.log_event(
                int(self.clock.now_h() // 24.0), self.clock.now_h(),
                "user_role_leak",
                json.dumps({"lane": lane, "indices": leaked}, sort_keys=True),
            )
        previous = self._last_request_pairs.get(lane)
        self._last_request_pairs[lane] = list(messages)
        if previous is None or not _env_bool("HARNESS_PREFIX_INVARIANT", False):
            return
        broke = prefix_break(previous, messages)
        if broke is not None:
            self.store.log_event(
                int(self.clock.now_h() // 24.0), self.clock.now_h(),
                "prefix_invariant_violation",
                json.dumps({"lane": lane, "index": broke,
                            "was": len(previous), "now": len(messages)},
                           sort_keys=True),
            )

    def _popup_repro(self, request: PopupRequest, messages: list,
                     day: int, t_h: float) -> dict:
        """The decide leg's stored request identity, wire-exact.

        The ledger used to record the request WITHOUT ``tools``, so a stored
        call could never be proven identical to what went out — which is how a
        cache reading survived three wrong explanations. Tools ride as a hash
        plus their names (the schemas rebuild from ``harness.tools``); the
        decode controls ride by value.
        """
        wire_tools = ([{"type": "function", "function": t}
                       for t in offered_tools(request)] if request.native else None)
        tools_hash, tool_names = tools_identity(wire_tools)
        return {"repro": {
            "model": getattr(self.client, "model", None),
            "system": self._last_stable_system or self._last_system_prompt,
            "messages": messages,
            "max_tokens": None,
            "temperature": 0.8,
            "json_mode": False,
            "reasoning_effort": self._thinking_effort,
            "tools_hash": tools_hash,
            "tool_names": tool_names,
            "tool_choice": None,   # never sent: see _popup_request_call
            "popup_kind": request.popup_kind,
            "timestamp": {"day": day, "t_h": t_h},
        }}

    def _popup_request_call(self, request: PopupRequest) -> RawReply:
        """One pop-up model call (the callable injected into the runner).

        Transport (2026-09-08): the requested function is the ONLY one
        offered. ``tool_choice`` is not sent at all: a forced choice is
        rejected in thinking mode, and the remaining value buys nothing
        (measured 2026-09-12 -- see the inline note below).

        Cache order (2026-09-07): the pop-up call is a byte-identical
        EXTENSION of the mainline call, not a request with its own prefix.
        It replays the turn's stable system (core + persona) and the same
        context stream, then appends the turn's state card and the pop-up
        block. Previously it passed the LEGACY full three-tier string as
        ``system`` — state card inside the system message, minute-resolution
        clock line and all — so every decision call presented a prefix no
        other call had ever sent and missed the cache in full. DeepSeek
        matches strictly from token 0 in 64-token units, so a differing
        system message costs the whole prefix, on a call that fires at every
        event boundary.

        The pop-up block rides as ``role="system"``: it is harness input,
        not something the user said. Native transport offers the tool
        schemas (``tool_choice=auto``); textual transport relies on the
        model's ``tool_decide_*: {...}`` marker reply. The returned
        ``RawReply`` carries the model's raw output for the runner to parse
        and persist (dual persistence). ``max_tokens`` stays None on pop-up
        calls (they are short verdicts and a cap must never starve a
        reasoning model — repo pitfall 3af0a5a).
        """
        # WS-E: never pass None content into the client (a stored
        # reasoning-only turn must serialize as "", never null).
        messages = [wire_message(m) for m in self._context_turns()]
        messages = append_system(messages, self._last_state_card)
        messages = append_system(messages, wrap_steer_marker(request.popup))
        # A re-ask restates the requirement; append_system folds it into the
        # block above, so the request still ends with ONE system message.
        messages = append_system(messages, request.nudge or "")
        # Native transport wraps Hermes-style schemas in the OpenAI
        # {"type": "function", "function": ...} form at the boundary.
        # Offer ONLY the requested function. A pop-up asks one named
        # question, and putting the other schemas on the table let the model
        # answer an event pop-up with tool_decide_reply -- reproduced against
        # the live gateway at 1-in-3 with all three offered, 0-in-3 with one.
        #
        # tool_choice is NOT SENT. A forced choice is unavailable on this
        # model -- it always thinks, and thinking mode 400s both
        #   tool_choice="required"  -> 400 "Thinking mode does not support
        #   tool_choice={name:...}  ->      this tool_choice"
        # -- and sending the only remaining value buys nothing: measured
        # against the live gateway on the real decide body (6 requests per arm,
        # interleaved), OMITTED parsed 6/6 tool calls while "auto" parsed 5/6,
        # with identical 97.2% cache and no latency penalty (4.8 s vs 6.8 s
        # mean). DeepSeek-Harness never sends the field either (its README
        # calls it unmapped vocabulary), so omitting it is also one less
        # divergence. Do not add a named choice back: that 400s every call.
        #
        # A prose reply with no tool call therefore remains possible. It is
        # bounded rather than prevented: the retry budget
        # (``steering.MAX_ATTEMPTS``) abandons a steer the model will not
        # answer usably, and the replayed native tool exchanges in context
        # give it the precedent that these calls are answered with a tool.
        native_tools = None
        native_choice = None
        if request.native and request.tools:
            # An unknown kind falls back to the full set rather than sending
            # none: a wrong-tool verdict is recoverable, no tool is not.
            offered = offered_tools(request)
            native_tools = [{"type": "function", "function": t} for t in offered]
            native_choice = None
        result = self.client.chat_with_meta(
            messages,
            # The mainline stable prefix, so this call extends it. Falls
            # back to the legacy full prompt only for callers that never
            # ran a mainline turn (pre-slice tests).
            system=self._last_stable_system or self._last_system_prompt,
            temperature=0.8,
            max_tokens=None,
            tools=native_tools,
            tool_choice=native_choice,
            reasoning_effort=self._thinking_effort,
        )
        self._note_request_prefix(request.popup_kind, messages)
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
        self._log_decision_call(request, messages, result)
        return RawReply(text=result.content or None, tool_calls=raw_tool_calls)

    def _log_decision_call(self, request: PopupRequest, messages: list[dict],
                           result) -> None:
        """Meter this pop-up call into the same ledger as a mainline turn.

        A decision is the SAME model on the SAME lane as everything else —
        only the question differs — so its tokens, cache split and cost
        belong in ``llm_calls`` like any other call. Until 2026-09-09 this
        write simply did not exist: ``decision_records`` held the verdict
        and nothing held the spend, so every pop-up and every parse-failure
        retry was invisible to token and cost accounting (more than half
        the evening's traffic in the first live day).

        ``role`` is the pop-up kind rather than "chat" so the two lanes can
        be told apart in the ledger; ``harness.audit`` selects
        ``role='chat'`` and is unaffected.
        """
        day = int(self.clock.now_h() // 24.0)
        t_h = self.clock.now_h()
        meta: dict = {"popup_kind": request.popup_kind,
                      "transport": "native" if request.native else "text",
                      "tool_calls": [tc.get("name")
                                     for tc in (result.tool_calls or [])]}
        if result.reasoning:
            meta["reasoning"] = result.reasoning
        repro_kwargs = (self._popup_repro(request, messages, day, t_h)
                        if self._accepts_repro else {})
        self.store.log_llm_call(
            day,
            t_h,
            request.popup_kind,
            (self._last_stable_system or "") + "\n" + repr(messages),
            result.content or "",
            getattr(self.client, "model", None),
            meta,
            **repro_kwargs,
            **self._usage_kwargs(result.usage, result.raw_cost),
        )

    def _conversation_context(self, limit: int = 4) -> str:
        """Condensed recent transcript for decide_reply pop-up inputs."""
        recent = self.store.recent_messages(limit=limit)
        return "\n".join(
            f"{m['role']}: {str(m['content'])[:200]}" for m in recent
        )

    def _mark_event_closed(self, item_id: str, outcome: str | None = None) -> None:
        """Server-side event close (``terminate_event`` verdict / ``abandon``
        action): the agenda item is no longer in progress — marked skipped so
        the NOW-semantics state card stops showing it.

        ``outcome`` is the model's own reason for closing it, recorded as
        what came of the item. Nothing is invented here: an item whose window
        merely elapsed gets a status and no outcome.
        """
        if not item_id or not hasattr(self.store, "update_agenda_item_status"):
            return
        self.store.update_agenda_item_status(item_id, "skipped")
        self._record_outcome(item_id, outcome)

    def _record_outcome(self, item_id: str, outcome: str | None) -> None:
        """Persist what came of an agenda item, when there is something real.

        The ONLY source is something the companion actually decided or said —
        a decide_event verdict's reason. Feeding the day planner invented
        outcomes would defeat the point of having them: the continuity has to
        be true, or the next day follows on from a fiction.
        """
        text = (outcome or "").strip()
        if not text or not item_id:
            return
        setter = getattr(self.store, "set_agenda_item_outcome", None)
        if setter is None:
            return
        setter(item_id, text)

    # -- availability negotiation (Inform-once -> Decide loop) ---------- #


    def on_message(self, user_text: str) -> TurnResult:
        """Process one user message: directive → snapshot → assemble → LLM."""
        return self._chat(user_text, proactive=False)

    def enqueue_proactive_decision(self, intent_id: str) -> int | None:
        """Queue the initiate/decline decision for one grounded intent.

        The steer payload carries the intent id plus its grounded hook so
        the decision pop-up renders without a second store lookup. Returns
        the steer id, or None when the steering seam is unavailable (the
        fire then proceeds undecided, today's behavior).
        """
        if self._steering is None:
            return None
        intent = self._lookup_intent(intent_id)
        if intent is None:
            return None
        day = self.clock.day()
        t_h = self.clock.now_h()
        return self._steering.enqueue(
            KIND_PROACTIVE,
            {
                "intent_id": intent.id,
                "hook": intent.hook,
                "reason": intent.reason,
                "source_type": intent.source_type,
                "source_id": intent.source_id,
                "valid_until": intent.valid_until_t_h,
            },
            day,
            t_h,
        )

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
                # Proactive-as-decision: the grounded fire is decided at
                # the turn's idle boundary before generation. The drain
                # executes tool_decide_proactive; a decline suppresses
                # the reply before any message is persisted.
                if self._decision is not None:
                    self.enqueue_proactive_decision(intent.id)
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
