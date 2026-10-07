"""Session — the daily loop and the turn.

Wires engine -> behavior -> life -> memory -> assembler -> client -> judge ->
store under a virtual clock. Engine replay contract (frozen in sim/run_daily):
per day t, the FIRST consumer of day_rng(seed, t) is cycle.step, then
mood.step; mood.step_endogenous consumes the SAME generator at day end, so a
session's mood sequence replays exactly like ``sim.run_daily`` for the seed.

A turn composes a ``CompanionSnapshot`` (the one place the lanes meet), puts
the day rows and state card in the stream, drains pending steers, assembles
the request and calls the client. A proactive turn is grounded on the EXACT
``ProactiveIntent`` id it fires for (never a reason lookup), and the outgoing
message persists ``intent_id``.

Lane rule: memory/life/persona never mutate each other. Memory writes happen
only through MemoryAgent at conversation/day boundaries; life writes only
through harness.life at day boundaries (agenda at rollover, step at
finalize), on the reserved LIFE stream (stream_rng(seed, 4, day)) — never
day_rng — so the engine replay contract is untouched.

Day lifecycle: rollover samples the day's state, persists daily_state and
plans the agenda; finalize judges the day (shadow by default), runs the
memory fork, steps the life lane, then applies the end-of-day engine update.

Resume: the latest daily_state row restores mu/eta, the cycle is replayed
from day 0, and life/memory state come from the store. A clock that restarts
BEHIND the store's day is fast-forwarded to it (resume never rewinds).
"""

from __future__ import annotations

import contextlib
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
from harness import habits, images, life, wire
from harness.actuation import controls_from_directive, to_brief
from harness.assembler import (
    AFFECTIVE_HEADER,
    CLOSING_HEADER,
    DEFAULT_PERSONA_CORE,
    RECENT_TURNS,
    append_system,
    apply_state_update,
    build_context_messages,
    is_state_update,
    day_plan_header,
    harness_text_in_user_roles,
    prefix_break,
    proactive_block,
    render_day_block,
    render_day_start_block,
    render_state_card,
    render_state_update,
    without_quiet_blocks,
    stable_system,
    stamped_stream,
)
from harness.behavior import BehaviorDirective, derive_behavior
from harness.client import LLMClient, fresh_session
from harness.clock import VirtualClock, hhmm
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
from harness.env import env_bool as _env_bool
from harness.faces import (
    FALLBACK_PREFIX,
    FaceCut,
    FaceEvent,
    face_from_continuation,
    face_tag_enabled,
    mod_blush,
    mod_scene,
    split_face,
    split_face_mods,
    strip_face,
    with_call_face_rule,
    with_face,
    with_face_instruction,
)
from harness.judge import JudgeResult, day_facts, judge_day
from harness import gateway
from harness.memory_fork import day_view, remember_day
from harness.life import LIFE_STREAM, transition_past_windows
from harness.memory import MemoryAgent
from harness.metering import MeteredClient
from harness.negotiation_contract import (
    HEADS_UP_LEAD_H,
    NegotiationPhase,
)
from harness.negotiation_coordinator import START_NOTE, NegotiationMixin
from harness.negotiation_state import (
    NegotiationState,
)
from harness.prompts import (
    CALL_END_ROW,
    CALL_HUNG_UP_RINGING_ROW,
    CALL_START_ROW,
    MISSED_CALL_NOTE,
    PRESENCE_END_ROW,
    PRESENCE_START_ROW,
    TURN_STYLE,
    TURN_STYLE_TOGETHER,
)
from harness.scheduler import CYCLE_START_KEY, VALID_REASONS, _record_from_row
from harness.score import synthetic_score as run_daily_synthetic_score
from harness.steering import (
    BOUNDARY_IDLE,
    DECISION_KINDS,
    KIND_EVENT_POPUP,
    KIND_INCOMING_CALL,
    KIND_PROACTIVE,
    KIND_USER_MESSAGE,
    Steer,
    SteeringQueue,
    render_steer_block,
    wrap_steer_marker,
)
from harness.store import SQLiteStore
from harness.tunables import (
    CLOSING_TENDENCY_ENABLED,
    MAX_TURNS,
    USER_AWAY_THRESHOLD_H,
    USER_LEFT_THRESHOLD_H,
    WIND_DOWN_GRACE_H,
)
from harness.tools import (
    TOOL_PAYLOAD,
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


#: Closing-tendency draw stream (engine.rng stream 6), keyed by
#: (conversation sequence, companion turn index) — deterministic per draw.
CONVERSATION_STREAM = 6

#: Two-phase close wind-down guidance rendered through the assembler's
#: ``closing_guidance`` channel into the next companion turn's state card.
WIND_DOWN_GUIDANCE = "You're wrapping up, say a natural goodbye."

#: Re-ask nudge when a generation comes back without prose (tool calls or
#: reasoning only); the same request is extended once.
REPLY_NUDGE = (
    "Your tool calls are recorded. Write your reply now: plain text in "
    "your own voice, no tool calls."
)

#: kv_store key holding the context compaction watermark (see
#: ``Session.context_epoch_id``).
CONTEXT_EPOCH_KEY = "context.epoch_id"

#: kv_store key of the cold-point prune: the stream rows it dropped and the
#: gap it was decided for (see ``Session._cold_prune``).
CONTEXT_PRUNED_KEY = "context.pruned"

#: Idle time after which the provider's prefix cache is gone (probe of
#: 2026-09-29: an unchanged prompt still hit after 105 min, missed from
#: 135 min). A request after this long pays the full prefill anyway, so
#: that is where the stream may shrink.
COLD_PRUNE_AFTER_H = 2.0

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

#: kv_store key of the voice call in progress (JSON; "" when none).
CALL_ACTIVE_KEY = "call.active"
#: kv_store key of the virtual time the last voice call ended.
CALL_LAST_END_KEY = "call.last_end_t_h"
#: Each call row is stored at least this far after the row before it, so
#: the (t_h, lane, id) merge in ``_context_turns`` keeps them in the order
#: they were sent (3.6 ms of virtual time).
CALL_TICK_H = 1e-6
#: The lanes of the ``(t_h, lane, id)`` merge in ``_context_turns``: what
#: happens AT one instant sorts stored context, his message, the decisions
#: taken at that boundary, then her reply.
_LANE_SYSTEM, _LANE_USER, _LANE_DECISION, _LANE_ASSISTANT = -1, 0, 1, 2


def _message_lane(role: str | None) -> int:
    """The merge lane of a stored message.

    A stored system row (the day-start block) needs lane -1, not the
    assistant's: it is persisted at the same t_h as the user message that
    triggered it, and sharing the assistant lane put it after the user
    message on turns with a decision and before it on turns without,
    reordering the append-only stream between calls.
    """
    return (_LANE_SYSTEM if role == "system"
            else _LANE_USER if role == "user" else _LANE_ASSISTANT)

#: Substrings that mark tool machinery in a streamed call reply: speech
#: stops at the first one.
_CALL_MARKUP_MARKERS = (
    "dsml", "tool_call", "tool_decide_", "<invoke", "<parameter",
    "function_call", "<antml",
)

class EmptyReply(RuntimeError):
    """A generation came back with neither prose nor a tool call."""


#: Characters that carry no words: a reply made only of these (after the face
#: tag) is no reply. Live 2026-10-02 sent "[neutral] ..." as a message.
_WORDLESS_RE = re.compile(r"[\s.\u2026,;:!?\-\u2013\u2014~*\"'`]*")


def _wordless(reply: str) -> bool:
    """True when ``reply`` holds no words once its face tags are cut."""
    return _WORDLESS_RE.fullmatch(strip_face(reply)) is not None


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
    """What one turn produced: the reply, the observable state and the
    delivery controls (``controls.response_delay_s``).

    The decision layer's channel outputs ride along so the runtime sends
    them without the session touching the channel:

    - ``notices`` — server notices for no-reply verdicts. A notice replaces
      the ordinary reply (one reply per user message, never both).
    - ``proactive_out`` — ``(reason, text)`` messages she sends as proactive
      outbound.
    - ``bubbles`` — with HARNESS_BUBBLES on and a split in the reply, the
      bubble texts for a paced multi-send; None sends one message.
    - ``streamed`` — True when ``bubbles`` were parsed off the backend
      stream (HARNESS_BUBBLE_STREAM) rather than post-hoc; delivery is the
      same either way. False whenever ``bubbles`` is None.
    """

    reply: str
    directive: BehaviorDirective
    day: int
    hour: float
    controls: GenerationControls | None = None
    notices: tuple[str, ...] = ()
    proactive_out: tuple[tuple[str, str], ...] = ()
    bubbles: tuple[str, ...] | None = None
    #: Her face for this reply (``harness.faces``), None when she has none.
    face: str | None = None
    streamed: bool = False
    #: True when a proactive fire was declined by her decide verdict: nothing
    #: was said, so the runtime must not record the slot as a contact.
    declined: bool = False
    #: True when she picked up his call: nothing goes to Telegram, the call
    #: turns carry her words.
    call_answered: bool = False


class _NoopMemory:
    """Memory fallback for stores without the memory tiers (the seam fake):
    empty retrieval, and no summary, so promote/update never run."""

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


#: The outcome of an event she left partway through to stay with him.
STOPPED_EARLY = "stopped early"

#: A missed day's events drain this far before its midnight, so the drain
#: still lands on that day (``int(t_h // 24)``).
DAY_END_EPS_H = 1e-6


@dataclass
class _SteerDrain:
    """What draining this turn's steers produced (filled in place by the
    steer handlers).

    ``decided_intents`` are proactive intents an initiate verdict cleared
    for this turn; ``proactive_declined`` marks a decline that suppresses
    the reply; ``undecided_intents`` are intents whose verdict never parsed
    — their fire is not sent either (no yes is not a yes).
    """

    notices: list[str] = field(default_factory=list)
    proactive_out: list[tuple[str, str]] = field(default_factory=list)
    injections: list[str] = field(default_factory=list)
    suppress_reply: bool = False
    decided_intents: list = field(default_factory=list)
    proactive_declined: bool = False
    undecided_intents: set = field(default_factory=set)
    #: Notes about what she just decided, appended to THIS turn's tail so the
    #: turn generates her words for it. The verdict's ``reason`` is audit
    #: data (``decision_records``), never dialogue.
    decided_notes: list[str] = field(default_factory=list)
    #: Close reason to apply AFTER the reply is persisted, when a verdict
    #: ends the conversation. Closing before generation left the user's
    #: goodbye unanswered (live 2026-09-07).
    close_after: str | None = None
    #: The incoming-call verdict: True answered, False let it ring out,
    #: None no call decided this turn.
    call_answered: bool | None = None


#: Tool-call markup a model may emit as reply CONTENT instead of as a
#: structured tool call. Live 2026-09-08: a turn came back as DeepSeek DSML
#: (``<｜｜DSML｜｜tool_calls>...``) and was persisted and sent verbatim —
#: neither empty nor malformed, just machinery where her words should be.
#: Fullwidth bars (U+FF5C) are DeepSeek's; the ASCII forms cover the other
#: families; the harness's own ``tool_decide_*: {...}`` notation is the same
#: mistake.
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
    r"^\s*tool_decide_(?:event|reply|proactive|call)\s*:\s*(?:\{.*?\})?",
)
_TOOL_MARKUP = re.compile("|".join(_TOOL_MARKUP_PATTERNS),
                          re.IGNORECASE | re.MULTILINE)

#: State event recorded whenever a mainline reply carried tool-call markup,
#: whether or not prose was salvaged from it.
EVENT_TOOL_MARKUP_LEAK = "reply_tool_markup"


def _call_markup_at(text: str) -> int | None:
    """Where tool machinery starts in a streamed call reply, or None."""
    low = text.lower()
    hits = [i for i in (low.find(m) for m in _CALL_MARKUP_MARKERS) if i >= 0]
    if not hits:
        return None
    cut = min(hits)
    # Back up over the tag opener ("<", "</", fullwidth bars) before it.
    while cut > 0 and text[cut - 1] in "<｜|/ ":
        cut -= 1
    return cut


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


def _with_photo_note(text: str, row: dict) -> str:
    """A transcript line for the forks, which read text: a photo the owner
    sent is named so the memory of the day keeps it."""
    count = len(images.of(row))
    if not count:
        return text
    note = "[sent a photo]" if count == 1 else f"[sent {count} photos]"
    return f"{note} {text}".rstrip()


def _with_bubble_instruction(system: str) -> str:
    """Append the bubble instruction when HARNESS_BUBBLES is on, then the
    face tag instruction when HARNESS_FACE_TAG is on (``harness.faces``).

    Import and flag read are both inside the guard on purpose: bubbles are
    an optional layer, and a harness built without the module (or with a
    broken flag) must still produce an ordinary single-message turn.
    """
    try:
        from harness.bubbles import BUBBLE_INSTRUCTION, bubbles_enabled

        if bubbles_enabled():
            system = system + "\n\n" + BUBBLE_INSTRUCTION
    except Exception:
        pass
    return with_face_instruction(system)


def _openai_tool_calls(calls) -> list[dict] | None:
    """ChatResult tool calls ({id, name, arguments_json}) in OpenAI shape.

    The runner's parser expects the wire shape; the client hands back its own.
    """
    if not calls:
        return None
    return [
        {
            "id": call.get("id"),
            "type": "function",
            "function": {
                "name": call.get("name"),
                "arguments": call.get("arguments_json"),
            },
        }
        for call in calls
    ]


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
        random_cycle_start: bool = False,
    ):
        self.store = store
        # Off by default: the engine's reference runs (run_daily) and the
        # seed-pinned tests start the cycle at ``persona.phi``.
        self._random_cycle_start = random_cycle_start
        store_path = str(getattr(store, "path", "") or "")
        if store_path and store_path != ":memory:":
            # Dumps sit next to the run's database; an in-memory store has
            # no directory, so it gets none (not the process's cwd).
            run_dir = os.path.dirname(os.path.abspath(store_path))
            wire.configure(os.path.join(run_dir, "wire"))
            images.configure(os.path.join(run_dir, "images"))
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
        aux_client = judge_client if judge_client is not None else client
        #: Judge-lane client (the product client for offline/fake runs),
        #: metered so its spend lands in ``llm_calls``.
        self.judge_client = MeteredClient(aux_client, store, clock, "aux_judge")
        #: End-of-day memory, the third fork (owner ruling 2026-09-26). Same
        #: lane as the judge and called right after it, so it reads the
        #: prefix the judge just banked. OPT-IN via ``HARNESS_MEMORY_FORK``,
        #: like the day planner: offline runs keep byte parity and never
        #: spend a scripted response on it.
        self._memory_client = (
            MeteredClient(aux_client, store, clock, "aux_memory")
            if _env_bool("HARNESS_MEMORY_FORK", False) else None
        )
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
        self._life_arcs: list[LifeArc] = store.list_life_arcs()
        try:
            _params = inspect.signature(store.add_message).parameters
        except (TypeError, ValueError):
            _params = {}
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

        self.cycle_state: CycleState = self._initial_cycle(persona, seed)
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
        # OFF by default: the closing draw closes at the drawn turn.
        self.two_phase_close = (two_phase_close
                                or _env_bool("HARNESS_TWO_PHASE_CLOSE", False))
        self._closing_pending_t_h: float | None = None
        self._sync_closing_pending()

        # Availability negotiations: one NegotiationState per
        # AgendaItem; rebuilt from persisted snapshots on resume.
        self._negotiations: dict[str, NegotiationState] = (
            self._restore_negotiations()
        )

        # Steering + decision layer: SteeringQueue wires on the store seam;
        # DecisionRunner builds when enabled (``HARNESS_DECISIONS`` or an
        # injected config; the per-knob variables never switch it on).
        self._steering: SteeringQueue | None = None
        self._decision: DecisionRunner | None = None
        self._decision_enabled = (decision_config is not None
                                  or _env_bool("HARNESS_DECISIONS", False))
        self._decision_cfg = (
            decision_config if decision_config is not None else load_decision_config()
        )
        self._thinking_effort = _load_thinking_effort()
        self._day_block: str | None = None
        self._day_block_day: int | None = None
        #: Memo of the last card's FULL text — the baseline a changed card
        #: updates against (an UPDATE row is never a baseline itself). The
        #: stream is append-only: an unchanged card is never re-sent, and a
        #: day's first card is always the full one.
        self._card_full, self._card_day = self._load_card_memo()
        #: The stable system the last mainline request sent: pop-up and fork
        #: calls start from it so they extend that request byte for byte.
        self._last_stable_system: str = ""
        #: Per-lane requests already sent this run: the prefix invariant
        #: compares within a lane (the decide leg legitimately EXTENDS the
        #: mainline array rather than matching it, so a global check would
        #: report a violation on every turn).
        self._last_request_pairs: dict[str, list] = {}
        #: the face tag of the call reply in flight (call_turn -> call_heard)
        self._call_face: str | None = None
        #: Steers drained for the turn currently being generated — requeued
        #: if the turn is interrupted (the LLM call is abandoned).
        self._turn_drained: list[int] = []
        #: Decision model calls made so far (``_decision_model_call``); the
        #: drain reads it to keep one pop-up per turn.
        self._decision_calls = 0
        #: The system blocks the last pop-up call appended, as sent.
        self._popup_blocks: list[str] = []
        #: The tool payload the last generation rode with — the wire identity
        #: the mainline repro records (the constant menu, by default). Starts
        #: as that menu so a fork before the first turn (the midnight planner
        #: or judge after a restart) still carries the schemas the prefix has.
        self._last_request_tools: list | None = TOOL_PAYLOAD
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
        #: The voice call in progress, persisted under ``CALL_ACTIVE_KEY``
        #: so a restart can close it: {"id", "started_t_h", "last_t_h"}.
        self._call: dict | None = self._load_call()
        #: The call id of the incoming-call turn being decided, if any.
        self._ringing_call_id: str | None = None
        #: Set (from the runtime) when he hangs up while it is still ringing.
        self._ring_cancel = None

    # -- resume / replay ----------------------------------------------- #

    def _initial_cycle(self, persona: PersonaParams, seed: int) -> CycleState:
        """Her cycle on day 0: with ``random_cycle_start``, a character starts
        at a random point of it.

        ``persona.phi`` left at 0 means unset (engine/types.py), so a new
        character draws φ uniformly over her first cycle's length, from the
        init stream after L_0 (L_0 itself is unchanged). A run that already
        has days but no kept φ began at ``persona.phi`` and keeps it; its
        cycle must not jump on the first restart after this change.
        """
        rng = rng_mod.init_rng(seed)
        state = cycle.init_state(persona, rng)
        if (not self._random_cycle_start or persona.phi != 0.0
                or not hasattr(self.store, "get_kv")):
            return state
        raw = self.store.get_kv(CYCLE_START_KEY)
        if raw is not None:
            return replace(state, cycle_day=float(raw))
        if self.store.latest_daily_state() is None:
            state = replace(state, cycle_day=float(rng.uniform(0.0, state.L_current)))
        self.store.set_kv(CYCLE_START_KEY, repr(state.cycle_day))
        return state

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
        # Yesterday's record feeds today's momentum (derive_behavior's
        # ``previous``); without it a restart read the change as zero.
        previous = self.store.load_daily_state(day - 1) if day > 0 else None
        if previous is not None:
            self._records[day - 1] = self._record_from_row(previous)
        # Reconstruct the day RNG at the post-rollover position by
        # consuming the same draws the rollover consumed — and keep the
        # advance the rollover kept: `_rollover` leaves `cycle_state` at the
        # NEXT day's start, so dropping it here stalled the cycle on every
        # restart (day d repeated day d-1's cycle_day and engine draws).
        rng_t = rng_mod.day_rng(self.seed, day)
        m, g, _phase, cycle_next = cycle.step(self.cycle_state, self.persona, rng_t)
        self.cycle_state = cycle_next
        mood.step(self.mood_state, self.persona, m, g, self.variant, rng_t)
        self._day_rng = rng_t
        # If the latest day was already finalized, re-apply its end-of-day
        # update so resume matches a fresh run.
        judgement = self.store.load_judgement(day)
        if judgement is not None:
            if self.synthetic_score:
                # The original finalize consumed the score draw BEFORE the
                # endogenous update.
                run_daily_synthetic_score(self._records[day].M, self.persona.N, rng_t)
            if self.feedback:
                self.mood_state = mood.update(
                    self.mood_state, self.persona, float(judgement["score"])
                )
            self.mood_state = mood.step_endogenous(self.mood_state, self.persona, rng_t)

        # Restore life state from the store; seed arcs lazily when a
        # persona exists but none persisted.
        self._ensure_life()
        if self._profile is not None and self.store.load_agenda(day) is None:
            self._generate_agenda(day)

        # Finalize crash window: judgement + NULL score marks the
        # crashed tail; steps complete once and are idempotent.
        if judgement is not None and latest.get("score") is None:
            self._complete_pending_finalize(day, judgement)

    _record_from_row = staticmethod(_record_from_row)

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
                result = self.judge(
                    transcript, self.judge_client, model=self.judge_model,
                    fork=self._aux_task_request,
                    day_marker=self._day_marker(day),
                    facts=self._day_facts(day),
                    reasoning_effort=self._thinking_effort,
                )
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
        if transcript and not self.synthetic_score:
            self._remember_day(day)

        # Life step for the day just ended; this sweep completes
        # memory-tail stragglers from a crash between close and tail.
        self._step_life(day)
        self._recover_conversation_memory_tails(day)

        if self.feedback:
            self.mood_state = mood.update(self.mood_state, self.persona, score)
        assert self._day_rng is not None
        self.mood_state = mood.step_endogenous(self.mood_state, self.persona, self._day_rng)
        self.store.update_daily_score(day, score)
        self._expire_day_steers(day)
        self._record_rotations(day, self.clock.now_h())
        self.store.log_event(
            day, self.clock.now_h(), "day_finalized",
            f"score={score:.3f} shadow={not self.feedback}",
        )

    def _remember_day(self, day: int) -> None:
        """The memory fork: what she learned about him today, written as
        episodes and user-model assertions (:mod:`harness.memory_fork`). A
        failed call loses the day's memories, never the day."""
        if self._memory_client is None or not hasattr(self._memory, "consolidate"):
            return
        messages = self.store.messages_for_day(day)
        his = [m for m in messages if m.get("role") == "user"]
        if not his:
            return
        known = [(a.key, a.value) for a in self.store.list_assertions()]
        try:
            remembered = remember_day(
                self._memory_client, lambda task: self._day_task_request(day, task),
                known=known, his_turns=his,
                reasoning_effort=self._thinking_effort, today=self._day_name(day),
            )
            written = self._memory.consolidate(
                remembered, day=day, messages=messages,
                ended_at_t_h=self.clock.now_h(),
            )
        except Exception as exc:  # noqa: BLE001 - the day must still close
            self.store.log_event(day, self.clock.now_h(), "memory_fork_failed",
                                 str(exc)[:200])
            return
        self.store.log_event(
            day, self.clock.now_h(), "memory_fork",
            f"facts={len(remembered.facts)} callbacks={len(remembered.callbacks)} "
            f"moments={len(remembered.moments)} dropped={remembered.dropped} "
            f"assertions={len(written)} summary={bool(remembered.summary)}",
        )

    def _day_name(self, day: int) -> str | None:
        """"Wednesday, September 30" for the memory fork, when anchored."""
        anchor = self._real_time_anchor()
        if anchor is None:
            return None
        try:
            real = anchor.real_at(day * 24.0 + 12.0)
        except Exception:  # noqa: BLE001 - a broken anchor loses only the name
            return None
        return f"{real:%A, %B} {real.day}"

    def finalize_current(self) -> None:
        """Finalize the current day if it has not been finalized yet and has
        messages.

        Intended for clean shutdown paths (CLI quit) — creates the
        finalized-latest-day state that `_resume_from` now handles. A day
        opened just before the shutdown (a restart's boundary day) has no
        messages yet; it is finalized at its own midnight with its real ones.
        """
        if self.current_day is None:
            return
        if self.store.load_judgement(self.current_day) is not None:
            return
        if not self.store.messages_for_day(self.current_day):
            return
        self.finalize_day(self.current_day)

    def _day_facts(self, day: int) -> str:
        """The day's counts and waits for the judge (:func:`judge.day_facts`)."""
        rows = [(float(m["t_h"]), m["role"], bool(m["proactive"]))
                for m in self.store.messages_for_day(day)
                if m["role"] in ("user", "assistant")]
        return day_facts(rows, self._clock_text, (day + 1) * 24.0)

    def _day_marker(self, day: int) -> str | None:
        """The day's plan header, when that row is in the stream.

        The forked judge reads a week of context; this is where its day
        starts. None when the day never emitted its plan block. A row
        written before 2026-10-03 opens "Saturday's plan:", a later one
        "Lily's Saturday plan:".
        """
        t_h, anchor = day * 24.0 + 12.0, self._real_time_anchor()
        name = self._profile.name if self._profile is not None else "Nova"
        headers = (day_plan_header(t_h, anchor, name), day_plan_header(t_h, anchor))
        for row in self.store.messages_for_day(day):
            text = str(row["content"] or "")
            if row["role"] == "system" and text.startswith(headers):
                return next(h for h in headers if text.startswith(h))
        return None

    def _transcript_for(self, day: int) -> str:
        # What the two of them said: cards and style rows are machinery.
        msgs = [m for m in self.store.messages_for_day(day)
                if m["role"] in ("user", "assistant")]
        if not msgs:
            return ""
        return "\n".join(
            f"{m['role']}: {_with_photo_note(strip_face(m['content']), m)}"
            for m in msgs)

    # -- life + memory drivers (session composes; lanes persist) ------- #

    def _ensure_life(self) -> None:
        """Seed persistent life arcs once per life epoch.

        The epoch goes to ``init_life`` so a wipe followed by a restart
        re-seeds under a FRESH id namespace instead of reusing the wiped
        generation's ids. Normal restarts keep the persisted arcs.
        """
        if self._life_arcs or self._profile is None or not self._profile.interests:
            return
        epoch = self._life_epoch()
        self._life_arcs = life.init_life(self.seed, self._profile, self.store, epoch=epoch)
        if self._life_arcs:
            self.store.log_event(
                self.current_day if self.current_day is not None else 0,
                self.clock.now_h(),
                "life_init",
                f"epoch={epoch} arcs={len(self._life_arcs)}",
            )

    def _life_epoch(self) -> int:
        """The epoch for the next seeding: prior ``life_init`` and
        ``life_wipe`` events in the audit log (it survives arc wipes and is
        deterministic across restarts)."""
        return sum(
            1 for e in self.store.events_since(0)
            if e.get("event") in ("life_init", "life_wipe")
        )

    def _generate_agenda(self, day: int) -> None:
        """Plan + persist today's agenda via the life lane (LIFE stream).

        The engine keeps the seeded selection, windows and ids; the day
        planner (when on) only names what she is doing. Replay never calls
        it: a day is generated only when the store has no agenda for it.
        """
        if self._profile is None:
            return
        rng = stream_rng(self.seed, LIFE_STREAM, day)
        life.generate_agenda(
            day, self._profile, self._life_arcs, self.store, rng,
            planner_client=self._planner_client(),
            fork=self._aux_task_request,
            weekday=self._weekday_name(day),
            plan=self._life_plan(), seed=self.seed,
            logger=lambda line: self.store.log_event(
                day, self.clock.now_h(), "day_planner", line
            ),
        )

    def _life_plan(self):
        """Her saved habits and commitment (:mod:`harness.habits`), drawn
        and saved the first time a persona needs them."""
        if self._profile is None:
            return None
        return habits.load_plan(self.store, self.seed, self._profile)

    def edit_today(self, *, off: bool = False,
                   start_h: float | None = None) -> str:
        """Change today's commitment: off for the day, or moved to
        ``start_h`` (local hour). The change enters her stream as an event
        she learns about — never a silent rewrite of her agenda. A session
        it now overlaps makes way for it. Returns a line for the command
        reply."""
        day = self.clock.day()
        agenda = self.store.load_agenda(day)
        block = next((it for it in (agenda.items if agenda else ())
                      if it.source_type == "commitment"), None)
        if block is None:
            return "nothing fixed on today's plan"
        if block.status != "planned":
            return f"today's {block.activity} is already {block.status}"
        now = self.clock.now_h()
        if off:
            changed = replace(block, status="skipped", outcome="day off")
            note = f"no {block.activity} today"
            reply = f"{block.activity} is off today"
        else:
            start = day * 24.0 + float(start_h)
            if start <= now:
                return "that time has already passed today"
            length = block.end_t_h - block.start_t_h
            changed = replace(block, start_t_h=start,
                              end_t_h=min(start + length, day * 24.0 + life.AWAKE_END_H))
            note = (f"{block.activity} moved to {hhmm(changed.start_t_h)}–"
                    f"{hhmm(changed.end_t_h)} today")
            reply = note
        items = []
        for it in agenda.items:
            if it.id == block.id:
                items.append(changed)
            elif (not off and it.status == "planned" and it.start_t_h > now
                  and it.start_t_h < changed.end_t_h
                  and it.end_t_h > changed.start_t_h):
                items.append(replace(it, status="skipped",
                                     outcome=f"made way for {block.activity}"))
            else:
                items.append(it)
        items.sort(key=lambda it: it.start_t_h)
        self.store.save_agenda(day, DailyAgenda(day=day, items=tuple(items)))
        self._persist_row("system", f"PLAN CHANGE ({hhmm(now)}): {note}.", now, day)
        self.store.log_event(day, now, "plan_change", note)
        return reply

    def _planner_client(self):
        """The client the day planner may use, or None to keep templates.

        OPT-IN via ``HARNESS_DAY_PLANNER`` (the live launcher turns it on):
        the planner shares the conversation client, and default OFF keeps
        offline runs and replays from spending a scripted response on it.
        """
        if not _env_bool("HARNESS_DAY_PLANNER", False):
            return None
        return MeteredClient(self.client, self.store, self.clock, "aux_day_planner")

    def _weekday_name(self, day: int) -> str:
        """Weekday of ``day`` from the real anchor, else a neutral word."""
        anchor = self._real_time_anchor()
        if anchor is None:
            return "today"
        try:
            return anchor.real_at(day * 24.0 + 12.0).strftime("%A")
        except Exception:
            return "today"

    def _step_life(self, day: int) -> None:
        """Advance the life lane for the day just ended (LIFE stream)."""
        if self._profile is None:
            return
        agenda = self.store.load_agenda(day)
        if agenda is None:
            return
        rng = stream_rng(self.seed, LIFE_STREAM, day)
        result = life.step_life(day, self._profile, self._life_arcs, agenda,
                                self.store, rng, plan=self._life_plan())
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
        if self.store.load_agenda(day) is None:
            return True
        return any(
            e.get("event") == "life_step" and e.get("day") == day
            for e in self.store.events_since(day)
        )

    def _expire_day_steers(self, day: int) -> None:
        """Abandon the day's unanswered pop-ups and proactive fires as it ends.

        They are about moments of a day that is over; left pending they pop
        up tomorrow (live 2026-10-02: steer 93, "read history start", outlived
        day 5). His own held messages are never expired.
        """
        if self._steering is None:
            return
        stale = [
            int(row["id"]) for row in self.store.pending_steers(limit=500)
            if row.get("kind") in (KIND_EVENT_POPUP, KIND_PROACTIVE)
            and int(row.get("day") or 0) <= day
        ]
        for steer_id in stale:
            self._steering.abandon(steer_id)
        if stale:
            self.store.log_event(
                day, self.clock.now_h(), "steers_expired",
                f"day={day} steers={stale}",
            )

    def _complete_pending_finalize(self, day: int, judgement: dict) -> None:
        """Finish the memory/life tail of a finalize_day that died after
        ``save_judgement`` (judgement present, score never persisted).

        Each step is guarded by its own persistence marker (L2 summary,
        ``life_step`` event), so it runs once and a crash inside it is itself
        recoverable. The end-of-day mood update is NOT re-applied here:
        ``_resume_from`` already did.
        """
        self._recover_conversation_memory_tails(day)
        if self._profile is not None and not self._life_step_done(day):
            self._step_life(day)
        self.store.update_daily_score(day, float(judgement["score"]))
        self.store.log_event(
            day, self.clock.now_h(), "day_finalized",
            f"score={float(judgement['score']):.3f} shadow={not self.feedback}",
        )

    def _recover_conversation_memory_tails(self, day: int) -> None:
        """Complete the memory tail of conversations closed during ``day``
        whose L2 summary never persisted (a crash between the close and its
        tail). Idempotent: a clean day re-runs nothing."""
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
        """Id of the currently open conversation, or None."""
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

        Idempotent and cheap: called at every wake and before every turn, so
        the close is recorded at its boundary instant. Returns the
        close_reason, or None when the conversation stays open.
        """
        conv = self._conversation
        if conv is None or self._call is not None:
            # No close runs while a voice call is on: only he ends a call.
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
        anchor = self._user_anchor_t_h(conv)
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
        qstart = int(now // 24.0) * 24.0 + self.timing.quiet_hours[0]
        if qstart <= now + 1e-12:
            qstart += 24.0
        candidates: list[float] = []
        if last < qstart:
            candidates.append(qstart)
        if self._closing_pending_t_h is not None:
            grace_deadline = self._closing_pending_t_h + WIND_DOWN_GRACE_H
            if grace_deadline > now + 1e-12:
                candidates.append(grace_deadline)
        anchor = self._user_anchor_t_h(conv)
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

    @classmethod
    def _user_anchor_t_h(cls, conv: Conversation) -> float:
        """Where user silence is measured from: his last turn, or the
        conversation's opening when he has not spoken in it."""
        anchor = cls._last_user_turn_t_h(conv)
        return conv.opened_t_h if anchor is None else anchor

    def is_user_away(self, t_h: float) -> bool:
        """Whether he is 'away': his silence (from his last turn, or the
        opening) has reached ``USER_AWAY_THRESHOLD_H`` but not the
        ``USER_LEFT_THRESHOLD_H`` that closes the conversation. False when
        none is open."""
        conv = self._conversation
        if conv is None:
            return False
        anchor = self._user_anchor_t_h(conv)
        elapsed = t_h - anchor
        return USER_AWAY_THRESHOLD_H <= elapsed < USER_LEFT_THRESHOLD_H

    @staticmethod
    def _quiet_start_at_or_before(t_h: float, quiet_hours) -> float:
        """Start hour of the quiet window containing (or ending at) t_h."""
        boundary = int(t_h // 24.0) * 24.0 + quiet_hours[0]
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

        DRAW DISCIPLINE: at each companion turn EXCEPT the conversation's
        first, draw ``uniform()`` from ``stream_rng(seed,
        CONVERSATION_STREAM, conv_seq, turn_index)`` and close with
        ``closing_tendency`` when it is below ``closing_tendency``. Keying by
        the conversation sequence gives each conversation its OWN draws (a
        turn-index-only key gives all of them the same one); the first-turn
        floor guarantees one full exchange before any taper. A conversation
        that survives the draw closes with ``max_turns`` at ``MAX_TURNS``.

        TWO-PHASE CLOSE (``two_phase_close``): same draws, but a fired draw
        persists ``closing_pending_t_h`` instead of closing; the next
        companion turn renders ``WIND_DOWN_GUIDANCE`` and closes with no
        second draw, or the grace deadline in
        ``check_conversation_lifecycle`` closes a silent user's conversation.
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
        """Two-phase close: persist the wind-down marker instead of closing."""
        self._closing_pending_t_h = t_h
        if hasattr(self.store, "set_conversation_closing_pending"):
            self.store.set_conversation_closing_pending(conv.id, t_h)
        self.store.log_event(
            int(t_h // 24.0), t_h, "wind_down_started", f"id={conv.id}"
        )

    def _sync_closing_pending(self) -> None:
        """Restore ``_closing_pending_t_h`` from the store's open
        conversation (resume)."""
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
        """Persist the close (``close_reason``), clear any wind-down marker
        and run the conversation's memory tail. Idempotent."""
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
        """L1 -> L2 -> L3 -> L4 at the conversation boundary, through the
        MemoryAgent. Once per conversation (summary-exists guard); a silent
        conversation has no provenance and is skipped. ``conv`` is closed.
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
        """What she is doing NOW (``life.current_activity_now``): only an item
        in progress at ``t_h`` and not skipped/shifted, the most salient when
        several overlap; None when nothing is in progress (never the day's
        most salient item regardless of time)."""
        items = self.store.list_agenda_items(day=day)
        if not items:
            return None
        return life.current_activity_now(DailyAgenda(day=day, items=tuple(items)), t_h)

    def _resolve_intent(self, reason: str | None) -> ProactiveIntent | None:
        """Legacy reason path: the first stored intent for ``reason``, or
        None (the turn then opens generically, claiming no source)."""
        if reason is None:
            return None
        for intent in self.store.list_proactive_intents():
            if intent.reason == reason:
                return intent
        return None

    def _lookup_intent(self, intent_id: str) -> ProactiveIntent | None:
        """The stored intent with EXACTLY this id, or None."""
        return self.store.load_proactive_intent(intent_id)

    def _real_time_anchor(self):
        """The store's attached RealTimeAnchor, or None when the run is
        unanchored (replay, fakes) — the assembler then renders no clock,
        never t_h."""
        return getattr(self.store, "_anchor", None)

    def _transition_agenda_windows(self, day: int, t_h: float) -> None:
        """Persist planned->completed for today's items whose window has
        fully passed at ``t_h`` (the state card renders from the same
        comparison, so render and status agree)."""
        agenda = self.store.load_agenda(day)
        if agenda is None:
            return
        for item in transition_past_windows(agenda, t_h, day):
            self.store.update_agenda_item_status(item.id, item.status)

    def _load_card_memo(self) -> tuple[str | None, int | None]:
        """The card memo a fresh process starts from: ``(full text, day)``.

        The kv pair is the fast path. Either half can be missing — a wiped
        kv, or a deploy that introduces a key with nothing behind it — and a
        loader that then cannot name the day reads an already-carded day as
        the day's FIRST card and re-sends the full card mid-day (the live
        run's 02:09 restart did exactly that). The stream is the record and
        backs the cache up: the newest FULL card row plus the update rows
        after it rebuild both halves.
        """
        try:
            full = self.store.get_kv("state_card.full")
            raw_day = self.store.get_kv("state_card.day")
        except AttributeError:  # minimal stores (tests/fakes) keep working
            return None, None
        try:
            day = int(raw_day) if raw_day is not None else None
        except (TypeError, ValueError):
            day = None
        if full is not None and day is not None:
            return full, day
        stream = self.store.last_card_state()
        if stream is None:
            return full, day
        text, stream_day, updates = stream
        if full is None:
            # Rebuild the accumulated text: the full card, then every update
            # row the stream carries after it, applied in order.
            for row in updates:
                text = apply_state_update(text, row)
            full = text
        if day is None or (stream_day is not None and stream_day > day):
            # The stream outranks the cache: a card row that landed without
            # its kv write must not read as an already-carded day's successor.
            day = stream_day
        return full, day

    def _ensure_state_card(
        self,
        snapshot,
        prompt_brief: str | None,
        day: int,
        t_h: float,
    ) -> None:
        """Append the state card as a stream row only when its state changed.

        A changed card appends as a section-level UPDATE against the memo's
        full text (the full card when the update would not be smaller); the
        day's first card is always the full one. State only: per-turn
        guidance (closing wind-down) never enters the persisted row — it
        rides the generation as a transient block. The memo holds the last
        FULL text, so an update row never becomes a baseline.
        """
        text = render_state_card(snapshot, prompt_brief=prompt_brief)
        if not text:
            return
        first_of_day = self._card_day != day
        if not first_of_day and text == self._card_full:
            return
        if (not first_of_day and self._card_full is not None
                and text == without_quiet_blocks(self._card_full)):
            # only a reach-out (or the retired intent slot) went away: no row
            self._remember_card(text, day)
            return
        if first_of_day or self._card_full is None:
            row = text
        else:
            row = render_state_update(self._card_full, text)
        self._persist_row("system", row, t_h, day)
        self._remember_card(text, day)

    def _remember_card(self, text: str, day: int) -> None:
        self._card_full = text
        self._card_day = day
        try:
            self.store.set_kv("state_card.full", text)
            self.store.set_kv("state_card.day", str(day))
        except AttributeError:
            pass

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
            agenda=tuple(self.store.list_agenda_items(day=day)),
            life_arcs=tuple(life.started_arcs(self._life_arcs, day)),
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

        Not the per-turn card (it re-sent unchanged text every turn) and not
        the system prefix (the agenda mutates as windows pass): appended
        once, it sits inside the cached prefix for the rest of the day. A
        persisted marker keeps a restart mid-day from repeating it.
        """
        if self._emitted(self.DAY_START_EMITTED, day):
            return
        block = render_day_start_block(
            snapshot, t_h=t_h, anchor=self._real_time_anchor()
        )
        if not block:
            return
        self._persist_row("system", block, t_h, day)
        self.store.log_event(day, t_h, self.DAY_START_EMITTED, f"{len(block)}chars")

    TURN_STYLE_EMITTED = "turn_style_row"

    #: Presence mode (owner, 2026-10-03): "apart" (default, texting) or
    #: "together" (he is physically there). The mode picks the writing
    #: register; /here and /leave flip it.
    PRESENCE_KEY = "life.presence"
    PRESENCE_APART = "apart"
    PRESENCE_TOGETHER = "together"

    def presence_mode(self) -> str:
        """The current presence mode; anything unrecognized reads as apart.
        Minimal stores without kv (tests/fakes) read as apart too."""
        try:
            value = self.store.get_kv(self.PRESENCE_KEY)
        except AttributeError:  # minimal stores (tests/fakes) keep working
            return self.PRESENCE_APART
        return (self.PRESENCE_TOGETHER if value == self.PRESENCE_TOGETHER
                else self.PRESENCE_APART)

    def set_presence(self, mode: str) -> str:
        """Set the presence mode (the /here and /leave hooks); returns the
        reply line. A flip writes the mode row into the stream — the
        voice-call rows' pattern — so the register swaps from the next
        message on."""
        if mode not in (self.PRESENCE_APART, self.PRESENCE_TOGETHER):
            return f"unknown presence mode '{mode}'."
        if self.presence_mode() == mode:
            if mode == self.PRESENCE_TOGETHER:
                return "he is already here — nothing changed."
            return "you were already apart — nothing changed."
        self.store.set_kv(self.PRESENCE_KEY, mode)
        day = self.current_day
        if day is not None:
            t_h = self.clock.now_h()
            row = (PRESENCE_START_ROW if mode == self.PRESENCE_TOGETHER
                   else PRESENCE_END_ROW)
            self._persist_row("system", row.format(time=self._clock_text(t_h)),
                              t_h, day)
            self.store.log_event(day, t_h, "presence_changed", f"mode={mode}")
        if mode == self.PRESENCE_TOGETHER:
            return "he is here now — scenes run in person from the next message."
        return "he left — back to texting."

    def _emit_turn_style_row(self, day: int, t_h: float) -> None:
        """Store the writing block once per day's stream, right after the
        day's plan (owner, 2026-09-28: "It needs to be sent once, at the
        start, not attached to every message"). Compaction happens at day
        rollover, so once per day is once per stream. A persisted marker keeps
        a restart mid-day from repeating it. ``HARNESS_TURN_STYLE=0`` turns it
        off for an A/B. The register follows the presence mode: a day that
        starts with him there opens with the together block."""
        if (not _env_bool("HARNESS_TURN_STYLE", True)
                or self._emitted(self.TURN_STYLE_EMITTED, day)):
            return
        style = (TURN_STYLE_TOGETHER
                 if self.presence_mode() == self.PRESENCE_TOGETHER
                 else TURN_STYLE)
        self._persist_row("system", style, t_h, day)
        self.store.log_event(day, t_h, self.TURN_STYLE_EMITTED, "")

    def _emitted(self, event: str, day: int) -> bool:
        """Whether ``event`` (a once-a-day row marker) was logged for ``day``."""
        try:
            return any(
                ev.get("event") == event and int(ev.get("day", -1)) == day
                for ev in self.store.events_since(day)
            )
        except Exception:  # noqa: BLE001 - the audit lane never breaks a turn
            return False

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
        meta: dict | None = None,
    ) -> int:
        """Persist one message; returns its row id. ``intent_id`` is the
        EXACT intent an outgoing proactive message fired for. Stores without
        ``conversation_id`` (the seam fake) are not passed it."""
        kwargs: dict = {"proactive": proactive, "session_id": session_id,
                        "intent_id": intent_id}
        if self._accepts_conversation_id:
            kwargs["conversation_id"] = conversation_id
        if meta:
            kwargs["meta"] = meta
        return self.store.add_message(role, content, t_h, day, **kwargs)

    def _persist_row(self, role: str, content: str, t_h: float, day: int, *,
                     proactive: bool = False, meta: dict | None = None) -> int:
        """Persist a row into the open conversation, if any."""
        conv = self._conversation
        return self._persist_message(
            role, content, t_h, day, proactive=proactive,
            session_id=self._memory_session_id(conv.id) if conv else "",
            conversation_id=conv.id if conv else None, meta=meta,
        )

    def _record_channel_text(
        self, text: str, t_h: float, *, decision_id: str | None = None,
    ) -> bool:
        """Append a message she sends outside a turn (a heads-up, a clock-
        driven departure) as her own proactive row, at the instant it goes out.

        What reaches him is what the stream holds: the next turn, the memory
        fork and the viewer all read it back as something she said. Returns
        False when this decision's text is already in the stream (a restart
        replaying a recorded decision), so the caller does not send it twice.
        """
        stamp = self._stream_stamp(t_h, _LANE_ASSISTANT)
        day = int(stamp // 24.0)
        if decision_id is not None and decision_id in self._sent_decision_ids():
            return False
        # No conversation_turns row: a turn in flight holds its own copy of
        # the conversation and numbers its turns from it.
        self._persist_row(
            "assistant", text, stamp, day, proactive=True,
            meta={"decision_id": decision_id} if decision_id else None,
        )
        return True

    def _sent_decision_ids(self, messages: list[dict] | None = None) -> set[str]:
        """Decision ids whose text rides the stream as her own message
        (``_record_channel_text``); read from the epoch's rows by default."""
        if messages is None:
            messages = self._epoch_messages()
        ids: set[str] = set()
        for m in messages:
            raw = m.get("meta")
            if not raw or m.get("role") != "assistant":
                continue
            try:
                meta = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                continue
            if isinstance(meta, dict) and meta.get("decision_id"):
                ids.add(str(meta["decision_id"]))
        return ids

    # -- model context projection (messages + decisions, in order) ----- #

    @staticmethod
    def _decision_context_messages(row: dict) -> list[dict] | None:
        """One past decision as a NATIVE tool exchange, or None if unusable.

        The pop-up block(s) it was asked with, then an assistant message
        carrying ``tool_calls`` and the matching ``role="tool"`` result. The
        native form is the precedent that makes the next pop-up unambiguous
        (a prose summary taught nothing: live 2026-09-07 answered three of
        seven pop-ups in prose). A malformed audit row returns None —
        context assembly never breaks on the audit lane.
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
        # The pop-up block(s) it was asked with come first, as sent, under the
        # same key: saved with the pair and pruned with it (owner ruling
        # 2026-10-03). Older rows, and server draws, have none.
        try:
            blocks = json.loads(row.get("popup_json") or "[]")
        except (TypeError, ValueError):
            blocks = []
        asked = [{"role": "system", "content": b} for b in blocks
                 if isinstance(b, str) and b] if isinstance(blocks, list) else []
        return [*asked, assistant, result]

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
        when_t = float(row.get("t_h") or 0.0)
        try:
            inputs = json.loads(row.get("inputs_json") or "null")
        except (TypeError, ValueError):
            inputs = None
        if isinstance(inputs, dict) and isinstance(
                inputs.get("boundary_t_h"), (int, float)):
            # A start decision reads back its boundary (the pop-up's Time),
            # which the row's stamp can trail (``_steer_event_popup``).
            when_t = float(inputs["boundary_t_h"])
        when = hhmm(when_t)
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
            elif row.get("popup_kind") == "tool_decide_proactive":
                outcome = ("decided to text him" if verdict.get("initiate")
                           else "decided not to text him")
            elif verdict.get("initiate"):
                outcome = f"started {label}"
            else:
                outcome = f"let {label} pass"
        elif "answer" in verdict:
            outcome = ("picked up his call" if verdict.get("answer")
                       else "let his call ring out")
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

        Called from the day rollover only: dropping messages shifts every
        byte after the drop, so it is paid once a day rather than every turn.
        The NEWEST ``CONTEXT_RETAIN_MESSAGES`` stay; the move is logged
        (``context_compacted``) so audit can explain the prefix break.
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

    def _epoch_messages(self) -> list[dict]:
        """The stored rows from the compaction epoch on."""
        if hasattr(self.store, "messages_since"):
            return self.store.messages_since(self.context_epoch_id(), limit=None)
        return self.store.recent_messages(limit=RECENT_TURNS)  # the seam fake

    def _context_turns(self) -> list[dict]:
        """The model's context stream: transcript turns AND its own decisions.

        APPEND-ONLY by construction: read from the compaction epoch
        (:meth:`context_epoch_id`), never as "the last N rows" —
        front-truncation moves the first byte every turn and nothing after
        the system message is reused. Merged by ``(t_h, lane, id)`` so a turn
        reads in its real order: his message, the decisions taken at that
        boundary (as native tool pairs), then her reply.
        """
        messages = self._epoch_messages()
        floor = float(messages[0].get("t_h", 0.0)) if messages else 0.0
        decisions: list[dict] = []
        try:
            if hasattr(self.store, "decisions_since"):
                # Epoch-wide, then filtered by DELIVERY time below: a decision
                # stamped before the stream's first row can still belong here.
                decisions = self.store.decisions_since(0.0)
        except Exception:  # the audit lane must never break a turn
            decisions = []
        if not decisions:
            return self._cold_prune([(("m", int(m.get("id", 0))), m) for m in messages])
        # A decision whose text went out as her message (a heads-up) is in
        # the stream as that message; replaying it as a tool pair too would
        # say it twice.
        sent = self._sent_decision_ids(messages)
        rows: list[tuple[float, int, int, dict, tuple]] = []
        for m in messages:
            rows.append((float(m.get("t_h", 0.0)), _message_lane(m.get("role")),
                         int(m.get("id", 0)), m, ("m", int(m.get("id", 0)))))
        for d in decisions:
            if d.get("replay_id") in sent:
                continue
            t = float(d.get("t_h") or 0.0)
            if float(d.get("delivered_t_h") or t) < floor:
                continue    # rode a stream that predates this epoch
            if t < floor:
                # Boundary stamped before the stream's first row (boot after
                # the boundary): the pair lands at the stream head, in order.
                t = floor
            pair = self._decision_context_messages(d)
            if pair is None:
                continue
            # The pair must stay adjacent and in order: a provider rejects an
            # assistant tool_calls message that is not followed by its result.
            # Up to two pop-up blocks ride ahead of it (block + re-ask nudge).
            for offset, message in enumerate(pair):
                rows.append((t, _LANE_DECISION, int(d.get("id", 0)) * 8 + offset,
                             message, ("d", int(d.get("id", 0)))))
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        return self._cold_prune([(r[4], r[3]) for r in rows])

    def _cold_prune(self, rows: list[tuple[tuple, dict]]) -> list[dict]:
        """The stream minus what cold points pruned; decides a new prune when
        this request starts one.

        A cold point is the first request after ``COLD_PRUNE_AFTER_H`` of no
        model call at all: the provider has dropped the cached prefix by then,
        so shrinking the stream costs no cache and every later request is
        cheaper. It drops (replay arm B, 2026-09-29: -30% cost, verdicts and
        the tool habit kept):
          - state cards (full or update) older than the newest full card;
          - decision tool pairs from before his last message.
        Her and his messages, the day rows, plan changes and call rows stay.

        The dropped rows are keyed ``("m", message id)`` / ``("d", decision
        id)`` and persisted with the gap they were decided for, so every lane
        (pop-up, forks, call) and a restart read the same stream, and a gap
        prunes once. Between cold points the stream is append-only again.
        ``HARNESS_COLD_PRUNE=0`` turns new prunes off.
        """
        if not hasattr(self.store, "get_kv"):
            return [m for _, m in rows]
        try:
            state = json.loads(self.store.get_kv(CONTEXT_PRUNED_KEY) or "{}")
        except ValueError:
            state = {}
        dropped = {tuple(k) for k in state.get("dropped", [])}
        rows = [(k, m) for k, m in rows if k not in dropped]
        now = self.clock.now_h()
        last = self.store.last_llm_call_t_h(now)
        if (last is None or now - last < COLD_PRUNE_AFTER_H
                or state.get("gap") == last
                or not _env_bool("HARNESS_COLD_PRUNE", True)):
            return [m for _, m in rows]
        full = [i for i, (_, m) in enumerate(rows) if m.get("role") == "system"
                and str(m.get("content") or "").startswith(AFFECTIVE_HEADER)]
        users = [i for i, (_, m) in enumerate(rows) if m.get("role") == "user"]
        new: set[tuple] = set()
        for i, (k, m) in enumerate(rows):
            text = str(m.get("content") or "")
            if (full and i < full[-1] and k[0] == "m" and m.get("role") == "system"
                    and (text.startswith(AFFECTIVE_HEADER) or is_state_update(text))):
                new.add(k)
            elif users and i < users[-1] and k[0] == "d":
                new.add(k)
        self.store.set_kv(CONTEXT_PRUNED_KEY, json.dumps(
            {"gap": last, "dropped": sorted(dropped | new)}))
        if new:
            # a sanctioned prefix break: the runtime witness starts over
            self._last_request_pairs.clear()
            self.store.log_event(
                int(now // 24.0), now, "context_pruned",
                json.dumps({"idle_h": round(now - last, 2),
                            "cards": sum(k[0] == "m" for k in new),
                            "decisions": sum(k[0] == "d" for k in new)},
                           sort_keys=True))
        return [m for k, m in rows if k not in new]

    def _stream_floor(self) -> tuple[float, int] | None:
        """``(t_h, lane)`` of the row that sorts last in the stream
        (``_context_turns``'s merge), or None for an empty stream."""
        try:
            messages = self._epoch_messages()
            decisions = (self.store.decisions_since(0.0)
                         if hasattr(self.store, "decisions_since") else [])
        except Exception:  # noqa: BLE001 - the audit lane never breaks a turn
            return None
        keys = [(float(m.get("t_h", 0.0)), _message_lane(m.get("role")))
                for m in messages]
        head = float(messages[0].get("t_h", 0.0)) if messages else 0.0
        for d in decisions:
            t = float(d.get("t_h") or 0.0)
            if float(d.get("delivered_t_h") or t) < head:
                continue
            keys.append((max(t, head), _LANE_DECISION))
        return max(keys) if keys else None

    def _stream_stamp(self, at: float, lane: int) -> float:
        """The time to stamp a row decided now for instant ``at``: ``at``,
        or the last stream row's time when ``at`` is earlier.

        Rule 3: a row stamped before a row the model has already been sent
        sorts in front of it and rewrites the request's middle. A tie that
        would still sort first (a decision at the instant of her reply) moves
        on by ``CALL_TICK_H``, and the clock with it. What the row SHOWS
        (a boundary time) is the caller's business.
        """
        floor = self._stream_floor()
        if floor is None:
            return at
        floor_t, floor_lane = floor
        stamp = max(at, floor_t)
        if stamp == floor_t and lane < floor_lane:
            stamp = floor_t + CALL_TICK_H
            now = self.clock.now_h()
            if stamp > now:
                self.clock.advance_hours(stamp - now)
        return stamp

    def _drain_steers(
        self, day: int, t_h: float, turn_id: str, *, own=None,
    ) -> _SteerDrain:
        """Apply the steers pending at this turn's idle boundary.

        One pop-up per turn; the rest defer (owner ruling 2026-10-03): the
        first decision steer that reaches the model takes the turn's one
        call, and every later decision steer — negotiation decide legs
        included — goes back to the queue unasked, its attempts untouched. A
        steer that settles without a model call (a replayed verdict, an
        expired fire) does not use the call. ``own`` picks the steer this
        turn exists for (its proactive fire, its ringing call); it goes first.
        His held messages are not decisions and always drain.

        SUPPRESS kills the ordinary reply, INJECT adds a marked block to the
        request, anything else is side-effect only. Only INJECT steers stay
        in ``_turn_drained`` (the requeue set).
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
        if own is not None:
            drained.sort(key=lambda st: not own(st))   # stable: own first
        self._turn_drained = [s.steer_id for s in drained]
        # A verdict resolving during this drain needs the turn to speak for
        # it (see NegotiationMixin._active_drain / GO_NOTE).
        self._active_drain = drain
        calls_before = self._decision_calls
        deferred: list[int] = []
        try:
            for steer in drained:
                if (steer.kind in DECISION_KINDS
                        and self._decision_calls > calls_before):
                    # The turn's one pop-up is spent: this one waits.
                    self._steering.release(steer.steer_id)
                    self._turn_drained.remove(steer.steer_id)
                    deferred.append(steer.steer_id)
                    continue
                outcome = self._apply_steer(steer, day=day, t_h=t_h, drain=drain)
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
                day, t_h, active_before=active_before,
                calls_before=calls_before,
            ):
                drain.suppress_reply = True
            if deferred:
                self.store.log_event(
                    day, t_h, "popups_deferred",
                    f"turn={turn_id} steers={deferred}",
                )
        except BaseException:
            for steer_id in self._turn_drained:
                self._steering.requeue(steer_id)
            self._turn_drained = []
            raise
        finally:
            self._active_drain = None
        return drain


    def settle_pending(self, reason: str) -> "_SteerDrain":
        """Resolve pending events on her own clock: no user, no reply.

        Called at startup and at every event instant by the runtime's life
        loop. The day block and the state card are in place BEFORE the
        resolutions run — she decides knowing the day she acts on. No reply
        is generated; what the resolutions themselves wrote to him (a
        heads-up) is already in the stream and comes back in the drain's
        ``proactive_out`` and ``notices`` for the runtime to send.
        """
        if self._call is not None:
            # A voice call is on: nothing settles until it ends (settling
            # makes model calls and writes to the stream).
            return _SteerDrain()
        t_h = self.clock.now_h()
        day = self.clock.day()
        # A restart past midnight: each day the bot was down through drains
        # its own events first, at its last instant, and only then is
        # judged — rolling straight to today judged it before its events
        # ran, and today's drain never looks at yesterday's agenda.
        while self.current_day is not None and self.current_day < day:
            # Its last instant, or later when rows already run past it (a
            # voice call that ran over midnight): the day-end rows sort
            # after the call's (rule 3).
            end = self._stream_stamp(
                (self.current_day + 1) * 24.0 - DAY_END_EPS_H, _LANE_SYSTEM,
            )
            self._settle_at(self.current_day, end, f"{reason}:day-end")
            self.ensure_day(self.current_day + 1)
        self.ensure_day(day)
        return self._settle_at(day, t_h, reason)

    def _directive(self, day: int, hour: float, *, wind_down: bool = True,
                   ) -> tuple[BehaviorDirective, GenerationControls, BehaviorBrief]:
        """Today's behavior at ``hour``: the directive, its controls (with
        the wind-down guidance while a two-phase close is pending, unless
        ``wind_down`` is off) and its brief."""
        assert self.current_record is not None
        directive = derive_behavior(
            self.current_record, self.timing, hour=hour,
            previous=self._records.get(day - 1),
        )
        controls = controls_from_directive(directive)
        if (wind_down and self.two_phase_close
                and self._closing_pending_t_h is not None):
            controls = replace(controls, closing_guidance=WIND_DOWN_GUIDANCE)
        return directive, controls, to_brief(directive)

    def _cache_day_block(self, snapshot, day: int) -> None:
        """Render the day's persona block once per day; the stable system
        is built from it all day."""
        if self._day_block is None or self._day_block_day != day:
            self._day_block = render_day_block(snapshot)
            self._day_block_day = day

    def _settle_at(self, day: int, t_h: float, reason: str) -> "_SteerDrain":
        """:meth:`settle_pending`'s body for one instant of ``day``."""
        self.check_conversation_lifecycle(t_h)
        directive, _controls, brief = self._directive(day, t_h % 24.0)
        snapshot = self._build_snapshot(day, t_h, brief=brief, intent=None, query=None)
        self._cache_day_block(snapshot, day)
        # Agenda first: the resolutions below must know what they act on.
        self._emit_day_start_block(snapshot, day, t_h)
        self._emit_turn_style_row(day, t_h)
        self._ensure_state_card(
            snapshot, directive.prompt_brief, day, t_h,
        )
        self._context_turns()   # a cold point prunes here, before any pop-up
        self._last_stable_system = _with_bubble_instruction(
            stable_system(self._day_block, snapshot))

        turn_id = f"settle#{day}@{t_h:.5f}"
        # Windows transition at turns, not here: marking a window completed
        # before the firing loop surveys an overdue event erases the
        # grounding its recovery needs (2026-09-13).
        drain = self._drain_steers(day, t_h, turn_id)
        # No turn follows a settle: a go verdict that asked the turn to speak
        # for it closes the conversation here, and she leaves without words
        # (a heads-up already said she would go).
        if drain.close_after is not None and self._conversation is not None:
            self._close_conversation(self._conversation, t_h, drain.close_after)
        self.store.log_event(
            day, t_h, "settle",
            f"reason={reason} notices={len(drain.notices)} "
            f"sent={len(drain.proactive_out)} "
            f"unspoken={len(drain.decided_notes)}",
        )
        return drain

    def life_instants_enabled(self) -> bool:
        """Whether agenda wake instants exist at all (decision-layer gate)."""
        return self._steering is not None and self._decision_enabled

    def next_event_instant(self, now: float) -> float | None:
        """Earliest future agenda instant her life loop must wake for.

        An item start, an item end, or a heads-up lead — the same instants
        ``_enqueue_event_popups`` fires on, surveyed forward instead of
        crossed. None means nothing ahead (the loop polls).
        """
        if not self.life_instants_enabled():
            return None
        day = self.clock.day()
        prev = self._last_boundary_check()
        candidates: list[float] = []
        for it in self.store.list_agenda_items(day=day):
            if it.status != "planned" or it.source_type in life.BACKGROUND_SOURCES:
                continue
            # A heads-up exists only while its start is still genuinely ahead.
            if it.start_t_h > now:
                hu = it.start_t_h - HEADS_UP_LEAD_H
                if hu > now and (prev is None or hu > prev):
                    candidates.append(hu)
            for at in (it.start_t_h, it.end_t_h):
                if at > now and (prev is None or at > prev):
                    candidates.append(at)
        return min(candidates) if candidates else None

    def _reask_for_reply(
        self, messages: list, system: str, max_tokens: int | None,
    ) -> tuple[str, object | None]:
        """One bounded re-ask when a generation came back without prose.

        Extends the SAME request once with ``REPLY_NUDGE``. A second empty
        answer returns ``("", None)`` and the caller drops the turn.
        """
        nudged = append_system([dict(m) for m in messages], REPLY_NUDGE)
        try:
            reply, _reasoning, usage, _raw = self._generate(
                nudged, system, max_tokens
            )
        except Exception:  # noqa: BLE001 - a failed re-ask drops the turn
            return "", None
        return reply or "", usage

    def _face_for_untagged(self, messages: list, system: str, said: str,
                           day: int, t_h: float) -> str | None:
        """The face for a reply she wrote without its tag.

        Extends the SAME request once more: her reply plus ``\n\nSprite:``
        as an assistant prefix (same system, tools and reasoning effort, so
        it rides the turn's cached prefix), and reads the one name she
        writes. Stored as her row's tag, it is what gets her history tagged
        until she tags on her own (harness.faces). Some gateway backends
        ignore the prefix and start reasoning (amadeus memory, 2026-09-29):
        a pick with no name is asked once more on a fresh session id. Only
        clients that declare ``supports_prefix`` are asked. No ``max_tokens``:
        a capped reasoning call returns nothing (see _load_thinking_effort).
        """
        client = self.client
        if not getattr(client, "supports_prefix", False) or not said.strip():
            return None
        prefill = [*messages, {"role": "assistant",
                               "content": said + FALLBACK_PREFIX, "prefix": True}]
        face = None
        for attempt in range(2):
            try:
                with (fresh_session(client, f"face{attempt}") if attempt
                      else contextlib.nullcontext()):
                    result = client.chat_with_meta(
                        prefill, system=system, temperature=0.0,
                        max_tokens=None,
                        reasoning_effort=self._thinking_effort,
                        tools=TOOL_PAYLOAD,
                    )
            except Exception:  # noqa: BLE001 - no face is a valid outcome
                break
            face = face_from_continuation(result.content)
            self.store.log_llm_call(
                day, t_h, "face_pick", FALLBACK_PREFIX, result.content,
                getattr(client, "model", None),
                {"attempt": attempt, "face": face},
                **self._usage_kwargs(getattr(result, "usage", None),
                                     getattr(result, "raw_cost", None)),
            )
            if face is not None:
                break
        self.store.log_event(day, t_h, "face_fallback",
                             json.dumps({"face": face}, sort_keys=True))
        return face

    def _generate(self, messages: list, system: str,
                  max_tokens: int | None, *, tools: list | None = TOOL_PAYLOAD):
        """Run the turn's LLM call; return (reply, reasoning, usage, raw_cost).

        A client without ``chat_with_meta`` yields None for the three extras.
        A reply with neither prose nor a tool call raises ``EmptyReply``
        (the client already retried empties). Tool-call markup is stripped
        from the prose, and a reply that is only markup raises
        (``_reject_tool_markup``).
        """
        self._last_request_tools = tools
        tool_calls = None
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
                tools=tools,
            )
            reply = result.content
            reasoning = result.reasoning
            usage = getattr(result, "usage", None)
            raw_cost = getattr(result, "raw_cost", None)
            tool_calls = getattr(result, "tool_calls", None)
        if not reply.strip():
            # Tool calls with no prose are legal (an unrequested verdict); the
            # caller decides what they mean. Neither prose nor a call is a
            # failure.
            if tool_calls:
                return "", reasoning, usage, raw_cost
            raise EmptyReply(
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
                         max_tokens: int | None, *, tools: list | None = TOOL_PAYLOAD):
        """Streamed generation: (reply, reasoning, usage, raw_cost, bubbles).

        With HARNESS_BUBBLE_STREAM on and a client with ``chat_stream``, a
        :class:`BubbleStreamer` releases each bubble as its boundary parses;
        otherwise this is :meth:`_generate` with no bubbles. The joined raw
        stream is the persisted reply. ``chat_stream`` yields text only, so
        reasoning, usage and cost are None (the ledger stores NULLs).
        ``bubbles`` is a tuple when at least two parsed, else None.
        """
        from harness.bubbles import BubbleStreamer, bubble_stream_enabled, parse_bubbles

        self._last_request_tools = tools
        chat_stream = getattr(self.client, "chat_stream", None)
        if chat_stream is None or not bubble_stream_enabled():
            # A direct caller, or no streaming surface: the canonical
            # non-streaming path, byte-identical reply.
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
            messages, system=system, max_tokens=max_tokens, tools=tools,
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
            raise EmptyReply(
                "refusing to persist empty assistant reply (stream carried "
                "no content)"
            )
        cleaned = self._reject_tool_markup(reply)
        if cleaned != reply:
            reply, parts = cleaned, parse_bubbles(cleaned)
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
        event: str | None = None,
        stamp_t_h: float | None = None,
        photos: list[str] | None = None,
    ) -> TurnResult:
        """Shared path for reactive and proactive messages.

        ``event`` names an event-started turn with no user message and no
        intent (``"incoming_call"``); it opens the conversation as his and
        skips the generic proactive opening. ``stamp_t_h`` is the arrival
        time of a message held during a voice call: the row is stored now
        and stamped with that time. ``photos`` are the stored references of
        the photos sent with ``user_text`` (:mod:`harness.images`).

        ``user_text=None`` means she initiates: no trailing user request,
        grounded on the EXACT ``intent`` (resolved by id in
        ``fire_proactive``), whose id the outgoing message persists.

        This is the IDLE boundary of the steering queue: pending steers are
        drained here; decision pop-ups run through the DecisionRunner, a
        no-reply verdict suppresses the ordinary reply, and non-decision
        steers ride the request as marked system blocks. The request reads
        ``_context_turns`` (messages AND past decisions, from the compaction
        epoch), so it only grows between boundaries.
        """
        t_h = self.clock.now_h()
        day = self.clock.day()
        self.ensure_day(day)
        assert self.current_record is not None
        # Close a stale open conversation before this turn (quiet
        # boundary crossed or user silence); this message opens a new one.
        self.check_conversation_lifecycle(t_h)

        directive, controls, brief = self._directive(day, self.clock.local_hour())

        # One conversation per exchange run; the memory session id is
        # the conversation id.
        conv = self._ensure_conversation(
            t_h,
            opened_by=(
                "user" if user_text is not None or event == "incoming_call"
                else "companion"
            ),
        )
        conv_id = conv.id
        session_id = self._memory_session_id(conv_id)
        turn_id = self._turn_id(conv)

        query = user_text
        if user_text is None and intent is not None:
            query = intent.hook

        snapshot = self._build_snapshot(day, t_h, brief=brief, intent=intent, query=query)
        self._cache_day_block(snapshot, day)
        # Day-scoped state enters the STREAM once, before the context is read
        # below, so it rides inside the cached prefix for the rest of the day.
        self._emit_day_start_block(snapshot, day, t_h)
        self._emit_turn_style_row(day, t_h)
        self._ensure_state_card(
            snapshot, directive.prompt_brief, day, t_h,
        )
        # Read before his message lands: a cold point prunes here.
        self._context_turns()
        stable = _with_bubble_instruction(stable_system(self._day_block, snapshot))
        if user_text is None and intent is None and event is None:
            # Ungrounded proactive call (legacy callers/tests): a generic
            # opening without any invented source claim.
            stable += "\n\n" + proactive_block()
        # The stable half the pop-up and fork calls extend.
        self._last_stable_system = stable
        if user_text is not None:
            user_meta: dict = {}
            if stamp_t_h is not None:
                user_meta["stamp_t_h"] = stamp_t_h
            if photos:
                user_meta["images"] = list(photos)
            mid = self._persist_message(
                "user", user_text, t_h, day,
                proactive=False, session_id=session_id, conversation_id=conv_id,
                meta=user_meta or None,
            )
            conv = self._record_turn(
                conv, "user", user_text, t_h, message_id=mid
            )

        # Drain pending steers into this turn; no-reply verdicts
        # suppress the reply, and delivered steers re-queue on error.
        def own(steer: Steer) -> bool:
            if steer.kind == KIND_PROACTIVE:
                return (intent is not None
                        and steer.payload.get("intent_id") == intent.id)
            return steer.kind == KIND_INCOMING_CALL and event == "incoming_call"

        drain = self._drain_steers(day, t_h, turn_id, own=own)

        # Agenda status transitions as windows pass, persisted via the
        # store; runs after the steering drain.
        self._transition_agenda_windows(day, t_h)

        def silent(**flags) -> TurnResult:
            """This turn's result with no reply of hers."""
            self._turn_drained = []
            return TurnResult(
                reply="", directive=directive, day=day,
                hour=self.clock.local_hour(), controls=controls,
                notices=tuple(drain.notices),
                proactive_out=tuple(drain.proactive_out), **flags,
            )

        if event == "incoming_call" and drain.call_answered:
            # She picked up: nothing goes to Telegram. The call-start row
            # opens the call; her first words come from the first call turn.
            if self._ring_cancel is not None and self._ring_cancel.is_set():
                # He hung up while it rang: the call never opens, and the
                # stream says so after her decision (no turn of hers).
                at = self._stream_stamp(t_h, _LANE_SYSTEM)
                self._persist_message(
                    "system",
                    CALL_HUNG_UP_RINGING_ROW.format(time=self._clock_text(at)),
                    at, day, proactive=False, session_id=session_id,
                    conversation_id=conv_id,
                )
                self.store.log_event(day, at, "call_hung_up_ringing",
                                     f"call={self._ringing_call_id}")
                return silent(call_answered=False)
            self._begin_call(self._ringing_call_id or "", day, t_h)
            return silent(call_answered=True)

        undecided = intent is not None and intent.id in drain.undecided_intents
        if (drain.proactive_declined or undecided) and user_text is None:
            # A declined proactive fire: the decide verdict said no. A verdict
            # that never parsed is not a yes either: live 2026-10-02 sent
            # "[neutral] ..." after the model reasoned "I should not send".
            # Nothing is generated or persisted — only the decision
            # record (written by the runner) and the suppressed intent
            # status remain. Reactive turns ignore the decline: a stray
            # proactive steer never kills a user's own message.
            self.store.log_event(
                day, t_h,
                "proactive_undecided_turn" if undecided
                else "proactive_declined_turn",
                f"turn={turn_id}",
            )
            return silent(declined=True)

        if drain.suppress_reply:
            # A no-reply verdict means no ordinary reply; the server
            # notice goes out instead.
            self.store.log_event(
                day, t_h, "decision_no_reply",
                f"turn={turn_id} notices={len(drain.notices)}",
            )
            return silent()

        # Internal events are system-level context, never user messages
        # (design: events=system, decisions=tools). Both blocks FOLD into the
        # trailing state card rather than stacking behind it — see
        # assembler.append_system for why adjacency is the bug.
        # The stream above was read before the drain; rebuild the request
        # now so this turn's decisions ride it as rows, not as prose.
        self._ensure_state_card(
            snapshot, directive.prompt_brief, day, t_h,
        )
        # The epoch bounds the span: a tail limit would re-impose the sliding
        # window the epoch exists to remove.
        _, messages = build_context_messages(
            snapshot, self._context_turns(), None,
            t_h=t_h, anchor=self._real_time_anchor(),
            day_block=self._day_block,
            limit=None,
        )

        if drain.injections:
            messages = append_system(messages, "\n".join(drain.injections))

        if drain.decided_notes:
            # Something she decided during this drain needs saying. The note
            # states WHAT she decided; the generation supplies her words.
            messages = append_system(messages, "\n".join(drain.decided_notes))

        if controls.closing_guidance:
            # Turn-scoped guidance: delivered once, never persisted — so it
            # can never leak into the next conversation's requests.
            messages = append_system(
                messages, f"{CLOSING_HEADER} {controls.closing_guidance}",
            )

        # reasoning_effort passes through HARNESS_THINKING_EFFORT when
        # set; the max_tokens cap is dropped then.
        max_tokens = (
            None if self._thinking_effort is not None else controls.max_tokens
        )
        # With HARNESS_BUBBLE_STREAM on, bubbles parse off the wire
        # (_generate_stream, which falls back to _generate for a client
        # without chat_stream); otherwise they split post-hoc below.
        try:
            if _bubble_stream_on():
                reply, reasoning, usage, raw_cost, streamed_bubbles = (
                    self._generate_stream(messages, stable, max_tokens)
                )
            else:
                reply, reasoning, usage, raw_cost = self._generate(
                    messages, stable, max_tokens
                )
                streamed_bubbles = None
        except EmptyReply:
            # No prose and no call: the bounded re-ask below owes him a reply
            # (live 2026-09-30, message 218 went unanswered when this raised).
            reply, reasoning, usage, raw_cost, streamed_bubbles = (
                "", None, None, None, None
            )
        except BaseException:
            # A dead generation never showed the model the injected steers:
            # hand them back to the next boundary.
            for steer_id in self._turn_drained:
                self._steering.requeue(steer_id)
            self._turn_drained = []
            raise
        self._note_request_prefix("chat", messages)
        if _wordless(reply):
            # A generation without prose owes a reply. One bounded re-ask;
            # a second empty answer is dropped, not persisted.
            streamed_bubbles = None
            reply, re_usage = self._reask_for_reply(messages, stable, max_tokens)
            if re_usage is not None:
                usage = re_usage
            if _wordless(reply):
                self.store.log_event(
                    day, t_h, "empty_reply_dropped",
                    f"turn={turn_id} retried=True",
                )
                return silent()
        # Her face: the tag she opened with, or one continuation that asks
        # for it (harness.faces). Her row keeps it; nobody else sees it.
        face, said = split_face(reply)
        said = said.lstrip()
        if face is None and face_tag_enabled():
            face = self._face_for_untagged(messages, stable, said, day, t_h)
        # Her row keeps every tag she wrote (a later paragraph often opens
        # with its own); what leaves the harness has them all cut.
        mid = self._persist_message(
            "assistant", with_face(face, said), t_h, day,
            proactive=proactive, session_id=session_id, conversation_id=conv_id,
            intent_id=intent.id if intent is not None else None,
        )
        said = strip_face(said)
        conv = self._record_turn(
            conv, "companion", said, t_h, message_id=mid
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
        # under #Thinking; non-reasoning runs store nothing). The provider's
        # own reasoning count rides with it, so "no reasoning" is on record
        # as 0 rather than read off the lengths.
        meta = {"reasoning": reasoning} if reasoning else {}
        if getattr(usage, "reasoning_tokens", None) is not None:
            meta["reasoning_tokens"] = usage.reasoning_tokens
        meta = meta or None
        usage_kwargs = self._usage_kwargs(usage, raw_cost)
        self.store.log_llm_call(
            day,
            t_h,
            "chat",
            # What was sent: the stable system and the request's messages.
            stable + "\n" + repr(messages),
            reply,
            getattr(self.client, "model", None),
            meta,
            **repro_kwargs,
            **usage_kwargs,
        )
        self.store.log_event(day, t_h, "assistant_reply", f"len={len(reply)}")
        self._record_rotations(day, t_h)
        self._turn_drained = []
        # Post-hoc split on the canonical path; the streamed path already
        # parsed incrementally (its bubbles came back from _generate_stream).
        if streamed_bubbles is None:
            bubbles = _split_into_bubbles(said)
        else:
            # the tag rode the first streamed bubble (or was one), and any
            # bubble can open with a tag of its own
            bubbles = tuple(
                b for b in (strip_face(x).strip() for x in streamed_bubbles) if b
            ) or None
        return TurnResult(
            reply=said,
            face=face,
            directive=directive,
            day=day,
            hour=self.clock.local_hour(),
            controls=controls,
            notices=tuple(drain.notices),
            proactive_out=tuple(drain.proactive_out),
            bubbles=bubbles,
            streamed=streamed_bubbles is not None,
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
        """Queue a user message arriving at ``t_h`` for the next boundary,
        where it becomes a ``tool_decide_reply`` pop-up about the event in
        progress.

        With nothing in progress there is nothing to gate, so nothing is
        queued (a queued one rendered the placeholder event ``"?"`` into
        ordinary turns). Returns the steer id, None when the layer is off or
        she is free.
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
        """Queue event pop-ups (heads-up, start, end) for the agenda
        boundaries crossed since the last check.

        The last check is persisted as a ``popup_boundary_check`` event, so a
        restart never re-enqueues a boundary but does replay every boundary
        since — her day ran while nobody watched it. A first boot has no last
        check: her life starts at the boot, and nothing before it is asked.

        Each boundary is enqueued AT ITS OWN INSTANT, the batch sorted by
        it, so a replayed morning reaches the model in the order it happened,
        and ``_omit_backlog_send`` can drop a reach-out about a window that
        closed hours ago.
        """
        if self._steering is None:
            return
        prev = self._last_boundary_check()
        items = self.store.list_agenda_items(day=day)
        now = t_h
        if prev is None:
            self.store.log_event(day, now, "popup_boundary_check",
                                 f"items={len(items)} first_boot")
            return
        if now <= prev:
            return  # already checked at this instant: nothing new can cross
        crossed: list[tuple[float, dict]] = []
        for it in items:
            if it.status != "planned" or it.source_type in life.BACKGROUND_SOURCES:
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
                if prev < at <= now:
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

    def _last_boundary_check(self) -> float | None:
        """When ``_enqueue_event_popups`` last checked, or None (first boot)."""
        prev: float | None = None
        for event in self.store.events_since(0):
            if event.get("event") == "popup_boundary_check":
                prev = float(event["t_h"])
        return prev

    def _steer_user_message(self, steer: Steer, payload: dict, *,
                            day: int, t_h: float, drain: _SteerDrain) -> str:
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
        # The pop-up restates the steer's message only when the transcript
        # does not already end on it. Decided in the same breath it arrived,
        # it IS the row above (the live decide leg showed it twice — ~45
        # chars, and a second copy the model has to reconcile); decided at a
        # later boundary, steps later, the line is the only anchor the
        # decision has and it stays.
        message = str(payload.get("message", ""))
        result = self._execute_decision(
            decision_id=f"steer-{steer.steer_id}",
            popup_kind="tool_decide_reply",
            inputs={
                "event_id": activity.item.id,
                "event_label": activity.item.activity,
                "state_label": "in_progress",
                "time": str(t_h),
                "latest_user_message": (
                    "" if message == self._latest_user_text() else message
                ),
                "conversation_context": self._conversation_context(),
            },
            steer=steer,
            day=day,
            t_h=t_h,
        )
        if result is None:
            return _STEER_CONSUMED  # re-queued: next boundary
        if result.verdict.get("reply") is False:
            drain.notices.append(result.notice or "")
            return _STEER_SUPPRESS
        if result.verdict.get("terminate_event"):
            reason = str(result.verdict.get("reason") or "").strip()
            self._mark_event_closed(
                activity.item.id,
                f"{STOPPED_EARLY}: {reason}" if reason else STOPPED_EARLY,
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
                           drain: _SteerDrain) -> str:
        """An agenda event starting or ending.

        Three boundaries, one decision. HEADS_UP (``HEADS_UP_LEAD_H`` ahead)
        tells her something is about to start, with no verdict. The decision
        is offered ONCE, at START (a negotiation re-offers it only when she
        deferred). END never calls the model: a passed window resolves
        server-side (``life.transition_past_windows``) and a skip already
        closed the item at its start.
        """
        state = str(payload.get("state", "start"))
        item_id = str(payload.get("item_id") or payload.get("event_id") or "")
        if state == "heads_up":
            if item_id:
                self._maybe_heads_up(item_id, day, t_h, steer, drain.proactive_out)
            return _STEER_CONSUMED
        if state == "end":
            return _STEER_CONSUMED
        if item_id and self._maybe_start_negotiation(
            item_id, day, t_h, steer, drain.proactive_out
        ):
            return _STEER_CONSUMED
        # The pop-up shows the model the boundary (`Time: 07:28`) and the
        # tool result says "recorded at 07:28" (``boundary_t_h``), so a
        # replayed morning does not contradict itself. The
        # row itself is stamped no earlier than the last row of the stream
        # (``_execute_decision``): decided after his message, or after a
        # voice call, a boundary stamp sorted it in front of rows the model
        # had already been sent (rule 3 outranks the boundary stamp).
        at = float(payload.get("time", t_h))
        result = self._execute_decision(
            decision_id=f"steer-{steer.steer_id}",
            popup_kind="tool_decide_event",
            inputs={
                "event_id": item_id,
                "event_label": str(payload.get("event") or "?"),
                "state_label": state,
                "time": str(at),
                # What the tool result shows as "recorded at" (not rendered
                # into the pop-up; the row's own stamp can be later).
                "boundary_t_h": at,
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
            # point; nothing asks again at the end boundary. Her reason is
            # about this item, so it is what came of it.
            self._mark_event_closed(item_id, str(verdict.get("reason") or ""))
        if verdict.get("initiate"):
            if self._omit_backlog_send(steer):
                self.store.log_event(
                    day, t_h, "decision_catchup_omit",
                    f"steer={steer.steer_id} event={label} "
                    f"enqueued={steer.t_h:.2f}",
                )
            else:
                # The TURN speaks for it: the verdict's `reason` is engine
                # rationale and stays in decision_records.
                drain.decided_notes.append(START_NOTE.format(activity=label))
        return _STEER_CONSUMED

    def _apply_steer(
        self,
        steer: Steer,
        *,
        day: int,
        t_h: float,
        drain: _SteerDrain,
    ) -> str:
        """Apply one delivered steer at a boundary; returns a ``_STEER_*``
        outcome: INJECT (no decision attached, or the layer is off: render
        the block into the request), CONSUMED (handled, or re-queued), or
        SUPPRESS (a no-reply verdict; its notice is in ``drain.notices``).
        """
        if self._decision is None:
            return _STEER_INJECT
        payload = steer.payload or {}
        kind = steer.kind
        if kind == KIND_USER_MESSAGE:
            return self._steer_user_message(
                steer, payload, day=day, t_h=t_h, drain=drain
            )
        if kind == KIND_EVENT_POPUP:
            return self._steer_event_popup(
                steer, payload, day=day, t_h=t_h, drain=drain,
            )
        if kind == KIND_PROACTIVE:
            return self._steer_proactive(
                steer, payload, day=day, t_h=t_h, drain=drain,
            )
        if kind == KIND_INCOMING_CALL:
            return self._steer_incoming_call(
                steer, payload, day=day, t_h=t_h, drain=drain,
            )
        # schedule_fire / day_rollover (or unknown kinds): the harness's own
        # paths own those flows; the block is still rendered as context.
        return _STEER_INJECT

    def _last_user_row(self) -> dict | None:
        """His most recent message among the last dozen rows, or None."""
        return next((m for m in reversed(self.store.recent_messages(limit=12))
                     if m.get("role") == "user"), None)

    def _latest_user_text(self) -> str:
        """Most recent user message text, or '' when none exists."""
        row = self._last_user_row()
        return str(row.get("content") or "") if row is not None else ""

    def _silence_hours(self, t_h: float) -> float | None:
        """Hours since the last user turn, or None when unknown."""
        conv = self._conversation
        anchor = self._last_user_turn_t_h(conv) if conv is not None else None
        if anchor is None:
            row = self._last_user_row()
            if row is None:
                return None
            anchor = float(row.get("t_h", t_h))
        return max(0.0, t_h - anchor)

    def _steer_proactive(
        self,
        steer: Steer,
        payload: dict,
        *,
        day: int,
        t_h: float,
        drain: _SteerDrain,
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
            self.store.log_event(
                day, t_h, "proactive_no_intent",
                f"steer={steer.steer_id} intent={intent_id!r}",
            )
            return _STEER_CONSUMED
        if intent.valid_until_t_h < t_h:
            # The fire's moment is gone: asking now decides a different moment
            # (live 2026-10-02 re-asked steer 83 twice after its window).
            self._steering.abandon(steer.steer_id)
            self.store.log_event(
                day, t_h, "proactive_expired",
                f"steer={steer.steer_id} intent={intent.id}",
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
            # Unparsed: this fire is not sent, and it is not re-asked at a
            # later boundary — a later yes would be about a different moment.
            self._steering.abandon(steer.steer_id)
            drain.undecided_intents.add(intent.id)
            self.store.log_event(
                day, t_h, "proactive_undecided",
                f"steer={steer.steer_id} intent={intent.id}",
            )
            return _STEER_CONSUMED
        if result.verdict.get("initiate"):
            drain.decided_intents.append(intent)
            return _STEER_CONSUMED
        self.store.update_proactive_intent_status(intent.id, "suppressed")
        self.store.log_event(
            day, t_h, "proactive_declined",
            f"steer={steer.steer_id} intent={intent.id}",
        )
        drain.proactive_declined = True
        return _STEER_CONSUMED

    def _decision_model_call(self, request: PopupRequest) -> RawReply:
        """The runner's model callable: one pop-up request, one reply."""
        self._decision_calls += 1
        return self._popup_request_call(request)

    def _record_rotations(self, day: int, t_h: float) -> None:
        """Save each gateway session rotation since the last call: the new id
        under ``KV_SESSION_ID`` (a restart stays on it) and an event."""
        for rotation in gateway.WATCH.drain():
            if (hasattr(self.store, "get_kv") and rotation.old is not None
                    and self.store.get_kv(gateway.KV_SESSION_ID) == rotation.old):
                self.store.set_kv(gateway.KV_SESSION_ID, rotation.new)
            self.store.log_event(day, t_h, "gateway_session_rotated",
                                 json.dumps({"old": rotation.old, "new": rotation.new,
                                             "reason": rotation.reason}))

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
        t_h = self._stream_stamp(t_h, _LANE_DECISION)
        self._popup_blocks = []
        try:
            result = self._decision.execute(
                decision_id,
                popup_kind,
                inputs,
                Capabilities(
                    has_native_tools=bool(
                        getattr(self.client, "supports_tools", False)
                    )
                ),
                self._decision_model_call,
                day=day,
                t_h=t_h,
                delivered_t_h=steer.delivered_t_h,
            )
        except DecisionRequeue:
            gateway.WATCH.note_decision(False)
            self._record_rotations(day, t_h)
            self._steering.requeue(steer.steer_id)
            return None
        if not result.from_replay and (result.source == "model" or result.parse_failed):
            gateway.WATCH.note_decision(not result.parse_failed)
        self._record_rotations(day, t_h)
        # Pop-up blocks are saved: the decision replays behind the block(s) it
        # was asked with, so the next request extends this one, and only the
        # cold-point prune drops them, with the pair (owner ruling 2026-10-03).
        if (self._popup_blocks and not result.from_replay
                and result.record_id is not None
                and hasattr(self.store, "set_decision_popup")):
            self.store.set_decision_popup(result.record_id, self._popup_blocks)
        self._popup_blocks = []
        return result

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

        The request's tools live in the call's own wire dump, which is where
        the ledger reads them from; the decode controls ride by value here.
        """
        return {"repro": {
            "model": getattr(self.client, "model", None),
            "system": self._last_stable_system,
            "messages": messages,
            "max_tokens": None,
            "temperature": 0.8,
            "json_mode": False,
            "reasoning_effort": self._thinking_effort,
            "tool_choice": None,   # never sent: see _popup_request_call
            "popup_kind": request.popup_kind,
            "timestamp": {"day": day, "t_h": t_h},
        }}

    def _stable_system_now(self) -> str:
        """The stable system as the next mainline turn will send it.

        Base-prefix rule (owner ruling, 2026-09-13): every request starts
        from the system prompt. A call that runs before any turn ever has
        (the day-0 planner at boot) renders the base prefix here — the same
        value ``render_day_block`` would produce, cached exactly as the
        mainline caches it — so the first turn's bytes are already fixed and
        its system is already warm on the provider cache.
        """
        day = self.clock.day()
        if self._day_block is None or self._day_block_day != day:
            profile = self._profile
            core = profile.core if profile is not None else self.persona_core
            self._day_block = (core or DEFAULT_PERSONA_CORE).strip()
            self._day_block_day = day
        return _with_bubble_instruction(stable_system(self._day_block))

    def _aux_task_request(self, task_text: str):
        """The request pair for one auxiliary task (owner ruling, 2026-09-13).

        The fork rule: an auxiliary call READS the exact request the last
        mainline turn sent — same stable system, same stamped stream, same
        card, and the same tool schemas — and folds its task into the
        trailing system block. The schemas ride because a provider banks the
        prompt as one token stream (system block, schemas, conversation):
        the fork used to drop them, so the prefix broke at the schemas and
        never banked the conversation (live 2026-09-20: the nightly judge
        hit 7% while its no-tool sibling hit 88% off the same bytes). Only
        the request bytes are shared; the output is engine input and never
        re-enters the stream (judge -> judgement row, planner -> agenda
        text), so nothing about her own context changes.

        Before any turn has ever run there is no request to fork, but the
        base prefix is still the system prompt: the pair renders the stable
        system and keeps the aux one-shot shape — the task as its single
        user-role instruction (ratified 2026-09-12).
        """
        if self._last_stable_system:
            messages = stamped_stream(self._context_turns(), self._real_time_anchor())
            messages = append_system(messages, task_text)
            return self._last_stable_system, messages, self._last_request_tools
        return (self._stable_system_now(), [{"role": "user", "content": task_text}],
                self._last_request_tools)

    def _day_task_request(self, day: int, task_text: str):
        """The request pair for a fork that reads one day only: the memory
        fork (owner ruling 2026-10-03). Same stable system and tool menu as
        her turns, so those bank on the cache; then ``day_view`` of the
        day's rows, stamped as the mainline stamps them, and the task."""
        rows = day_view(self.store.messages_for_day(day),
                        plan_header=self._day_marker(day))
        messages = stamped_stream(rows, self._real_time_anchor())
        messages = append_system(messages, task_text)
        system = self._last_stable_system or self._stable_system_now()
        return system, messages, self._last_request_tools

    def _popup_request_call(self, request: PopupRequest) -> RawReply:
        """One pop-up model call (the callable injected into the runner).

        The call is a byte-identical EXTENSION of the mainline request: the
        turn's stable system and the same stamped stream (through the ONE
        builder — a raw rebuild diverged at the first user message on
        2026-09-12), then the pop-up block as ``role="system"`` (harness
        input, never his words). Native transport offers the constant tool
        menu; textual transport relies on the ``tool_decide_*: {...}``
        marker reply. ``max_tokens`` stays None: a cap must never starve a
        reasoning model (repo pitfall 3af0a5a).
        """
        messages = stamped_stream(self._context_turns(), self._real_time_anchor())
        base = len(messages)
        messages = append_system(messages, wrap_steer_marker(request.popup))
        # A re-ask restates the requirement as its own appended block.
        messages = append_system(messages, request.nudge or "")
        # What this call appended, byte for byte: a decision that comes of it
        # keeps these blocks in the stream (``_execute_decision``).
        self._popup_blocks = [m["content"] for m in messages[base:]]
        # Every schema is offered, one constant menu (owner ruling
        # 2026-09-13): tools are part of the cached prefix and never toggle.
        # The measured cost: the model can answer with another tool (8
        # wrong-tool answers on days 4-5 of the 2026-09-27 run); those are
        # parse failures and count against the retry budget.
        #
        # tool_choice is NOT SENT. The model always thinks, and thinking mode
        # 400s both "required" and a named choice; "auto" buys nothing
        # (measured on the live decide body: omitted parsed 6/6 tool calls,
        # "auto" 5/6, same 97.2% cache). A prose reply therefore stays
        # possible; the retry budget (``steering.MAX_ATTEMPTS``) bounds it and
        # the replayed tool exchanges in context give the precedent.
        result = self.client.chat_with_meta(
            messages,
            system=self._last_stable_system,
            temperature=0.8,
            max_tokens=None,
            tools=TOOL_PAYLOAD if request.native and request.tools else None,
            tool_choice=None,
            reasoning_effort=self._thinking_effort,
        )
        self._note_request_prefix(request.popup_kind, messages)
        self._log_decision_call(request, messages, result)
        return RawReply(text=result.content or None,
                        tool_calls=_openai_tool_calls(result.tool_calls))

    def _log_decision_call(self, request: PopupRequest, messages: list[dict],
                           result) -> None:
        """Meter this pop-up call into ``llm_calls`` like a mainline turn.

        ``role`` is the pop-up kind rather than "chat" so the ledger tells
        the two apart (``harness.audit`` selects ``role='chat'``).
        """
        day = int(self.clock.now_h() // 24.0)
        t_h = self.clock.now_h()
        meta: dict = {"popup_kind": request.popup_kind,
                      "transport": "native" if request.native else "text",
                      "tool_calls": [tc.get("name")
                                     for tc in (result.tool_calls or [])]}
        if result.reasoning:
            meta["reasoning"] = result.reasoning
        if getattr(result.usage, "reasoning_tokens", None) is not None:
            meta["reasoning_tokens"] = result.usage.reasoning_tokens
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
            f"{m['role']}: {strip_face(str(m['content']))[:200]}" for m in recent
        )

    def _mark_event_closed(self, item_id: str, outcome: str | None = None) -> None:
        """Server-side event close (``terminate_event`` verdict / ``abandon``
        action): the item is marked skipped so the state card stops showing
        it as in progress.

        ``outcome`` is her own reason for closing it, recorded as what came
        of the item — the ONLY source of outcomes, since the next day's plan
        follows on from them and must not follow on from a fiction.
        """
        if not item_id:
            return
        self.store.update_agenda_item_status(item_id, "skipped")
        text = (outcome or "").strip()
        setter = getattr(self.store, "set_agenda_item_outcome", None)
        if text and setter is not None:
            setter(item_id, text)

    # -- voice call ------------------------------------------------------ #

    def _load_call(self) -> dict | None:
        get = getattr(self.store, "get_kv", None)
        if get is None:
            return None
        raw = get(CALL_ACTIVE_KEY)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) and value.get("id") else None

    def _save_call(self, call: dict | None) -> None:
        self._call = call
        setter = getattr(self.store, "set_kv", None)
        if setter is not None:
            setter(CALL_ACTIVE_KEY, json.dumps(call) if call else "")

    def call_active(self) -> dict | None:
        """The voice call in progress ({"id", "started_t_h", ...}), or None."""
        return dict(self._call) if self._call else None

    def _call_tick(self) -> float:
        """The time for the next call row: now, and strictly after the row
        before it. Advances the clock when it has to (by microseconds)."""
        now = self.clock.now_h()
        floor = now
        recent = self.store.recent_messages(limit=1)
        if recent:
            floor = max(floor, float(recent[-1].get("t_h") or 0.0) + CALL_TICK_H)
        if self._call and self._call.get("last_t_h") is not None:
            floor = max(floor, float(self._call["last_t_h"]) + CALL_TICK_H)
        if floor > now:
            self.clock.advance_hours(floor - now)
        return self.clock.now_h()

    def _clock_text(self, t_h: float) -> str:
        """HH:MM as he reads it: real local time when anchored."""
        anchor = self._real_time_anchor()
        if anchor is None:
            return hhmm(t_h)
        real = anchor.real_at(t_h)
        return f"{real.hour:02d}:{real.minute:02d}"

    def _call_row(self, role: str, content: str, t_h: float, day: int) -> int:
        mid = self._persist_row(role, content, t_h, day)
        if self._call is not None:
            self._save_call({**self._call, "last_t_h": t_h})
        return mid

    def incoming_call(self, call_id: str, *, cancelled=None) -> TurnResult:
        """He is calling her: an event-started turn with the pick-up decision.

        The ``incoming_call`` steer is decided through the decision layer
        (``tool_decide_call``). Answered: the call-start row is written and
        the result carries ``call_answered`` with no text. Let ring out: the
        same turn writes her text about the missed call, which the runtime
        sends like any reply. Needs the steering queue and the decision
        layer; raises RuntimeError without them.

        ``cancelled`` (anything with ``is_set()``) is his hang-up while it
        rings: set before she picks up, the call does not open and a system
        row says he hung up first. A decline is unaffected (her text about
        the missed call still goes out).
        """
        if self._steering is None or self._decision is None:
            raise RuntimeError("voice calls need the decision layer")
        if self._call is not None:
            raise RuntimeError("a call is already active")
        t_h = self.clock.now_h()
        day = self.clock.day()
        steer_id = self._steering.enqueue(
            KIND_INCOMING_CALL, {"call_id": call_id, "time": t_h}, day, t_h,
        )
        self._ringing_call_id = call_id
        self._ring_cancel = cancelled
        try:
            return self._chat(None, proactive=True, event="incoming_call")
        except BaseException:
            # Never let a dead ringing turn pick the call up later.
            self._steering.abandon(steer_id)
            raise
        finally:
            self._ringing_call_id = None
            self._ring_cancel = None

    def _steer_incoming_call(self, steer: Steer, payload: dict, *,
                             day: int, t_h: float,
                             drain: _SteerDrain) -> str:
        """The pick-up decision: mood, energy and what she is doing are in
        the stream already (state card, day plan); the pop-up names her
        current activity. A decision that cannot be read is a missed call."""
        call_id = str(payload.get("call_id") or "")
        if not call_id or call_id != self._ringing_call_id:
            # A ring whose turn died: the call is long gone.
            self.store.log_event(day, t_h, "call_stale",
                                 f"steer={steer.steer_id} call={call_id}")
            return _STEER_CONSUMED
        activity = self._current_activity(day, t_h)
        label = None
        if activity is not None:
            label = (activity.item.activity if activity.item is not None
                     else activity.description) or None
        result = self._execute_decision(
            decision_id=f"call-{steer.steer_id}",
            popup_kind="tool_decide_call",
            inputs={
                "call_id": call_id,
                "current_activity": label,
                "time": str(t_h),
            },
            steer=steer,
            day=day,
            t_h=t_h,
        )
        if result is None:
            # Unreadable verdict: he cannot be left ringing until a later
            # boundary, so the ring ends here as a missed call.
            self._steering.abandon(steer.steer_id)
            self.store.log_event(day, t_h, "call_decision_failed",
                                 f"steer={steer.steer_id} call={call_id}")
            answered = False
        else:
            answered = bool(result.verdict.get("answer"))
        drain.call_answered = answered
        if not answered:
            drain.decided_notes.append(
                MISSED_CALL_NOTE.format(time=self._clock_text(t_h))
            )
        self.store.log_event(day, t_h, "call_decided",
                             f"call={call_id} answer={answered}")
        return _STEER_CONSUMED

    def _begin_call(self, call_id: str, day: int, t_h: float) -> None:
        """Open the call: persist its state, then the call-start row (a
        stream row, so the system prompt does not change)."""
        self._save_call({"id": call_id, "started_t_h": t_h, "last_t_h": t_h})
        start = self._call_tick()
        self._save_call({"id": call_id, "started_t_h": start, "last_t_h": t_h})
        self._call_row("system",
                       with_call_face_rule(
                           CALL_START_ROW.format(time=self._clock_text(start))),
                       start, day)
        self.store.log_event(day, start, "call_started", f"call={call_id}")

    def call_turn(self, text: str | None, *, cancel=None):
        """One call turn: store his words, stream her reply as sentences.

        A generator of spoken sentences. Nothing of hers is stored here: the
        runtime reports what he heard through :meth:`call_heard`. The request
        is the ordinary main-stream request (same system prompt, same tool
        menu), so it extends the one before it; no pop-ups are drained and
        no close runs. Thinking is off (``HARNESS_CALL_THINKING``). Logged in
        ``llm_calls`` as ``chat`` with ``meta.voice``.
        """
        from harness.call import split_sentences, thinking_extra

        if self._call is None:
            raise RuntimeError("no call is active")
        assert self.current_record is not None and self.current_day is not None
        day = self.current_day
        t_h = self._call_tick()
        directive, controls, brief = self._directive(
            day, self.clock.local_hour(), wind_down=False)
        conv = self._ensure_conversation(t_h, opened_by="user")
        snapshot = self._build_snapshot(day, t_h, brief=brief, intent=None,
                                        query=text or None)
        self._cache_day_block(snapshot, day)
        self._ensure_state_card(snapshot, directive.prompt_brief, day, t_h)
        if text:
            t_user = self._call_tick()
            mid = self._call_row("user", text, t_user, day)
            conv = self._record_turn(conv, "user", text, t_user, message_id=mid)
        recent = self._context_turns()
        stable, messages = build_context_messages(
            snapshot, recent, None,
            t_h=t_h, anchor=self._real_time_anchor(),
            day_block=self._day_block,
            limit=None,
        )
        stable = _with_bubble_instruction(stable)
        self._last_stable_system = stable
        self._last_request_tools = TOOL_PAYLOAD
        self._note_request_prefix("chat", messages)

        extra = thinking_extra()
        effort = None if extra is not None else self._thinking_effort
        max_tokens = controls.max_tokens if effort is None else None
        meta: dict = {}
        raw: list[str] = []
        spoken: list[str] = []
        buf = ""
        leaked = False
        # her face tag opens the reply: cut it before the sentences, send it
        # ahead of them, and keep it for the row call_heard stores
        facecut = FaceCut()
        self._call_face = None
        self._call_blush = None
        self._call_scene = None
        try:
            for piece in self.client.chat_stream(
                messages, system=stable, max_tokens=max_tokens,
                tools=TOOL_PAYLOAD, reasoning_effort=effort,
                extra=extra, cancel=cancel, meta=meta,
            ):
                raw.append(piece)
                if leaked:
                    continue
                piece = facecut.feed(piece)
                if facecut.face is not None and (self._call_face is None
                                                 or facecut.blush != self._call_blush
                                                 or facecut.scene != self._call_scene):
                    self._call_face = self._call_face or facecut.face
                    self._call_blush = facecut.blush
                    self._call_scene = facecut.scene
                    yield FaceEvent(facecut.face, facecut.blush, facecut.scene)
                buf += piece
                cut = _call_markup_at(buf)
                if cut is not None:
                    leaked = True
                    buf = buf[:cut]
                sentences, buf = split_sentences(buf)
                if leaked and buf.strip():
                    sentences.append(buf.strip())
                    buf = ""
                for sentence in sentences:
                    if cancel is not None and cancel.is_set():
                        break
                    # a later sentence can open with a face of its own: it
                    # changes the sprite and is never spoken
                    later, later_mods, sentence = split_face_mods(sentence)
                    if later is not None:
                        self._call_face = self._call_face or later
                        yield FaceEvent(later, mod_blush(later_mods), mod_scene(later_mods))
                    sentence = sentence.strip()
                    if not sentence:
                        continue
                    spoken.append(sentence)
                    yield sentence
                if cancel is not None and cancel.is_set():
                    break
            buf += facecut.flush() if not leaked else ""
            later, later_mods, buf = split_face_mods(buf)
            if later is not None:
                self._call_face = self._call_face or later
                yield FaceEvent(later, mod_blush(later_mods), mod_scene(later_mods))
            if buf.strip() and not (cancel is not None and cancel.is_set()):
                spoken.append(buf.strip())
                yield buf.strip()
        finally:
            reply = "".join(raw)
            if leaked:
                self.store.log_event(
                    day, t_h, EVENT_TOOL_MARKUP_LEAK,
                    f"voice=1 spoken={len(' '.join(spoken))}chars "
                    f"raw={reply[:120]!r}",
                )
            repro = self._repro_kwargs(stable, messages, max_tokens, controls,
                                       None, day, t_h)
            if repro:
                repro["repro"]["reasoning_effort"] = effort
                repro["repro"]["extra"] = extra
            row_meta: dict = {"voice": True, "call_id": self._call.get("id")
                              if self._call else None}
            if meta.get("cancelled") or (cancel is not None and cancel.is_set()):
                row_meta["cancelled"] = True
            if meta.get("reasoning"):
                row_meta["reasoning"] = meta["reasoning"]
            usage = meta.get("usage")
            if getattr(usage, "reasoning_tokens", None) is not None:
                row_meta["reasoning_tokens"] = usage.reasoning_tokens
            self.store.log_llm_call(
                day, t_h, "chat", stable + "\n" + repr(messages), reply,
                getattr(self.client, "model", None), row_meta,
                **repro, **self._usage_kwargs(usage, meta.get("raw_cost")),
            )

    def call_heard(self, text: str, cut: bool = False) -> int | None:
        """Store what he heard of her last reply as her assistant row.

        Exactly ``text``, behind the face tag her reply opened with (the
        tag never reaches him; her history needs it to keep tagging, see
        ``harness.faces``). An empty text stores nothing. Returns the row id.
        """
        if self._call is None:
            raise RuntimeError("no call is active")
        day = self.current_day
        heard = text or ""
        face, self._call_face = self._call_face, None
        mid = None
        t_h = self.clock.now_h()
        if heard.strip():
            t_h = self._call_tick()
            mid = self._call_row("assistant", with_face(face, heard), t_h, day)
            conv = self._conversation
            if conv is not None:
                self._record_turn(conv, "companion", heard, t_h, message_id=mid)
        self.store.log_event(day, t_h, "call_heard",
                             f"len={len(heard)} cut={bool(cut)}")
        return mid

    def end_call(self, reason: str, *, shown_end_t_h: float | None = None) -> None:
        """Write the call-end row and leave call mode. No-op without a call.

        ``shown_end_t_h`` is the end time the row shows (a restart shows the
        last call row's time, not the restart's).
        """
        call = self._call
        if call is None:
            return
        day = self.current_day
        t_h = self._call_tick()
        start = float(call.get("started_t_h") or t_h)
        end = t_h if shown_end_t_h is None else max(start, shown_end_t_h)
        minutes = max(0, round((end - start) * 60.0))
        self._call_row(
            "system",
            CALL_END_ROW.format(start=self._clock_text(start),
                                end=self._clock_text(end), minutes=minutes),
            t_h, day,
        )
        self.store.log_event(day, t_h, "call_ended",
                             f"call={call.get('id')} reason={reason} "
                             f"minutes={minutes}")
        setter = getattr(self.store, "set_kv", None)
        if setter is not None:
            setter(CALL_LAST_END_KEY, repr(t_h))
        self._save_call(None)

    def recover_open_call(self) -> bool:
        """At startup: close a call the last run left open. True if one was."""
        call = self._call
        if call is None:
            return False
        self.end_call("restart", shown_end_t_h=call.get("last_t_h"))
        return True

    def on_message(self, user_text: str, *,
                   arrived_t_h: float | None = None,
                   photos: list[bytes] | None = None) -> TurnResult:
        """Process one user message: directive → snapshot → assemble → LLM.

        ``arrived_t_h`` is set for a message held during a voice call and
        answered after it: the row is stamped with its arrival time.
        ``photos`` are the raw image files sent with it; each is stored
        (:func:`harness.images.save`) and rides the turn as a reference. An
        unreadable one is logged (``photo_unreadable``) and left out; a
        message left with nothing at all raises ``ValueError``.
        """
        refs: list[str] = []
        for data in photos or ():
            try:
                refs.append(images.save(data))
            except (ValueError, RuntimeError) as exc:
                self.store.log_event(
                    self.clock.day(), self.clock.now_h(), "photo_unreadable",
                    f"bytes={len(data)} error={exc}",
                )
        if photos and not refs and not (user_text or "").strip():
            raise ValueError("an empty message with no readable photo")
        if refs:
            self.store.log_event(
                self.clock.day(), self.clock.now_h(), "photo_received",
                " ".join(refs),
            )
        return self._chat(user_text, proactive=False, stamp_t_h=arrived_t_h,
                          photos=refs or None)

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
        """Assistant-initiated message.

        ``intent_id`` is the EXACT id of a stored ``ProactiveIntent`` inside
        its validity window (two intents with the same reason are never
        interchangeable); the outgoing message persists it.

        ``reason`` (deprecated; never mixed with ``intent_id``) fires by
        reason type: the newest stored intent for it, or a generic opening.
        An unknown reason, or an argument that is neither a known intent id
        nor a valid reason, raises ``ValueError``.
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
