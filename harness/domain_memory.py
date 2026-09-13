"""Memory-layer domain types: the L2/L4 vocabulary, re-exported by
``harness.domain`` so existing imports keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

class MemoryKind(Enum):
    """Category of an episodic memory."""

    USER_FACT = "user_fact"
    USER_PREFERENCE = "user_preference"
    SHARED_EPISODE = "shared_episode"
    COMPANION_EPISODE = "companion_episode"
    RELATIONSHIP_EVENT = "relationship_event"
    CALLBACK = "callback"


class MemoryPolicy(Enum):
    """Memory conditioning policy for generation/eval."""

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
    """Canonical L4 taxonomy: the 8 categories every store shares.

    Stores consume this enum directly; categories are never inferred from
    string prefixes or free-form keys.
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
    """An observed, labeled snapshot of the USER's affect at time ``t_h``.

    Distinct from ``CompanionBehaviorState``: no implicit conversion.
    """

    t_h: float
    valence: float
    arousal: float
    label: str
