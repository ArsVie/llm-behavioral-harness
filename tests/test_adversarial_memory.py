"""A9 adversarial wave — MEMORY attack class (plan §9, cases M-1..M-8).

Attacks on L1/L2/L3/L4: contradictory facts must not surface stale truth,
irrelevant high-salience memories must not crowd out relevant ones,
provenance chains must stay intact, and a memory wipe must never produce
phantom continuity.
"""

from __future__ import annotations

import dataclasses

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


# --------------------------------------------------------------------------- #
# M-1 / M-2: contradictory facts (cat Luna / no Luna)
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# M-3: irrelevant high-salience memory
# --------------------------------------------------------------------------- #


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

    # Leg 1 — experimental variant: the topicality boost must save the
    # relevant low-salience memory from the high-salience distractors.
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

    # Leg 2 — faithful condition: formula-exact 0.35/0.30/0.35, NO hidden
    # topicality. Crowding-out here is the documented contrast (plan §5-A4 T2).
    agent_faithful = MemoryAgent(store)
    ctx_f = agent_faithful.retrieve("pottery class", context={"t_h": 200.0}, limit=8)
    assert not any("pottery" in e.summary for e in ctx_f.episodes), (
        "faithful STRUCTURED_MEMORY must be formula-exact (0.35 semantic + "
        "0.30 strength + 0.35 importance) with no topicality boost"
    )
    store.close()


# --------------------------------------------------------------------------- #
# M-4 / M-5: provenance — no sourceless episodes, no hallucinated L4
# --------------------------------------------------------------------------- #


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

    # direct store attack: an episode with empty source turns must be rejected
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


# --------------------------------------------------------------------------- #
# M-6: user affect != companion state
# --------------------------------------------------------------------------- #


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
    # no implicit conversion either way: mutating one never touches the other
    playful = CompanionBehaviorState("d1", 0.9, 0.9, 0.8, 0.95)
    neutral_obs = UserAffectObservation(10.0, 0.1, 0.1, "neutral")
    assert neutral_obs.label == "neutral" and neutral_obs.valence == 0.1
    assert playful.playfulness == 0.95  # unchanged, no leakage
    assert not hasattr(neutral_obs, "playfulness")
    assert not hasattr(playful, "label")


# --------------------------------------------------------------------------- #
# M-7: 12-turn horizon recall + no false recall
# --------------------------------------------------------------------------- #


def test_m7_bruno_recall_across_horizon_and_no_false_recall(tmp_path):
    """M-7: 'My dog's name is Bruno.' (day 2) must be retrievable at day 20
    WITH its verbatim anchor even after 12+ turns and many days; a query about
    something never said must return nothing relevant (false recall = 0)."""
    store = _store(tmp_path, "m7.db")
    agent = MemoryAgent(store)
    _day_session(store, 2, "My dog's name is Bruno", agent=agent)
    # flood many turns + days so the transcript horizon cannot carry it
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


# --------------------------------------------------------------------------- #
# M-8: memory wipe / goldfish reset
# --------------------------------------------------------------------------- #


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

    # simulated DB wipe of the memory tiers (L3/L4/L2 rows gone)
    store.conn.execute("DELETE FROM memory_episodes")
    store.conn.execute("DELETE FROM memory_episode_sources")
    store.conn.execute("DELETE FROM memory_session_summaries")
    store.conn.execute("DELETE FROM user_model_assertions")
    store.conn.commit()

    # restart: fresh agent over the same file
    store2 = _store(tmp_path, "m8.db")
    agent2 = MemoryAgent(store2)
    ctx = agent2.retrieve("cat", context={"t_h": 21 * 24.0})
    assert ctx.episodes == ()  # no phantom episodes
    assert not store2.list_assertions(), "phantom L4 truth after wipe"
    # intent referencing a wiped memory suppresses
    ghost = ProactiveIntent(
        "pi_ghost", "callback", "callback", "ep-day-2-0",
        "Callback: remind me to water the plants", 500.0, 506.0, 0.6,
        "episode:ep-day-2-0",
    )
    assert content_gate(ghost, store2, now_h=500.0).code == "no_source"
    # L1/L2 rebuild proceeds cleanly
    agent2.record_turn("user", "hello again", 505.0, "day-21")
    agent2.record_turn("assistant", "hi", 505.1, "day-21")
    summary = agent2.close_session("day-21", ended_at_t_h=22 * 24.0)
    assert summary is not None and summary.source_turn_ids
    assert store2.load_session_summary("day-21") is not None
    store.close()
    store2.close()
