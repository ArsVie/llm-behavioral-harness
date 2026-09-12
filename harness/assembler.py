"""Prompt assembler — CompanionSnapshot → bounded system prompt (W-E1 + Wave 2 + v2).

Wave 2 (A1 central integration): the system prompt is assembled from a
``CompanionSnapshot`` — the single place where the lanes (persona, behavior,
life, memory, conversation, proactive intent) meet before composition.
Sections are BOUNDED by construction (persona core, behavioral guidance,
activity, capped agenda, 1-3 life arcs, N memories with N =
``MEMORY_EPISODES_MAX``, proactive intent block when present).

Context construction v2: the assembled prompt is the full THREE-TIER context.
Order changed 2026-09-07 — the PERSONA now leads, so the model reads who it is
before it reads how to handle its state card:

  1. DAY-START block — the PERSONA block, rendered once per day
     (``render_day_block``); the day-plan agenda lives in the STATE CARD
     (tier 3) so this tier is fully stable (byte-identical every turn).
  2. STABLE rules core — ``prompts.SYSTEM_CORE_WITH_TOOLS`` (constant,
     contains NO state): how to hold the state card, plus the tool protocol.
  3. STATE CARD — mood brief (``BehaviorDirective.prompt_brief``, the SINGLE
     source, rendered VERBATIM with no label of its own), energy/availability,
     current activity, pulled memories (quoted evidence), user-model facts,
     proactive intent if any, arriving-event pop-up when injected.

W2+W3 (time-aware, sectioned card): fixed-order named sections —
``TEMPORAL FRAME`` (rendered ONLY when anchored, never raw ``t_h``),
``AFFECTIVE BEARING``, ``BEHAVIORAL BEARING`` (band prose, never floats),
``CURRENT INTENT`` (reserved slot until S5). Agenda statuses transition
``planned → completed`` as windows pass (``life.transition_past_windows``,
persisted per turn by the session) so render and store agree.

Prompt boundary (Iteration-2 A5, invariants 14/15/16): raw recent dialogue
is NEVER rendered into the system prompt — it lives in the user/assistant
message payload only (``build_messages``), so each turn appears exactly
once. Verbatim memory anchors are structurally marked as QUOTED historical
conversation (``MEMORY_EVIDENCE_HEADER``) so user-authored text retrieved
as memory can never silently gain system-level instruction authority. The
assembler never receives engine state: the snapshot carries only domain
objects, and no section renders cycle/phase/hormone internals.

Structural prompt cache: the assembled request is split into a STABLE prefix
and a VOLATILE tail. The stable prefix — the day-start PERSONA block plus the
``SYSTEM_CORE_WITH_TOOLS`` rules core — is byte-identical every turn and across
conversations for a fixed profile. The volatile tail — the state card
(temporal frame / affective / behavioral bearing / current intent, activity,
arcs, memories, about-you, proactive, pop-up + the agenda plan) — rides as a
TRAILING system message via ``build_context_messages``. Caching is purely
structural; no ``cache_control`` field exists on this provider.

Request N+1 is an extension of request N, and that is ASSERTED rather than
asserted-in-prose: ``tests/test_cache_prefix_gate.py`` walks every provider
call of a mixed day and fails if any one of them is not a byte-prefix
extension of the previous. It holds because the session reads its transcript
from a stored compaction epoch (``Session._context_turns``), not from a
rolling tail — front-truncation would move the first byte after the system
message every turn. Two provider facts make that the load-bearing choice
rather than a refinement: matching is strict from token 0, and the storage
unit is 64 tokens with reliable hits only around a ~1024-token shared prefix,
which the ~180-token stable prefix cannot reach alone.

``assemble_snapshot`` builds the same three tiers as one string, for aux
callers and tests. It is no longer byte-identical to the pre-2026-09-07
layout (persona/rules order swapped, rules condensed, closing section and
mood-brief label dropped); ``tests/test_prompt_cache_order.py`` pins the
current bytes so an unlabelled change still fails.

Leakage invariant (frozen): this module never receives engine state — the
snapshot carries only domain objects. The rendered behavioral prose and all
section headers contain no raw numbers, no ``mu``/``eta``, no phase labels,
no ``cycle_day``. The proactive block renders the intent's CONCRETE HOOK
verbatim — never "Contact reason: <reason>".

Budget enforcement (frozen + v2): ``MAX_PROMPT_CHARS`` cap with deterministic
WHOLE-SECTION drops from lowest priority upward (text is never mangled);
per-section budgets cap every section by construction. v2 addition: the
decision/steering payload sections — current activity and the event/pop-up
block — are PINNED: drop rules evict every other section first and never
drop them.

The legacy W-E1 entry point ``build_messages`` (transcript tail + user
request) is preserved verbatim for pre-slice callers.
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
    MEMORIES_HEADER,
    MEMORY_EVIDENCE_HEADER,
    SYSTEM_CORE_WITH_TOOLS,
)

#: Default persona core used when the caller provides none.
#:
#: A persona says what someone IS and how they behave. It does not define
#: itself by negation ("you are not an assistant") and does not list generic
#: companion traits -- both read as hedging to the model and produce the safe,
#: middle-of-the-road voice they were meant to prevent. Real personas live in
#: ``persona.core`` (per-profile data); this is the stand-in for callers that
#: supply none.
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
    "then open with a concrete, verifiable observation. Never guilt-trip, nag, "
    "or imply the user owes you contact."
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
_PRIO_TEMPORAL = 1
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

#: Sections that are DAY-scoped, not per-moment.
#:
#: These change at most a few times a day — the plan itself, her arcs, what
#: the memory system concluded about him — yet the state card re-sent all of
#: them on every single turn. Measured on the live store, the card at 10:00
#: and at 18:42 differed by ONE line (``Current activity``); everything else
#: was byte-identical and re-transmitted ~270 chars per turn.
#:
#: They cannot live in the stable system prefix either — that must be
#: byte-identical all day, and the agenda mutates as windows pass (which is
#: exactly why ``render_day_block`` pushed the agenda out to the tail). The
#: place that satisfies both constraints is the message STREAM: emitted once
#: when the day rolls over, then inside the cached prefix for every
#: subsequent turn of that day, because the stream is append-only.
_DAY_SCOPED_PRIOS: frozenset[int] = frozenset({
    _PRIO_AGENDA,       # today's plan
    _PRIO_ARCS,         # active life arcs
    _PRIO_USER_MODEL,   # L4 conclusions about him
})


def proactive_block(hook: str | None = None) -> str:
    """The proactive system-prompt block: opening + grounded hook verbatim.

    ``hook=None`` (legacy ungrounded calls) renders the generic opening with
    ``DEFAULT_PROACTIVE_HOOK`` — no invented source claim.
    """
    return PROACTIVE_OPENING.format(hook=(hook or DEFAULT_PROACTIVE_HOOK).strip())


#: Message keys carried through to the provider beyond role/content.
#:
#: A recorded decision replays as a NATIVE tool exchange (assistant
#: ``tool_calls`` + ``role="tool"`` result). Copying only role and content --
#: which every call site here used to do -- silently drops the pairing and
#: the provider rejects an assistant tool_calls message with no result, so
#: the keys have to survive the copy.
#: Separator between blocks folded into one trailing system message.
SYSTEM_BLOCK_SEPARATOR = "\n\n"


def wire_pair(message: dict) -> tuple:
    """The cache-relevant identity of one message: role + content.

    Compared as pairs so a rebuilt message object carrying identical text is
    not mistaken for a change (the provider sees bytes, not object identity).
    """
    return (message.get("role"), message.get("content"))


def prefix_break(previous: list, current: list) -> int | None:
    """The first index at which ``current`` stops extending ``previous``.

    Port of DeepSeek-Harness's derivation invariant (``invariant.ts``: the
    request must equal the derived surface): the prefix cache only ever pays
    for a byte-identical HEAD, so a request that rewrites an earlier message
    silently re-bills the whole prompt. ``None`` means append-only.
    """
    for index, message in enumerate(previous):
        if index >= len(current) or wire_pair(current[index]) != wire_pair(message):
            return index
    return None


def append_system(messages: list[dict], content: str | None) -> list[dict]:
    """Append a system block, FOLDING it into a trailing system message.

    Two adjacent ``role="system"`` messages are the bug behind the 2026-09-08
    live leak. The turn's tail could stack up to four of them — state card,
    injections, decided-notes, and on an aux call the pop-up — with the last
    assistant turn far behind. The model reads a run of system blocks as one
    undifferentiated instruction block and answers whichever question it
    latches onto: a pop-up as prose, or a conversational turn as a tool call
    (``<｜｜DSML｜｜tool_calls>`` reached the channel verbatim).

    So the wire never carries two system messages in a row. Blocks fold into
    one, separated by a blank line, and the shape stays
    ``... -> assistant -> system -> (reply)``: exactly one place where the
    model is being addressed, immediately before it answers.

    Folding rather than interleaving a synthetic assistant turn is deliberate
    — an assistant message the model never produced is a lie in its own
    history. Replayed DECISIONS keep their real
    ``assistant tool_calls -> role="tool"`` exchange (see
    ``session._decision_context_messages``); this is only about the live tail.
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
    """One store/context row as a provider message.

    Keeps role and content (``None`` normalized to ``""``) plus any tool
    keys the row carries, and nothing else -- store rows also hold ids,
    timestamps and day indices that must never reach the wire.
    """
    out: dict = {"role": turn["role"], "content": turn.get("content") or ""}
    for key in _WIRE_EXTRA_KEYS:
        if turn.get(key) is not None:
            out[key] = turn[key]
    return out


def build_messages(
    recent_turns: list[dict],
    user_request: str,
    limit: int | None = RECENT_TURNS,
) -> list[dict]:
    """Transcript (oldest→newest) + current user request.

    `recent_turns` are store message rows ({role, content, ...}); only role
    and content are used. Assistant turns are included so the model keeps
    style continuity; the user request is always last.

    ``limit=None`` takes the turns AS GIVEN -- the caller has already bounded
    the span (the session's compaction epoch). A numeric limit keeps the
    legacy tail slice for pre-epoch callers; note that slicing from the FRONT
    is what breaks prefix reuse, so the mainline passes None.
    """
    messages: list[dict] = [
        wire_message(turn)
        for turn in (recent_turns if limit is None else recent_turns[-limit:])
    ]
    messages.append({"role": "user", "content": user_request})
    return messages


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

    Anchors are verbatim user/companion excerpts; they are rendered with the
    ``MEMORY_EVIDENCE_HEADER`` marking the whole block as quoted historical
    conversation, never as instructions (invariant 15). The caller renders
    the header BEFORE the lines so no anchor text precedes the marker.
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
    """L4 facts about the user (derived consolidated assertions — NOT
    verbatim quotes, so they need no quoted-evidence marker; they are the
    memory system's conclusions, rendered as third-person facts)."""
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
    """State-card energy/availability prose (template selection only — the
    ENERGY channel maps to fixed prose, never to a raw number)."""
    if brief.energy > 0.7:
        return AVAILABILITY_HIGH
    if brief.energy < 0.35:
        return AVAILABILITY_LOW
    return AVAILABILITY_MID


def _band_line(value: float, high: str, mid: str, low: str) -> str:
    """Band-template selection shared by the availability/behavioral lines
    (the ``_availability_line`` pattern): one fixed prose string per band,
    never a raw number."""
    if value > 0.7:
        return high
    if value < 0.35:
        return low
    return mid


def _behavioral_bearing(brief: BehaviorBrief) -> str:
    """BEHAVIORAL BEARING prose (W3): the behavioral channels
    ``derive_behavior`` computes — initiative and reactivity straight from
    ``BehaviorBrief``, persistence mapped to ``1 - closing_tendency``
    (staying power in the conversation; there is no literal persistence
    channel — this is the honest existing signal). Rendered as PROSE via
    band-template selection (``_band_line``), never raw floats (G2); the
    BehaviorTrace is never rendered (invariant)."""
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


def _day_period(hour: int) -> str:
    """Morning/afternoon/evening/night band of a local hour (temporal line).

    Fixed mapping: 05:00–11:59 morning, 12:00–16:59 afternoon,
    17:00–21:59 evening, else night. (The plan's example line reads
    "Saturday afternoon" at 15:24.)
    """
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _partition_agenda(
    items, t_h: float
) -> tuple[list, list, list, list]:
    """(done earlier, missed, happening now, later today) — the state-card
    agenda partition (S2 decision 3: past items kept, labeled).

    Bucket rule per item: ``completed`` → done earlier; ``skipped`` →
    MISSED; ``shifted`` → later today (the slot is not happening at its
    window — it was moved); anything else falls to the window comparison
    ``end_t_h <= t_h`` → done, ``start_t_h <= t_h`` → now, else later. The
    status transition (``life.transition_past_windows``) keys off the same
    comparison, so the render and the persisted status agree by
    construction. Pure function of (item window/status, t_h).

    Skipped is its own bucket because a finished slot is not the same fact
    as a done one. Live 2026-09-08: ``read history`` was force-skipped and
    the card still listed it under "Done earlier", so her own context said
    she had read while the recalled memory said she missed it. She happened
    to believe the memory.
    """
    done: list = []
    missed: list = []
    now: list = []
    later: list = []
    for it in items:
        if it.status == "completed":
            done.append(it)
        elif it.status == "skipped":
            missed.append(it)
        elif it.status == "shifted":
            later.append(it)
        elif t_h >= it.end_t_h:
            done.append(it)
        elif t_h >= it.start_t_h:
            now.append(it)
        else:
            later.append(it)
    return done, missed, now, later


def render_temporal_section(snapshot: CompanionSnapshot, t_h: float, anchor) -> str | None:
    """TEMPORAL FRAME section (W2): current-time/day line + agenda partition.

    The line reads the wall clock from the REAL date via ``anchor.real_at``
    (W1) and the VIRTUAL day index: ``It is 15:24, Saturday afternoon —
    day 0.`` (weekday + day period from the real local time; day N is
    ``int(t_h // 24)``). The partition keeps past items, labeled (spec
    decision 3): ``Done earlier`` / ``Happening now`` / ``Later today``.

    ``anchor`` must expose ``real_at(t_h) -> aware datetime`` (the
    ``RealTimeAnchor`` contract). Returns None when no anchor is given
    (replay / unanchored runs): the temporal section is omitted entirely —
    it never falls back to rendering ``t_h`` directly (that would leak
    engine numbers, G2).
    """
    if anchor is None:
        return None
    real = anchor.real_at(t_h)
    line = (
        f"It is {real.hour:02d}:{real.minute:02d}, "
        f"{real.strftime('%A')} {_day_period(real.hour)} — day {int(t_h // 24)}."
    )
    done, missed, now, later = _partition_agenda(snapshot.agenda, t_h)
    parts = [line]
    for label, items in (
        ("Done earlier", done),
        ("Did not happen", missed),
        ("Happening now", now),
        ("Later today", later),
    ):
        if items:
            parts.append(label + ":\n" + "\n".join(_agenda_lines(items)))
    return TEMPORAL_HEADER + "\n" + "\n".join(parts)


def _agenda_plan_lines(snapshot: CompanionSnapshot) -> list[str]:
    """Today's agenda PLAN lines (planned/shifted items only, capped at
    ``AGENDA_ITEMS_MAX``) — the day-plan view. Skipped/past items are not
    happening at their slot (NOW semantics). Shared by the day block and the
    volatile state-card AGENDA section so both render the identical text.
    """
    agenda = [
        it for it in snapshot.agenda if it.status in ("planned", "shifted")
    ][:AGENDA_ITEMS_MAX]
    return _agenda_lines(agenda)


def render_day_block(snapshot: CompanionSnapshot) -> str:
    """Tier-2 DAY-START block: the PERSONA ONLY (WS-D reduced scope).

    Rendered ONCE per day (WS4 wires this into ``ensure_day`` so the block
    stays stable within the day; ``assemble_snapshot`` falls back to
    rendering it per call when no cached block is passed via ``day_block``).

    WS-D (structural prompt cache): this block is part of the STABLE prefix —
    it must be byte-identical every turn and across conversations for a
    fixed profile. The pre-WS-D day-plan agenda part is day-level state
    (it changes when items complete / windows pass) and therefore MOVED to
    the volatile tail: the state card's AGENDA section
    (``render_state_card``) renders the identical plan lines, and the
    TEMPORAL FRAME section renders the per-moment partition. Contains no
    per-moment state and no agenda — those live in the state card.
    """
    core = (snapshot.persona.core or DEFAULT_PERSONA_CORE).strip()
    return core


def assemble_snapshot(
    snapshot: CompanionSnapshot,
    *,
    controls: GenerationControls | None = None,
    prompt_brief: str | None = None,
    popup: str | None = None,
    day_block: str | None = None,
    t_h: float | None = None,
    anchor=None,
) -> str:
    """Assemble ONE system prompt from a ``CompanionSnapshot`` (3-tier).

    Public signature backward compatible: ``assemble_snapshot(snapshot,
    controls=...)`` behaves exactly as before, now producing the full
    three-tier context. W2/W3 additions are keyword-only and optional:
    ``t_h`` + ``anchor`` together render the TEMPORAL FRAME section (the
    current-time/day line + agenda partition); with either absent the
    section is omitted (replay / unanchored runs — never falls back to
    raw ``t_h``).

    Tiers, in order:

      1. STABLE system core (``prompts.SYSTEM_CORE_WITH_TOOLS``) — constant,
         contains no state.
      2. DAY-START block — ``day_block`` when a pre-rendered (cached) block
         is passed (WS4 wires ``render_day_block`` into ``ensure_day``),
         else rendered from the snapshot: the PERSONA block (WS-D: the
         day-plan agenda moved to the state card, tier 3).
      3. STATE CARD — the named W3 sections in fixed order (the day-plan
         AGENDA section; TEMPORAL FRAME when anchored; AFFECTIVE BEARING —
         the mood brief (``prompt_brief``: the 'Current bearing' prose from
         ``BehaviorDirective.prompt_brief``, the SINGLE source — the
         assembler never re-renders it) plus the availability line, both
         VERBATIM from the pre-wave renderer; BEHAVIORAL BEARING — the
         initiative/reactivity/persistence channels as prose; CURRENT
         INTENT — the reserved placeholder slot), then the unchanged card
         sections: current activity, active life arcs, relevant memories
         (quoted evidence), about-you facts, proactive block (only when
         ``snapshot.proactive_intent`` is set, hook verbatim), closing
         guidance (only when ``controls`` carries it), and the pinned
         event/pop-up block when ``popup`` is provided.

    Recent dialogue is deliberately NOT a section (invariant 14): it lives
    in the user/assistant message payload, so every turn appears exactly
    once. If the joined prompt exceeds ``MAX_PROMPT_CHARS``, whole sections
    are dropped deterministically from lowest priority upward (text is never
    mangled) — EXCEPT the pinned sections (day-plan agenda, current intent
    slot, current activity, pop-up block), which are never dropped.

    WS-D cache order: this whole string is the LEGACY/aux full 3-tier system
    prompt (byte-identical to the pre-WS-D layout — the agenda block merely
    relocated from the day block into the state card). The session mainline
    uses ``build_context_messages`` instead: the stable prefix (tiers 1+2)
    stays the system message, and ``render_state_card`` (tier 3) rides as
    a TRAILING system message.
    """
    sections = _state_card_sections(
        snapshot, controls=controls, prompt_brief=prompt_brief,
        popup=popup, t_h=t_h, anchor=anchor,
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
    t_h: float | None,
    anchor,
) -> list[tuple[int, bool, str]]:
    """The VOLATILE state-card sections (never the stable core/day block).

    WS-D: this block is the volatile TAIL of the assembled request — it
    changes every turn (time line, mood brief, activity, retrieved memory,
    agenda transitions) and must never enter the stable prefix. Includes the
    day-plan AGENDA section (priority 0, pinned): pre-WS-D it lived in the
    day-start block; it is day-level state (items complete / windows pass),
    so it moved here with the agenda plan text byte-identical
    (``_agenda_plan_lines``).
    """
    sections: list[tuple[int, bool, str]] = []

    agenda_lines = _agenda_plan_lines(snapshot)
    if agenda_lines:
        # Agenda section is exempt from budget eviction.
        sections.append(
            (_PRIO_AGENDA, _PINNED, AGENDA_HEADER + "\n" + "\n".join(agenda_lines))
        )

    temporal = (
        render_temporal_section(snapshot, t_h, anchor)
        if t_h is not None
        else None
    )
    if temporal:
        sections.append((_PRIO_TEMPORAL, False, temporal))

    # AFFECTIVE BEARING: mood brief line plus availability line.
    affective: list[str] = []
    if prompt_brief:
        # The AFFECTIVE BEARING section header already names this block and
        # the brief opens with its own "Current bearing:", so the extra
        # MOOD_BRIEF_HEADER read as a third label on one line.
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

    Deterministic whole-section budget enforcement: keep sections from
    highest priority down while the running total fits; drop whole sections,
    never mangle. Pinned sections are always kept.
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
    t_h: float | None = None,
    anchor=None,
) -> str:
    """The PER-MOMENT state card — what is true right now and nothing else.

    Day-scoped material (the plan, her arcs, the user model) is NOT here: it
    goes out once at rollover through :func:`render_day_start_block`. This
    card carries only what actually changes turn to turn — the temporal
    frame, her bearing, what she is doing, what memory retrieved for THIS
    query, a proactive hook, a pop-up.

    Budget: the same whole-section trim as the full prompt, applied to the
    tail on its own (the system message carries no state, so
    ``MAX_PROMPT_CHARS`` bounds the JOINED prompt; the tail alone is far
    smaller). Returns "" when no section survives (empty snapshot).

    The tail is EPHEMERAL: rebuilt each turn, never carried forward, which is
    what keeps the history append-only in shape. It also FOLDS into whatever
    system block the turn already has (see :func:`append_system`) rather than
    standing alone in the position that means "the thing you are answering" —
    the card asks for no reaction, and putting it there is what let a pop-up
    appended behind it read as one instruction blob.
    """
    sections = [
        s for s in _state_card_sections(
            snapshot, controls=controls, prompt_brief=prompt_brief,
            popup=popup, t_h=t_h, anchor=anchor,
        )
        if s[0] not in _DAY_SCOPED_PRIOS
    ]
    return _join_stable_plus_sections([], sections)


def _day_plan_header(t_h: float | None, anchor) -> str:
    """Header for a day's plan, naming WHICH day it describes.

    The block is emitted once and then lives in the stream as history, so on
    day 1 the model sees yesterday's block and today's, both of which used to
    open "Today's agenda:". Two blocks claiming to be today is ambiguous;
    dating them is not.

    Falls back to the undated header when the run is unanchored (replay and
    most tests), where there is no real calendar to name and a day index would
    be the raw engine coordinate this module never renders.
    """
    if t_h is None or anchor is None:
        return AGENDA_HEADER
    try:
        return f"{anchor.real_at(t_h).strftime('%A')}'s plan:"
    except Exception:  # noqa: BLE001 - a broken anchor must not lose the plan
        return AGENDA_HEADER


def render_day_start_block(
    snapshot: CompanionSnapshot, *, t_h: float | None = None, anchor=None,
) -> str:
    """The DAY-scoped block, emitted once when the day rolls over.

    Her plan for the day, her active arcs, and what the memory system has
    concluded about him. It goes into the message STREAM as a single system
    message, not into the system prompt and not into the per-turn card:

    * the system prefix must be byte-identical all day, and the agenda
      mutates as windows pass, so it cannot go there;
    * the per-turn card is re-sent every turn, so day-stable text there is
      pure waste — ~270 chars a turn on the live store.

    The stream is append-only, so a block emitted once at rollover sits
    inside the cached prefix for every later turn of that day: sent once,
    read all day. Returns "" when the day has nothing worth stating.

    ``t_h``/``anchor`` only date the plan header (see
    :func:`_day_plan_header`) — yesterday's block stays in the stream as
    history, so each needs to say which day it describes. They do not add any
    per-moment content: this block must stay constant for the whole day, or
    the "written once" property that makes it cache-free is a lie.
    """
    sections = [
        s for s in _state_card_sections(
            snapshot, controls=None, prompt_brief=None, popup=None,
            t_h=None, anchor=None,
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

    STABLE system: the day-start PERSONA block + ``SYSTEM_CORE_WITH_TOOLS``,
    in that order (byte-identical every turn and across conversations for a
    fixed profile; ``day_block`` is the session-cached persona block, else
    rendered here).

    Messages: the context stream (oldest→newest, tail-limited), then the user
    request when given, then the VOLATILE state card as a TRAILING system
    message (``render_state_card``: temporal frame / affective / behavioral
    bearing, agenda plan, activity, arcs, memories, about-you, proactive,
    pop-up).

    The trailing-tail placement is what MAKES a byte-identical extension
    possible; it delivers one only when ``recent_turns`` grows rather than
    slides, which is why the session passes ``limit=None`` over an
    epoch-anchored read. Caching is structural on this provider; there is no
    ``cache_control``.

    Session wiring: ``_chat`` calls this for the mainline model call and
    hands the returned halves to ``_popup_request_call``, so a decision call
    extends the mainline request instead of sending a prefix of its own.
    """
    system = "\n\n".join(
        [
            day_block if day_block is not None else render_day_block(snapshot),
            SYSTEM_CORE_WITH_TOOLS,
        ]
    )
    if user_request is not None:
        messages = build_messages(recent_turns, user_request, limit=limit)
    else:
        messages = [
            wire_message(turn)
            for turn in (recent_turns if limit is None else recent_turns[-limit:])
        ]
    tail = render_state_card(
        snapshot,
        controls=controls, prompt_brief=prompt_brief, popup=popup,
        t_h=t_h, anchor=anchor,
    )
    messages = append_system(messages, tail)
    return system, messages
