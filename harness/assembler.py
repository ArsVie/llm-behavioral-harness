"""Prompt assembler — CompanionSnapshot → bounded system prompt.

Stable core + day block, then the per-moment state card; sections are bounded
and dropped whole over ``MAX_PROMPT_CHARS`` (pinned ones never).
"""

from __future__ import annotations

import json
import re

from harness import images
from harness.clock import hhmm
from harness.domain import BehaviorBrief, CompanionSnapshot, GenerationControls
from harness.prompts import (
    ACTIVITY_HEADER,
    AGENDA_HEADER,
    ARCS_HEADER,
    AVAILABILITY_HIGH,
    AVAILABILITY_LOW,
    AVAILABILITY_MID,
    CLOSING_HEADER,
    FICTION_FRAME,
    FREE_WILL,
    HARNESS_PROMPT_MARKERS,
    SYSTEM_CORE_WITH_TOOLS,
)
from harness.steering import STEER_MARKER_CLOSE, STEER_MARKER_LEAD

#: Default persona core used when the caller provides none (real personas live in ``persona.core``).
DEFAULT_PERSONA_CORE = (
    "You are Nova. You keep odd hours, hold opinions you did not check with "
    "anyone first, and pay close attention to the person in front of you."
)

#: How many recent transcript turns to carry into the prompt.
RECENT_TURNS = 12

# --------------------------------------------------------------------------- #
AGENDA_ITEMS_MAX = 10
"""Cap on today's agenda items rendered into the prompt."""

LIFE_ARCS_MAX = 3
"""Cap on active life arcs rendered (plan: 1-3)."""

MAX_PROMPT_CHARS = 12000
"""Overall character budget of the assembled system prompt."""

#: Proactive opening: why she thought of him, and nothing about what to
#: write (owner, 2026-10-03: "just give her why she thought of the user and
#: let her send whatever she wants").
PROACTIVE_OPENING = "You are reaching out first. {hook}"

#: Fallback hook used when no grounded proactive intent is available.
DEFAULT_PROACTIVE_HOOK = "Something from your own day"
_PROACTIVE_STARTER = PROACTIVE_OPENING.split("{hook}", 1)[0].strip()

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
_PRIO_AVAILABILITY = 2  #: sibling of the mood brief — same eviction band
_PRIO_BEHAVIORAL = 3
_PRIO_ACTIVITY = 5
_PRIO_ARCS = 6
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
})

#: First line of a mid-day state UPDATE row: a changed card appends only the
#: sections that changed; a full card rides only at a day's first card (or
#: when the update would not be smaller). The lead NAMES the row and nothing
#: more — "only what changed" is the row's own shape, not something the model
#: needs told (owner call 2026-09-20).
STATE_UPDATE_LEAD = "STATE UPDATE:"

#: Leads earlier builds wrote. A stream that spans the rename carries both
#: shapes, and the memo loader walks exactly such a stream, so every READER
#: accepts the pair; only the renderer writes the new one.
STATE_UPDATE_LEADS = (STATE_UPDATE_LEAD, "STATE UPDATE — only what changed:")


def is_state_update(text: str) -> bool:
    """True when ``text`` opens as a section-level state UPDATE row (any lead)."""
    return text.startswith(STATE_UPDATE_LEADS)


def proactive_block(hook: str | None = None) -> str:
    """The proactive block: what reminded her of him, from the grounded hook
    (``proactive.compose_hook``; ``None`` -> the default hook)."""
    return PROACTIVE_OPENING.format(hook=_thought_of_him(
        (hook or DEFAULT_PROACTIVE_HOOK).strip()))


def _thought_of_him(hook: str) -> str:
    """"<what> reminded you of him." for a hook; a memory hook is something
    she remembered about him."""
    label, sep, rest = hook.partition(": ")
    if not sep:
        label, rest = "", hook
    if label == "Arc":
        rest = "your project " + rest.split(", next: ", 1)[0]
    elif label == "Agenda":
        # "pottery (09:30–10:30), happening now" -> "pottery (09:30–10:30, happening now)"
        m = re.fullmatch(r"(.*)\), (.+)", rest)
        rest = f"{m[1]}, {m[2]})" if m else rest
    elif label == "Finished":
        m = re.fullmatch(r"(.*), (.+ ago|just now)", rest)
        rest = "finishing " + (f"{m[1]} ({m[2]})" if m else rest)
    elif label in ("Callback", "Shared", "Memory", "Check in"):
        return f"You remembered this about him: {rest}"
    return f"{rest[:1].upper()}{rest[1:]} reminded you of him."


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
    STEER_MARKER_LEAD,            # harness.steering: steers AND pop-ups
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
    """Append a system block as its own message — the stream never rewrites."""
    text = (content or "").strip()
    if not text:
        return messages
    return messages + [{"role": "system", "content": text}]


_WIRE_EXTRA_KEYS = ("tool_calls", "tool_call_id", "name")


def wire_message(turn: dict) -> dict:
    """One store/context row as a provider message (role, content + tool keys)."""
    out: dict = {"role": turn["role"], "content": turn.get("content") or ""}
    for key in _WIRE_EXTRA_KEYS:
        if turn.get(key) is not None:
            out[key] = turn[key]
    return out


def stamp_time(text: str, t_h: float | None, anchor) -> str:
    """"hey | Time: 21:42" - a block carries the time it was delivered.

    Unanchored runs (replay/offline) return the text unchanged.
    """
    if not text or anchor is None or t_h is None:
        return text
    real = anchor.real_at(t_h)
    return f"{text} | Time: {real.hour:02d}:{real.minute:02d}"


def user_content(text: str, t_h: float | None, anchor,
                 photos: list[str] | None = None) -> str | list[dict]:
    """A user turn's wire content: the stamped text, or with photos the
    multimodal part list (:func:`harness.images.parts`).

    ONE spelling for the turn as it is first sent and as it is replayed from
    its row, so request N+1 still starts with request N byte for byte. A
    photo without a caption still carries its time.
    """
    if not photos:
        return stamp_time(text, t_h, anchor)
    if text:
        stamped = stamp_time(text, t_h, anchor)
    elif anchor is not None and t_h is not None:
        real = anchor.real_at(t_h)
        stamped = f"Time: {real.hour:02d}:{real.minute:02d}"
    else:
        stamped = ""
    return images.parts(stamped, photos)


def stamped_stream(turns: list[dict], anchor) -> list[dict]:
    """The context stream as it goes on the wire: user turns carry their clock.

    ONE builder for every request that reads the store; unanchored runs stamp
    nothing (replay parity).
    """
    out: list[dict] = []
    for turn in turns:
        message = wire_message(turn)
        if message.get("role") == "user":
            message["content"] = user_content(
                str(message.get("content", "")), _stamp_t(turn), anchor,
                images.of(turn),
            )
        out.append(message)
    return out


def build_messages(
    recent_turns: list[dict],
    user_request: str | None,
    limit: int | None = RECENT_TURNS,
    *,
    anchor=None,
    t_h: float | None = None,
    photos: list[str] | None = None,
) -> list[dict]:
    """Transcript (oldest→newest) + current user request.

    ``limit=None`` takes the turns AS GIVEN (the caller already bounded the
    span); the user request is always last, with its ``photos`` (stored
    references, :mod:`harness.images`).
    """
    pairs = recent_turns if limit is None else recent_turns[-limit:]
    messages = stamped_stream(pairs, anchor)
    if user_request is not None:
        content = user_content(user_request, t_h, anchor, photos)
        messages.append({"role": "user", "content": content})
    return messages


def _stamp_t(turn: dict) -> float | None:
    """The time a user turn is stamped with: its arrival time when the row
    carries one (a message held during a voice call and answered after it,
    ``meta.stamp_t_h``), else the row's own time."""
    meta = turn.get("meta") if isinstance(turn, dict) else None
    if isinstance(meta, str) and "stamp_t_h" in meta:
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    if isinstance(meta, dict) and isinstance(meta.get("stamp_t_h"), (int, float)):
        return float(meta["stamp_t_h"])
    return _row_t(turn)


def _row_t(turn: dict) -> float | None:
    """The virtual time of a stored turn, or None when the row does not carry one."""
    value = turn.get("t_h") if isinstance(turn, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


# --------------------------------------------------------------------------- #


def _band_line(value: float, high: str, mid: str, low: str) -> str:
    """Band-template selection shared by the availability/behavioral lines:
    prose only, never a raw number."""
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
    return [f"- {it.activity} ({hhmm(it.start_t_h)}–{hhmm(it.end_t_h)})" for it in agenda]


def render_day_block(snapshot: CompanionSnapshot) -> str:
    """Tier-2 DAY-START block: the PERSONA ONLY, byte-identical within a day.

    No per-moment state and no agenda — those live in the state card.
    """
    return (snapshot.persona.core or DEFAULT_PERSONA_CORE).strip()


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
    return "\n\n".join([FREE_WILL, block, FICTION_FRAME, SYSTEM_CORE_WITH_TOOLS])


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
    parts = [FREE_WILL,
             day_block if day_block is not None else render_day_block(snapshot),
             FICTION_FRAME,
             SYSTEM_CORE_WITH_TOOLS]
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

    # AFFECTIVE BEARING: the mood brief. Availability varies on its own
    # clock (energy), so it rides its own section — a mid-day UPDATE can
    # replace one without resending the other.
    if prompt_brief:
        sections.append((
            _PRIO_AFFECTIVE, False,
            AFFECTIVE_HEADER + "\n" + prompt_brief.strip(),
        ))
    brief = snapshot.current_behavior
    if brief is not None:
        sections.append((_PRIO_AVAILABILITY, False, _band_line(
            brief.energy, AVAILABILITY_HIGH, AVAILABILITY_MID, AVAILABILITY_LOW)))
        sections.append((_PRIO_BEHAVIORAL, False, _behavioral_bearing(brief)))

    if snapshot.current_activity is not None:
        # Current activity is exempt from budget eviction.
        sections.append(
            (_PRIO_ACTIVITY, _PINNED, f"{ACTIVITY_HEADER} {snapshot.current_activity.description}")
        )

    arcs = [a for a in snapshot.life_arcs if a.status == "active"][:LIFE_ARCS_MAX]
    if arcs:
        lines = [f"- {a.name}: {a.next_intention}" for a in arcs]
        sections.append((_PRIO_ARCS, False, ARCS_HEADER + "\n" + "\n".join(lines)))

    # No memories and no user model here: what she remembers comes from the
    # memory system (not built yet), never from the card (owner call
    # 2026-09-28).

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


_CARD_BLOCK_HEADER = re.compile(r"^([A-Za-z][A-Za-z /()&-]{2,}):")


def card_blocks(text: str) -> list[tuple[str, str]]:
    """(key, block) for every blank-line block of a card or update row.

    A block's key is the header its first line carries (``CURRENT INTENT:``,
    ``Current activity: read history``); the full block text is what a
    section-level diff compares and re-sends.
    """
    blocks: list[tuple[str, str]] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        first = block.splitlines()[0]
        match = _CARD_BLOCK_HEADER.match(first)
        blocks.append(((match.group(1) if match else first), block))
    return blocks


#: The clear marker a section-level update carries for a section that went
#: away — written by :func:`render_state_update` and read back by
#: :func:`apply_state_update`.
_CLEARED_SUFFIX = ": (cleared)"


def _leaves_quietly(key: str, block: str) -> bool:
    """A block that leaves the card without a ``(cleared)`` row: a reach-out
    is answered by her message right after it, and the CURRENT INTENT slot
    (retired 2026-10-03) only ever held a placeholder."""
    return key == CURRENT_INTENT_HEADER[:-1] or block.startswith(_PROACTIVE_STARTER)


def without_quiet_blocks(card: str) -> str:
    """``card`` minus the blocks that leave it quietly."""
    return "\n\n".join(block for key, block in card_blocks(card)
                       if not _leaves_quietly(key, block))


def render_state_update(prev_card: str, card: str) -> str:
    """The stream bytes for a changed card: only what changed.

    Section-level against the previous full card — changed sections verbatim
    and ``(cleared)`` for sections that went away — falling back to the full
    card when the update would not be smaller or nothing recognizably changed.
    """
    if not prev_card:
        return card
    prev = dict(card_blocks(prev_card))
    blocks = card_blocks(card)
    keys = {key for key, _ in blocks}
    changed = [block for key, block in blocks if prev.get(key) != block]
    cleared = [key for key, block in prev.items()
               if key not in keys and not _leaves_quietly(key, block)]
    if not changed and not cleared:
        return card
    update = "\n\n".join(
        [STATE_UPDATE_LEAD] + changed + [f"{key}{_CLEARED_SUFFIX}" for key in cleared]
    )
    return update if len(update) < len(card) else card


def apply_state_update(prev_card: str, update: str) -> str:
    """The full card a section-level update row leaves behind.

    The inverse of :func:`render_state_update` for the rows the stream
    carries: changed sections replace by key, ``(cleared)`` sections drop, a
    key the update introduces lands at the end. A row that is not an update
    row — a full card, a day-start block, anything else the stream holds —
    leaves ``prev_card`` untouched, so a loader can walk a stream and apply
    whatever it finds.
    """
    if not prev_card or not is_state_update(update):
        return prev_card
    blocks = dict(card_blocks(prev_card))
    order = [key for key, _ in card_blocks(prev_card)]
    for key, block in card_blocks(update)[1:]:
        # The clear marker is keyed by TEXT, not by the header grammar: a
        # block whose first line carries no parseable header (the per-turn
        # guidance a proactive turn writes) clears through the same suffix,
        # and `card_blocks` keys it differently on the way back in.
        if block.endswith(_CLEARED_SUFFIX):
            key = block[: -len(_CLEARED_SUFFIX)]
            blocks.pop(key, None)
            order = [k for k in order if k != key]
            continue
        if key not in blocks:
            order.append(key)
        blocks[key] = block
    return "\n\n".join(blocks[key] for key in order)


def day_plan_header(t_h: float | None, anchor, name: str | None = None) -> str:
    """Header for a day's plan, dated with the real weekday when anchored:
    "Lily's Saturday plan:" (owner, 2026-10-03), or "Saturday's plan:"
    without a ``name`` (the rows written before)."""
    if t_h is None or anchor is None:
        return AGENDA_HEADER
    try:
        weekday = anchor.real_at(t_h).strftime('%A')
    except Exception:  # noqa: BLE001 - a broken anchor must not lose the plan
        return AGENDA_HEADER
    return f"{name}'s {weekday} plan:" if name else f"{weekday}'s plan:"


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
    header = day_plan_header(t_h, anchor, snapshot.persona.name)
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
    t_h: float | None = None,
    anchor=None,
    day_block: str | None = None,
    limit: int | None = RECENT_TURNS,
    photos: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """(stable system, messages) — the cache-ordered request pair.

    STABLE system: day-start persona block + ``SYSTEM_CORE_WITH_TOOLS``.
    Messages: the context stream, then the user request when given. The state
    card is a stream row (:meth:`Session._ensure_state_card`), never a tail.
    """
    return stable_system(day_block, snapshot), build_messages(
        recent_turns, user_request, limit=limit, anchor=anchor, t_h=t_h,
        photos=photos,
    )
