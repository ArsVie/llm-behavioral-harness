"""Pop-up decision tools: the server draws the inputs, the model returns a
verdict + reason; both are recorded as state and a replay never re-rolls.
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

#: Inform-phase variant of ``tool_decide_event`` (mention only, no action).
def offered_tools(request: PopupRequest) -> list[dict]:
    """The schema matching ``request.popup_kind``; unknown kinds get the full set."""
    wanted = [t for t in request.tools if t.get("name") == request.popup_kind]
    return wanted or list(request.tools)


def tools_identity(tools: list[dict] | None) -> tuple[str | None, list[str]]:
    """(stable hash of the ``tools`` payload, tool names); ``(None, [])`` when empty."""
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

    Recorded as a ``decision_parse_failed`` state event before it is raised.
    """


class DecisionRequeue(DecisionError):
    """Parse-failure policy ``requeue``: raise so the caller re-queues the pop-up."""


@dataclass
class Capabilities:
    """Client capabilities: whether native tool calls are available."""

    has_native_tools: bool = False


@dataclass(frozen=True)
class RawReply:
    """The model's raw output for one pop-up, persisted verbatim.

    ``text`` (textual transport) and/or ``tool_calls`` (native); the runner
    prefers the tool call when both are set.
    """

    text: str | None = None
    tool_calls: list[dict] | None = None


@dataclass(frozen=True)
class PopupRequest:
    """Everything the injected model callable needs to make the LLM call.

    ``popup`` is the block the model reads; ``tools`` is what it may call
    (mention-only on inform legs, the verdict schema on decide legs).
    """

    popup_kind: str
    popup: str
    tools: list[dict]
    native: bool
    inputs: dict
    #: Requirement restated for a re-ask (``NATIVE_REASK``); None on a first attempt.
    nudge: str | None = None


#: Appended to a native pop-up whose reply carried no tool call AND no text;
#: used for at most one extra call.
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
    """Render a pop-up time input as HH:MM; anything non-numeric passes through."""
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
    """Render the pop-up block for ``popup_kind`` from ``inputs``.

    ``time``/``valid_until`` render as HH:MM and ``inputs`` is NOT mutated.
    Negotiation lines (``phase``, ``skippable``, ``delay_count``,
    ``window_ending``) appear only when the caller supplies those keys.
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
    """Extract the brace-balanced payload beginning at ``text[start] == '{'``;
    nested braces inside string values do not truncate it.
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

    Accepts a JSON object (missing optionals default) or the
    ``{yes, "too tired"}`` shorthand mapped onto the pop-up kind's flag key.
    ``phase="inform"`` on decide_event expects ``{message: str}``, normalizing
    the legacy forms onto it (``message := reason``). Raises ``ValueError``
    on anything else.
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
        # One-shot: no action/defer dimension; conservative default: decline.
        return _proactive_verdict(obj)
    if popup_kind == "tool_decide_reply":
        return _reply_verdict(obj)
    raise ValueError(f"unknown popup_kind: {popup_kind!r}")


def _inform_verdict(obj: dict) -> dict:
    """The inform leg: a natural mention, no action.

    Legacy ``reason`` text normalizes onto ``message`` and is preserved.
    """
    message = obj.get("message")
    if not isinstance(message, str):
        message = obj["reason"] if isinstance(obj.get("reason"), str) else ""
    verdict: dict = {"message": message}
    if isinstance(obj.get("reason"), str):
        verdict["reason"] = obj["reason"]
    return verdict


#: The model-facing tri-state ``initiate`` mapped onto the canonical
#: ``(initiate, action)`` pair consumers speak.
_INITIATE_TRISTATE: dict[str, tuple[bool, str]] = {
    "yes": (True, "follow"),
    "no": (False, "abandon"),
    "defer": (False, "defer"),
}


def _event_verdict(obj: dict) -> dict:
    """The decide leg for an agenda event.

    Tri-state ``initiate`` (yes/no/defer) collapses onto the canonical
    ``{initiate: bool, reason, action: follow|abandon|defer}``; a defer carries
    ``initiate: False``. Defaults are conservative: no initiation unless the
    model said so, and an unrecognised ``action`` is dropped.
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
    """A model-supplied defer N, clamped to [DEFER_N_MIN, DEFER_N_MAX]; None
    when absent or unusable. Bools are rejected (``True`` is an ``int``)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return min(max(n, DEFER_N_MIN), DEFER_N_MAX)


def _proactive_verdict(obj: dict) -> dict:
    """The decide leg for a grounded proactive intent.

    One-shot ``{initiate: bool, reason: str}``; defaults to declining.
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
    """True when the deciding flag is a real bool; an inform verdict needs a
    non-empty mention (a silent inform is a protocol failure)."""
    if popup_kind == "tool_decide_event" and phase == "inform":
        return isinstance(verdict.get("message"), str) and bool(
            verdict["message"].strip()
        )
    return isinstance(verdict.get(_verdict_key(popup_kind)), bool)


def parse_textual_reply(
    popup_kind: str, text: str, phase: str | None = None,
) -> dict:
    """Find the ``tool_decide_*: {...}`` marker in reply text and parse the payload.

    Tolerant of quotes, linebreaks and surrounding prose.
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


def _salvage_arguments(popup_kind: str, text: str, phase: str | None) -> dict:
    """Read a verdict out of non-JSON native arguments.

    Raises only when the text carries no payload at all.
    """
    try:
        return parse_textual_reply(popup_kind, text, phase=phase)
    except ValueError:
        start = text.find("{")
        payload = _brace_payload(text, start) if start >= 0 else None
        if payload is None:
            raise
        return parse_verdict(popup_kind, payload, phase=phase)


def parse_native_reply(
    popup_kind: str, tool_calls: list[dict], phase: str | None = None,
) -> dict:
    """Extract the verdict from a native function-calling response.

    ``tool_calls`` entries are the OpenAI shape; the first call named
    ``popup_kind`` wins. ``phase`` selects the inform verdict shape.
    """
    for call in tool_calls or []:
        fn = call.get("function") or {}
        name = fn.get("name", "")
        if name != popup_kind:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            text = args.strip()
            if text:
                try:
                    args = json.loads(text)
                except ValueError:
                    # Malformed arguments are salvaged, not rejected: the raw
                    # text is re-read as a textual reply before any failure.
                    return _salvage_arguments(popup_kind, text, phase)
            else:
                args = {}
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
    """Server-fill ``defer_turns`` on a defer verdict (``action == 'defer'``).

    The model's own ``turns`` wins; otherwise N is mapped from the reason text
    (:func:`map_defer_turns`). Other verdicts pass through untouched.
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

    verbose OFF: ``"{name} saw your message but chose not to reply yet"``;
    verbose ON: ``"{name} is not replying, reason: {Reason}"``.
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
    """Load the decision configuration from the environment (see the module
    docstring for the env vars)."""
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

    One ``execute`` call per pop-up. A record already present for
    ``decision_id`` is replayed verbatim -- the model is NEVER called again.
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

        Returns ``(verdict, source, transport, raw_reply, parse_failed)``. A
        parse failure is always RECORDED before the policy decides what next.
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
            # Neither a tool call nor text: ask once more with the requirement
            # stated before calling it a parse failure.
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

        Only a ``reply: false`` verdict spends budget; at exhaustion the
        verdict is REPLACED by a forced reply, logged with the budget that
        triggered it. Proactive decisions never touch the budget.
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

        ``decision_id`` is the stable natural key: a record already present for
        it is replayed verbatim (never re-rolled). ``day``/``t_h`` default to
        the pop-up ``time`` input.
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

        # Defer verdicts carry the server-filled N.
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
        """Accepted no-reply verdicts recorded so far this day (keyed on ``day``)."""
        return sum(
            1
            for row in self.store.decisions_for_day(day)
            if row.get("popup_kind") == "tool_decide_reply"
            and row.get("budget_consumed")
            and row.get("replay_id") != decision_id
        )

    def _draw_verdict(self, popup_kind: str, phase: str | None = None) -> dict:
        """Server-drawn verdict (``decision_source=server_draw``) from the
        injected RNG (a dedicated stream, never the day_rng draw order)."""
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
        """Replay path: read the recorded verdict, NEVER re-roll. The model is
        not called; a ``decision_replayed`` state event marks the read."""
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
