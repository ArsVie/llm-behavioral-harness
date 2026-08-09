"""Tests for harness/domain.py — A1 domain contracts (vertical slice Wave 0).

Covers: every type instantiable (happy path), frozen-ness of every dataclass,
ProactiveIntent source fields required (no None, no defaults), MemoryContext
holding all four tiers + anchors, and UserAffectObservation being a DISTINCT
type from CompanionBehaviorState (no shared field names, no conversion).
Iteration-2 Gate 0 additions: ContactOpportunity (timing signal, no semantic
reason), ProactiveIntent.opportunity_id (additive default), the canonical
8-category L4 taxonomy defined exactly once, and MemoryPolicy distinguishing
the research-faithful condition from the experimental topicality variant.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

import pytest

from harness import domain


# ---------------------------------------------------------------------------
# Happy-path factories (one per type)
# ---------------------------------------------------------------------------

def _interest() -> domain.Interest:
    return domain.Interest(name="photography", bucket="exact", salience=0.8)


def _relation() -> domain.InterestRelation:
    return domain.InterestRelation(from_interest="photography", to_interest="portraits", strength=0.7)


def _routine() -> domain.Routine:
    return domain.Routine(name="morning walk", start_frac=0.25, duration_h=1.0, cadence=0.9, salience=0.6)


def _persona() -> domain.PersonaProfile:
    return domain.PersonaProfile(
        name="Mira",
        core="Curious and warm; she notices small things and follows her interests patiently.",
        interests=(_interest(),),
        routines=(_routine(),),
    )


def _life_arc() -> domain.LifeArc:
    return domain.LifeArc(
        id="arc:pottery",
        name="Learning pottery",
        interest="crafts",
        started_day=4,
        progress=0.37,
        status="active",
        next_intention="practice glazing the bowl",
    )


def _agenda_item() -> domain.AgendaItem:
    return domain.AgendaItem(
        id="agenda_item:pottery_2026_08_08",
        start_t_h=15.0,
        end_t_h=17.0,
        activity="Pottery class",
        source_type="arc",
        source_id="arc:pottery",
        salience=0.7,
        status="planned",
    )


def _agenda() -> domain.DailyAgenda:
    return domain.DailyAgenda(day=8, items=(_agenda_item(),))


def _current_activity() -> domain.CurrentActivity:
    return domain.CurrentActivity(t_h=15.5, item=_agenda_item(), description="Shaping a bowl on the wheel")


def _affect() -> domain.AffectMetadata:
    return domain.AffectMetadata(
        user_valence=0.4,
        user_arousal=0.2,
        companion_valence=0.3,
        intensity=0.5,
        conflict=0.0,
        comfort=0.8,
        vulnerability=0.1,
        relationship_relevance=0.6,
        emotional_peak=False,
    )


def _session_summary() -> domain.SessionSummary:
    return domain.SessionSummary(
        session_id="session:42",
        started_at_t_h=96.0,
        ended_at_t_h=96.75,
        summary="Talked about the pottery class and weekend plans.",
        topics=("pottery", "weekend"),
        user_facts=("user enjoys ceramics",),
        preference_updates=(),
        companion_events=("mentioned glazing anxiety",),
        relationship_events=(),
        callbacks=("ask about the kiln firing on day 10",),
        affect_observations=(_affect(),),
        emotional_peak=False,
        importance=0.6,
        source_turn_ids=(101, 102),
    )


def _episode() -> domain.EpisodicMemory:
    return domain.EpisodicMemory(
        id="ep:7",
        summary="User said their dog's name is Bruno.",
        category=domain.MemoryKind.USER_FACT,
        occurred_at_t_h=48.5,
        created_at_t_h=49.0,
        importance=0.8,
        access_count=2,
        last_accessed_t_h=120.0,
        affect=_affect(),
        source_session_id="session:12",
        source_turn_ids=(55,),
        verbatim_anchors=("My dog's name is Bruno.",),
        tags=("dog", "family"),
    )


def _assertion() -> domain.UserModelAssertion:
    return domain.UserModelAssertion(
        key="pet.dog.name",
        value="Bruno",
        confidence=0.95,
        updated_at_t_h=49.0,
        source_memory_ids=("ep:7",),
        status="current",
    )


def _user_model() -> domain.UserModel:
    return domain.UserModel(
        identity="Alex",
        stable_preferences=(_assertion(),),
        current_preferences=(),
        boundaries=(),
        vulnerabilities=(),
        recurring_interests=(),
        relationship_patterns=(),
        important_entities=(),
    )


def _user_affect() -> domain.UserAffectObservation:
    return domain.UserAffectObservation(t_h=120.5, valence=-0.3, arousal=0.4, label="frustrated")


def _behavior_state() -> domain.CompanionBehaviorState:
    return domain.CompanionBehaviorState(directive_ref="directive:88", initiative=0.6, energy=0.7, warmth=0.8, playfulness=0.4)


def _turn() -> domain.Turn:
    return domain.Turn(role="user", text="My dog's name is Bruno.", t_h=48.5)


def _proactive_intent() -> domain.ProactiveIntent:
    return domain.ProactiveIntent(
        id="intent:3",
        reason="finished pottery class",
        source_type="agenda_item",
        source_id="agenda_item:pottery_2026_08_08",
        hook="You just finished the pottery class scheduled this afternoon; you were nervous about glazing the bowl.",
        created_t_h=17.1,
        valid_until_t_h=20.0,
        salience=0.8,
        evidence="agenda_item:pottery_2026_08_08 status=completed at t_h=17.0 (arc:pottery next_intention='practice glazing the bowl')",
    )


def _opportunity() -> domain.ContactOpportunity:
    return domain.ContactOpportunity(
        id="opportunity:313",
        desired_t_h=17.05,
        created_t_h=16.9,
        valid_until_t_h=20.0,
        hazard_components={"base": 0.041, "circadian": 1.32, "initiative": 1.18, "prior_score": 0.91},
        initiative_multiplier=1.18,
        previous_score_multiplier=0.91,
    )


def _controls() -> domain.GenerationControls:
    return domain.GenerationControls(max_tokens=600, response_delay_s=0.0, closing_tendency=0.3, initiative_factor=1.0)


def _brief() -> domain.BehaviorBrief:
    return domain.BehaviorBrief(
        valence=0.2,
        energy=0.6,
        reactivity=0.4,
        warmth=0.7,
        expressiveness=0.5,
        playfulness=0.4,
        reflectiveness=0.5,
        initiative=0.6,
        response_length_scale=1.0,
        response_delay_s=0.0,
        closing_tendency=0.3,
    )


def _memory_context() -> domain.MemoryContext:
    return domain.MemoryContext(
        recent_turns=(_turn(),),
        session_context=(_session_summary(),),
        episodes=(_episode(),),
        user_model=_user_model(),
        evidence_anchors=("My dog's name is Bruno.",),
    )


def _snapshot() -> domain.CompanionSnapshot:
    return domain.CompanionSnapshot(
        persona=_persona(),
        current_behavior=_brief(),
        current_activity=_current_activity(),
        agenda=(_agenda_item(),),
        life_arcs=(_life_arc(),),
        memory_context=_memory_context(),
        recent_conversation=(_turn(),),
        proactive_intent=_proactive_intent(),
    )


# One happy-path instance per dataclass type (MemoryKind is an Enum, tested separately).
ALL_INSTANCES: dict[type, object] = {
    domain.Interest: _interest(),
    domain.InterestRelation: _relation(),
    domain.Routine: _routine(),
    domain.PersonaProfile: _persona(),
    domain.LifeArc: _life_arc(),
    domain.AgendaItem: _agenda_item(),
    domain.DailyAgenda: _agenda(),
    domain.CurrentActivity: _current_activity(),
    domain.AffectMetadata: _affect(),
    domain.SessionSummary: _session_summary(),
    domain.EpisodicMemory: _episode(),
    domain.UserModelAssertion: _assertion(),
    domain.UserModel: _user_model(),
    domain.UserAffectObservation: _user_affect(),
    domain.CompanionBehaviorState: _behavior_state(),
    domain.MemoryContext: _memory_context(),
    domain.ProactiveIntent: _proactive_intent(),
    domain.ContactOpportunity: _opportunity(),
    domain.GenerationControls: _controls(),
    domain.BehaviorBrief: _brief(),
    domain.Turn: _turn(),
    domain.CompanionSnapshot: _snapshot(),
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_every_type_instantiable() -> None:
    for cls, obj in ALL_INSTANCES.items():
        assert isinstance(obj, cls), f"{cls.__name__} did not instantiate"


def test_every_dataclass_is_frozen() -> None:
    for cls, obj in ALL_INSTANCES.items():
        first_field = dataclasses.fields(cls)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, first_field, getattr(obj, first_field))


def test_memory_kind_members() -> None:
    assert {member.name for member in domain.MemoryKind} == {
        "USER_FACT",
        "USER_PREFERENCE",
        "SHARED_EPISODE",
        "COMPANION_EPISODE",
        "RELATIONSHIP_EVENT",
        "CALLBACK",
    }
    assert _episode().category is domain.MemoryKind.USER_FACT


def test_proactive_intent_has_no_none_source_fields() -> None:
    intent = _proactive_intent()
    for field_name in ("source_type", "source_id", "hook", "evidence"):
        value = getattr(intent, field_name)
        assert value is not None
        assert isinstance(value, str) and value != ""
    # Source fields are required: omitting evidence must fail at construction.
    with pytest.raises(TypeError):
        domain.ProactiveIntent(
            id="intent:4",
            reason="check-in",
            source_type="agenda_item",
            source_id="agenda_item:pottery_2026_08_08",
            hook="You just finished the pottery class.",
            created_t_h=17.1,
            valid_until_t_h=20.0,
            salience=0.5,
        )


def test_memory_context_holds_all_tiers() -> None:
    turn = _turn()
    summary = _session_summary()
    episode = _episode()
    model = _user_model()
    ctx = _memory_context()
    assert ctx.recent_turns == (turn,)  # L1
    assert ctx.session_context == (summary,)  # L2
    assert ctx.episodes == (episode,)  # L3
    assert ctx.user_model == model  # L4
    assert ctx.evidence_anchors == ("My dog's name is Bruno.",)
    # L4 projection may legitimately be absent before any consolidation exists.
    empty = domain.MemoryContext(
        recent_turns=(), session_context=(), episodes=(), user_model=None, evidence_anchors=()
    )
    assert empty.user_model is None


def test_user_affect_observation_distinct_from_companion_behavior_state() -> None:
    uao = _user_affect()
    cbs = _behavior_state()
    # Different classes, neither is an instance of the other.
    assert type(uao) is domain.UserAffectObservation
    assert type(cbs) is domain.CompanionBehaviorState
    assert not isinstance(uao, domain.CompanionBehaviorState)
    assert not isinstance(cbs, domain.UserAffectObservation)
    # No shared field names -> no accidental coercion between the two.
    uao_fields = {f.name for f in dataclasses.fields(domain.UserAffectObservation)}
    cbs_fields = {f.name for f in dataclasses.fields(domain.CompanionBehaviorState)}
    assert uao_fields.isdisjoint(cbs_fields)


def test_companion_snapshot_happy_path() -> None:
    snap = _snapshot()
    assert snap.persona.name == "Mira"
    assert snap.current_behavior is not None
    assert snap.current_activity is not None
    assert len(snap.agenda) == 1
    assert len(snap.life_arcs) == 1
    assert len(snap.memory_context.recent_turns) == 1
    assert len(snap.recent_conversation) == 1
    assert snap.proactive_intent is not None
    # Optional slots may be None, but a present intent stays fully grounded.
    bare = domain.CompanionSnapshot(
        persona=_persona(),
        current_behavior=None,
        current_activity=None,
        agenda=(),
        life_arcs=(),
        memory_context=domain.MemoryContext(
            recent_turns=(), session_context=(), episodes=(), user_model=None, evidence_anchors=()
        ),
        recent_conversation=(),
        proactive_intent=None,
    )
    assert bare.current_behavior is None
    assert bare.proactive_intent is None


# ---------------------------------------------------------------------------
# Iteration-2 Gate-0 contracts (ContactOpportunity, L4 taxonomy, MemoryPolicy)
# ---------------------------------------------------------------------------

def test_contact_opportunity_shape_and_no_semantic_reason() -> None:
    """ContactOpportunity is a timing signal only — never a semantic reason."""
    opp = _opportunity()
    fields = {f.name for f in dataclasses.fields(domain.ContactOpportunity)}
    assert fields == {
        "id",
        "desired_t_h",
        "created_t_h",
        "valid_until_t_h",
        "hazard_components",
        "initiative_multiplier",
        "previous_score_multiplier",
    }
    # No semantic-reason vocabulary anywhere in the shape.
    assert "reason" not in fields
    assert "semantic" not in fields
    assert "schedule" not in fields
    # Hazard components are exactly the stochastic sources, with float values.
    assert opp.hazard_components == {
        "base": 0.041,
        "circadian": 1.32,
        "initiative": 1.18,
        "prior_score": 0.91,
    }
    assert all(isinstance(v, float) for v in opp.hazard_components.values())
    assert isinstance(opp.id, str)
    for name in ("desired_t_h", "created_t_h", "valid_until_t_h"):
        assert isinstance(getattr(opp, name), float)


def test_proactive_intent_opportunity_id_defaults_none_and_links_opportunity() -> None:
    """opportunity_id defaults to None (additive) and links the opportunity."""
    intent = _proactive_intent()
    assert intent.opportunity_id is None
    linked = domain.ProactiveIntent(
        id="intent:87",
        reason="finished pottery class",
        source_type="agenda_item",
        source_id="agenda_item:pottery_2026_08_08",
        hook="You just finished the pottery class scheduled this afternoon.",
        created_t_h=17.1,
        valid_until_t_h=20.0,
        salience=0.8,
        evidence="agenda_item:pottery_2026_08_08 status=completed at t_h=17.0",
        opportunity_id="opportunity:313",
    )
    assert linked.opportunity_id == "opportunity:313"
    # opportunity_id is the ONLY defaulted field: every source field stays
    # required, and the source invariant still holds.
    defaulted = [
        f.name for f in dataclasses.fields(domain.ProactiveIntent) if f.default is not dataclasses.MISSING
    ]
    assert defaulted == ["opportunity_id"]
    for field_name in ("id", "reason", "source_type", "source_id", "hook", "evidence"):
        assert getattr(linked, field_name) != ""


def test_l4_taxonomy_canonical_eight_categories() -> None:
    """The canonical L4 taxonomy is exactly the 8 plan categories."""
    assert {member.name for member in domain.UserModelCategory} == {
        "IDENTITY",
        "STABLE_PREFERENCE",
        "CURRENT_PREFERENCE",
        "BOUNDARY",
        "VULNERABILITY",
        "RECURRING_INTEREST",
        "RELATIONSHIP_PATTERN",
        "IMPORTANT_ENTITY",
    }
    # Lowercase-string values per repo style (like MemoryKind).
    for member in domain.UserModelCategory:
        assert isinstance(member.value, str)
        assert member.value == member.name.lower()
    assert len(list(domain.UserModelCategory)) == 8


def _enum_member_names(enum_cls: Any) -> set[str]:
    """Member names of an enum class (helper for the exactly-once scan)."""
    return {member.name for member in enum_cls}


def test_l4_taxonomy_defined_exactly_once() -> None:
    """No second enum in the module carries the canonical L4 member set."""
    canonical_names = {
        "IDENTITY",
        "STABLE_PREFERENCE",
        "CURRENT_PREFERENCE",
        "BOUNDARY",
        "VULNERABILITY",
        "RECURRING_INTEREST",
        "RELATIONSHIP_PATTERN",
        "IMPORTANT_ENTITY",
    }
    carriers = [
        name
        for name, obj in vars(domain).items()
        if isinstance(obj, type)
        and issubclass(obj, Enum)
        and _enum_member_names(obj) == canonical_names
    ]
    assert carriers == ["UserModelCategory"]


def test_memory_policy_members_and_experimental_distinction() -> None:
    """STRUCTURED_MEMORY is faithful; the topicality variant is experimental."""
    assert {member.name for member in domain.MemoryPolicy} == {
        "RAW_CONTEXT",
        "VERBATIM_RAG",
        "STRUCTURED_MEMORY",
        "STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT",
    }
    for member in domain.MemoryPolicy:
        assert isinstance(member.value, str)
        assert member.value == member.name.lower()
    # Faithful condition is not experimental; the named variant is.
    assert not domain.MemoryPolicy.STRUCTURED_MEMORY.is_experimental
    assert domain.MemoryPolicy.STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT.is_experimental
    assert not domain.MemoryPolicy.RAW_CONTEXT.is_experimental
    assert not domain.MemoryPolicy.VERBATIM_RAG.is_experimental
    # The two conditions are distinct policy values.
    assert domain.MemoryPolicy.STRUCTURED_MEMORY != domain.MemoryPolicy.STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT
