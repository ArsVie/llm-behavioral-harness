"""Memory pipeline tests — A5 (L1/L2/L3/L4, ZifaMem-style).

Covers the Memory Gate (§13 of plans/companion-vertical-slice-2026-08.md):
L1->L2, promotion, evidence retention, L4 consolidation + revision, affect
as metadata, user-affect/companion-state separation, the 0.35/0.30/0.35
reranker, temporal anchors, the three policy baselines, the hard budget and
the no-unprovenanced-truth rule.

A2's store seam (save_session_summary/insert_episode/save_embedding/
upsert_assertion/...) has not landed yet (wip/vslice-a2 tip == Gate-0 main),
so all tests run against ``FakeStore`` below — a seam-faithful in-memory
implementation of the §15 store contract. MemoryAgent only ever touches the
seam, so it works unchanged against the real store once A2 merges.
"""

from __future__ import annotations

import inspect
from dataclasses import fields, replace

import pytest

import harness.memory as memory
from harness.domain import (
    AffectMetadata,
    CompanionBehaviorState,
    EpisodicMemory,
    MemoryKind,
    MemoryContext,
    SessionSummary,
    Turn,
    UserAffectObservation,
    UserModel,
    UserModelAssertion,
)
from harness.memory import (
    IMPORTANCE_WEIGHT,
    SEM_WEIGHT,
    STRENGTH_WEIGHT,
    MAX_CONTEXT_CHARS,
    MemoryAgent,
    PromotionPolicy,
    deterministic_summarizer,
    raw_history,
    simple_retrieval,
)

# ---------------------------------------------------------------------------
# Seam-faithful in-memory store (A2 §15 contract; lands in store.py later)
# ---------------------------------------------------------------------------

_BUCKET_BY_PREFIX = [
    ("user:", "identity"),
    ("preference:", "current_preferences"),
    ("boundary:", "boundaries"),
    ("vulnerability:", "vulnerabilities"),
    ("interest:", "recurring_interests"),
    ("relationship:", "relationship_patterns"),
    ("entity:", "important_entities"),
]


class FakeStore:
    """In-memory implementation of the §15 store seam (memory tiers + L1)."""

    def __init__(self) -> None:
        self._messages: list[dict] = []
        self._next_id = 1
        self._judgements: dict[int, dict] = {}
        self._summaries: dict[str, SessionSummary] = {}
        self._episodes: dict[str, EpisodicMemory] = {}
        self._embeddings: dict[str, list[float]] = {}
        self._assertions: list[UserModelAssertion] = []

    # -- L1 (messages) ------------------------------------------------------
    def add_message(self, role, content, t_h, day, proactive=False, session_id=None) -> int:
        row = {
            "id": self._next_id,
            "role": role,
            "content": content,
            "t_h": float(t_h),
            "day": day,
            "proactive": int(proactive),
            "session_id": session_id,
        }
        self._next_id += 1
        self._messages.append(row)
        return row["id"]

    def recent_messages(self, limit: int = 12) -> list[dict]:
        # Mirrors the real store: newest `limit` rows, oldest -> newest.
        return self._messages[-limit:]

    def messages_for_day(self, day: int) -> list[dict]:
        return [m for m in self._messages if m["day"] == day]

    def messages_for_session(self, session_id: str) -> list[dict]:
        return [m for m in self._messages if m.get("session_id") == session_id]

    def load_judgement(self, day: int) -> dict | None:
        return self._judgements.get(day)

    def save_judgement(self, day, score, justification="", model="test", shadow=1) -> None:
        self._judgements[day] = {
            "day": day, "score": score, "justification": justification,
            "model": model, "shadow": shadow,
        }

    # -- L2 ----------------------------------------------------------------
    def save_session_summary(self, summary: SessionSummary) -> None:
        self._summaries[summary.session_id] = summary

    def load_session_summary(self, session_id: str) -> SessionSummary | None:
        return self._summaries.get(session_id)

    # -- L3 ----------------------------------------------------------------
    def insert_episode(self, ep: EpisodicMemory) -> str:
        self._episodes[ep.id] = ep
        return ep.id

    def get_episode(self, episode_id: str) -> EpisodicMemory | None:
        return self._episodes.get(episode_id)

    def list_episodes(self, limit: int = 500, category=None) -> list[EpisodicMemory]:
        eps = list(self._episodes.values())
        if category is not None:
            eps = [e for e in eps if e.category == category]
        return eps[:limit]

    def touch_episode(self, episode_id: str, t_h: float) -> None:
        ep = self._episodes.get(episode_id)
        if ep is not None:
            self._episodes[episode_id] = replace(
                ep, access_count=ep.access_count + 1, last_accessed_t_h=float(t_h)
            )

    def save_embedding(self, episode_id: str, vector: list[float]) -> None:
        self._embeddings[episode_id] = list(vector)

    def load_embeddings(self) -> list[tuple[str, list[float]]]:
        return list(self._embeddings.items())

    # -- L4 ----------------------------------------------------------------
    def upsert_assertion(self, a: UserModelAssertion) -> None:
        """Supersedes any same-key current assertion by status flip."""
        for i, old in enumerate(self._assertions):
            if old.key == a.key and old.status == "current":
                self._assertions[i] = replace(old, status="superseded")
        self._assertions.append(a)

    def list_assertions(self, status: str | None = "current") -> list[UserModelAssertion]:
        if status is None:
            return list(self._assertions)
        return [a for a in self._assertions if a.status == status]

    def supersede_assertion(self, key: str) -> None:
        for i, old in enumerate(self._assertions):
            if old.key == key and old.status == "current":
                self._assertions[i] = replace(old, status="superseded")

    def get_assertion(self, key: str) -> UserModelAssertion | None:
        for a in reversed(self._assertions):
            if a.key == key and a.status == "current":
                return a
        return None

    def load_user_model(self) -> UserModel:
        identity = ""
        buckets: dict[str, tuple[UserModelAssertion, ...]] = {
            name: ()
            for name in (
                "stable_preferences", "current_preferences", "boundaries",
                "vulnerabilities", "recurring_interests", "relationship_patterns",
                "important_entities",
            )
        }
        for a in self.list_assertions("current"):
            for prefix, bucket in _BUCKET_BY_PREFIX:
                if a.key.startswith(prefix):
                    if bucket == "identity":
                        identity = a.value  # projection string, not an assertion tuple
                    else:
                        buckets[bucket] = buckets[bucket] + (a,)
                    break
        return UserModel(identity=identity, **buckets)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_day(
    store: FakeStore,
    day: int,
    turns: list[tuple[str, str]],
    *,
    judgement: float | None = None,
    agent: MemoryAgent | None = None,
) -> SessionSummary:
    """Record turns for a day, close the session, promote, update the model."""
    agent = agent or MemoryAgent(store)
    session_id = f"day-{day}"
    t = day * 24.0 + 9.0
    for role, text in turns:
        agent.record_turn(role, text, t, session_id)
        t += 0.1
    if judgement is not None:
        store.save_judgement(day, judgement)
    summary = agent.close_session(session_id, ended_at_t_h=day * 24.0 + 23.0)
    agent.promote(summary)
    agent.update_user_model(summary)
    return summary


def make_episode(
    episode_id: str,
    text: str,
    *,
    importance: float = 0.6,
    created: float = 100.0,
    tags: tuple[str, ...] = (),
    category: MemoryKind = MemoryKind.USER_FACT,
    affect: AffectMetadata | None = None,
    anchors: tuple[str, ...] = ("exact source text",),
    turn_ids: tuple[int, ...] = (1,),
    access_count: int = 0,
) -> EpisodicMemory:
    return EpisodicMemory(
        id=episode_id,
        summary=text,
        category=category,
        occurred_at_t_h=created,
        created_at_t_h=created,
        importance=importance,
        access_count=access_count,
        last_accessed_t_h=None,
        affect=affect,
        source_session_id="day-2",
        source_turn_ids=turn_ids,
        verbatim_anchors=anchors,
        tags=tags,
    )


class VocabEmbedder:
    """One-hot unit vectors over a fixed vocab — exact cosine control.

    A vocab word contributes whenever it appears as a substring (works for
    controlled single-token test vocabularies like "xxx" -> "x").
    """

    def __init__(self, vocab: list[str]) -> None:
        self._words = vocab
        self.dim = len(vocab)

    def __call__(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        lowered = text.lower()
        for i, w in enumerate(self._words):
            if w in lowered:
                vec[i] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]


def _context_chars(ctx: MemoryContext) -> int:
    return memory._context_chars(ctx)


# ---------------------------------------------------------------------------
# Bruno: the 12-turn-horizon flagship test
# ---------------------------------------------------------------------------


def test_bruno_crosses_12_turn_horizon():
    store = FakeStore()
    agent = MemoryAgent(store)

    # Day 2: the user discloses the dog's name (sessions are calendar days).
    run_day(
        store, 2,
        [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
        judgement=0.6, agent=agent,
    )
    assert any("Bruno" in e.summary for e in store.list_episodes())
    assert store.get_assertion("user:dog:name") is not None

    # Several days of small talk: >12 stored messages AFTER the Bruno turn,
    # none of which mention the dog.
    for day in (3, 4):
        turns = []
        for i in range(5):
            turns.append(("user", f"Day {day} small talk message {i} about the weather."))
            turns.append(("assistant", f"Day {day} reply {i} about the weather too."))
        run_day(store, day, turns, agent=agent)

    recent = store.recent_messages(12)
    assert len(recent) == 12
    assert not any("Bruno" in m["content"] for m in recent)
    assert all(m["id"] > 2 for m in recent)  # >12 messages after the Bruno turn

    # Day 20: a relevant query still surfaces dog=Bruno — from L3/L4, not L1.
    ctx = agent.retrieve("what is my dog's name?", context={"t_h": 20 * 24 + 12.0})

    assert not any("Bruno" in t.text for t in ctx.recent_turns)          # not L1
    assert any("Bruno" in e.summary for e in ctx.episodes)               # L3
    assert any("Bruno" in a for a in ctx.evidence_anchors)               # exact evidence
    assert ctx.session_context and "Bruno" in ctx.session_context[0].summary  # L2
    assert ctx.user_model is not None
    # L4 projection: the dog fact is consolidated under identity (a string)
    # and its assertion record carries the value.
    assert "Bruno" in ctx.user_model.identity
    dog_assertion = store.get_assertion("user:dog:name")
    assert dog_assertion is not None and "Bruno" in dog_assertion.value
    assert dog_assertion.status == "current"
    assert ctx.evidence_anchors[0] == "My dog's name is Bruno."  # verbatim


# ---------------------------------------------------------------------------
# Promotion policy (L2 -> L3)
# ---------------------------------------------------------------------------


def test_promotion_high_importance_session_promotes():
    store = FakeStore()
    agent = MemoryAgent(store)
    summary = run_day(
        store, 2,
        [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
        judgement=0.6, agent=agent,
    )
    assert summary.importance >= 0.5
    episodes = agent.promote(summary)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.category == MemoryKind.USER_FACT
    assert ep.summary == "user's dog is named Bruno"
    assert ep.verbatim_anchors == ("My dog's name is Bruno.",)
    assert ep.source_turn_ids == (1,)
    assert ep.importance == summary.importance


def test_promotion_mundane_session_does_not_promote():
    store = FakeStore()
    agent = MemoryAgent(store)
    summary = run_day(
        store, 4,
        [
            ("user", "How are you?"),
            ("assistant", "Doing well, thanks for asking."),
            ("user", "Pretty good."),
            ("assistant", "Glad to hear it."),
            ("user", "That sounds nice."),
        ],
        agent=agent,
    )
    assert summary.importance < 0.5
    assert summary.emotional_peak is False
    assert agent.promote(summary) == []
    assert store.list_episodes() == []


def test_promotion_emotional_peak_promotes_below_threshold():
    store = FakeStore()
    agent = MemoryAgent(store)
    summary = run_day(
        store, 3,
        [("user", "I'm so angry!!! This is awful!!!"), ("assistant", "I'm really sorry.")],
        judgement=-0.9, agent=agent,
    )
    assert summary.importance < 0.5          # importance path says NO...
    assert summary.emotional_peak is True    # ...but the peak path says YES
    episodes = agent.promote(summary)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.category == MemoryKind.SHARED_EPISODE
    assert ep.affect is not None and ep.affect.emotional_peak is True
    assert ep.importance < 0.5


def test_promotion_policy_configurable_disables_peaks():
    store = FakeStore()
    agent = MemoryAgent(store, policy=PromotionPolicy(importance_threshold=0.5, promote_emotional_peaks=False))
    summary = run_day(
        store, 3,
        [("user", "I'm so angry!!! This is awful!!!"), ("assistant", "I'm really sorry.")],
        judgement=-0.9, agent=agent,
    )
    assert summary.emotional_peak and summary.importance < 0.5
    assert agent.promote(summary) == []


def test_promote_is_idempotent():
    store = FakeStore()
    agent = MemoryAgent(store)
    summary = run_day(
        store, 2,
        [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
        judgement=0.6, agent=agent,
    )
    agent.promote(summary)
    agent.promote(summary)
    assert len(store.list_episodes()) == 1


# ---------------------------------------------------------------------------
# L4 consolidation and revision
# ---------------------------------------------------------------------------


def test_l4_compatible_facts_consolidate_no_duplicates():
    store = FakeStore()
    agent = MemoryAgent(store)
    for day in (2, 5):
        run_day(
            store, day,
            [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
            judgement=0.6, agent=agent,
        )
    current = store.list_assertions("current")
    dog = [a for a in current if a.key == "user:dog:name"]
    assert len(dog) == 1                      # consolidated — no duplicate facts
    assert dog[0].confidence == pytest.approx(0.6 + 0.15)
    assert dog[0].status == "current"
    assert dog[0].source_memory_ids == ("ep-day-2-0", "ep-day-5-0")


def test_l4_contradiction_supersedes_keeps_provenance():
    store = FakeStore()
    agent = MemoryAgent(store)
    run_day(
        store, 2,
        [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
        judgement=0.6, agent=agent,
    )
    run_day(
        store, 8,
        [("user", "Actually, my dog's name is Rex now."), ("assistant", "Got it.")],
        judgement=0.5, agent=agent,
    )
    # Old assertion is superseded, provenance intact.
    superseded = [a for a in store.list_assertions("superseded") if a.key == "user:dog:name"]
    assert len(superseded) == 1
    assert superseded[0].value == "user's dog is named Bruno"
    assert superseded[0].source_memory_ids == ("ep-day-2-0",)
    # New assertion is current.
    cur = store.get_assertion("user:dog:name")
    assert cur is not None
    assert cur.value == "user's dog is named Rex"
    assert cur.status == "current"
    assert cur.source_memory_ids == ("ep-day-8-0",)
    # Nothing deleted: both records exist.
    assert len(store.list_assertions(status=None)) == 2


def test_negation_supersedes_stale_fact_across_keys():
    """M-1b gate fix (A9 Gate 3): "I don't have Luna anymore" must kill the
    stale "user has a cat named Luna" assertion even though the keys differ
    (user:luna vs user:cat) — the L4 projection must not surface stale truth."""
    store = FakeStore()
    agent = MemoryAgent(store)

    # Day 2: positive fact WITH the name kept inside the value.
    run_day(
        store, 2,
        [("user", "I have a cat named Luna."), ("assistant", "Aww, cute!")],
        judgement=0.6, agent=agent,
    )
    cat = store.get_assertion("user:cat")
    assert cat is not None and cat.status == "current"
    assert "luna" in cat.value.lower()

    # Day 5: name-based negation — different key, no noun.
    run_day(
        store, 5,
        [("user", "I don't have Luna anymore."),
         ("assistant", "Oh, I'm sorry to hear that.")],
        judgement=0.7, agent=agent,
    )
    # stale cat claim superseded (cross-key via subject match)
    assert store.get_assertion("user:cat") is None
    superseded = [a for a in store.list_assertions("superseded")
                  if a.key == "user:cat"]
    assert len(superseded) == 1
    assert "luna" in superseded[0].value.lower()
    # the negation itself is current and provenanced
    neg = store.get_assertion("user:luna")
    assert neg is not None
    assert "no longer has" in neg.value
    assert neg.source_memory_ids == ("ep-day-5-0",)
    # L4 projection no longer contains the stale cat claim
    model = store.load_user_model()
    assert "cat" not in (model.identity or "").lower()
    assert "no longer" in (model.identity or "").lower()


def test_l4_assertions_traceable_to_episodes_to_turns():
    store = FakeStore()
    agent = MemoryAgent(store)
    run_day(
        store, 2,
        [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
        judgement=0.6, agent=agent,
    )
    run_day(
        store, 8,
        [("user", "Actually, my dog's name is Rex now."), ("assistant", "Got it.")],
        judgement=0.5, agent=agent,
    )
    messages_by_id = {m["id"]: m for m in store.messages_for_day(2) + store.messages_for_day(8)}
    for a in store.list_assertions(status=None):
        assert a.source_memory_ids, "assertion without episode provenance"
        for ep_id in a.source_memory_ids:
            ep = store.get_episode(ep_id)
            assert ep is not None, f"assertion -> missing episode {ep_id}"
            assert ep.source_turn_ids, "episode without turn provenance"
            for tid in ep.source_turn_ids:
                assert tid in messages_by_id, f"episode -> missing turn {tid}"
                if ep.verbatim_anchors:
                    assert messages_by_id[tid]["content"] in ep.verbatim_anchors


def test_no_assertion_without_promoted_episode_source():
    store = FakeStore()
    agent = MemoryAgent(store)
    # A mild preference day that does NOT reach the promotion threshold:
    # the fact must not become L4 truth without a source episode.
    summary = run_day(store, 6, [("user", "I like jazz.")], agent=agent)
    assert agent.promote(summary) == []
    assert agent.update_user_model(summary) == []
    assert store.list_assertions(status=None) == []


# ---------------------------------------------------------------------------
# Affect, separation, provenance, temporal anchors
# ---------------------------------------------------------------------------


def test_affect_is_metadata_on_memories_no_emotional_db():
    store = FakeStore()
    agent = MemoryAgent(store)
    summary = run_day(
        store, 2,
        [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
        judgement=0.6, agent=agent,
    )
    # Affect lives on the summary and on the episode — nothing else.
    assert summary.affect_observations
    assert all(isinstance(o, AffectMetadata) for o in summary.affect_observations)
    ep = store.list_episodes()[0]
    assert ep.affect is not None and isinstance(ep.affect, AffectMetadata)
    assert ep.affect.emotional_peak is False
    # No second "emotional memory DB": the store exposes no emotion store.
    assert not hasattr(store, "emotions")
    assert not any("emotion" in name for name in dir(store))


def test_user_affect_vs_companion_state_no_cross_contamination():
    ua_fields = {f.name for f in fields(UserAffectObservation)}
    cb_fields = {f.name for f in fields(CompanionBehaviorState)}
    assert ua_fields.isdisjoint(cb_fields)  # no shared fields, ever
    # memory.py emits affect metadata, never companion behavior state, and
    # never converts between the two.
    source = inspect.getsource(memory)
    assert "CompanionBehaviorState" not in source
    assert "UserAffectObservation" not in source


def test_provenance_required_no_record_without_source_ids():
    store = FakeStore()
    agent = MemoryAgent(store)
    run_day(
        store, 2,
        [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
        judgement=0.6, agent=agent,
    )
    summary = store.load_session_summary("day-2")
    assert summary is not None
    bare = replace(summary, source_turn_ids=())
    with pytest.raises(ValueError):
        agent.promote(bare)
    with pytest.raises(ValueError):
        agent.update_user_model(bare)
    # record_turn always yields a provenanced row.
    agent.record_turn("user", "Hello.", 25.0, "day-1")
    row = store.recent_messages(1)[0]
    assert row["id"] > 0 and row["content"] == "Hello."


def test_temporal_anchors_survive_summarization():
    store = FakeStore()
    agent = MemoryAgent(store)
    run_day(
        store, 2,
        [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
        judgement=0.6, agent=agent,
    )
    summary = store.load_session_summary("day-2")
    assert summary is not None
    ep = store.list_episodes()[0]
    assert summary.started_at_t_h == pytest.approx(2 * 24 + 9.0)
    assert summary.ended_at_t_h == pytest.approx(2 * 24 + 23.0)
    assert ep.occurred_at_t_h == pytest.approx(2 * 24 + 9.0)   # the turn's t_h
    assert ep.created_at_t_h == pytest.approx(2 * 24 + 23.0)   # session end


def test_episodes_link_back_to_exact_turns():
    store = FakeStore()
    agent = MemoryAgent(store)
    run_day(
        store, 2,
        [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
        judgement=0.6, agent=agent,
    )
    for ep in store.list_episodes():
        for tid in ep.source_turn_ids:
            msg = next(m for m in store.messages_for_day(2) if m["id"] == tid)
            assert msg["content"] in ep.verbatim_anchors
    assert store.list_episodes()[0].verbatim_anchors == ("My dog's name is Bruno.",)


# ---------------------------------------------------------------------------
# Retrieval: reranker, budgets, baselines, L1
# ---------------------------------------------------------------------------


def test_retrieval_uses_035_030_035_formula():
    assert (SEM_WEIGHT, STRENGTH_WEIGHT, IMPORTANCE_WEIGHT) == (0.35, 0.30, 0.35)
    store = FakeStore()
    # Near-orthogonal embeddings: sem(q,j) ~ 0 for both candidates, so the
    # 0.30*strength + 0.35*importance terms decide (importance dominates).
    vocab = VocabEmbedder(["x", "y", "z"])
    store.insert_episode(make_episode("ep-a", "episode about x", importance=0.3, tags=("x",)))
    store.insert_episode(make_episode("ep-b", "episode about y", importance=0.9, tags=("y",)))
    store.save_embedding("ep-a", vocab("xxx"))
    store.save_embedding("ep-b", vocab("yyy"))
    agent = MemoryAgent(store, embedder=vocab)
    ctx = agent.retrieve("zzz", context={"t_h": 100.0})

    assert ctx.episodes and ctx.episodes[0].id == "ep-b"  # importance dominates
    # Literal formula check on the module function.
    qv = vocab("zzz")
    ep = store.get_episode("ep-b")
    assert ep is not None
    strength = memory.episodic_strength(ep, 100.0)
    expected = (
        0.35 * memory._cosine(qv, vocab("yyy"))
        + 0.30 * strength
        + 0.35 * ep.importance
    )
    assert memory.score_memory(qv, vocab("yyy"), ep, strength) == pytest.approx(expected)
    assert memory._cosine(qv, vocab("yyy")) == pytest.approx(0.0, abs=1e-9)


def test_irrelevant_high_salience_does_not_beat_relevant_low_salience():
    store = FakeStore()
    vocab = VocabEmbedder(["dog", "pottery", "bruno", "name", "my", "class"])
    store.insert_episode(
        make_episode(
            "ep-a", "my dog's name is bruno", importance=0.3, tags=("dog", "bruno"),
            anchors=("My dog's name is Bruno.",), access_count=2,
        )
    )
    store.insert_episode(
        make_episode(
            "ep-b", "user started pottery class", importance=0.9, tags=("pottery", "class"),
            anchors=("I started pottery class.",),
        )
    )
    store.save_embedding("ep-a", vocab("my dog's name is bruno"))
    store.save_embedding("ep-b", vocab("user started pottery class"))
    agent = MemoryAgent(store, embedder=vocab)
    ctx = agent.retrieve("what is my dog's name?", context={"t_h": 100.0})

    assert ctx.episodes[0].id == "ep-a"                      # relevant wins
    assert ctx.episodes[0].importance < ctx.episodes[1].importance  # despite salience
    assert ctx.evidence_anchors[0] == "My dog's name is Bruno."


def test_retrieval_touches_accessed_episodes():
    store = FakeStore()
    vocab = VocabEmbedder(["dog"])
    store.insert_episode(make_episode("ep-a", "user's dog", importance=0.8, tags=("dog",)))
    store.save_embedding("ep-a", vocab("dog dog dog"))
    agent = MemoryAgent(store, embedder=vocab)
    ctx = agent.retrieve("dog", context={"t_h": 150.0})
    assert ctx.episodes and ctx.episodes[0].access_count == 0  # snapshot before touch
    ep = store.get_episode("ep-a")
    assert ep is not None
    assert ep.access_count == 1 and ep.last_accessed_t_h == pytest.approx(150.0)
    # Strength rises with access: a second retrieval ranks it higher.
    before = memory.episodic_strength(ep, 150.0)
    agent.retrieve("dog", context={"t_h": 150.0})
    again = store.get_episode("ep-a")
    assert again is not None
    after = memory.episodic_strength(again, 150.0)
    assert after > before


def test_memory_context_hard_budget():
    store = FakeStore()
    agent = MemoryAgent(store)
    long_text = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor " * 10).strip()
    assert len(long_text) >= 600
    turns = [("user", long_text), ("assistant", "ok")] * 8
    run_day(store, 1, turns, agent=agent)
    ctx = agent.retrieve("anything", context={"t_h": 2 * 24 + 12.0})
    assert len(ctx.recent_turns) <= 12
    assert _context_chars(ctx) <= MAX_CONTEXT_CHARS


def test_baselines_raw_history_and_simple_retrieval():
    store = FakeStore()
    agent = MemoryAgent(store)
    run_day(store, 2, [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
            judgement=0.6, agent=agent)
    for i in range(3):
        agent.record_turn("user", f"small talk {i}", 3 * 24 + i, "day-3")

    # RAW_HISTORY: the recent transcript only.
    hist = raw_history(store)
    assert isinstance(hist, tuple) and all(isinstance(t, Turn) for t in hist)
    assert hist[-1].text == "small talk 2"

    # SIMPLE_RAG: sem-only ranking ignores importance.
    vocab = VocabEmbedder(["dog", "pottery"])
    eps = [
        make_episode("ep-a", "user's dog", importance=0.3, tags=("dog",)),
        make_episode("ep-b", "pottery class", importance=0.9, tags=("pottery",)),
    ]
    emb = {"ep-a": vocab("dog dog dog"), "ep-b": vocab("pottery pottery")}
    top = simple_retrieval("my dog", eps, emb, embedder=vocab)
    assert top and top[0].id == "ep-a"

    # STRUCTURED: the full MemoryAgent pipeline (covered by every other test).


def test_record_turn_persists_exact_text_l1():
    store = FakeStore()
    agent = MemoryAgent(store)
    agent.record_turn("user", "Hello there, exactly this.", 25.0, "day-1")
    agent.record_turn("assistant", "Hi!", 25.1, "day-1")
    rows = store.messages_for_day(1)
    assert rows[0]["content"] == "Hello there, exactly this."  # verbatim, never summarized
    assert rows[0]["role"] == "user" and rows[0]["t_h"] == pytest.approx(25.0)
    assert rows[0]["session_id"] == "day-1"
    assert rows[1]["content"] == "Hi!"


def test_summarizer_is_deterministic_and_injectable():
    store = FakeStore()
    calls: list[str] = []

    def spy_summarizer(session_id, messages, judgement, started, ended):
        calls.append(session_id)
        return deterministic_summarizer(session_id, messages, judgement, started, ended)

    agent = MemoryAgent(store, summarizer=spy_summarizer)
    run_day(store, 2, [("user", "My dog's name is Bruno.")], judgement=0.6, agent=agent)
    assert calls == ["day-2"]
    # Identical inputs -> identical summaries (no RNG anywhere).
    s1 = store.load_session_summary("day-2")
    assert s1 is not None
    s2 = agent.close_session("day-2", ended_at_t_h=2 * 24 + 23.0)
    assert s1 == s2
    assert s1.importance == pytest.approx(0.5 + 0.15 * 0.51 + 0.10 * (1 / 12))


def test_invalid_session_id_rejected():
    store = FakeStore()
    agent = MemoryAgent(store)
    with pytest.raises(ValueError):
        agent.record_turn("user", "hi", 1.0, "not-a-day-session")
