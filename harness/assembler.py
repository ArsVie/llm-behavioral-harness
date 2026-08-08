"""Prompt assembler — CompanionSnapshot → bounded system prompt (W-E1 + Wave 2).

Wave 2 (A1 central integration): the system prompt is assembled from a
``CompanionSnapshot`` — the single place where the lanes (persona, behavior,
life, memory, conversation, proactive intent) meet before composition.
Sections are BOUNDED by construction: persona core; current behavioral
guidance (prose rendered from the conversation-safe ``BehaviorBrief``, never
raw channels); current activity; today's agenda (capped); 1-3 active life
arcs; N relevant memories (hard budget, N = ``MEMORY_EPISODES_MAX``); recent
conversation (tail-limited); the proactive intent block when present.

Leakage invariant (frozen): this module never receives engine state — the
snapshot carries only domain objects. The rendered behavioral prose and all
section headers contain no raw numbers, no ``mu``/``eta``, no phase labels,
no ``cycle_day``. The proactive block renders the intent's CONCRETE HOOK
verbatim — never "Contact reason: <reason>".

The legacy W-E1 entry points ``build_system_prompt`` (persona + directive
brief) and ``build_messages`` (transcript tail + user request) are preserved
verbatim for the ablation/harness-off conditions and pre-slice callers.
"""

from __future__ import annotations

from harness.behavior import BehaviorDirective
from harness.domain import CompanionSnapshot, GenerationControls

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
# Wave 2: CompanionSnapshot assembly
# --------------------------------------------------------------------------- #


def _render_behavior_brief(brief) -> str:
    """Conversation-safe prose from the BehaviorBrief channels.

    Mirrors A3's ``_render_brief`` style (bearing / pace / texture / care)
    using ONLY the brief's channel values — never raw numbers, never engine
    state. The BehaviorBrief has no momentum channel, so continuity phrasing
    is omitted here.
    """
    if brief.valence > 0.35:
        bearing = "quietly bright"
    elif brief.valence < -0.35:
        bearing = "a little tender and inward"
    else:
        bearing = "even and grounded"

    if brief.energy > 0.7:
        pace = "lively and readily engaged"
    elif brief.energy < 0.35:
        pace = "low-energy and unhurried"
    else:
        pace = "calmly present"

    if brief.playfulness > brief.reflectiveness + 0.12:
        texture = "Favor light wit and small spontaneous touches over big declarations."
    elif brief.reflectiveness > brief.playfulness + 0.12:
        texture = "Favor thoughtful pauses, precise words, and one sincere observation."
    else:
        texture = "Balance lightness with one grounded, personal observation."

    care = (
        "Keep care intact; warmth should remain visible even when the mood is subdued."
        if brief.warmth < 0.62
        else "Keep the affection natural, specific, and free of exaggerated sweetness."
    )
    return " ".join(
        (
            f"Current bearing: {bearing}, {pace}.",
            texture,
            care,
            "Do not name or explain the internal state; show it through cadence, "
            "word choice, initiative, and conversational length.",
        )
    )


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
    """(episode lines, anchor lines) — both capped by their budgets."""
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


def _conversation_lines(snapshot: CompanionSnapshot) -> list[str]:
    return [f"{t.role}: {t.text}" for t in snapshot.recent_conversation[-RECENT_TURNS:]]


def assemble_snapshot(
    snapshot: CompanionSnapshot,
    *,
    controls: GenerationControls | None = None,
) -> str:
    """Assemble ONE system prompt from a ``CompanionSnapshot``.

    Bounded sections, highest-priority first:

      0 persona core · 1 behavioral guidance · 2 current activity ·
      3 active life arcs · 4 relevant memories · 5 about you (L4) ·
      6 today's agenda · 7 proactive block · 8 closing guidance ·
      9 recent conversation

    The proactive block appears ONLY when ``snapshot.proactive_intent`` is
    set, and renders its ``hook`` verbatim (never a reason label). The
    ``closing_guidance`` appears only when ``controls`` carries it. If the
    joined prompt exceeds ``MAX_PROMPT_CHARS``, whole sections are dropped
    deterministically from lowest priority upward (text is never mangled).
    """
    sections: list[tuple[int, str]] = []

    core = (snapshot.persona.core or DEFAULT_PERSONA_CORE).strip()
    sections.append((0, core))

    if snapshot.current_behavior is not None:
        sections.append(
            (1, f"Current behavioral guidance: {_render_behavior_brief(snapshot.current_behavior)}")
        )

    if snapshot.current_activity is not None:
        sections.append((2, f"Current activity: {snapshot.current_activity.description}"))

    arcs = [a for a in snapshot.life_arcs if a.status == "active"][:LIFE_ARCS_MAX]
    if arcs:
        lines = [f"- {a.name} — {a.next_intention}" for a in arcs]
        sections.append((3, "Active life arcs:\n" + "\n".join(lines)))

    ep_lines, anchor_lines = _memory_lines(snapshot)
    if ep_lines:
        sections.append(
            (4, "Relevant memories:\n" + "\n".join(ep_lines + anchor_lines))
        )

    um_lines = _user_model_lines(snapshot)
    if um_lines:
        sections.append((5, "About you:\n" + "\n".join(um_lines)))

    agenda = [
        it for it in snapshot.agenda if it.status in ("planned", "shifted")
    ][:AGENDA_ITEMS_MAX]
    if agenda:
        sections.append((6, "Today's agenda:\n" + "\n".join(_agenda_lines(agenda))))

    if snapshot.proactive_intent is not None:
        sections.append(
            (7, proactive_block(snapshot.proactive_intent.hook))
        )

    if controls is not None and controls.closing_guidance:
        sections.append((8, f"Closing guidance: {controls.closing_guidance}"))

    conv_lines = _conversation_lines(snapshot)
    if conv_lines:
        sections.append((9, "Recent conversation:\n" + "\n".join(conv_lines)))

    # Deterministic budget enforcement: keep sections from highest priority
    # down while the running total fits; drop whole sections, never mangle.
    ordered = sorted(sections, key=lambda item: item[0])
    kept: list[str] = []
    total = 0
    for _, text in ordered:
        cost = len(text) + 2  # +2 for the blank line separator
        if total + cost > MAX_PROMPT_CHARS:
            continue
        kept.append(text)
        total += cost
    return "\n\n".join(kept)
