"""Companion domain contracts — the higher-order companion concepts above the
stochastic engine. Stdlib only; every type is a frozen dataclass (or Enum) and
timestamps are absolute float hours ``t_h``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal


@dataclass(frozen=True)
class Interest:
    """A named interest with its portfolio bucket and salience (0..1)."""

    name: str
    bucket: str  # "exact" | "adjacent" | "independent"
    salience: float  # 0..1


@dataclass(frozen=True)
class InterestRelation:
    """Directed edge of the interest graph, with strength (0..1)."""

    from_interest: str
    to_interest: str
    strength: float


@dataclass(frozen=True)
class Routine:
    """A recurring daily routine: start fraction of the day, duration, cadence."""

    name: str
    start_frac: float  # 0..1 of the day
    duration_h: float
    cadence: float  # daily probability 0..1
    salience: float  # 0..1


@dataclass(frozen=True)
class UserProfile:
    """The user's onboarding identity: a display name plus their interests.

    Interest names are plain strings and may not exist in the catalog.
    """

    name: str
    interests: tuple[str, ...]


@dataclass(frozen=True)
class PersonaProfile:
    """The companion's stable identity: prose core plus interests and routines."""

    name: str
    core: str  # <= 2 sentences of prose
    interests: tuple[Interest, ...]
    routines: tuple[Routine, ...]


@dataclass(frozen=True)
class LifeArc:
    """A persistent life arc (e.g. learning pottery), tied to an interest."""

    id: str
    name: str
    interest: str  # Interest.name
    started_day: int
    progress: float  # 0..1
    status: str  # "active" | "completed" | "abandoned"
    next_intention: str


@dataclass(frozen=True)
class AgendaItem:
    """One scheduled activity slot, always traceable to a persistent source."""

    id: str
    start_t_h: float
    end_t_h: float
    activity: str
    source_type: str  # "arc" | "interest" | "routine"
    source_id: str
    salience: float
    status: str  # "planned" | "completed" | "skipped" | "shifted"
    outcome: str | None = None
    """What actually came of it, once the window has passed.

    An agenda item used to resolve to a STATUS and nothing else, so nothing
    ever happened inside one: the day left no trace to talk about, arcs could
    not accumulate content, and a proactive hook fired as bare as
    "Finished: practice sketching". This carries the fact instead.

    Only ever written from something the companion actually decided or said
    (a decide_event verdict at the closing boundary) — never invented ahead
    of time, and never filled in just because a window elapsed.
    """

@dataclass(frozen=True)
class DailyAgenda:
    """The agenda of one day, as an immutable tuple of items."""

    day: int
    items: tuple[AgendaItem, ...]


@dataclass(frozen=True)
class CurrentActivity:
    """What the companion is doing right now (``item`` may be unscheduled)."""

    t_h: float
    item: AgendaItem | None
    description: str




@dataclass(frozen=True)
class CompanionBehaviorState:
    """The companion's own behavioral state, derived from a BehaviorDirective.

    Distinct from ``UserAffectObservation``: no shared fields, no implicit
    conversion. ``directive_ref`` identifies the directive that produced it.
    """

    directive_ref: str
    initiative: float
    energy: float
    warmth: float
    playfulness: float




# Memory-layer types live in harness.domain_memory; re-exported here so
# existing imports keep working unchanged.
from harness.domain_memory import (  # noqa: F401 - compat re-export
    AffectMetadata,
    EpisodicMemory,
    MemoryKind,
    MemoryPolicy,
    SessionSummary,
    UserAffectObservation,
    UserModel,
    UserModelAssertion,
    UserModelCategory,
)

@dataclass(frozen=True)
class MemoryContext:
    """The bounded memory slice handed to composition.

    L1 ``recent_turns`` + L2 ``session_context`` + L3 ``episodes`` +
    L4 ``user_model`` (``None`` before any consolidation) + ``evidence_anchors``
    (exact verbatim excerpts).
    """

    recent_turns: tuple[Turn, ...]
    session_context: tuple[SessionSummary, ...]
    episodes: tuple[EpisodicMemory, ...]
    user_model: UserModel | None
    evidence_anchors: tuple[str, ...]



@dataclass(frozen=True)
class ContactOpportunity:
    """A plausible time to consider initiating contact — and nothing more.

    It carries NO semantic reason; motivation is resolved separately into a
    grounded ``ProactiveIntent`` linked back via ``opportunity_id``.
    ``hazard_components`` maps hazard-source names to their contributions.
    """

    id: str
    desired_t_h: float
    created_t_h: float
    valid_until_t_h: float
    hazard_components: dict[str, float]
    initiative_multiplier: float
    previous_score_multiplier: float


@dataclass(frozen=True)
class ProactiveIntent:
    """A grounded reason to contact the user.

    Every field except ``opportunity_id`` is REQUIRED and non-empty: there is
    no proactive reason without a source. ``evidence`` is the provenance
    chain; ``opportunity_id`` links to the ``ContactOpportunity`` that made
    this a plausible moment.
    """

    id: str
    reason: str
    source_type: str
    source_id: str
    hook: str
    created_t_h: float
    valid_until_t_h: float
    salience: float
    evidence: str
    opportunity_id: str | None = None


@dataclass(frozen=True)
class GenerationControls:
    """Mechanical generation parameters derived from a behavioral directive."""

    max_tokens: int
    response_delay_s: float
    closing_tendency: float
    initiative_factor: float
    closing_guidance: str = ""
    # assembler-visible continuation policy derived from closing_tendency.


@dataclass(frozen=True)
class BehaviorBrief:
    """Conversation-safe behavioral channels (no raw engine state)."""

    valence: float
    energy: float
    reactivity: float
    warmth: float
    expressiveness: float
    playfulness: float
    reflectiveness: float
    initiative: float
    response_length_scale: float
    response_delay_s: float
    closing_tendency: float


@dataclass(frozen=True)
class Turn:
    """One conversation turn: role, exact text, timestamp."""

    role: str
    text: str
    t_h: float


@dataclass(frozen=True)
class ConversationTurn:
    """One turn inside a ``Conversation``.

    ``speaker`` is ``"user"`` or ``"companion"`` — the ``role`` strings on
    persisted ``messages`` rows are ``"user"``/``"assistant"`` instead.
    ``turn_index`` is 0-based within the conversation.
    """

    speaker: Literal["user", "companion"]
    text: str
    t_h: float
    turn_index: int
    conversation_id: str


@dataclass(frozen=True)
class Conversation:
    """One sustained multi-turn dialogue.

    Opened by the first message of either party, closed by exactly one
    ``close_reason`` value, or still open (``closed_t_h`` and ``close_reason``
    both ``None``). ``closing_tendency`` is mechanically observable: a high
    value raises the probability of ``close_reason == "closing_tendency"`` and
    shortens the conversation. Sessions, judge sampling and relational metrics
    key off this boundary.
    """

    id: str
    opened_t_h: float
    closed_t_h: float | None
    opened_by: Literal["user", "companion"]
    close_reason: Literal[
        "closing_tendency", "user_left", "quiet_hours", "max_turns"
    ] | None
    turns: tuple[ConversationTurn, ...]


@dataclass(frozen=True)
class CompanionSnapshot:
    """Integration contract: the single place lanes meet before composition.

    Persona, behavior, activity, agenda, life arcs, memory context and the
    recent conversation; optional slots are ``None`` when not applicable, but
    a present ``ProactiveIntent`` is always fully grounded.
    """

    persona: PersonaProfile
    current_behavior: BehaviorBrief | None
    current_activity: CurrentActivity | None
    agenda: tuple[AgendaItem, ...]
    life_arcs: tuple[LifeArc, ...]
    memory_context: MemoryContext
    recent_conversation: tuple[Turn, ...]
    proactive_intent: ProactiveIntent | None


@dataclass(frozen=True)
class AblationClaim:
    """Effectiveness contract of one matrix condition.

    ``check(cell_records, full_records)`` is evaluated by the pre-flight
    against the condition's own run records vs FULL's; a failing claim marks
    the condition a NULL ablation. ``channel`` is one of the four ablatable
    channels; ``min_days`` is the horizon before which the ablated mechanism
    cannot have acted (a below-horizon claim is NOT EVALUABLE, never FAIL;
    ``1`` means evaluable at any horizon). ``cell_records``/``full_records``
    carry at least ``n_proactive``, ``n_reactive``, ``n_assistant_turns``,
    ``n_blank_assistant_turns``, ``n_conversations`` and
    ``mean_turns_per_conversation``.
    """

    condition: str
    channel: Literal["timing", "memory_store", "generation_controls", "life_state"]
    assertion: str
    check: Callable[[dict, dict], bool]
    min_days: int = 1
    measure: Callable[[dict, dict], dict] | None = None
