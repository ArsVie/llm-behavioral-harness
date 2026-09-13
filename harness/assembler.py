"""Prompt assembler — CompanionSnapshot → bounded system prompt.

Stable core + day block, then the per-moment state card; sections are bounded
and dropped whole over ``MAX_PROMPT_CHARS`` (pinned ones never).
"""

from __future__ import annotations

from harness.clock import hhmm
from harness.domain import BehaviorBrief, CompanionSnapshot, GenerationControls
from harness.prompts import (
    ABOUT_YOU_HEADER,
    ACTIVITY_HEADER,
    AGENDA_HEADER,
    ARCS_HEADER,
    AVAILABILITY_HIGH,
    AVAILABILITY_LOW,
    AVAILABILITY_MID,
    CLOSING_HEADER,
    HARNESS_PROMPT_MARKERS,
    MEMORIES_HEADER,
    MEMORY_EVIDENCE_HEADER,
    SYSTEM_CORE_WITH_TOOLS,
)
from harness.steering import STEER_MARKER_CLOSE, STEER_MARKER_OPEN

#: Default persona core used when the caller provides none (real personas live in ``persona.core``).
DEFAULT_PERSONA_CORE = (
    "You are Nova. You keep odd hours, hold opinions you did not check with "
    "anyone first, and pay close attention to the person in front of you."
)

#: How many recent transcript turns to carry into the prompt.
RECENT_TURNS = 12

# --------------------------------------------------------------------------- #
AGENDA_ITEMS_MAX = 4
"""Cap on today's agenda items rendered into the prompt."""

LIFE_ARCS_MAX = 3
"""Cap on active life arcs rendered (plan: 1-3)."""

MEMORY_EPISODES_MAX = 6
"""Hard budget N on relevant memories rendered into the prompt."""

MEMORY_ANCHOR_CHAR_BUDGET = 400
"""Total characters of verbatim evidence anchors rendered (never truncated —
anchors that do not fit whole are dropped)."""

USER_MODEL_ASSERTIONS_MAX = 6
"""Cap on L4 user-model facts rendered into the prompt."""

MAX_PROMPT_CHARS = 12000
"""Overall character budget of the assembled system prompt."""

#: Proactive opening template.
PROACTIVE_OPENING = (
    "You are reaching out first. {hook}\n"
    "State what you are reaching out about naturally in your FIRST sentence, "
    "then open with a concrete, verifiable observation."
)

#: Fallback hook used when no grounded proactive intent is available.
DEFAULT_PROACTIVE_HOOK = (
    "Something from your own day is worth sharing — a small moment, a "
    "finished task, or a thought that surfaced."
)

# --------------------------------------------------------------------------- #
TEMPORAL_HEADER = "TEMPORAL FRAME:"
AFFECTIVE_HEADER = "AFFECTIVE BEARING:"
BEHAVIORAL_HEADER = "BEHAVIORAL BEARING:"
CURRENT_INTENT_HEADER = "CURRENT INTENT:"

#: The temporal partition's labels — the role-convention sensor scans exactly this vocabulary.
TEMPORAL_DONE_LABEL = "Done earlier"
TEMPORAL_MISSED_LABEL = "Did not happen"
TEMPORAL_NOW_LABEL = "Happening now"
TEMPORAL_LATER_LABEL = "Later today"

#: Placeholder text for the CURRENT INTENT slot.
CURRENT_INTENT_PLACEHOLDER = "No active intent."

#: Behavioral band prose templates.
_BEHAVIOR_INITIATIVE_HIGH = (
    "You tend to reach out first and carry the conversation forward."
)
_BEHAVIOR_INITIATIVE_MID = (
    "You reach out when something matters, and otherwise follow the user's lead."
)
_BEHAVIOR_INITIATIVE_LOW = (
    "You mostly follow the user's lead, letting them set the pace."
)
_BEHAVIOR_REACTIVITY_HIGH = "You respond quickly and pick up on what is said."
_BEHAVIOR_REACTIVITY_MID = "You respond readily to what is said."
_BEHAVIOR_REACTIVITY_LOW = "You respond at your own pace, unhurried."
_BEHAVIOR_PERSISTENCE_HIGH = (
    "You tend to stay in the conversation and see it through."
)
_BEHAVIOR_PERSISTENCE_MID = (
    "You stay in the conversation while it keeps meaning something."
)
_BEHAVIOR_PERSISTENCE_LOW = "Your participation tends to wind down quickly."

# --------------------------------------------------------------------------- #
_PRIO_AGENDA = 0
_PRIO_AFFECTIVE = 2
_PRIO_BEHAVIORAL = 3
_PRIO_CURRENT_INTENT = 4
_PRIO_ACTIVITY = 5
_PRIO_ARCS = 6
_PRIO_MEMORIES = 7
_PRIO_USER_MODEL = 8
_PRIO_PROACTIVE = 9
_PRIO_CLOSING = 10
_PRIO_POPUP = 11

#: Pinned sections are exempt from budget eviction.
_PINNED = True

#: Sections that are DAY-scoped, not per-moment: emitted once at rollover into
#: the message stream, never into the system prefix or the per-turn card.
_DAY_SCOPED_PRIOS: frozenset[int] = frozenset({
    _PRIO_AGENDA,       # today's plan
    _PRIO_ARCS,         # active life arcs
    _PRIO_USER_MODEL,   # L4 conclusions about him
})


def proactive_block(hook: str | None = None) -> str:
    """The proactive block — opening + grounded hook verbatim (``None`` -> default hook)."""
    return PROACTIVE_OPENING.format(hook=(hook or DEFAULT_PROACTIVE_HOOK).strip())


#: Message keys carried through to the provider beyond role/content — a decision
#: replay breaks without ``tool_calls``/``tool_call_id``; blocks join on "\n\n".
SYSTEM_BLOCK_SEPARATOR = "\n\n"


def wire_pair(message: dict) -> tuple:
    """The cache-relevant identity of one message: role + content."""
    return (message.get("role"), message.get("content"))


def prefix_break(previous: list, current: list) -> int | None:
    """First index where ``current`` stops extending ``previous``; ``None`` = append-only."""
    for index, message in enumerate(previous):
        if index >= len(current) or wire_pair(current[index]) != wire_pair(message):
            return index
    return None


#: Tokens ONLY the harness writes: ``[STEER ...]`` and the labelled section
#: vocabulary. A hit inside a USER-role message means the convention broke.
HARNESS_MARKER_TOKENS = (
    STEER_MARKER_OPEN,            # harness.steering: steers AND pop-ups
    STEER_MARKER_CLOSE,
    *HARNESS_PROMPT_MARKERS,      # prompts.py: the labelled section vocabulary
    TEMPORAL_HEADER,
    AFFECTIVE_HEADER,
    BEHAVIORAL_HEADER,
    CURRENT_INTENT_HEADER,
    TEMPORAL_DONE_LABEL,
    TEMPORAL_MISSED_LABEL,
    TEMPORAL_NOW_LABEL,
    TEMPORAL_LATER_LABEL,
)


def harness_text_in_user_roles(messages: list) -> list[int]:
    """Indices of user-role messages carrying harness-written marker text.

    User-role content is whatever the user said; a hit is a leak, not a drop.
    """
    hits: list[int] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and any(t in content for t in HARNESS_MARKER_TOKENS):
            hits.append(index)
    return hits


def append_system(messages: list[dict], content: str | None) -> list[dict]:
    """Append a system block, FOLDING it into a trailing system message.

    The wire never carries two system messages in a row: blocks join on a blank
    line so the tail reads ``... -> assistant -> system -> (reply)``.
    """
    text = (content or "").strip()
    if not text:
        return messages
    if messages and messages[-1].get("role") == "system":
        merged = dict(messages[-1])
        merged["content"] = (
            (merged.get("content") or "").rstrip()
            + SYSTEM_BLOCK_SEPARATOR + text
        )
        return messages[:-1] + [merged]
    return messages + [{"role": "system", "content": text}]


_WIRE_EXTRA_KEYS = ("tool_calls", "tool_call_id", "name")


def wire_message(turn: dict) -> dict:
    """One store/context row as a provider message (role, content + tool keys)."""
    out: dict = {"role": turn["role"], "content": turn.get("content") or ""}
    for key in _WIRE_EXTRA_KEYS:
        if turn.get(key) is not None:
            out[key] = turn[key]
    return out


def _stamp_user_turn(text: str, t_h: float | None, anchor) -> str:
    """"hey | Time: 21:42" - a user turn carries the time it arrived.

    Unanchored runs (replay/offline) return the text unchanged.
    """
    if not text or anchor is None or t_h is None:
        return text
    real = anchor.real_at(t_h)
    return f"{text} | Time: {real.hour:02d}:{real.minute:02d}"


def stamped_stream(turns: list[dict], anchor) -> list[dict]:
    """The context stream as it goes on the wire — user turns carry their clock.

    ONE builder for every request that reads the store; unanchored runs stamp
    nothing (replay parity).
    """
    out: list[dict] = []
    for turn in turns:
        message = wire_message(turn)
        if message.get("role") == "user":
            message["content"] = _stamp_user_turn(
                str(message.get("content", "")), _row_t(turn), anchor
            )
        out.append(message)
    return out


def build_messages(
    recent_turns: list[dict],
    user_request: str,
    limit: int | None = RECENT_TURNS,
    *,
    anchor=None,
    t_h: float | None = None,
) -> list[dict]:
    """Transcript (oldest→newest) + current user request.

    ``limit=None`` takes the turns AS GIVEN (the caller already bounded the
    span); the user request is always last.
    """
    pairs = recent_turns if limit is None else recent_turns[-limit:]
    messages = stamped_stream(pairs, anchor)
    if user_request is not None:
        messages.append(
            {"role": "user", "content": _stamp_user_turn(user_request, t_h, anchor)}
        )
    return messages


def _row_t(turn: dict) -> float | None:
    """The virtual time of a stored turn, or None when the row does not carry one."""
    value = turn.get("t_h") if isinstance(turn, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


# --------------------------------------------------------------------------- #


def _local_hour(t_h: float) -> str:
    """HH:MM of the local hour for an absolute t_h (see ``clock.hhmm``)."""
    return hhmm(t_h)


def _agenda_lines(items) -> list[str]:
    lines = []
    for it in items:
        lines.append(f"- {it.activity} ({_local_hour(it.start_t_h)}–{_local_hour(it.end_t_h)})")
    return lines


def _memory_lines(snapshot: CompanionSnapshot) -> tuple[list[str], list[str]]:
    """(episode lines, anchor lines) — both capped by their budgets.

    Anchors are verbatim excerpts; the caller renders the evidence header first.
    """
    episodes = snapshot.memory_context.episodes[:MEMORY_EPISODES_MAX]
    ep_lines = [f"- {e.summary}" for e in episodes]
    anchor_lines: list[str] = []
    chars = 0
    for e in episodes:
        for a in e.verbatim_anchors:
            if chars + len(a) > MEMORY_ANCHOR_CHAR_BUDGET:
                continue
            anchor_lines.append(f'  anchor: "{a}"')
            chars += len(a)
    return ep_lines, anchor_lines


def _user_model_lines(snapshot: CompanionSnapshot) -> list[str]:
    """L4 user-model facts — derived conclusions, not verbatim quotes (no marker)."""
    um = snapshot.memory_context.user_model
    if um is None:
        return []
    facts: list = []
    for bucket in (
        um.stable_preferences,
        um.current_preferences,
        um.boundaries,
        um.vulnerabilities,
        um.recurring_interests,
        um.relationship_patterns,
        um.important_entities,
    ):
        facts.extend(bucket)
    return [f"- {a.value}" for a in facts[:USER_MODEL_ASSERTIONS_MAX]]


def _availability_line(brief: BehaviorBrief) -> str | None:
    """Energy/availability prose — template selection only, never a raw number."""
    if brief.energy > 0.7:
        return AVAILABILITY_HIGH
    if brief.energy < 0.35:
        return AVAILABILITY_LOW
    return AVAILABILITY_MID


def _band_line(value: float, high: str, mid: str, low: str) -> str:
    """Band-template selection shared by the availability/behavioral lines."""
    if value > 0.7:
        return high
    if value < 0.35:
        return low
    return mid


def _behavioral_bearing(brief: BehaviorBrief) -> str:
    """BEHAVIORAL BEARING prose — behavioral channels as band templates, never
    raw floats. The BehaviorTrace is never rendered."""
    return BEHAVIORAL_HEADER + "\n" + "\n".join(
        (
            _band_line(
                brief.initiative,
                _BEHAVIOR_INITIATIVE_HIGH,
                _BEHAVIOR_INITIATIVE_MID,
                _BEHAVIOR_INITIATIVE_LOW,
            ),
            _band_line(
                brief.reactivity,
                _BEHAVIOR_REACTIVITY_HIGH,
                _BEHAVIOR_REACTIVITY_MID,
                _BEHAVIOR_REACTIVITY_LOW,
            ),
            _band_line(
                1.0 - brief.closing_tendency,
                _BEHAVIOR_PERSISTENCE_HIGH,
                _BEHAVIOR_PERSISTENCE_MID,
                _BEHAVIOR_PERSISTENCE_LOW,
            ),
        )
    )


def _agenda_plan_lines(snapshot: CompanionSnapshot) -> list[str]:
    """Today's agenda plan lines — planned/shifted only, capped at
    ``AGENDA_ITEMS_MAX``; shared by the day block and the state-card AGENDA."""
    agenda = [
        it for it in snapshot.agenda if it.status in ("planned", "shifted")
    ][:AGENDA_ITEMS_MAX]
    return _agenda_lines(agenda)


def render_day_block(snapshot: CompanionSnapshot) -> str:
    """Tier-2 DAY-START block: the PERSONA ONLY, byte-identical within a day.

    No per-moment state and no agenda — those live in the state card.
    """
    core = (snapshot.persona.core or DEFAULT_PERSONA_CORE).strip()
    return core


def stable_system(day_block: str | None, snapshot: CompanionSnapshot | None = None) -> str:
    """The STABLE system — the base prefix of every request.

    ``day_block`` is the session's cached persona block; without it the block
    renders from ``snapshot``.
    """
    if day_block is not None:
        block = day_block
    else:
        assert snapshot is not None, "a day block needs the cache or a snapshot"
        block = render_day_block(snapshot)
    return "\n\n".join([block, SYSTEM_CORE_WITH_TOOLS])


def assemble_snapshot(
    snapshot: CompanionSnapshot,
    *,
    controls: GenerationControls | None = None,
    prompt_brief: str | None = None,
    popup: str | None = None,
    day_block: str | None = None,
) -> str:
    """Assemble ONE system prompt from a ``CompanionSnapshot`` (3-tier).

    Stable core + day block, then the state-card sections. Over
    ``MAX_PROMPT_CHARS`` whole sections drop from lowest priority upward, and
    pinned sections always stay.
    """
    sections = _state_card_sections(
        snapshot, controls=controls, prompt_brief=prompt_brief,
        popup=popup,
    )
    # Stable parts first, then the budget-trimmed state card.
    parts = [day_block if day_block is not None else render_day_block(snapshot)]
    parts.append(SYSTEM_CORE_WITH_TOOLS)
    return _join_stable_plus_sections(parts, sections)


def _state_card_sections(
    snapshot: CompanionSnapshot,
    *,
    controls: GenerationControls | None,
    prompt_brief: str | None,
    popup: str | None,
) -> list[tuple[int, bool, str]]:
    """The VOLATILE state-card sections (never the stable core or day block)."""
    sections: list[tuple[int, bool, str]] = []

    agenda_lines = _agenda_plan_lines(snapshot)
    if agenda_lines:
        # Agenda section is exempt from budget eviction.
        sections.append(
            (_PRIO_AGENDA, _PINNED, AGENDA_HEADER + "\n" + "\n".join(agenda_lines))
        )

    # AFFECTIVE BEARING: mood brief line plus availability line.
    affective: list[str] = []
    if prompt_brief:
        affective.append(prompt_brief.strip())
    if snapshot.current_behavior is not None:
        availability = _availability_line(snapshot.current_behavior)
        if availability:
            affective.append(availability)
    if affective:
        sections.append((_PRIO_AFFECTIVE, False, AFFECTIVE_HEADER + "\n" + "\n".join(affective)))

    if snapshot.current_behavior is not None:
        sections.append((_PRIO_BEHAVIORAL, False, _behavioral_bearing(snapshot.current_behavior)))

    # CURRENT INTENT: reserved placeholder slot, exempt from budget eviction.
    sections.append(
        (
            _PRIO_CURRENT_INTENT,
            _PINNED,
            CURRENT_INTENT_HEADER + "\n" + CURRENT_INTENT_PLACEHOLDER,
        )
    )

    if snapshot.current_activity is not None:
        # Current activity is exempt from budget eviction.
        sections.append(
            (_PRIO_ACTIVITY, _PINNED, f"{ACTIVITY_HEADER} {snapshot.current_activity.description}")
        )

    arcs = [a for a in snapshot.life_arcs if a.status == "active"][:LIFE_ARCS_MAX]
    if arcs:
        lines = [f"- {a.name} — {a.next_intention}" for a in arcs]
        sections.append((_PRIO_ARCS, False, ARCS_HEADER + "\n" + "\n".join(lines)))

    ep_lines, anchor_lines = _memory_lines(snapshot)
    if ep_lines:
        sections.append(
            (
                _PRIO_MEMORIES,
                False,
                MEMORIES_HEADER
                + "\n"
                + MEMORY_EVIDENCE_HEADER
                + "\n"
                + "\n".join(ep_lines + anchor_lines),
            )
        )

    um_lines = _user_model_lines(snapshot)
    if um_lines:
        sections.append((_PRIO_USER_MODEL, False, ABOUT_YOU_HEADER + "\n" + "\n".join(um_lines)))

    if snapshot.proactive_intent is not None:
        sections.append(
            (_PRIO_PROACTIVE, False, proactive_block(snapshot.proactive_intent.hook))
        )

    if controls is not None and controls.closing_guidance:
        sections.append(
            (_PRIO_CLOSING, False, f"{CLOSING_HEADER} {controls.closing_guidance}")
        )

    if popup:
        # The pop-up block is exempt from budget eviction.
        sections.append((_PRIO_POPUP, _PINNED, popup))

    return sections


def _join_stable_plus_sections(
    stable_parts: list[str],
    sections: list[tuple[int, bool, str]],
) -> str:
    """Join stable parts + budget-enforced state-card sections.

    Whole sections from highest priority down while the total fits; never mangle
    text and never drop a pinned section.
    """
    ordered = sorted(sections, key=lambda item: item[0])
    total = sum(len(p) + 2 for p in stable_parts)
    kept: list[str] = []
    for _, pinned, text in ordered:
        cost = len(text) + 2  # +2 for the blank line separator
        if pinned or total + cost <= MAX_PROMPT_CHARS:
            kept.append(text)
            total += cost
    return "\n\n".join(stable_parts + kept)


def render_state_card(
    snapshot: CompanionSnapshot,
    *,
    controls: GenerationControls | None = None,
    prompt_brief: str | None = None,
    popup: str | None = None,
) -> str:
    """The PER-MOMENT state card — what is true right now and nothing else.

    Day-scoped material is not here (it goes out once at rollover). Returns ""
    when no section survives.
    """
    sections = [
        s for s in _state_card_sections(
            snapshot, controls=controls, prompt_brief=prompt_brief,
            popup=popup,
        )
        if s[0] not in _DAY_SCOPED_PRIOS
    ]
    return _join_stable_plus_sections([], sections)


def _day_plan_header(t_h: float | None, anchor) -> str:
    """Header for a day's plan, dated with the real weekday when anchored."""
    if t_h is None or anchor is None:
        return AGENDA_HEADER
    try:
        return f"{anchor.real_at(t_h).strftime('%A')}'s plan:"
    except Exception:  # noqa: BLE001 - a broken anchor must not lose the plan
        return AGENDA_HEADER


def render_day_start_block(
    snapshot: CompanionSnapshot, *, t_h: float | None = None, anchor=None,
) -> str:
    """The DAY-scoped block (plan, arcs, user model): emitted once at rollover.

    Goes into the message stream as one system message, so it is sent once and
    then rides the cached prefix all day. ``t_h``/``anchor`` only date the plan
    header. Returns "" when the day has nothing to state.
    """
    sections = [
        s for s in _state_card_sections(
            snapshot, controls=None, prompt_brief=None, popup=None,
        )
        if s[0] in _DAY_SCOPED_PRIOS
    ]
    header = _day_plan_header(t_h, anchor)
    if header != AGENDA_HEADER:
        sections = [
            (prio, pinned, text.replace(AGENDA_HEADER, header, 1)
             if prio == _PRIO_AGENDA else text)
            for prio, pinned, text in sections
        ]
    return _join_stable_plus_sections([], sections)


def build_context_messages(
    snapshot: CompanionSnapshot,
    recent_turns: list[dict],
    user_request: str | None = None,
    *,
    controls: GenerationControls | None = None,
    prompt_brief: str | None = None,
    popup: str | None = None,
    t_h: float | None = None,
    anchor=None,
    day_block: str | None = None,
    limit: int | None = RECENT_TURNS,
) -> tuple[str, list[dict]]:
    """(stable system, messages) — the cache-ordered request pair.

    STABLE system: day-start persona block + ``SYSTEM_CORE_WITH_TOOLS``.
    Messages: the context stream, then the user request when given, then the
    volatile state card as a TRAILING system message.
    """
    system = stable_system(day_block, snapshot)
    if user_request is not None:
        messages = build_messages(
            recent_turns, user_request, limit=limit, anchor=anchor, t_h=t_h,
        )
    else:
        messages = stamped_stream(
            recent_turns if limit is None else recent_turns[-limit:], anchor
        )
    tail = render_state_card(
        snapshot,
        controls=controls, prompt_brief=prompt_brief, popup=popup,
    )
    messages = append_system(messages, tail)
    return system, messages
