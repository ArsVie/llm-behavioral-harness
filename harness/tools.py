"""Pop-up decision tools for the harness (WS2, runtime redesign D1).

Two "pop-up" tools (user directives L361/L365/L369, session item #21/#22):
the server draws the pop-up inputs ``{Event, State, Time}`` and the MODEL
returns a verdict + prose reason. Verdict + inputs are recorded as state
next to the reason (audit: reason shows the user, inputs debug the draw).
Replay reads the recorded verdict and NEVER re-rolls (deterministic replay
is sacred — see ``DecisionRunner.execute``).

- ``tool_decide_event`` — fired ONCE at an event's start boundary (and
  again only when the model itself deferred). The model answers one
  tri-state field: ``{initiate: 'yes'|'no'|'defer', reason: str,
  turns?: int}``. It normalizes onto the canonical internal shape
  ``{initiate: bool, reason: str, action: 'follow'|'abandon'|'defer'}``.
  The END boundary is NOT a decision — no model call: a window that fully
  passes resolves server-side (``life.transition_past_windows``).
- ``tool_decide_reply`` — fired when a user message arrives while an event
  is in progress (L356). Verdict:
  ``{reply: bool, reason: str, terminate_event: bool}``. A no-reply verdict
  triggers a server-side notice per the verbose flag; optionally the event
  is terminated and the user intent followed.
- ``tool_decide_proactive`` — fired when a grounded proactive intent is due
  (proactive-as-decision, WS2): the pop-up inputs ``{Proactive: hook,
  Reason: ..., Source: type:id, Validity: ...}`` and the verdict is whether
  to initiate the proactive message now
  ``{initiate: bool, reason: str}``. ``initiate: false`` declines the fire
  WITHOUT a server notice — the intent is simply consumed (decline is the
  everyday no-go verdict, unlike a decide_reply no-reply which the user
  must never mistake for silence). No action/follow/defer dimension and no
  negotiation: the proactive decision is one-shot.

Availability-event negotiation (G0 contract): the ``tool_decide_event``
request/inputs gain ``phase`` ("inform" | "decide") and ``skippable``
(bool), plus ``delay_count`` (int) and ``window_ending`` (bool) on decide
legs; :func:`render_popup` draws them so the model sees the negotiation
state. Verdict rules:

- **Inform phase** (phase == "inform"): the model produces a natural
  mention with NO go/skip/delay action. The verdict shape is
  ``{message: str}`` — the mention. The legacy ``{initiate, reason}`` form
  (forced by the pinned decide-phase schema) and the
  ``{yes/no, "reason"}`` shorthand are also accepted and normalized onto it
  (``message := reason``); an ``action`` key is deliberately dropped. A
  model-supplied ``reason`` is preserved alongside the canonical
  ``message`` (records written by either transport keep the mention).
- **Decide phase** (phase == "decide", the default when absent): the
  tri-state ``{initiate, reason, turns?}`` above. A verdict with
  ``action == 'defer'`` always carries a ``defer_turns``
  (``negotiation_contract.DEFER_TURNS_KEY``): the model's own ``turns``
  when it named one (clamped to [DEFER_N_MIN, DEFER_N_MAX]), else the
  runtime maps the reason text through
  ``negotiation_contract.DEFER_N_PATTERNS`` deterministically (see
  :func:`map_defer_turns`) so a vague "a bit longer" still lands on a
  number. The recorded decision verdict carries the final ``defer_turns``
  (back-filled on replay too, so every defer verdict carries its N).
- **Backward compatibility**: pop-ups without the new input keys render
  exactly as before, and legacy verdicts parse exactly as before (phase
  defaults to "decide" when absent). ``tool_decide_reply`` is untouched.

Transport (reviewer-endorsed D1): native function calling when the client
has it, textual fallback (``tool_decide_event: {...}`` parsed from the reply
content — matches the user's sketch) behind capability detection. The RAW
reply AND the parsed verdict are both persisted (dual persistence); a parse
failure is a LOUD recorded event (``state_events: decision_parse_failed``),
never a silent skip. ``decision_on_parse_failure`` config:
``requeue`` (default) | ``server_draw`` | ``abort``.

Budget (L361/L365): a per-day window of accepted no-reply verdicts; the
window resets at day rollover. ``0`` = must always reply (every no-reply
verdict is rejected), unset/empty = off (unlimited). At exhaustion the
no-reply verdict is rejected, a reply is forced, and the state event
``budget_exhausted_forced_reply`` is recorded.

Decision source (L365, "we're not making a calculator", but test both):
default ``model``; ``server_draw`` draws the verdict from the injected
seeded RNG (a dedicated stream, never the day_rng draw order) for the #22
comparison.

Config — env-only, no config.yaml. Loader: :func:`load_decision_config`.
Defaults:

======================  ============  ======================================
Env var                 Default       Meaning
======================  ============  ======================================
HARNESS_VERBOSE         0             server ALWAYS notifies on no-reply;
                                      0 = short notice, 1 = with the reason
HARNESS_BUDGET          (unset)       no-reply budget per day; 0 = always
                                      reply; N = N no-replies allowed;
                                      unset/empty = off (unlimited)
HARNESS_DECISION_SOURCE model         model | server_draw
HARNESS_DECISION_PARSE_FAILURE requeue requeue | server_draw | abort
HARNESS_TOOL_MODE       auto          auto | native | textual
======================  ============  ======================================
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Protocol

from harness.negotiation_contract import (
    DEFER_N_MAX,
    DEFER_N_MIN,
    DEFER_TURNS_KEY,
)
from harness.clock import duration, hhmm
from harness.negotiation_state import map_defer_n
from harness.env import env_bool as _env_bool
from harness.steering import NO_ACTIVE_EVENT

# Tool schemas (Hermes-style {name, description, parameters})

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "tool_decide_event",
        "description": (
            "Your call on the event in the pop-up block above. The block "
            "carries the context (Event, State, Time, ...) — do NOT echo it "
            "back. Skippable no means the event is a commitment: go."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "initiate": {
                    "type": "string",
                    "enum": ["yes", "no", "defer"],
                    "description": "yes = go now, no = skip it, defer = stay "
                                   "where you are and be asked again later.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why you chose that.",
                },
                "turns": {
                    "type": "integer",
                    "description": "Only with defer: how many more turns you "
                                   "want. Omit and the runtime picks.",
                },
            },
            "required": ["initiate", "reason"],
        },
    },
    {
        "name": "tool_decide_reply",
        "description": (
            "Pop-up decision fired when a user message arrives while an "
            "event is in progress. The pop-up inputs {{Event, State, Time}} "
            "and the latest user message are already in the pop-up block — "
            "do NOT echo them back. Fill ONLY the verdict: whether to reply "
            "in context (e.g. \"I'm in class, what do you want\") or not "
            "reply (the server notifies the user), and whether the event "
            "should be terminated to follow the user's intent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reply": {
                    "type": "boolean",
                    "description": "Whether to reply now: true = reply in "
                                   "context, false = do not reply (server "
                                   "notifies the user).",
                },
                "reason": {
                    "type": "string",
                    "description": "Short plain-language reason for the "
                                   "verdict.",
                },
                "terminate_event": {
                    "type": "boolean",
                    "description": "Whether the in-progress event should be "
                                   "terminated to follow the user's intent.",
                },
            },
            "required": ["reply", "reason"],
        },
    },
    {
        "name": "tool_decide_proactive",
        "description": (
            "Pop-up decision fired when a grounded proactive intent is "
            "due (the companion considering reaching out first). The "
            "pop-up block already carries the intent context (Proactive "
            "hook, Reason, Source, Validity, and the Latest user message "
            "when one exists) — do NOT echo it back. Fill ONLY the "
            "verdict: whether to initiate the proactive message now, "
            "with a prose reason. initiate=false declines this fire — "
            "the opportunity is consumed quietly and no message goes "
            "out; nothing else happens."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "initiate": {
                    "type": "boolean",
                    "description": "Whether to send the proactive message "
                                   "now: true = initiate, false = decline "
                                   "this fire.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short plain-language reason for the "
                                   "verdict.",
                },
            },
            "required": ["initiate", "reason"],
        },
    },
]

#: Inform-phase variant of ``tool_decide_event``: the verdict is the
#: natural mention only (``{message: str}``, no go/skip/delay action).
def offered_tools(request: PopupRequest) -> list[dict]:
    """The schemas a pop-up offers: exactly the function it asks about.

    A pop-up asks one named question, and offering the rest let the model answer
    an event pop-up with ``tool_decide_reply`` (1-in-3 with three offered,
    0-in-3 with one). An unknown kind falls back to the full set — a wrong-tool
    verdict is recoverable, no tool is not. One definition, used by the request
    builder AND by the ledger's wire-identity record.
    """
    wanted = [t for t in request.tools if t.get("name") == request.popup_kind]
    return wanted or list(request.tools)


def tools_identity(tools: list[dict] | None) -> tuple[str | None, list[str]]:
    """The wire identity of a ``tools`` payload: a stable hash and the names.

    A replayed call is only a replay if its tool payload matches too, and the
    ledger used to record the request WITHOUT ``tools`` — so a stored call could
    never be proven identical to what went out. The schemas rebuild from this
    module, so the hash (not the full payload) is what rides with each call.

    ``None`` / empty means the request carried no tools at all, which is a fact
    worth storing: the mainline reply leg sends none by design.
    """
    if not tools:
        return None, []
    canonical = json.dumps(tools, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    names = [((t.get("function") or t).get("name") or "") for t in tools]
    return hashlib.sha256(canonical.encode()).hexdigest(), names


TOOL_SCHEMAS_INFORM: list[dict] = [
    {
        "name": "tool_decide_event",
        "description": (
            "The pop-up block already carries the event context (Event, "
            "State, Time, Phase: inform) — do NOT echo it back. Phase "
            "inform: the event is coming up; just mention it naturally in "
            "message. This is NOT a verdict: do not initiate, do not "
            "choose follow/abandon/defer, do not leave."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Your natural one-line mention of the "
                                   "upcoming event.",
                },
            },
            "required": ["message"],
        },
    },
]

#: Verdict keys for a ``tool_decide_event`` call (decide phase).
EVENT_VERDICT_KEYS = ("initiate", "reason")
#: Verdict keys for a ``tool_decide_reply`` call.
REPLY_VERDICT_KEYS = ("reply", "reason")
#: Verdict keys for a ``tool_decide_proactive`` call.
PROACTIVE_VERDICT_KEYS = ("initiate", "reason")

#: Canonical state-event names recorded by the decision layer.
EVENT_DECISION_PARSE_FAILED = "decision_parse_failed"
EVENT_BUDGET_FORCED_REPLY = "budget_exhausted_forced_reply"
EVENT_DECISION_REPLAYED = "decision_replayed"


class DecisionError(RuntimeError):
    """Base class for decision-layer failures (always loud, never silent)."""


class DecisionParseError(DecisionError):
    """The model's raw reply could not be parsed into a verdict.

    The failure is recorded as a ``decision_parse_failed`` state event
    BEFORE this is raised, and the raw reply is persisted.
    """


class DecisionRequeue(DecisionError):
    """Parse-failure policy ``requeue``: raise so the caller re-queues the
    pop-up and delivers it at the next safe boundary (the raw reply stays
    persisted; the verdict is not)."""


@dataclass
class Capabilities:
    """Client capabilities injected by WS4 (protocol: ``has_native_tools``).

    The DecisionRunner never imports harness.client (WS3 owns it); callers
    pass a real capability object or a fake for tests.
    """

    has_native_tools: bool = False


@dataclass(frozen=True)
class RawReply:
    """The model's raw output for one pop-up, exactly as produced.

    Exactly one of ``text`` (textual transport) / ``tool_calls`` (native
    transport) is set; both may be set when a native-capable model answers
    in text anyway (the runner prefers the tool call). This is what gets
    persisted verbatim (dual persistence: raw reply + parsed verdict).
    """

    text: str | None = None
    tool_calls: list[dict] | None = None


@dataclass(frozen=True)
class PopupRequest:
    """Everything the injected model callable needs to make the LLM call.

    The callable (wired by WS4, which owns the transcript) builds the real
    request: it embeds ``popup`` where the model should see it, offers
    ``tools`` when ``native`` is True, and may use ``inputs`` (e.g.
    ``conversation_context``) to assemble the message list. On inform legs
    ``tools`` is the mention-only variant (``TOOL_SCHEMAS_INFORM``); on
    decide legs it is the pinned verdict schema (``TOOL_SCHEMAS``). The
    negotiation inputs (``phase``, ``skippable``, ``delay_count``,
    ``window_ending``) ride along in ``inputs`` and are drawn by
    :func:`render_popup`.
    """

    popup_kind: str
    popup: str
    tools: list[dict]
    native: bool
    inputs: dict
    #: Restated requirement for a re-ask (see ``NATIVE_REASK``); None on a
    #: first attempt, so every existing caller keeps its exact request.
    nudge: str | None = None


#: Appended to a native pop-up whose reply carried no tool call AND no text.
#: The model runs in thinking mode, where the gateway rejects a forced
#: ``tool_choice`` with a 400, so the requirement is restated instead. The
#: re-ask is bounded to one extra call: an unchanged prompt that produced
#: nothing once is not worth sending twice (10 of 27 decision calls on the
#: last live run came back as an empty completion).
NATIVE_REASK = (
    "Your previous reply carried no tool call. Call {tool} now, with the "
    "verdict object as its arguments -- no prose, no explanation."
)

#: Callable returning the raw model reply for a pop-up request.
ModelCall = Callable[[PopupRequest], RawReply]


@dataclass
class DecisionResult:
    """Outcome of one pop-up decision, fully recorded in the store."""

    decision_id: str
    popup_kind: str
    verdict: dict
    source: str            # 'model' | 'server_draw' | 'replay'
    transport: str         # 'native' | 'textual' | 'server_draw' | 'replay'
    record_id: int | None
    budget_consumed: bool = False
    forced: bool = False   # budget exhaustion forced a reply
    from_replay: bool = False
    raw_reply: str | None = None
    notice: str | None = None
    parse_failed: bool = False

    @property
    def reason(self) -> str:
        return str(self.verdict.get("reason", ""))


# Pop-up rendering

def _clock(value) -> str:
    """Render a pop-up time input as HH:MM.

    Inputs are PERSISTED RAW (``decision_records.inputs_json`` keeps the
    absolute ``t_h`` for replay and audit); the conversion to wall-clock
    happens here, at the render boundary, so the model never reads an engine
    coordinate. Non-numeric values (already-formatted strings, "?") pass
    through untouched.
    """
    if isinstance(value, bool) or value is None:
        return "?"
    if isinstance(value, (int, float)):
        return hhmm(float(value))
    text = str(value)
    try:
        return hhmm(float(text))
    except ValueError:
        return text


def render_popup(popup_kind: str, inputs: dict) -> str:
    """Render the pop-up block exactly per the user's L369 sketch.

    decide_event::

        {Event: gym, State: start, Time: 19:30}
        {Initiate:{yes,no,defer}, Reason: ""}

    decide_reply::

        {Event: gym, State: in_progress, Time: 19:30}
        {Reply:{yes,no}, Reason: "", Terminate_event:{yes,no}}
        Latest user message: "are you coming to class?"

    ``inputs`` keys used: event_id/event_label -> Event, state_label ->
    State, time -> Time, latest_user_message (decide_reply only).

    TIME (2026-09-07): ``time`` and ``valid_until`` render as HH:MM and
    ``silence_h`` as a plain duration. The inputs dict is NOT mutated -- the
    raw ``t_h`` is what ``decision_records.inputs_json`` persists, so replay
    and audit keep the engine coordinate while the model reads a clock. The
    live run showed ``Time: 78.96`` in front of the model; that was the bug.

    Negotiation context (G0 contract, decide_event only): when the caller
    supplies the keys, extra lines are drawn so the model sees the phase
    and the negotiation state: ``phase`` ("inform" | "decide"), ``skippable``
    (bool), ``delay_count`` (int, decide only) and ``window_ending`` (bool,
    decide only). Inform legs draw ``{Message: ""}`` instead of the verdict
    line (the mention, no go/skip/delay action); decide legs keep the L369
    sketch. Pop-ups WITHOUT these keys render byte-identically to the
    legacy sketch (backward compatible).
    """
    event = inputs.get("event_label") or inputs.get("event_id") or NO_ACTIVE_EVENT
    state = inputs.get("state_label") or NO_ACTIVE_EVENT
    time = _clock(inputs.get("time"))
    if popup_kind == "tool_decide_event":
        lines = [
            f"{{Event: {event}, State: {state}, Time: {time}}}",
            # Inform legs ask for the mention, decide legs for the verdict.
            (
                '{Message: ""}'
                if inputs.get("phase") == "inform"
                else '{Initiate:{yes,no,defer}, Reason: ""}'
            ),
        ]
        # Negotiation lines render only when the caller supplies the keys.
        if "phase" in inputs:
            lines.append(f"Phase: {inputs['phase']}")
        skippable = inputs.get("skippable")
        if isinstance(skippable, bool):
            lines.append(f"Skippable: {'yes' if skippable else 'no'}")
        delay_count = inputs.get("delay_count")
        if isinstance(delay_count, int):
            lines.append(f"Delays so far: {delay_count}")
        window_ending = inputs.get("window_ending")
        if isinstance(window_ending, bool):
            lines.append(f"Window ending: {'yes' if window_ending else 'no'}")
        return "\n".join(lines)
    if popup_kind == "tool_decide_reply":
        lines = [
            f"{{Event: {event}, State: {state}, Time: {time}}}",
            '{Reply:{yes,no}, Reason: "", Terminate_event:{yes,no}}',
        ]
        latest = inputs.get("latest_user_message")
        if latest:
            lines.append(f'Latest user message: "{latest}"')
        return "\n".join(lines)
    if popup_kind == "tool_decide_proactive":
        hook = (
            inputs.get("hook") or inputs.get("intent")
            or inputs.get("event_label") or "?"
        )
        reason = inputs.get("reason") or inputs.get("intent_reason") or "?"
        source_type = inputs.get("source_type") or inputs.get("source") or "?"
        source_id = inputs.get("source_id") or inputs.get("source_ref") or "?"
        validity = _clock(
            inputs.get("valid_until") or inputs.get("validity")
        )
        lines = [
            f"{{Proactive: {hook}, Reason: {reason}, "
            f"Source: {source_type}:{source_id}, Validity: {validity}}}",
            '{Initiate:{yes,no}, Reason: ""}',
        ]
        latest = inputs.get("latest_user_message")
        if latest:
            lines.append(f'Latest user message: "{latest}"')
        silence_h = inputs.get("silence_h")
        if isinstance(silence_h, (int, float)):
            lines.append(f"User silence: {duration(float(silence_h))}")
        return "\n".join(lines)
    raise ValueError(f"unknown popup_kind: {popup_kind!r}")


# Verdict parsing (native tool_calls + textual fallback)

def _brace_payload(text: str, start: int) -> str | None:
    """Extract the brace-balanced payload beginning at ``text[start] == '{'``.

    Tolerant of newlines and nested braces inside string values, so a reason
    containing ``}`` does not truncate the payload.
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


#: Textual marker per tool, tolerant of quotes/whitespace/linebreaks.
_TEXTUAL_MARKER = re.compile(
    r"tool_(decide_event|decide_reply|decide_proactive)\s*:\s*(\{)",
    re.IGNORECASE,
)

#: Shorthand payload: {yes, "too tired"} / {no, "too tired"}.
_SHORTHAND = re.compile(
    r"^\{\s*(yes|no|true|false|1|0)\s*,\s*\"((?:[^\"\\]|\\.)*)\"\s*\}$",
    re.IGNORECASE | re.DOTALL,
)


def _parse_shorthand(payload: str) -> dict | None:
    m = _SHORTHAND.match(payload.strip())
    if not m:
        return None
    token = m.group(1).lower()
    affirmative = token in ("yes", "true", "1")
    reason = m.group(2)
    return {"verdict": affirmative, "reason": reason}


def parse_verdict(popup_kind: str, payload: str, phase: str | None = None) -> dict:
    """Parse one textual pop-up payload into a verdict dict.

    Accepts (in order):
      1. a JSON object — ``{"initiate": true, "reason": "..."}`` for
         decide_event, ``{"reply": false, "reason": "...",
         "terminate_event": false}`` for decide_reply (missing optional
         verdict fields default: ``terminate_event=False``,
         ``action=None``, ``reason=""``);
      2. the L369 shorthand — ``{yes, "too tired"}`` / ``{no, "too tired"}``
         mapped onto the ``initiate``/``reply`` key of the pop-up kind.

    ``phase="inform"`` (G0 negotiation) switches decide_event to the
    inform verdict: ``{message: str}`` — the natural mention, no
    go/skip/delay action. The legacy ``{initiate, reason}`` form and the
    shorthand are normalized onto it (``message := reason``).

    Raises ``ValueError`` on anything else.
    """
    raw = payload.strip()
    if not raw:
        raise ValueError("empty pop-up payload")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        verdict = _normalize_verdict(popup_kind, obj, phase=phase)
        if _valid_verdict(popup_kind, verdict, phase=phase):
            return verdict
        raise ValueError(
            f"JSON verdict missing required key for {popup_kind}: "
            f"{sorted(obj)}"
        )
    short = _parse_shorthand(raw)
    if short is not None:
        verdict = _normalize_verdict(
            popup_kind, {_verdict_key(popup_kind): short["verdict"],
                         "reason": short["reason"]},
            phase=phase,
        )
        if _valid_verdict(popup_kind, verdict, phase=phase):
            return verdict
    raise ValueError(
        f"unparseable {popup_kind} payload (expected JSON object or "
        f'{{yes, "reason"}} shorthand): {raw[:200]!r}'
    )


def _verdict_key(popup_kind: str) -> str:
    if popup_kind in ("tool_decide_event", "tool_decide_proactive"):
        return "initiate"
    if popup_kind == "tool_decide_reply":
        return "reply"
    raise ValueError(f"unknown popup_kind: {popup_kind!r}")


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("yes", "true", "1", "y"):
            return True
        if v in ("no", "false", "0", "n"):
            return False
    return None


def _normalize_verdict(
    popup_kind: str, obj: dict, phase: str | None = None,
) -> dict:
    """Coerce raw keys to the canonical verdict shape with safe defaults."""
    if popup_kind == "tool_decide_event":
        if phase == "inform":
            return _inform_verdict(obj)
        return _event_verdict(obj)
    if popup_kind == "tool_decide_proactive":
        # Proactive intents are one-shot decisions: no inform phase, no
        # action/defer dimension. Conservative default: decline.
        return _proactive_verdict(obj)
    if popup_kind == "tool_decide_reply":
        return _reply_verdict(obj)
    raise ValueError(f"unknown popup_kind: {popup_kind!r}")


def _inform_verdict(obj: dict) -> dict:
    """The inform leg: a natural mention, no action.

    Legacy forms carried the text under ``reason``; both normalize onto
    ``message``, and ``reason`` is preserved when it was a string so the
    audit trail keeps whatever the model actually sent.
    """
    message = obj.get("message")
    if not isinstance(message, str):
        message = obj["reason"] if isinstance(obj.get("reason"), str) else ""
    verdict: dict = {"message": message}
    if isinstance(obj.get("reason"), str):
        verdict["reason"] = obj["reason"]
    return verdict


#: The model-facing tri-state ``initiate`` mapped onto the canonical
#: ``(initiate, action)`` pair every consumer and every recorded row speaks.
_INITIATE_TRISTATE: dict[str, tuple[bool, str]] = {
    "yes": (True, "follow"),
    "no": (False, "abandon"),
    "defer": (False, "defer"),
}


def _event_verdict(obj: dict) -> dict:
    """The decide leg for an agenda event.

    The model answers ONE tri-state field: ``initiate`` in
    {yes, no, defer}, plus a ``reason`` and — only on a defer — an
    optional ``turns``. That collapses onto the canonical internal shape
    ``{initiate: bool, reason: str, action: follow|abandon|defer}``, which
    is what the negotiation machine, the session and every persisted
    ``verdict_json`` row already speak; ``defer`` is not an initiation, so
    it carries ``initiate: False``.

    The pre-tri-state form (boolean ``initiate`` with a separate ``action``)
    still parses unchanged, so recorded verdicts keep replaying: a bool
    leaves ``action`` alone rather than deriving one.

    Defaults are the conservative ones: she does not initiate unless the
    model actually said so, and an unrecognised ``action`` is dropped rather
    than passed through.
    """
    verdict: dict = {"initiate": False, "reason": "", "action": None}
    raw = obj.get("initiate", obj.get("verdict"))
    if isinstance(raw, str) and raw.strip().lower() in _INITIATE_TRISTATE:
        verdict["initiate"], verdict["action"] = (
            _INITIATE_TRISTATE[raw.strip().lower()]
        )
    else:
        flag = _as_bool(raw)
        if flag is not None:
            verdict["initiate"] = flag
    if isinstance(obj.get("reason"), str):
        verdict["reason"] = obj["reason"]
    if obj.get("action") in ("follow", "abandon", "defer"):
        verdict["action"] = obj["action"]
    if verdict["action"] == "defer":
        turns = _as_turns(obj.get("turns"))
        if turns is not None:
            verdict[DEFER_TURNS_KEY] = turns
    return verdict


def _as_turns(value: Any) -> int | None:
    """A model-supplied defer N, clamped, or None when it said nothing.

    Bools are rejected before ints (``True`` is an ``int`` in Python and
    would otherwise read as one turn).
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return min(max(n, DEFER_N_MIN), DEFER_N_MAX)


def _proactive_verdict(obj: dict) -> dict:
    """The decide leg for a grounded proactive intent.

    One-shot: ``{initiate: bool, reason: str}`` only. Conservative default:
    decline (``initiate: false``) unless the model actually said to fire.
    """
    verdict: dict = {"initiate": False, "reason": ""}
    flag = _as_bool(obj.get("initiate", obj.get("verdict")))
    if flag is not None:
        verdict["initiate"] = flag
    if isinstance(obj.get("reason"), str):
        verdict["reason"] = obj["reason"]
    return verdict


def _reply_verdict(obj: dict) -> dict:
    """The decide leg for a user message arriving mid-event."""
    verdict: dict = {"reply": False, "reason": "", "terminate_event": False}
    flag = _as_bool(obj.get("reply", obj.get("verdict")))
    if flag is not None:
        verdict["reply"] = flag
    if isinstance(obj.get("reason"), str):
        verdict["reason"] = obj["reason"]
    term = _as_bool(obj.get("terminate_event"))
    if term is not None:
        verdict["terminate_event"] = term
    return verdict


def _valid_verdict(
    popup_kind: str, verdict: dict, phase: str | None = None,
) -> bool:
    """A verdict is valid when the deciding flag is a real bool; an inform
    verdict is valid when it carries a non-empty mention (a silent inform
    is a protocol failure, recorded loudly like any other parse failure)."""
    if popup_kind == "tool_decide_event" and phase == "inform":
        return isinstance(verdict.get("message"), str) and bool(
            verdict["message"].strip()
        )
    return isinstance(verdict.get(_verdict_key(popup_kind)), bool)


def parse_textual_reply(
    popup_kind: str, text: str, phase: str | None = None,
) -> dict:
    """Locate ``tool_decide_event: {...}`` / ``tool_decide_reply: {...}`` in
    free-form reply text and parse the payload.

    Tolerant of quotes, linebreaks and surrounding prose (the model may
    think out loud before or after the marker, per the user's sketch
    ``{name}: {thinking} tool_decide_event: {yes, "too tired"}``).
    ``phase`` selects the inform verdict shape for decide_event (see
    :func:`parse_verdict`); defaults to decide-phase (legacy) parsing.
    """
    m = _TEXTUAL_MARKER.search(text)
    if not m:
        raise ValueError(
            f"no '{popup_kind}:' marker found in reply text"
        )
    found_kind = "tool_" + m.group(1).lower()
    payload = _brace_payload(text, m.start(2))
    if payload is None:
        raise ValueError(f"unbalanced braces after '{found_kind}:' marker")
    return parse_verdict(found_kind, payload, phase=phase)


def parse_native_reply(
    popup_kind: str, tool_calls: list[dict], phase: str | None = None,
) -> dict:
    """Extract the verdict from a native function-calling response.

    ``tool_calls`` entries are ``{"id", "type", "function": {"name",
    "arguments"}}`` (OpenAI shape). The first call whose name matches the
    pop-up kind wins; its ``arguments`` (JSON string or dict) are parsed.
    ``phase`` selects the inform verdict shape for decide_event (see
    :func:`parse_verdict`); defaults to decide-phase (legacy) parsing.
    """
    for call in tool_calls or []:
        fn = call.get("function") or {}
        name = fn.get("name", "")
        if name != popup_kind:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            args = json.loads(args) if args.strip() else {}
        if not isinstance(args, dict):
            raise ValueError(
                f"native {popup_kind} arguments are not an object: {args!r}"
            )
        return _normalize_verdict(popup_kind, args, phase=phase)
    raise ValueError(
        f"no tool call named {popup_kind} in native reply "
        f"(got: {[ (c.get('function') or {}).get('name') for c in tool_calls or [] ]})"
    )


# Defer turns: the server fills N

#: Re-export of negotiation_state.map_defer_n so both paths share one mapping.
map_defer_turns = map_defer_n


def fill_defer_turns(verdict: dict) -> dict:
    """Server-fill ``defer_turns`` on a defer verdict (G0 contract).

    Only when ``action == 'defer'``; all other verdicts pass through
    untouched. The model MAY name its own N (``turns`` on the call, clamped
    to [DEFER_N_MIN, DEFER_N_MAX] by verdict normalization and carried here
    as ``defer_turns``) — that wins, since it asked for it. When it named
    none, N is mapped deterministically from the reason text (see
    :func:`map_defer_turns`), so a vague "just a bit longer" still lands on
    a concrete number. Callers must treat the returned verdict as the final
    recorded shape.
    """
    if verdict.get("action") != "defer":
        return verdict
    out = dict(verdict)
    if not isinstance(out.get(DEFER_TURNS_KEY), int):
        out[DEFER_TURNS_KEY] = map_defer_turns(str(verdict.get("reason", "")))
    return out


# Notice builder

def build_notice(name: str, verdict: dict, verbose: bool) -> str | None:
    """Server notice for a no-reply verdict; None when she replies.

    verbose OFF: ``"{name} saw your message but chose not to reply yet"``
    verbose ON:  ``"{name} is not replying, reason: {Reason}"``
    """
    if verdict.get("reply") is not False:
        return None
    if verbose:
        return f"{name} is not replying, reason: {verdict.get('reason', '')}"
    return f"{name} saw your message but chose not to reply yet"


# Config (env-only)

@dataclass
class DecisionConfig:
    """Resolved decision-layer configuration (env-only, no config.yaml)."""

    verbose: bool = False
    budget: int | None = None        # None = off/unlimited; 0 = always reply
    decision_source: str = "model"   # 'model' | 'server_draw'
    parse_failure_mode: str = "requeue"  # 'requeue' | 'server_draw' | 'abort'
    tool_mode: str = "auto"          # 'auto' | 'native' | 'textual'
    name: str = "Lily"

    def __post_init__(self) -> None:
        if self.decision_source not in ("model", "server_draw"):
            raise ValueError(
                f"HARNESS_DECISION_SOURCE must be 'model' or 'server_draw', "
                f"got {self.decision_source!r}"
            )
        if self.parse_failure_mode not in ("requeue", "server_draw", "abort"):
            raise ValueError(
                f"HARNESS_DECISION_PARSE_FAILURE must be one of "
                f"requeue|server_draw|abort, got {self.parse_failure_mode!r}"
            )
        if self.tool_mode not in ("auto", "native", "textual"):
            raise ValueError(
                f"HARNESS_TOOL_MODE must be one of auto|native|textual, "
                f"got {self.tool_mode!r}"
            )




def _env_budget() -> int | None:
    """HARNESS_BUDGET: unset/empty -> None (off); '0' -> 0 (always reply);
    otherwise a non-negative int (per-day window)."""
    raw = os.environ.get("HARNESS_BUDGET")
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"HARNESS_BUDGET must be an integer or empty (off), got {raw!r}"
        ) from exc
    if value < 0:
        raise ValueError(f"HARNESS_BUDGET must be >= 0, got {value}")
    return value


def load_decision_config() -> DecisionConfig:
    """Load the decision configuration from the environment.

    Env vars (see module docstring): ``HARNESS_VERBOSE``, ``HARNESS_BUDGET``,
    ``HARNESS_DECISION_SOURCE``, ``HARNESS_DECISION_PARSE_FAILURE``,
    ``HARNESS_TOOL_MODE``. WS4 calls this once at startup and passes the
    resulting ``DecisionConfig`` to the runner.
    """
    return DecisionConfig(
        verbose=_env_bool("HARNESS_VERBOSE"),
        budget=_env_budget(),
        decision_source=os.environ.get(
            "HARNESS_DECISION_SOURCE", "model"
        ).strip().lower() or "model",
        parse_failure_mode=os.environ.get(
            "HARNESS_DECISION_PARSE_FAILURE", "requeue"
        ).strip().lower() or "requeue",
        tool_mode=os.environ.get("HARNESS_TOOL_MODE", "auto").strip().lower()
        or "auto",
        name=os.environ.get("HARNESS_NAME", "Lily"),
    )


# Store protocol (duck-typed: SQLiteStore or test fakes)

class DecisionStore(Protocol):
    """The store surface the runner needs (subset of SQLiteStore)."""

    def record_decision(
        self, day: int, t_h: float, popup_kind: str, event_id: str | None,
        event_label: str | None, state_label: str | None, time: str | None,
        inputs_json: str | None, raw_reply: str | None,
        verdict_json: str | None, source: str, transport: str,
        delivered_t_h: float | None, budget_consumed: int, *,
        replay_id: str | None = None,
    ) -> int: ...

    def decision_for_replay(self, decision_id: str) -> dict | None: ...

    def decisions_for_day(self, day: int) -> list[dict]: ...

    def log_event(
        self, day: int, t_h: float, event: str, detail: str | None = None
    ) -> None: ...


# DecisionRunner

#: Server-drawn verdict reason (decision_source=server_draw).
SERVER_DRAW_REASON = "server draw (decision_source=server_draw)"
#: Reason attached to the forced reply at budget exhaustion.
FORCED_REPLY_REASON = "budget exhausted — forced reply"


class DecisionRunner:
    """Executes pop-up decisions end to end and persists everything.

    One ``execute`` call per pop-up: replay check -> transport selection ->
    model call (or server draw) -> parse -> budget enforcement -> dual
    persistence -> notice. Deterministic replay: when a decision record
    already exists for ``decision_id`` (the natural key), the recorded
    verdict is returned and the model is NEVER called again.
    """

    def __init__(
        self,
        store: DecisionStore,
        *,
        verbose: bool = False,
        budget: int | None = None,
        decision_source: str = "model",
        parse_failure_mode: str = "requeue",
        tool_mode: str = "auto",
        rng: Any | None = None,
        draw_p: float = 0.6,
        name: str = "Lily",
    ):
        self.store = store
        self.verbose = bool(verbose)
        self.budget = budget            # None = off; 0 = always reply
        self.decision_source = decision_source
        self.parse_failure_mode = parse_failure_mode
        self.tool_mode = tool_mode
        self.rng = rng                  # injected Generator (dedicated stream)
        self.draw_p = draw_p
        self.name = name

    # -- public API ---------------------------------------------------------

    def _obtain_verdict(self, decision_id: str, popup_kind: str, phase: str,
                        inputs: dict, capabilities: Capabilities,
                        call: ModelCall, day: int, t_h: float):
        """Get one verdict, from the server draw or from the model.

        Returns ``(verdict, source, transport, raw_reply, parse_failed)``.
        A parse failure is always RECORDED before the configured policy
        decides what happens next — requeue (re-ask at the next boundary),
        server_draw (fall back to the draw, transport marked as the
        fallback), or abort. The record is the point: a chatty model that
        answers a pop-up in prose leaves an audit trail either way.
        """
        source = self.decision_source
        if source == "server_draw":
            return self._draw_verdict(popup_kind, phase=phase), source, \
                "server_draw", None, False
        transport = self._choose_transport(capabilities)
        request = PopupRequest(
            popup_kind=popup_kind,
            popup=render_popup(popup_kind, inputs),
            # Inform legs get the mention-only schema; decide legs get the verdict schema.
            tools=TOOL_SCHEMAS_INFORM if phase == "inform" else TOOL_SCHEMAS,
            native=(transport == "native"),
            inputs=inputs,
        )
        raw = call(request)
        if (transport == "native" and not raw.tool_calls
                and not (raw.text or "").strip()):
            # Nothing to parse and nothing to repair: the model thought and
            # answered with neither a tool call nor text. Ask once more with
            # the requirement stated, rather than spending the retry budget
            # re-sending the prompt it just ignored.
            raw = call(replace(request, nudge=NATIVE_REASK.format(
                tool=popup_kind)))
        raw_reply = self._raw_to_text(raw, transport)
        try:
            verdict = self._parse_raw(popup_kind, raw, transport, phase=phase)
        except DecisionParseError:
            self._record_parse_failure(
                decision_id, popup_kind, transport, raw_reply, day, t_h
            )
            if self.parse_failure_mode == "requeue":
                raise DecisionRequeue(
                    f"{popup_kind} parse failed (decision {decision_id}) — "
                    f"re-queue for the next boundary"
                ) from None
            if self.parse_failure_mode == "server_draw":
                return (self._draw_verdict(popup_kind, phase=phase),
                        "server_draw", "server_draw_fallback", raw_reply, True)
            raise DecisionParseError(
                f"{popup_kind} parse failed (decision {decision_id}) — "
                f"aborting per HARNESS_DECISION_PARSE_FAILURE=abort"
            ) from None
        return verdict, source, transport, raw_reply, False

    def _apply_reply_budget(self, popup_kind: str, verdict: dict,
                            decision_id: str, day: int, t_h: float):
        """Enforce the daily no-reply budget; returns (verdict, forced, used).

        Only a ``reply: false`` verdict spends budget. Once the day's budget
        is gone the verdict is REPLACED by a forced reply — the companion
        may decline to answer, but not indefinitely, or a user could be
        silently ignored all day. The override is logged with the budget
        that triggered it. Proactive decisions never touch the budget: a
        declined fire is consumed quietly, and no-reply slots belong to
        decide_reply alone.
        """
        if popup_kind != "tool_decide_reply" or verdict.get("reply") is not False:
            return verdict, False, 0
        used = self._no_replies_used(day, decision_id)
        if self.budget is None or used < self.budget:
            return verdict, False, 1
        self.store.log_event(
            day, t_h, EVENT_BUDGET_FORCED_REPLY,
            json.dumps(
                {"decision_id": decision_id, "popup_kind": popup_kind,
                 "day": day, "budget": self.budget},
                sort_keys=True,
            ),
        )
        return {
            "reply": True,
            "reason": FORCED_REPLY_REASON,
            "terminate_event": False,
            "forced": True,
        }, True, 0

    def execute(
        self,
        decision_id: str,
        popup_kind: str,
        inputs: dict,
        capabilities: Capabilities,
        call: ModelCall,
        *,
        day: int | None = None,
        t_h: float | None = None,
        delivered_t_h: float | None = None,
    ) -> DecisionResult:
        """Run one pop-up decision; always persists a decision record.

        ``decision_id`` is the stable natural key (e.g. the steer id): a
        record already present for it is replayed verbatim (never re-rolled).
        ``call`` is the injected model callable (WS4 wraps the real client).
        ``capabilities`` gates native vs textual transport. ``day``/``t_h``
        default to the pop-up ``time`` input (``day = int(t_h // 24)``).
        """
        if popup_kind not in (
            "tool_decide_event", "tool_decide_reply", "tool_decide_proactive",
        ):
            raise ValueError(f"unknown popup_kind: {popup_kind!r}")
        # Phase: "inform" = mention-only verdict; "decide" = full verdict.
        phase = inputs.get("phase", "decide")
        if phase not in ("inform", "decide"):
            raise ValueError(
                f"decision inputs phase must be 'inform' or 'decide', "
                f"got {phase!r}"
            )
        if day is None or t_h is None:
            derived = self._derive_clock(inputs)
            if day is None:
                day = derived[0]
            if t_h is None:
                t_h = derived[1]

        replay = self.store.decision_for_replay(decision_id)
        if replay is not None:
            return self._replay_result(decision_id, popup_kind, replay, day, t_h)

        verdict, source, transport, raw_reply, parse_failed = (
            self._obtain_verdict(
                decision_id, popup_kind, phase, inputs, capabilities, call,
                day, t_h,
            )
        )

        # Defer verdicts carry the server-filled N; model-supplied
        # defer_turns is dropped and replaced by the reason mapping.
        if popup_kind == "tool_decide_event":
            verdict = fill_defer_turns(verdict)

        verdict, forced, budget_consumed = self._apply_reply_budget(
            popup_kind, verdict, decision_id, day, t_h
        )

        record_id = self.store.record_decision(
            day,
            t_h,
            popup_kind,
            inputs.get("event_id"),
            inputs.get("event_label"),
            inputs.get("state_label"),
            str(inputs.get("time")) if inputs.get("time") is not None else None,
            json.dumps(inputs, ensure_ascii=False, sort_keys=True),
            raw_reply,
            json.dumps(verdict, ensure_ascii=False, sort_keys=True),
            source,
            transport,
            delivered_t_h,
            budget_consumed,
            replay_id=decision_id,
        )

        notice = None
        if popup_kind == "tool_decide_reply" and verdict.get("reply") is False:
            notice = build_notice(self.name, verdict, self.verbose)

        return DecisionResult(
            decision_id=decision_id,
            popup_kind=popup_kind,
            verdict=verdict,
            source=source,
            transport=transport,
            record_id=record_id,
            budget_consumed=bool(budget_consumed),
            forced=forced,
            from_replay=False,
            raw_reply=raw_reply,
            notice=notice,
            parse_failed=parse_failed,
        )

    # -- internals ----------------------------------------------------------

    def _derive_clock(self, inputs: dict) -> tuple[int, float]:
        time = inputs.get("time")
        if time is None:
            raise ValueError(
                "decision inputs carry no 'time' and no day/t_h were given"
            )
        t_h = float(time)
        return int(t_h // 24), t_h

    def _choose_transport(self, capabilities: Capabilities) -> str:
        if self.tool_mode == "native":
            return "native"
        if self.tool_mode == "textual":
            return "textual"
        return "native" if capabilities.has_native_tools else "textual"

    def _parse_raw(
        self, popup_kind: str, raw: RawReply, transport: str,
        phase: str | None = None,
    ) -> dict:
        try:
            if transport == "native" and raw.tool_calls:
                verdict = parse_native_reply(
                    popup_kind, raw.tool_calls, phase=phase
                )
            elif raw.text and raw.text.strip():
                # Textual reply or prose fallback: try the textual marker.
                verdict = parse_textual_reply(popup_kind, raw.text, phase=phase)
            else:
                raise ValueError("model returned no content at all")
        except (ValueError, json.JSONDecodeError) as exc:
            if phase == "inform" and raw.text and raw.text.strip():
                # Inform mentions may arrive as plain prose with no marker;
                # the model's own words are the mention.
                return {"message": raw.text.strip()}
            raise DecisionParseError(
                f"{popup_kind} verdict parse failed ({transport}): {exc}"
            ) from exc
        if not _valid_verdict(popup_kind, verdict, phase=phase):
            raise DecisionParseError(
                f"{popup_kind} verdict missing deciding flag ({transport}): "
                f"{verdict!r}"
            )
        return verdict

    @staticmethod
    def _raw_to_text(raw: RawReply, transport: str) -> str | None:
        if raw.tool_calls:
            return json.dumps(raw.tool_calls, ensure_ascii=False)
        return raw.text

    def _record_parse_failure(
        self, decision_id: str, popup_kind: str, transport: str,
        raw_reply: str | None, day: int, t_h: float,
    ) -> None:
        """LOUD parse failure: a state event + the raw reply persisted."""
        detail = json.dumps(
            {
                "decision_id": decision_id,
                "popup_kind": popup_kind,
                "transport": transport,
                "parse_failure_mode": self.parse_failure_mode,
                "raw_excerpt": (raw_reply or "")[:500],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.store.log_event(day, t_h, EVENT_DECISION_PARSE_FAILED, detail)

    def _no_replies_used(self, day: int, decision_id: str) -> int:
        """Accepted no-reply verdicts recorded so far this day (the budget
        window resets at day rollover: it is keyed on ``day``)."""
        return sum(
            1
            for row in self.store.decisions_for_day(day)
            if row.get("popup_kind") == "tool_decide_reply"
            and row.get("budget_consumed")
            and row.get("replay_id") != decision_id
        )

    def _draw_verdict(self, popup_kind: str, phase: str | None = None) -> dict:
        """Server-drawn verdict (decision_source=server_draw, #22). The RNG
        is injected (dedicated stream, never the day_rng draw order) so the
        draws are deterministic per seed and independent of the engine's
        stream layout."""
        if self.rng is None:
            raise DecisionError(
                "decision_source=server_draw requires an injected rng "
                "(a dedicated stream Generator)"
            )
        affirmative = float(self.rng.random()) < self.draw_p
        if popup_kind == "tool_decide_event":
            if phase == "inform":
                return {"message": SERVER_DRAW_REASON}
            return {
                "initiate": affirmative,
                "reason": SERVER_DRAW_REASON,
                "action": None,
            }
        if popup_kind == "tool_decide_proactive":
            return {
                "initiate": affirmative,
                "reason": SERVER_DRAW_REASON,
            }
        return {
            "reply": affirmative,
            "reason": SERVER_DRAW_REASON,
            "terminate_event": False,
        }

    def _replay_result(
        self, decision_id: str, popup_kind: str, record: dict,
        day: int, t_h: float,
    ) -> DecisionResult:
        """Replay path: read the recorded verdict, NEVER re-roll. The model
        is not called; a ``decision_replayed`` state event marks the read."""
        verdict = json.loads(record["verdict_json"]) if record.get(
            "verdict_json"
        ) else {}
        # Back-fill defer_turns on old defer verdicts from the recorded reason.
        if popup_kind == "tool_decide_event":
            verdict = fill_defer_turns(verdict)
        self.store.log_event(
            day, t_h, EVENT_DECISION_REPLAYED,
            json.dumps(
                {"decision_id": decision_id, "record_id": record.get("id")},
                sort_keys=True,
            ),
        )
        notice = None
        if popup_kind == "tool_decide_reply" and verdict.get("reply") is False:
            notice = build_notice(self.name, verdict, self.verbose)
        return DecisionResult(
            decision_id=decision_id,
            popup_kind=popup_kind,
            verdict=verdict,
            source="replay",
            transport="replay",
            record_id=record.get("id"),
            budget_consumed=bool(record.get("budget_consumed")),
            forced=bool((verdict or {}).get("forced")),
            from_replay=True,
            raw_reply=record.get("raw_reply"),
            notice=notice,
        )
