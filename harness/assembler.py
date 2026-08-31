"""Prompt assembler — CompanionSnapshot → bounded system prompt (W-E1 + Wave 2 + v2).

Wave 2 (A1 central integration): the system prompt is assembled from a
``CompanionSnapshot`` — the single place where the lanes (persona, behavior,
life, memory, conversation, proactive intent) meet before composition.
Sections are BOUNDED by construction: persona core; current behavioral
guidance (prose rendered from the conversation-safe ``BehaviorBrief``, never
raw channels); current activity; today's agenda (capped); 1-3 active life
arcs; N relevant memories (hard budget, N = ``MEMORY_EPISODES_MAX``); the
proactive intent block when present.

Context construction v2 (WS1, design plans/harness-runtime-design-2026-08-14.md
§2.1, user L393): the assembled prompt is the full THREE-TIER context:

  1. STABLE system core — ``prompts.SYSTEM_CORE_WITH_TOOLS``: how to read
     the {state} card, compliance, tool protocol, show-don't-announce,
     never-name-the-state. Constant, contains NO state.
  2. DAY-START block — the PERSONA block, rendered once per day
     (``render_day_block``; WS4 wires it into ``ensure_day``). WS-D: the
     pre-WS-D day-plan agenda part moved to the STATE CARD (tier 3) so this
     tier is fully stable (byte-identical every turn day after day).
  3. STATE CARD — at every conversation start and refreshable mid-
     conversation: mood brief (the 'Current bearing' prose from
     ``BehaviorDirective.prompt_brief`` — the SINGLE source; the divergent
     local re-renderer is deleted), energy/availability, current activity,
     pulled memories (quoted evidence), user-model facts, proactive intent
     if any, and the arriving-event pop-up block when one is injected.

W2+W3 (time-aware, sectioned card): the state card is restructured into
named sections in fixed order — ``TEMPORAL FRAME`` (current-time/day line
from ``anchor.real_at`` + the agenda partition Done earlier / Happening
now / Later today; rendered ONLY when the run is anchored, never raw
``t_h``), ``AFFECTIVE BEARING`` (the pre-wave renderer's mood brief +
availability line VERBATIM — a clean slot the codebook fills later, G5),
``BEHAVIORAL BEARING`` (initiative / reactivity / persistence-as-
``1 - closing_tendency`` as band prose, never floats), and ``CURRENT
INTENT`` (an empty reserved slot until S5). Agenda item statuses
transition ``planned → completed`` as windows pass (``life.transition_
past_windows``, persisted per turn by the session) so the render and the
store agree.

Prompt boundary (Iteration-2 A5, invariants 14/15/16): raw recent dialogue
is NEVER rendered into the system prompt — it lives in the user/assistant
message payload only (``build_messages``), so each turn appears exactly
once. Verbatim memory anchors are structurally marked as QUOTED historical
conversation (``MEMORY_EVIDENCE_HEADER``) so user-authored text retrieved
as memory can never silently gain system-level instruction authority. The
assembler never receives engine state: the snapshot carries only domain
objects, and no section renders cycle/phase/hormone internals.

WS-D (structural prompt cache, reduced 2026-08-19): the assembled request is
split into a STABLE prefix and a VOLATILE tail. The stable prefix — the
``SYSTEM_CORE_WITH_TOOLS`` core + the day-start PERSONA block (rendered
persona only; the agenda is day-plan state and moved to the tail) — is
byte-identical every turn and across conversations for a fixed profile, so
request N+1 is a byte-identical extension of request N (DeepSeek-read
finding: caching is 100% structural, zero ``cache_control``). The volatile
tail — the state card (temporal frame / affective / behavioral bearing /
current intent, activity, arcs, memories, about-you, proactive, closing,
pop-up + the agenda plan) — rides as the TRAILING user message via the
``build_context_messages`` seam. ``assemble_snapshot`` keeps the legacy
full 3-tier system string byte-identical (aux/experiment callers); the
session mainline wires ``build_context_messages`` so the state card leaves
the system message.

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

The legacy W-E1 entry points ``build_system_prompt`` (persona + directive
brief) and ``build_messages`` (transcript tail + user request) are preserved
verbatim for the ablation/harness-off conditions and pre-slice callers.
"""

from __future__ import annotations

from harness.behavior import BehaviorDirective
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
    MOOD_BRIEF_HEADER,
    SYSTEM_CORE_WITH_TOOLS,
)

#: Default persona core used when the caller provides none.
DEFAULT_PERSONA_CORE = (
    "You are Nova, a warm and attentive companion. You have your own days, "
    "your own rhythms, and a genuine interest in the person you talk to. You "
    "are not a customer-service assistant: you speak like a close friend "
    "would — with your own cadence, your own quirks, your own silences."
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


def proactive_block(hook: str | None = None) -> str:
    """The proactive system-prompt block: opening + grounded hook verbatim.

    ``hook=None`` (legacy ungrounded calls) renders the generic opening with
    ``DEFAULT_PROACTIVE_HOOK`` — no invented source claim.
    """
    return PROACTIVE_OPENING.format(hook=(hook or DEFAULT_PROACTIVE_HOOK).strip())


def build_system_prompt(
    persona_core: str | None = None,
    directive: BehaviorDirective | None = None,
) -> str:
    """One system message: persona core + optional current behavioral guidance.

    With `directive=None` the prompt contains ONLY the persona — this is the
    "harness off" condition for the ablation experiment (persona preserved,
    dynamic guidance removed).
    """
    core = (persona_core or DEFAULT_PERSONA_CORE).strip()
    if directive is None:
        return core
    brief = directive.prompt_brief.strip()
    if brief:
        return f"{core}\n\nCurrent behavioral guidance: {brief}"
    return core


def build_messages(
    recent_turns: list[dict],
    user_request: str,
    limit: int = RECENT_TURNS,
) -> list[dict]:
    """Transcript (tail-limited, oldest→newest) + current user request.

    `recent_turns` are store message rows ({role, content, ...}); only role
    and content are used. Assistant turns are included so the model keeps
    style continuity; the user request is always last.
    """
    messages: list[dict] = []
    for turn in recent_turns[-limit:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_request})
    return messages


# --------------------------------------------------------------------------- #


def _local_hour(t_h: float) -> str:
    """HH:MM of the local hour for an absolute t_h."""
    local = t_h % 24.0
    hh = int(local)
    mm = int(round((local - hh) * 60.0)) % 60
    return f"{hh:02d}:{mm:02d}"


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
) -> tuple[list, list, list]:
    """(done earlier, happening now, later today) — the state-card agenda
    partition (S2 decision 3: past items kept, labeled).

    Bucket rule per item: ``completed``/``skipped`` → done earlier (the
    slot is finished either way); ``shifted`` → later today (the slot is
    not happening at its window — it was moved); anything else falls to the
    window comparison ``end_t_h <= t_h`` → done, ``start_t_h <= t_h`` →
    now, else later. The status transition (``life.transition_past_windows``)
    keys off the same comparison, so the render and the persisted status
    agree by construction. Pure function of (item window/status, t_h).
    """
    done: list = []
    now: list = []
    later: list = []
    for it in items:
        if it.status in ("completed", "skipped"):
            done.append(it)
        elif it.status == "shifted":
            later.append(it)
        elif t_h >= it.end_t_h:
            done.append(it)
        elif t_h >= it.start_t_h:
            now.append(it)
        else:
            later.append(it)
    return done, now, later


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
    done, now, later = _partition_agenda(snapshot.agenda, t_h)
    parts = [line]
    for label, items in (
        ("Done earlier", done),
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
    the TRAILING user message.
    """
    sections = _state_card_sections(
        snapshot, controls=controls, prompt_brief=prompt_brief,
        popup=popup, t_h=t_h, anchor=anchor,
    )
    # Stable parts first, then the budget-trimmed state card.
    parts = [SYSTEM_CORE_WITH_TOOLS]
    parts.append(day_block if day_block is not None else render_day_block(snapshot))
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
        if anchor is not None and t_h is not None
        else None
    )
    if temporal:
        sections.append((_PRIO_TEMPORAL, False, temporal))

    # AFFECTIVE BEARING: mood brief line plus availability line.
    affective: list[str] = []
    if prompt_brief:
        affective.append(f"{MOOD_BRIEF_HEADER} {prompt_brief.strip()}")
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
    """The VOLATILE state-card block, standalone (the trailing user message).

    WS-D: this is the structural tail of the assembled request — the state
    card (incl. the day-plan AGENDA section) is appended as the LAST user
    message so the stable prefix (system core + persona) stays byte-identical
    every turn and request N+1 is a byte-identical extension of request N.
    Budget: the same whole-section trim as the legacy full prompt, applied
    to the tail on its own (the system message carries no state, so
    ``MAX_PROMPT_CHARS`` bounds the JOINED prompt; the tail alone is far
    smaller). Returns "" when no section survives (empty snapshot).
    """
    sections = _state_card_sections(
        snapshot, controls=controls, prompt_brief=prompt_brief,
        popup=popup, t_h=t_h, anchor=anchor,
    )
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
    limit: int = RECENT_TURNS,
) -> tuple[str, list[dict]]:
    """(stable system, messages) — the WS-D cache-ordered request pair.

    STABLE system: ``SYSTEM_CORE_WITH_TOOLS`` + the day-start PERSONA block
    (byte-identical every turn and across conversations for a fixed profile;
    ``day_block`` is the session-cached persona block, else rendered here).

    Messages: transcript tail (oldest→newest, tail-limited), then the user
    request when given, then the VOLATILE state card as the TRAILING user
    message (``render_state_card``: temporal frame / affective / behavioral
    bearing, agenda plan, activity, arcs, memories, about-you, proactive,
    closing, pop-up). The trailing-tail placement makes request N+1 a
    byte-identical extension of request N up to the tail — the DeepSeek-read
    structural-cache contract (no ``cache_control`` needed).

    WS-D session wiring: ``_chat`` calls this for the mainline model call and
    keeps ``assemble_snapshot`` (the full 3-tier string) for
    ``_last_system_prompt`` so pop-up aux calls replay the mainline prefix.
    """
    system = "\n\n".join(
        [
            SYSTEM_CORE_WITH_TOOLS,
            day_block if day_block is not None else render_day_block(snapshot),
        ]
    )
    if user_request is not None:
        messages = build_messages(recent_turns, user_request, limit=limit)
    else:
        messages = [
            {"role": turn["role"], "content": turn["content"]}
            for turn in recent_turns[-limit:]
        ]
    tail = render_state_card(
        snapshot,
        controls=controls, prompt_brief=prompt_brief, popup=popup,
        t_h=t_h, anchor=anchor,
    )
    if tail:
        messages.append({"role": "user", "content": tail})
    return system, messages
