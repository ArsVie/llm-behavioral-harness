"""Companion domain contracts — vertical slice Wave 0 (A1).

Owns the higher-order companion concepts that live ABOVE the stochastic engine:
interests, routines, persona, life arcs, agendas, memory tiers, proactive
intents, generation controls, and the integration contract ``CompanionSnapshot``.

Scope and conventions
---------------------
* Stdlib only (``dataclasses``, ``enum``, ``typing``). No imports from sqlite,
  httpx, engine, or other harness modules — persistence and LLM concerns belong
  to their own layers.
* All timestamps are absolute float hours ``t_h`` (t_h = 0.0 is day 0 at 00:00;
  local hour = t_h % 24; the day of an event is ``int(t_h // 24)``).
* Every type below is a frozen dataclass (or an Enum); instances are immutable
  values, never mutated in place.

Invariants (binding)
--------------------
1. ``UserAffectObservation`` and ``CompanionBehaviorState`` are DISTINCT types:
   they share no field names and there is NO implicit conversion between them,
   ever. Observing the companion's behavior state never implies anything about
   the user's affect (and vice versa); any mapping between the two must be an
   explicit, documented transformation elsewhere — never inside these types.

2. ``MemoryContext`` carries all four memory tiers plus evidence anchors:
   L1 ``recent_turns``, L2 ``session_context``, L3 ``episodes``,
   L4 ``user_model`` (a consolidated projection; ``None`` only while no
   consolidated model exists yet), and ``evidence_anchors`` (exact verbatim
   excerpts that ground the context).

3. ``ProactiveIntent`` has NO optional source fields: ``source_type``,
   ``source_id``, ``hook`` and ``evidence`` are all required and non-empty.
   There can be no proactive reason without a source — a schedule event points
   at ``agenda_item:pottery_2026_08_08``, never at ``reason="schedule"``.

4. No cycle-phase labels and no raw engine state (internal phase labels,
   hormone variables, mood parameters, or cycle-day indices) appear in any
   domain object or conversation-visible string.

5. ``ContactOpportunity`` carries NO semantic reason: it means only that the
   stochastic scheduling process indicates a plausible time to consider
   initiating contact. Semantic motivation is resolved afterward into a
   ``ProactiveIntent``, which MUST be grounded in a real source.

6. The L4 memory taxonomy is defined exactly once, as the canonical enum
   ``UserModelCategory`` (8 categories). Stores consume this single enum and
   never infer categories from string prefixes or free-form keys.

7. ``MemoryPolicy`` distinguishes the research-faithful structured-memory
   condition (``STRUCTURED_MEMORY``) from the explicitly experimental
   topicality-boosted variant (``STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT``,
   flagged by ``is_experimental``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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


class MemoryKind(Enum):
    """Category of an episodic memory."""

    USER_FACT = "user_fact"
    USER_PREFERENCE = "user_preference"
    SHARED_EPISODE = "shared_episode"
    COMPANION_EPISODE = "companion_episode"
    RELATIONSHIP_EVENT = "relationship_event"
    CALLBACK = "callback"


class MemoryPolicy(Enum):
    """Memory conditioning policy for generation/eval (module invariant 7).

    ``STRUCTURED_MEMORY`` is the research-faithful condition; the
    topicality-boosted variant is a SEPARATELY NAMED experiment and is
    flagged by ``is_experimental``. ``RAW_CONTEXT`` and ``VERBATIM_RAG``
    are honest baselines.
    """

    RAW_CONTEXT = "raw_context"
    VERBATIM_RAG = "verbatim_rag"
    STRUCTURED_MEMORY = "structured_memory"
    STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT = "structured_memory_topicality_experiment"

    @property
    def is_experimental(self) -> bool:
        """True only for the explicitly experimental topicality variant."""
        return self is MemoryPolicy.STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT


@dataclass(frozen=True)
class AffectMetadata:
    """Affect is metadata ON memories — there is no separate emotional store."""

    user_valence: float
    user_arousal: float
    companion_valence: float
    intensity: float
    conflict: float
    comfort: float
    vulnerability: float
    relationship_relevance: float
    emotional_peak: bool


@dataclass(frozen=True)
class SessionSummary:
    """L2 memory: structured summary of one completed session."""

    session_id: str
    started_at_t_h: float
    ended_at_t_h: float
    summary: str
    topics: tuple[str, ...]
    user_facts: tuple[str, ...]
    preference_updates: tuple[str, ...]
    companion_events: tuple[str, ...]
    relationship_events: tuple[str, ...]
    callbacks: tuple[str, ...]
    affect_observations: tuple[AffectMetadata, ...]
    emotional_peak: bool
    importance: float
    source_turn_ids: tuple[int, ...]


@dataclass(frozen=True)
class EpisodicMemory:
    """L3 memory: an important event, always linked back to exact source turns."""

    id: str
    summary: str
    category: MemoryKind
    occurred_at_t_h: float
    created_at_t_h: float
    importance: float
    access_count: int
    last_accessed_t_h: float | None
    affect: AffectMetadata | None
    source_session_id: str
    source_turn_ids: tuple[int, ...]
    verbatim_anchors: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True)
class UserModelAssertion:
    """One consolidated fact about the user, with provenance and status."""

    key: str
    value: str
    confidence: float
    updated_at_t_h: float
    source_memory_ids: tuple[str, ...]
    status: str  # "current" | "superseded"


class UserModelCategory(Enum):
    """Canonical L4 taxonomy — defined exactly ONCE (module invariant 6).

    The 8 categories shared by every store (SQLite and test doubles alike).
    Stores consume this single enum directly; categories are never inferred
    from string prefixes or free-form keys.
    """

    IDENTITY = "identity"
    STABLE_PREFERENCE = "stable_preference"
    CURRENT_PREFERENCE = "current_preference"
    BOUNDARY = "boundary"
    VULNERABILITY = "vulnerability"
    RECURRING_INTEREST = "recurring_interest"
    RELATIONSHIP_PATTERN = "relationship_pattern"
    IMPORTANT_ENTITY = "important_entity"


@dataclass(frozen=True)
class UserModel:
    """L4 consolidated user model; new evidence updates, never piles up."""

    identity: str
    stable_preferences: tuple[UserModelAssertion, ...]
    current_preferences: tuple[UserModelAssertion, ...]
    boundaries: tuple[UserModelAssertion, ...]
    vulnerabilities: tuple[UserModelAssertion, ...]
    recurring_interests: tuple[UserModelAssertion, ...]
    relationship_patterns: tuple[UserModelAssertion, ...]
    important_entities: tuple[UserModelAssertion, ...]


@dataclass(frozen=True)
class UserAffectObservation:
    """An observed, labeled snapshot of the USER's affect at time t_h.

    Distinct from ``CompanionBehaviorState``: no shared fields, no implicit
    conversion between the two (module invariant 1).
    """

    t_h: float
    valence: float
    arousal: float
    label: str


@dataclass(frozen=True)
class CompanionBehaviorState:
    """The companion's own behavioral state, derived from a BehaviorDirective.

    Distinct from ``UserAffectObservation``: no shared fields, no implicit
    conversion between the two (module invariant 1). ``directive_ref`` is an
    opaque id of the BehaviorDirective that produced this state.
    """

    directive_ref: str
    initiative: float
    energy: float
    warmth: float
    playfulness: float


@dataclass(frozen=True)
class MemoryContext:
    """The bounded memory slice handed to composition (module invariant 2).

    L1 ``recent_turns`` + L2 ``session_context`` + L3 ``episodes`` +
    L4 ``user_model`` (``None`` only before any consolidation) +
    ``evidence_anchors`` (exact verbatim excerpts).
    """

    recent_turns: tuple[Turn, ...]
    session_context: tuple[SessionSummary, ...]
    episodes: tuple[EpisodicMemory, ...]
    user_model: UserModel | None
    evidence_anchors: tuple[str, ...]


@dataclass(frozen=True)
class ContactOpportunity:
    """A plausible time to consider initiating contact — and nothing more.

    Produced by the stochastic scheduling process (module invariant 5); it
    carries NO semantic reason such as ``"schedule"``. Semantic motivation
    is resolved afterward into a grounded ``ProactiveIntent``, which links
    back to this opportunity via ``opportunity_id``.

    ``hazard_components`` maps hazard-source names to their contributions
    (e.g. ``{"base": 0.041, "circadian": 1.32, "initiative": 1.18,
    "prior_score": 0.91}``); ``initiative_multiplier`` and
    ``previous_score_multiplier`` are the multiplier values applied to the
    hazard at this opportunity.
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
    """A grounded reason to contact the user (module invariant 3).

    Every field except ``opportunity_id`` is REQUIRED and non-empty —
    especially ``source_type`` / ``source_id`` / ``hook`` / ``evidence``.
    There is no proactive reason without a source; ``evidence`` is the
    provenance chain. ``opportunity_id`` links to the ``ContactOpportunity``
    that made this a plausible moment (default ``None`` for additive
    compatibility while schedulers still create intents directly; it becomes
    required once real opportunities flow).
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
    # assembler-visible continuation policy derived from closing_tendency;
    # never an unused number (plan §15 seam).


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
class CompanionSnapshot:
    """Integration contract: the single place lanes meet before composition.

    Persona, behavior, activity, agenda, life arcs, memory context, recent
    conversation and (optionally) the proactive intent that justifies a
    spontaneous message. Optional slots are ``None`` when not applicable —
    but a present ``ProactiveIntent`` is always fully grounded.
    """

    persona: PersonaProfile
    current_behavior: BehaviorBrief | None
    current_activity: CurrentActivity | None
    agenda: tuple[AgendaItem, ...]
    life_arcs: tuple[LifeArc, ...]
    memory_context: MemoryContext
    recent_conversation: tuple[Turn, ...]
    proactive_intent: ProactiveIntent | None
