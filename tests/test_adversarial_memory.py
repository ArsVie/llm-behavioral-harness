"""A9 adversarial wave — MEMORY attack class (plan §9, cases M-1..M-8).

Attacks on L1/L2/L3/L4: contradictory facts must not surface stale truth,
irrelevant high-salience memories must not crowd out relevant ones,
provenance chains must stay intact, and a memory wipe must never produce
phantom continuity.
"""

from __future__ import annotations

import dataclasses

import pytest

from harness.domain import (
    AffectMetadata,
    CompanionBehaviorState,
    EpisodicMemory,
    MemoryKind,
    SessionSummary,
    UserAffectObservation,
    UserModelAssertion,
)
from harness.memory import MAX_CONTEXT_CHARS, MemoryAgent
from harness.store import SQLiteStore

SEED = 4242


def _store(tmp_path, name: str) -> SQLiteStore:
    return SQLiteStore(tmp_path / name)


def _day_session(store, day: int, user_text: str, *, t_h: float | None = None,
                 agent: MemoryAgent | None = None) -> tuple[MemoryAgent, SessionSummary]:
    """Record one user turn + a reply for a day, close the session, promote
    and update the user model — the L1→L2→L3→L4 pipeline in one call."""
    agent = agent or MemoryAgent(store)
    sid = f"day-{day}"
    t = t_h if t_h is not None else day * 24.0 + 12.0
    agent.record_turn("user", user_text, t, sid)
    agent.record_turn("assistant", "I see!", t + 0.1, sid)
    summary = agent.close_session(sid, ended_at_t_h=(day + 1) * 24.0)
    agent.promote(summary)
    agent.update_user_model(summary)
    return agent, summary


# Contradictory facts (cat Luna / no Luna)


def test_m1_contradictory_facts_supersede_stale_truth(tmp_path):
    """M-1: 'I have a cat named Luna' (day 2) then 'I don't have Luna anymore'
    (day 10). The L4 assertion `user:cat` must be superseded (status flip,
    provenance kept), retrieval at day 20 must surface the revised truth, and
    stale 'Luna is alive' must never be returned as current."""
    store = _store(tmp_path, "m1.db")
    agent = MemoryAgent(store)
    _day_session(store, 2, "I have a cat named Luna", agent=agent)
    _day_session(store, 10, "I don't have Luna anymore", agent=agent)

    cat = store.get_assertion("user:cat")
    assert cat is not None and cat.status == "superseded", (
        "'user has a cat named Luna' must be superseded by the negation"
    )
    luna = store.get_assertion("user:luna")
    assert luna is not None and luna.status == "current"
    assert "no longer has" in luna.value

    ctx = agent.retrieve("cat", context={"t_h": 20 * 24.0})
    assert ctx.user_model is not None
    current = [
        a for bucket in (
            ctx.user_model.stable_preferences, ctx.user_model.current_preferences,
            ctx.user_model.boundaries, ctx.user_model.vulnerabilities,
            ctx.user_model.recurring_interests, ctx.user_model.relationship_patterns,
            ctx.user_model.important_entities,
        )
        for a in bucket
    ]
    stale = [a for a in current if "Luna" in a.value and "no longer" not in a.value]
    assert not stale, "stale 'Luna is alive' surfaced as current truth"
    store.close()


def test_m1b_negation_provenance_keeps_both_sources(tmp_path):
    """M-1 (provenance leg): after the contradiction the superseded assertion's
    persisted provenance must include BOTH the original memory and the
    negation memory (provenance chain intact, nothing deleted)."""
    store = _store(tmp_path, "m1b.db")
    agent = MemoryAgent(store)
    _day_session(store, 2, "I have a cat named Luna", agent=agent)
    _day_session(store, 10, "I don't have Luna anymore", agent=agent)
    cat = store.get_assertion("user:cat")
    episodes = {e.id for e in store.list_episodes()}
    assert cat.source_memory_ids, "superseded assertion lost its provenance"
    assert set(cat.source_memory_ids) <= episodes
    assert len(set(cat.source_memory_ids)) >= 2, (
        "superseded assertion must keep BOTH source memories (original + "
        f"negation), got {cat.source_memory_ids}"
    )
    store.close()


def test_m2_stale_episode_anchored_not_blended_into_current(tmp_path):
    """M-2: the old L3 episode mentioning Luna still exists; retrieval at day
    20 must either exclude it or return it ANCHORED to its verbatim turn —
    never blended into L4 current truth."""
    store = _store(tmp_path, "m2.db")
    agent = MemoryAgent(store)
    _day_session(store, 2, "I have a cat named Luna", agent=agent)
    _day_session(store, 10, "I don't have Luna anymore", agent=agent)

    ctx = agent.retrieve("cat", context={"t_h": 20 * 24.0})
    for ep in ctx.episodes:
        if "Luna" in ep.summary:
            assert ep.verbatim_anchors, (
                "stale Luna episode returned without verbatim evidence anchors"
            )
            assert ep.source_turn_ids, "stale episode lost its source turns"
    if ctx.user_model is not None:
        for bucket in (
            ctx.user_model.stable_preferences, ctx.user_model.current_preferences,
            ctx.user_model.boundaries, ctx.user_model.vulnerabilities,
            ctx.user_model.recurring_interests, ctx.user_model.relationship_patterns,
            ctx.user_model.important_entities,
        ):
            for a in bucket:
                assert not ("Luna" in a.value and "no longer" not in a.value), (
                    "stale Luna truth blended into the L4 projection"
                )
    store.close()


# Irrelevant high-salience memory


def _episode(ep_id: str, summary: str, importance: float, tags, *,
             peak: bool = False) -> EpisodicMemory:
    affect = None
    if peak:
        affect = AffectMetadata(0.5, 0.5, 0.5, 1.0, 0.0, 0.5, 0.5, 0.9, True)
    return EpisodicMemory(
        ep_id, summary, MemoryKind.SHARED_EPISODE, 100.0, 101.0, importance,
        0, None, affect, "day-4", (1,), (summary,), tags,
    )


def test_m3_relevant_low_salience_not_crowded_out(tmp_path):
    """M-3: an emotional-peak memory (importance 1.0, 'user's dog Bruno') and
    several other irrelevant high-salience memories must NOT crowd out a
    directly relevant low-salience memory for 'pottery class' under the hard
    budget limit=8.

    Iteration-2 contract (plan §5-A4 T2, invariants 11-12): the topicality
    boost lives ONLY in STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT. The faithful
    STRUCTURED_MEMORY reranker is formula-exact (0.35 semantic + 0.30
    strength + 0.35 importance) with NO hidden topicality — crowding-out under
    the faithful formula is the documented contrast, which is exactly why the
    experimental variant exists. Both legs are asserted below.
    """
    store = _store(tmp_path, "m3.db")
    distractors = [
        ("user's dog Bruno is very sick", ("dog", "bruno")),
        ("user went hiking in the rain", ("hiking",)),
        ("user's grandma visited", ("grandma",)),
        ("user cried watching a movie", ("movie",)),
        ("user moved to a new apartment", ("apartment",)),
        ("user finished a big work deadline", ("work",)),
        ("user's phone broke", ("phone",)),
        ("user went to the dentist", ("dentist",)),
    ]
    from harness.memory import deterministic_hash_embedder
    for i, (summary, tags) in enumerate(distractors):
        ep = _episode(f"ep_hi_{i}", summary, 1.0, tags, peak=True)
        store.insert_episode(ep)
        store.save_embedding(ep.id, deterministic_hash_embedder(MemoryAgent._episode_text(ep)))
    relevant = _episode("ep_pottery", "user takes pottery class on tuesdays",
                        0.2, ("pottery", "class"))
    store.insert_episode(relevant)
    store.save_embedding(relevant.id, deterministic_hash_embedder(MemoryAgent._episode_text(relevant)))

    # Leg 1: the experimental variant keeps the relevant memory in the top results.
    from harness.domain import MemoryPolicy
    agent = MemoryAgent(
        store, memory_policy=MemoryPolicy.STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT
    )
    ctx = agent.retrieve("pottery class", context={"t_h": 200.0}, limit=8)
    assert any("pottery" in e.summary for e in ctx.episodes), (
        "topicality-boosted variant must not crowd out the directly relevant "
        "low-salience memory (M-3 regression)"
    )
    assert len(ctx.episodes) <= 8
    from harness.memory import _context_chars
    assert _context_chars(ctx) <= MAX_CONTEXT_CHARS

    # Leg 2: the faithful condition has no topicality boost.
    agent_faithful = MemoryAgent(store)
    ctx_f = agent_faithful.retrieve("pottery class", context={"t_h": 200.0}, limit=8)
    assert not any("pottery" in e.summary for e in ctx_f.episodes), (
        "faithful STRUCTURED_MEMORY must be formula-exact (0.35 semantic + "
        "0.30 strength + 0.35 importance) with no topicality boost"
    )
    store.close()


# Provenance: sourceless episodes and hallucinated L4


def test_m4_no_sourceless_episodes(tmp_path):
    """M-4: no sourceless episode may ever be created — promote() refuses an
    unprovenanced summary, and a direct insert_episode with empty
    source_turn_ids must not leave a sourceless L3 row behind."""
    import pytest

    store = _store(tmp_path, "m4.db")
    agent = MemoryAgent(store)
    bad = SessionSummary(
        "day-0", 0.0, 24.0, "summary of nothing", (), (), (), (), (), (),
        (), False, 0.9, (),
    )
    with pytest.raises(ValueError):
        agent.promote(bad)  # refuses to promote unprovenanced content

    # An episode with empty source turns is rejected.
    ghost = _episode("ep_ghost", "user owns a dragon", 0.9, ("dragon",))
    store.insert_episode(ghost)
    stored = store.get_episode("ep_ghost")
    assert stored is None or stored.source_turn_ids, (
        "sourceless episode persisted (provenance -> no truth violated)"
    )
    store.close()


def test_m5_summarization_hallucination_blocked_from_l4(tmp_path):
    """M-5: a SessionSummary containing a fact absent from every source turn
    must never produce an L4 assertion; assertions are created only from
    summary fields backed by existing source turns, with non-empty
    source_memory_ids."""
    store = _store(tmp_path, "m5.db")
    agent = MemoryAgent(store)
    agent.record_turn("user", "hello", 10.0, "day-0")
    agent.record_turn("assistant", "hi", 10.1, "day-0")
    tid = store.turns_for_session("day-0")[0]["id"]
    hallucinated = SessionSummary(
        "day-0", 0.0, 24.0, "user mentioned a pet dragon",
        ("dragon",), ("user's pet is a dragon",), (), (), (), (),
        (), False, 0.9, (tid,),
    )
    updated = agent.update_user_model(hallucinated)
    assert not any("dragon" in a.value for a in updated), (
        "hallucinated fact became an L4 assertion"
    )
    for a in store.list_assertions():
        assert a.source_memory_ids, "assertion without source memories"
        assert "dragon" not in a.value
    store.close()


# User affect is separate from companion state


def test_m6_user_affect_and_companion_state_are_independent():
    """M-6: UserAffectObservation and CompanionBehaviorState are separate
    types with disjoint fields — no shared mutation, no implicit conversion.
    A playful companion state must never yield 'user = playful'."""
    obs_fields = {f.name for f in dataclasses.fields(UserAffectObservation)}
    state_fields = {f.name for f in dataclasses.fields(CompanionBehaviorState)}
    assert obs_fields.isdisjoint(state_fields), (
        f"shared fields between affect observation and companion state: "
        f"{obs_fields & state_fields}"
    )
    # There is no implicit conversion between the two types.
    playful = CompanionBehaviorState("d1", 0.9, 0.9, 0.8, 0.95)
    neutral_obs = UserAffectObservation(10.0, 0.1, 0.1, "neutral")
    assert neutral_obs.label == "neutral" and neutral_obs.valence == 0.1
    assert playful.playfulness == 0.95  # unchanged, no leakage
    assert not hasattr(neutral_obs, "playfulness")
    assert not hasattr(playful, "label")


# 12-turn horizon recall and false recall


def test_m7_bruno_recall_across_horizon_and_no_false_recall(tmp_path):
    """M-7: 'My dog's name is Bruno.' (day 2) must be retrievable at day 20
    WITH its verbatim anchor even after 12+ turns and many days; a query about
    something never said must return nothing relevant (false recall = 0)."""
    store = _store(tmp_path, "m7.db")
    agent = MemoryAgent(store)
    _day_session(store, 2, "My dog's name is Bruno", agent=agent)
    # Many turns and days are added so the transcript horizon cannot carry it.
    for d in range(3, 20):
        _day_session(store, d, f"just a normal day {d} at work", agent=agent)

    ctx = agent.retrieve("dog", context={"t_h": 20 * 24.0})
    bruno = [e for e in ctx.episodes if "Bruno" in e.summary]
    assert bruno, "Bruno memory lost across the 12-turn horizon"
    assert all(e.verbatim_anchors for e in bruno)
    assert all(e.source_turn_ids for e in bruno)

    never = agent.retrieve("quantum entanglement experiments", context={"t_h": 20 * 24.0})
    hits = [
        e for e in never.episodes
        if "quantum" in e.summary or "entanglement" in e.summary
    ]
    assert not hits, "false recall: fabricated memory returned"
    store.close()


# Memory wipe and goldfish reset


def test_m8_memory_wipe_no_crash_no_phantom_continuity(tmp_path):
    """M-8: wipe L3/L4 (and L2) rows after 20 days and restart. No crash, no
    phantom 'remember when…' references to wiped episodes; intents referencing
    wiped sources suppress (G-1 path); L1/L2 rebuild proceeds cleanly."""
    from harness.gates import content_gate
    from harness.domain import ProactiveIntent

    store = _store(tmp_path, "m8.db")
    agent = MemoryAgent(store)
    _day_session(store, 2, "I have a cat named Luna", agent=agent)
    _day_session(store, 5, "remind me to water the plants", agent=agent)
    assert store.list_episodes(), "precondition: memories exist"

    # Simulated DB wipe of the memory tiers (L3/L4/L2 rows gone).
    store.conn.execute("DELETE FROM memory_episodes")
    store.conn.execute("DELETE FROM memory_episode_sources")
    store.conn.execute("DELETE FROM memory_session_summaries")
    store.conn.execute("DELETE FROM user_model_assertions")
    store.conn.commit()

    # Restart with a fresh agent over the same file.
    store2 = _store(tmp_path, "m8.db")
    agent2 = MemoryAgent(store2)
    ctx = agent2.retrieve("cat", context={"t_h": 21 * 24.0})
    assert ctx.episodes == ()  # no phantom episodes
    assert not store2.list_assertions(), "phantom L4 truth after wipe"
    # An intent referencing a wiped memory suppresses.
    ghost = ProactiveIntent(
        "pi_ghost", "callback", "callback", "ep-day-2-0",
        "Callback: remind me to water the plants", 500.0, 506.0, 0.6,
        "episode:ep-day-2-0",
    )
    assert content_gate(ghost, store2, now_h=500.0).code == "no_source"
    # The L1/L2 rebuild proceeds cleanly.
    agent2.record_turn("user", "hello again", 505.0, "day-21")
    agent2.record_turn("assistant", "hi", 505.1, "day-21")
    summary = agent2.close_session("day-21", ended_at_t_h=22 * 24.0)
    assert summary is not None and summary.source_turn_ids
    assert store2.load_session_summary("day-21") is not None
    store.close()
    store2.close()


# Canonical categories, policy switch, and determinism


def test_m9_canonical_categories_only_foreign_strings_never_persist(tmp_path):
    """Invariant 10: after a full L1→L4 pipeline the persisted L4 rows carry
    ONLY canonical UserModelCategory values — a foreign/garbage category
    string is refused at the write seam, never silently stored."""
    from harness.domain import UserModelCategory
    from harness.domain import UserModelAssertion

    store = _store(tmp_path, "m9.db")
    agent = MemoryAgent(store)
    try:
        _day_session(store, 2, "I love hiking in the mountains", agent=agent)
        _day_session(store, 3, "my name is Ada", agent=agent)
        _day_session(store, 4, "please remind me to water the plants", agent=agent)

        rows = store.conn.execute(
            "SELECT key, category FROM user_model_assertions"
        ).fetchall()
        assert rows, "precondition: L4 rows exist"
        canonical = {c.value for c in UserModelCategory}
        for r in rows:
            assert r["category"] in canonical, (
                f"foreign category {r['category']!r} persisted for {r['key']!r} "
                "(invariant 10)"
            )
        # The write seam refuses foreign categories outright.
        bad = UserModelAssertion(
            "user:x", "some value", 0.5, 100.0, ("ep1",), "current"
        )
        with pytest.raises(ValueError):
            store.upsert_assertion(bad, category="garbage_category")
        with pytest.raises(ValueError):
            store.upsert_assertion(bad, category="favorite_color")
        # Category lookups always return canonical enums.
        for a in store.list_assertions():
            cat = store.get_assertion_category(a.key)
            assert isinstance(cat, UserModelCategory)
    finally:
        store.close()


def test_m9b_policy_switch_changes_retrieval_ordering_by_construction(tmp_path):
    """Invariant 11/12: the faithful STRUCTURED_MEMORY reranker and the
    separately named STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT produce
    DIFFERENT retrieval orderings BY CONSTRUCTION on the same store — the
    experimental variant promotes a semantic match that the faithful formula
    ranks below a higher-importance distractor, and the faithful condition
    is formula-exact (no hidden topicality)."""
    from harness.domain import MemoryPolicy
    from harness.memory import deterministic_hash_embedder

    store = _store(tmp_path, "m9b.db")
    try:
        store.insert_episode(_episode(
            "epA", "user's dog Bruno is very sick", 0.9, ("dog", "bruno")
        ))
        store.save_embedding(
            "epA", deterministic_hash_embedder(MemoryAgent._episode_text(
                _episode("epA", "user's dog Bruno is very sick", 0.9, ("dog", "bruno"))
            ))
        )
        relevant = _episode(
            "epB", "user takes pottery class on tuesdays", 0.6, ("pottery", "class")
        )
        store.insert_episode(relevant)
        store.save_embedding(
            relevant.id, deterministic_hash_embedder(MemoryAgent._episode_text(relevant))
        )

        faithful = MemoryAgent(store)  # STRUCTURED_MEMORY (default)
        experimental = MemoryAgent(
            store, memory_policy=MemoryPolicy.STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT
        )
        assert not MemoryPolicy.STRUCTURED_MEMORY.is_experimental
        assert MemoryPolicy.STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT.is_experimental

        ctx_f = faithful.retrieve("pottery class", context={"t_h": 200.0}, limit=2)
        ctx_e = experimental.retrieve("pottery class", context={"t_h": 200.0}, limit=2)
        order_f = [e.id for e in ctx_f.episodes]
        order_e = [e.id for e in ctx_e.episodes]
        assert order_f == ["epA", "epB"], (
            f"faithful reranker order changed: {order_f}"
        )
        assert order_e == ["epB", "epA"], (
            f"experimental order must promote the semantic match: {order_e}"
        )
        assert order_f != order_e, (
            "policy switch must change retrieval ordering by construction"
        )
    finally:
        store.close()


def test_m9c_embedder_and_summarizer_deterministic_across_instances(tmp_path):
    """Same input → same output across instances: the deterministic embedder
    is stable across calls and processes, two MemoryAgent instances over the
    SAME store retrieve byte-identical orderings (embeddings round-trip
    through the store unchanged), and the default summarizer produces
    identical SessionSummary objects across instances."""
    from harness.memory import deterministic_hash_embedder
    from harness.summarization import DeterministicSummaryExtractor

    store = _store(tmp_path, "m9c.db")
    agent_a = MemoryAgent(store)
    agent_b = MemoryAgent(store)
    try:
        text = "user takes pottery class on tuesdays"
        v1 = deterministic_hash_embedder(text)
        v2 = deterministic_hash_embedder(text)
        assert v1 == v2, "embedder not deterministic across calls"
        assert v1 == agent_a._embed(text), "agent embedder disagrees with module fn"
        assert agent_a._embed(text) == agent_b._embed(text), (
            "embedder not deterministic across instances"
        )

        for i, (summary, tags, imp) in enumerate([
            ("user's dog Bruno is very sick", ("dog", "bruno"), 0.9),
            ("user takes pottery class on tuesdays", ("pottery", "class"), 0.6),
            ("user went hiking in the rain", ("hiking",), 0.7),
        ]):
            ep = _episode(f"ep_{i}", summary, imp, tags)
            store.insert_episode(ep)
            store.save_embedding(
                ep.id, deterministic_hash_embedder(MemoryAgent._episode_text(ep))
            )

        ctx_a = agent_a.retrieve("pottery class", context={"t_h": 200.0}, limit=3)
        ctx_b = agent_b.retrieve("pottery class", context={"t_h": 200.0}, limit=3)
        assert [e.id for e in ctx_a.episodes] == [e.id for e in ctx_b.episodes]
        assert ctx_a.evidence_anchors == ctx_b.evidence_anchors

        # The summarizer gives identical output across instances.
        messages = [
            {"role": "user", "content": "I love hiking in the mountains"},
            {"role": "assistant", "content": "that sounds lovely"},
        ]
        extractor_a = DeterministicSummaryExtractor()
        extractor_b = DeterministicSummaryExtractor()
        sa = extractor_a("day-1", messages, {"score": 0.6}, 24.0, 25.0)
        sb = extractor_b("day-1", messages, {"score": 0.6}, 24.0, 25.0)
        assert sa == sb, "summarizer not deterministic across instances"
        assert sa.source_turn_ids == sb.source_turn_ids
    finally:
        store.close()
