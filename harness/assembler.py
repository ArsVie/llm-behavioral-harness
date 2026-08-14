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
  2. DAY-START block — personality + today's agenda, rendered once per day
     (``render_day_block``; WS4 wires it into ``ensure_day``).
  3. STATE CARD — at every conversation start and refreshable mid-
     conversation: mood brief (the 'Current bearing' prose from
     ``BehaviorDirective.prompt_brief`` — the SINGLE source; the divergent
     local re-renderer is deleted), energy/availability, current activity,
     pulled memories (quoted evidence), user-model facts, proactive intent
     if any, and the arriving-event pop-up block when one is injected.

Prompt boundary (Iteration-2 A5, invariants 14/15/16): raw recent dialogue
is NEVER rendered into the system prompt — it lives in the user/assistant
message payload only (``build_messages``), so each turn appears exactly
once. Verbatim memory anchors are structurally marked as QUOTED historical
conversation (``MEMORY_EVIDENCE_HEADER``) so user-authored text retrieved
as memory can never silently gain system-level instruction authority. The
assembler never receives engine state: the snapshot carries only domain
objects, and no section renders cycle/phase/hormone internals.

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

#: Default persona core used when the caller provides none. Configurable at
#: runtime via --persona-core / config; this is a neutral starting voice.
DEFAULT_PERSONA_CORE = (
    "You are Nova, a warm and attentive companion. You have your own days, "
    "your own rhythms, and a genuine interest in the person you talk to. You "
    "are not a customer-service assistant: you speak like a close friend "
    "would — with your own cadence, your own quirks, your own silences."
)

#: How many recent transcript turns to carry into the prompt.
RECENT_TURNS = 12

# --------------------------------------------------------------------------- #
# Wave 2 hard budgets (frozen by A1): the assembled prompt is bounded by
# construction — each section is capped before joining, and a deterministic
# section-level trim guarantees the overall character budget.
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

#: Proactive opening template. The hook is the GROUNDED, concrete detail from
#: the ProactiveIntent — never a reason label.
PROACTIVE_OPENING = (
    "You are reaching out first. {hook}\n"
    "State what you are reaching out about naturally in your FIRST sentence, "
    "then open with a concrete, verifiable observation. Never guilt-trip, nag, "
    "or imply the user owes you contact."
)

#: Fallback hook for LEGACY direct calls to ``Session.fire_proactive`` with no
#: grounded intent in the store (pre-slice callers / tests). It claims no
#: specific source — it is an opening instruction, not a fabricated reason.
DEFAULT_PROACTIVE_HOOK = (
    "Something from your own day is worth sharing — a small moment, a "
    "finished task, or a thought that surfaced."
)

# --------------------------------------------------------------------------- #
# v2: section priorities (lowest dropped first under budget pressure) and
# the pinned decision/steering payload sections.
# --------------------------------------------------------------------------- #
_PRIO_MOOD_BRIEF = 2
_PRIO_ACTIVITY = 3
_PRIO_AVAILABILITY = 4
_PRIO_ARCS = 5
_PRIO_MEMORIES = 6
_PRIO_USER_MODEL = 7
_PRIO_PROACTIVE = 8
_PRIO_CLOSING = 9
_PRIO_POPUP = 10

#: Pinned sections (reviewer requirement): the decision/steering payload —
#: state-card essentials (current activity, event/pop-up block) — is NEVER
#: dropped by the budget trim; other sections are evicted first. Pinned
#: sections are bounded by construction (one activity line; a small pop-up
#: block), so the cap still holds for every realistic snapshot.
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
# Wave 2 + v2: CompanionSnapshot assembly
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


def render_day_block(snapshot: CompanionSnapshot) -> str:
    """Tier-2 DAY-START block: personality + today's agenda.

    Rendered ONCE per day (WS4 wires this into ``ensure_day`` so the block
    stays stable within the day; ``assemble_snapshot`` falls back to
    rendering it per call when no cached block is passed via ``day_block``).
    The agenda part carries only planned/shifted items (skipped items are
    not happening at their slot — NOW semantics) capped at
    ``AGENDA_ITEMS_MAX``. Contains no per-moment state: the mood brief,
    activity and the rest live in the state card.
    """
    core = (snapshot.persona.core or DEFAULT_PERSONA_CORE).strip()
    agenda = [
        it for it in snapshot.agenda if it.status in ("planned", "shifted")
    ][:AGENDA_ITEMS_MAX]
    parts = [core]
    if agenda:
        parts.append(AGENDA_HEADER + "\n" + "\n".join(_agenda_lines(agenda)))
    return "\n\n".join(parts)


def assemble_snapshot(
    snapshot: CompanionSnapshot,
    *,
    controls: GenerationControls | None = None,
    prompt_brief: str | None = None,
    popup: str | None = None,
    day_block: str | None = None,
) -> str:
    """Assemble ONE system prompt from a ``CompanionSnapshot`` (3-tier).

    Public signature backward compatible: ``assemble_snapshot(snapshot,
    controls=...)`` behaves exactly as before, now producing the full
    three-tier context.

    Tiers, in order:

      1. STABLE system core (``prompts.SYSTEM_CORE_WITH_TOOLS``) — constant,
         contains no state.
      2. DAY-START block — ``day_block`` when a pre-rendered (cached) block
         is passed (WS4 wires ``render_day_block`` into ``ensure_day``),
         else rendered from the snapshot: personality + today's agenda.
      3. STATE CARD — mood brief (``prompt_brief``: the 'Current bearing'
         prose from ``BehaviorDirective.prompt_brief``, the SINGLE source —
         the assembler never re-renders it), energy/availability, current
         activity, active life arcs, relevant memories (quoted evidence),
         about-you facts, proactive block (only when
         ``snapshot.proactive_intent`` is set, hook verbatim), closing
         guidance (only when ``controls`` carries it), and the pinned
         event/pop-up block when ``popup`` is provided.

    Recent dialogue is deliberately NOT a section (invariant 14): it lives
    in the user/assistant message payload, so every turn appears exactly
    once. If the joined prompt exceeds ``MAX_PROMPT_CHARS``, whole sections
    are dropped deterministically from lowest priority upward (text is never
    mangled) — EXCEPT the pinned decision/steering payload sections (current
    activity, pop-up block), which are never dropped.
    """
    sections: list[tuple[int, bool, str]] = []

    if prompt_brief:
        sections.append(
            (_PRIO_MOOD_BRIEF, False, f"{MOOD_BRIEF_HEADER} {prompt_brief.strip()}")
        )

    if snapshot.current_behavior is not None:
        availability = _availability_line(snapshot.current_behavior)
        if availability:
            sections.append((_PRIO_AVAILABILITY, False, availability))

    if snapshot.current_activity is not None:
        # PINNED: decision-payload essential — never dropped by the trim.
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
        # PINNED: the arriving-event/steering payload — never dropped.
        sections.append((_PRIO_POPUP, _PINNED, popup))

    # Tier 1 + tier 2 first (stable core + day-start block), then the state
    # card under deterministic budget enforcement: keep sections from highest
    # priority down while the running total fits; drop whole sections, never
    # mangle. Pinned sections are always kept.
    parts = [SYSTEM_CORE_WITH_TOOLS]
    parts.append(day_block if day_block is not None else render_day_block(snapshot))

    ordered = sorted(sections, key=lambda item: item[0])
    total = sum(len(p) + 2 for p in parts)
    kept: list[str] = []
    for _, pinned, text in ordered:
        cost = len(text) + 2  # +2 for the blank line separator
        if pinned or total + cost <= MAX_PROMPT_CHARS:
            kept.append(text)
            total += cost
    parts.extend(kept)
    return "\n\n".join(parts)
