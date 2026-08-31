"""Memory pipeline tests — A4 (L1/L2/L3/L4, ZifaMem-style, Iteration 2).

Covers the Memory Gate (§13 of plans/companion-vertical-slice-2026-08.md)
and the Iteration-2 A4 tasks (plan §5-A4): canonical L4 category
consumption (T1), the research-faithful 0.35/0.30/0.35 reranker with the
topicality boost confined to the separately named experiment (T2), the
embedder interfaces (T3), the summarization interfaces (T4), the
L4->L3->L2->raw-turns provenance chain (T5) and the metal preference
revision across 78 days (T6).

``FakeStore`` below is a seam-faithful in-memory mirror of the A7
Iteration-2 store contract: ``upsert_assertion`` accepts the canonical
``category`` kwarg and stores it on the row; ``load_user_model`` buckets
current assertions by their STORED category (keys are never parsed);
``get_assertion`` returns the most recent row of a key regardless of status
(full history). The same scenarios run against the real ``SQLiteStore`` in
the ``*_sqlite`` tests.
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
    MemoryPolicy,
    SessionSummary,
    Turn,
    UserAffectObservation,
    UserModel,
    UserModelAssertion,
    UserModelCategory,
)
from harness.embeddings import (
    DeterministicHashEmbedder,
    RealSemanticEmbedder,
)
from harness.memory import (
    IMPORTANCE_WEIGHT,
    SEM_WEIGHT,
    STRENGTH_WEIGHT,
    MAX_CONTEXT_CHARS,
    MemoryAgent,
    PromotionPolicy,
    deterministic_hash_embedder,
    deterministic_summarizer,
    raw_history,
    simple_retrieval,
)
from harness.store import SQLiteStore
from harness.summarization import (
    DeterministicSummaryExtractor,
    SemanticSummaryExtractor,
)

# Seam-faithful in-memory store

_LEGACY_PREFIX_CATEGORIES = [
    ("identity", UserModelCategory.IDENTITY),
    ("stable_preferences", UserModelCategory.STABLE_PREFERENCE),
    ("current_preferences", UserModelCategory.CURRENT_PREFERENCE),
    ("preference", UserModelCategory.CURRENT_PREFERENCE),
    ("boundaries", UserModelCategory.BOUNDARY),
    ("boundary", UserModelCategory.BOUNDARY),
    ("vulnerabilities", UserModelCategory.VULNERABILITY),
    ("vulnerability", UserModelCategory.VULNERABILITY),
    ("recurring_interests", UserModelCategory.RECURRING_INTEREST),
    ("interest", UserModelCategory.RECURRING_INTEREST),
    ("relationship_patterns", UserModelCategory.RELATIONSHIP_PATTERN),
    ("relationship", UserModelCategory.RELATIONSHIP_PATTERN),
    ("important_entities", UserModelCategory.IMPORTANT_ENTITY),
    ("entity", UserModelCategory.IMPORTANT_ENTITY),
]

_CATEGORY_TO_GROUP = {
    UserModelCategory.STABLE_PREFERENCE: "stable_preferences",
    UserModelCategory.CURRENT_PREFERENCE: "current_preferences",
    UserModelCategory.BOUNDARY: "boundaries",
    UserModelCategory.VULNERABILITY: "vulnerabilities",
    UserModelCategory.RECURRING_INTEREST: "recurring_interests",
    UserModelCategory.RELATIONSHIP_PATTERN: "relationship_patterns",
    UserModelCategory.IMPORTANT_ENTITY: "important_entities",
}


def _legacy_category_from_key(key: str) -> UserModelCategory:
    """Mirror of the A7 store's write-time derivation for legacy keys."""
    head, _, _ = key.partition(":")
    if key == "identity" or head == "identity":
        return UserModelCategory.IDENTITY
    for prefix, cat in _LEGACY_PREFIX_CATEGORIES:
        if head == prefix:
            return cat
    return UserModelCategory.IMPORTANT_ENTITY


class FakeStore:
    """In-memory mirror of the A7 §15 store contract (canonical L4 categories).

    Matches SQLiteStore's Iteration-2 semantics: ``upsert_assertion``
    accepts the canonical ``category`` kwarg and stores it on the row;
    ``load_user_model`` buckets current assertions by their STORED category
    (never by parsing keys); ``get_assertion`` returns the most recent row
    of a key regardless of status (full history, pitfall 38).
    """

    def __init__(self) -> None:
        self._messages: list[dict] = []
        self._next_id = 1
        self._judgements: dict[int, dict] = {}
        self._summaries: dict[str, SessionSummary] = {}
        self._episodes: dict[str, EpisodicMemory] = {}
        self._embeddings: dict[str, list[float]] = {}
        self._assertions: list[UserModelAssertion] = []
        self._assertion_categories: list[UserModelCategory] = []

    # -- L1 (messages) ---
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
        # Mirrors the real store: newest rows, oldest -> newest.
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

    # -- L2 ---
    def save_session_summary(self, summary: SessionSummary) -> None:
        self._summaries[summary.session_id] = summary

    def load_session_summary(self, session_id: str) -> SessionSummary | None:
        return self._summaries.get(session_id)

    # -- L3 ---
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

    # -- L4 ---
    def upsert_assertion(self, a: UserModelAssertion, *, category=None) -> None:
        """Insert an assertion; a new ``current`` one supersedes (status
        flip, provenance kept) the previous ``current`` row of the same key.
        The canonical category is stored on the row (A7 contract)."""
        if category is None:
            cat = _legacy_category_from_key(a.key)
        elif isinstance(category, UserModelCategory):
            cat = category
        else:
            cat = UserModelCategory(str(category))  # canonical only; raises
        if a.status == "current":
            for i, old in enumerate(self._assertions):
                if old.key == a.key and old.status == "current":
                    self._assertions[i] = replace(old, status="superseded")
        self._assertions.append(a)
        self._assertion_categories.append(cat)

    def list_assertions(self, status: str | None = "current", category=None) -> list[UserModelAssertion]:
        out = []
        for a, cat in zip(self._assertions, self._assertion_categories):
            if status is not None and a.status != status:
                continue
            if category is not None:
                want = (
                    category.value
                    if isinstance(category, UserModelCategory)
                    else UserModelCategory(str(category)).value
                )
                if cat.value != want:
                    continue
            out.append(a)
        return out

    def supersede_assertion(self, key: str, *, source_memory_ids=None, updated_at_t_h=None) -> None:
        """Flip every current assertion of ``key`` to superseded; optional
        provenance/timestamp rewrite (A9 M-1b leg)."""
        for i, old in enumerate(self._assertions):
            if old.key == key and old.status == "current":
                kw: dict = {"status": "superseded"}
                if source_memory_ids is not None:
                    kw["source_memory_ids"] = tuple(source_memory_ids)
                if updated_at_t_h is not None:
                    kw["updated_at_t_h"] = float(updated_at_t_h)
                self._assertions[i] = replace(old, **kw)

    def get_assertion(self, key: str) -> UserModelAssertion | None:
        """Most recent assertion row for ``key`` (any status — full history)."""
        for a in reversed(self._assertions):
            if a.key == key:
                return a
        return None

    def get_assertion_category(self, key: str) -> UserModelCategory | None:
        """Canonical category of the most recent assertion row for ``key``."""
        for i in range(len(self._assertions) - 1, -1, -1):
            if self._assertions[i].key == key:
                return self._assertion_categories[i]
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
        for a, cat in zip(self._assertions, self._assertion_categories):
            if a.status != "current":
                continue
            if cat is UserModelCategory.IDENTITY:
                identity = a.value  # projection string, not an assertion tuple
            else:
                group = _CATEGORY_TO_GROUP[cat]
                buckets[group] = buckets[group] + (a,)
        return UserModel(identity=identity, **buckets)


# Helpers


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
        save_day_judgement(store, day, judgement)
    summary = agent.close_session(session_id, ended_at_t_h=day * 24.0 + 23.0)
    agent.promote(summary)
    agent.update_user_model(summary)
    return summary


def save_day_judgement(store, day: int, score: float) -> None:
    """Persist a synthetic judge score through either store contract."""
    try:
        store.save_judgement(day, score)
    except TypeError:
        # SQLiteStore's signature requires justification/model/shadow.
        store.save_judgement(day, score, "", "test", True)


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


# Bruno: 12-turn-horizon flagship test


def test_bruno_crosses_12_turn_horizon():
    store = FakeStore()
    agent = MemoryAgent(store)

    # Day 2: the user discloses the dog's name.
    run_day(
        store, 2,
        [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
        judgement=0.6, agent=agent,
    )
    assert any("Bruno" in e.summary for e in store.list_episodes())
    assert store.get_assertion("user:dog:name") is not None

    # Several days of small talk: >12 stored messages after the Bruno turn, none mentioning the dog.
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

    # Day 20: the query still surfaces dog=Bruno from L3/L4.
    ctx = agent.retrieve("what is my dog's name?", context={"t_h": 20 * 24 + 12.0})

    assert not any("Bruno" in t.text for t in ctx.recent_turns)          # not L1
    assert any("Bruno" in e.summary for e in ctx.episodes)               # L3
    assert any("Bruno" in a for a in ctx.evidence_anchors)               # exact evidence
    assert ctx.session_context and "Bruno" in ctx.session_context[0].summary  # L2
    assert ctx.user_model is not None
    # L4: the dog fact is consolidated under identity; the record carries the value.
    assert "Bruno" in ctx.user_model.identity
    dog_assertion = store.get_assertion("user:dog:name")
    assert dog_assertion is not None and "Bruno" in dog_assertion.value
    assert dog_assertion.status == "current"
    assert ctx.evidence_anchors[0] == "My dog's name is Bruno."  # verbatim


# Promotion policy (L2 -> L3)


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


# L4 consolidation and revision


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

    # Day 2: positive fact, name kept inside the value.
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
    # Stale cat claim superseded cross-key; the latest user:cat row is superseded.
    stale = store.get_assertion("user:cat")
    assert stale is not None and stale.status == "superseded"
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
    # Mild preference day below the promotion threshold.
    summary = run_day(store, 6, [("user", "I like jazz.")], agent=agent)
    assert agent.promote(summary) == []
    assert agent.update_user_model(summary) == []
    assert store.list_assertions(status=None) == []


# Affect, separation, provenance, temporal anchors


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
    # memory.py emits affect metadata, not companion behavior state.
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
    # record_turn yields a provenanced row.
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


# Retrieval: reranker, budgets, baselines, L1


def test_retrieval_uses_035_030_035_formula():
    assert (SEM_WEIGHT, STRENGTH_WEIGHT, IMPORTANCE_WEIGHT) == (0.35, 0.30, 0.35)
    store = FakeStore()
    # Near-orthogonal embeddings: sem ~ 0 for both, so strength+importance decide.
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

# STRUCTURED: the full MemoryAgent pipeline.


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


# T1: canonical L4 categories


def test_t1_canonical_categories_preference_relationship_identity():
    """Canonical enum consumption: preference -> CURRENT_PREFERENCE,
    relationship pattern -> RELATIONSHIP_PATTERN, identity -> IDENTITY —
    through the fake store's public projection."""
    store = FakeStore()
    agent = MemoryAgent(store, policy=PromotionPolicy(importance_threshold=0.3))
    run_day(store, 2, [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
            judgement=0.6, agent=agent)
    run_day(store, 3, [("user", "I love metal."), ("assistant", "Nice!")],
            judgement=0.5, agent=agent)
    run_day(store, 4, [("user", "Thank you for listening."), ("assistant", "Anytime!")],
            judgement=0.9, agent=agent)

    model = store.load_user_model()
    assert "Bruno" in model.identity                                    # identity -> IDENTITY
    assert any("metal" in a.value for a in model.current_preferences)   # preference -> CURRENT_PREFERENCE
    assert any("gratitude" in a.value for a in model.relationship_patterns)  # relationship -> RELATIONSHIP_PATTERN

    # The canonical category is stored on the assertion row, not inferred from keys.
    assert store.get_assertion_category("user:dog:name") is UserModelCategory.IDENTITY
    assert store.get_assertion_category("preference:like:metal") is UserModelCategory.CURRENT_PREFERENCE
    assert store.get_assertion_category("relationship:gratitude") is UserModelCategory.RELATIONSHIP_PATTERN

    # Category-filtered listing works through the public seam.
    prefs = store.list_assertions(status="current", category=UserModelCategory.CURRENT_PREFERENCE)
    assert [a.key for a in prefs] == ["preference:like:metal"]
    ids = store.list_assertions(status="current", category=UserModelCategory.IDENTITY)
    assert [a.key for a in ids] == ["user:dog:name"]

    # The extractor assigns canonical categories directly.
    facts = memory._extract_facts(store.messages_for_day(3))
    assert facts and facts[0].category is UserModelCategory.CURRENT_PREFERENCE
    facts2 = memory._extract_facts(store.messages_for_day(2))
    assert facts2 and facts2[0].category is UserModelCategory.IDENTITY
    facts3 = memory._extract_facts(store.messages_for_day(4))
    assert facts3 and facts3[0].category is UserModelCategory.RELATIONSHIP_PATTERN


def test_t1_canonical_categories_sqlite(tmp_path):
    """Same T1 scenario against SQLite-backed storage. Projection asserts
    run once the A7 canonical-category store is merged (the v2 store lacks
    the category column and buckets ``user:``/``preference:``/``relationship:``
    heads under important_entities, so the projection legs are
    capability-skipped; key-based and provenance asserts run on both)."""
    store = SQLiteStore(tmp_path / "t1.db")
    try:
        agent = MemoryAgent(store, policy=PromotionPolicy(importance_threshold=0.3))
        run_day(store, 2, [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
                judgement=0.6, agent=agent)
        run_day(store, 3, [("user", "I love metal."), ("assistant", "Nice!")],
                judgement=0.5, agent=agent)
        run_day(store, 4, [("user", "Thank you for listening."), ("assistant", "Anytime!")],
                judgement=0.9, agent=agent)

        # Key-based facts exist with the right values/status on either store.
        assert store.get_assertion("user:dog:name") is not None
        assert store.get_assertion("preference:like:metal") is not None
        assert store.get_assertion("relationship:gratitude") is not None
        if hasattr(store, "get_assertion_category"):
            # Canonical projection: each category maps to its enum.
            model = store.load_user_model()
            assert any("metal" in a.value for a in model.current_preferences)
            assert any("gratitude" in a.value for a in model.relationship_patterns)
            assert "Bruno" in model.identity
            assert store.get_assertion_category("user:dog:name") is UserModelCategory.IDENTITY
            assert store.get_assertion_category("preference:like:metal") is UserModelCategory.CURRENT_PREFERENCE
            assert store.get_assertion_category("relationship:gratitude") is UserModelCategory.RELATIONSHIP_PATTERN
            prefs = store.list_assertions(status="current", category=UserModelCategory.CURRENT_PREFERENCE)
            assert [a.key for a in prefs] == ["preference:like:metal"]
        else:
            pytest.skip("A7 canonical-category store not merged yet (no category column)")
    finally:
        store.close()


# T2: reranker + MemoryPolicy


def test_t2_memory_policy_respected_and_experimental_flag():
    assert MemoryPolicy.STRUCTURED_MEMORY.is_experimental is False
    assert MemoryPolicy.STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT.is_experimental is True
    assert MemoryPolicy.RAW_CONTEXT.is_experimental is False
    assert MemoryPolicy.VERBATIM_RAG.is_experimental is False

    store = FakeStore()
    assert MemoryAgent(store).memory_policy is MemoryPolicy.STRUCTURED_MEMORY
    agent = MemoryAgent(store, memory_policy=MemoryPolicy.VERBATIM_RAG)
    assert agent.memory_policy is MemoryPolicy.VERBATIM_RAG


def test_t2_structured_memory_formula_exact_no_hidden_topicality():
    """STRUCTURED_MEMORY applies EXACTLY 0.35*sem + 0.30*strength +
    0.35*importance — a relevant low-salience memory may be crowded out by
    an irrelevant high-salience one (the faithful condition's accepted
    property). The topicality boost lives ONLY in the separately named
    STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT variant, where the same scenario
    keeps the relevant memory."""
    store = FakeStore()
    vocab = VocabEmbedder(["dog", "pottery"])
    peak = AffectMetadata(0.5, 0.5, 0.5, 1.0, 0.0, 0.5, 0.5, 0.9, True)
    store.insert_episode(
        make_episode("ep-relevant", "user takes pottery class", importance=0.15,
                     tags=("pottery", "class"))
    )
    store.save_embedding("ep-relevant", vocab("pottery pottery"))
    store.insert_episode(
        make_episode("ep-distractor", "user's dog is very sick", importance=0.9,
                     tags=("dog",), affect=peak)
    )
    store.save_embedding("ep-distractor", vocab("dog dog dog"))

    # Expected ordering from the literal formula.
    qv = vocab("pottery")
    emb = dict(store.load_embeddings())
    scores = {}
    for ep in store.list_episodes():
        s = memory.episodic_strength(ep, 100.0)
        scores[ep.id] = memory.score_memory(qv, emb[ep.id], ep, s)
    assert scores["ep-distractor"] > scores["ep-relevant"]
    formula_order = sorted(scores, key=scores.get, reverse=True)

    faithful = MemoryAgent(store, embedder=vocab)
    ctx = faithful.retrieve("pottery", context={"t_h": 100.0})
    assert [e.id for e in ctx.episodes] == formula_order[:2]
    assert ctx.episodes[0].id == "ep-distractor"  # faithful: importance dominates

    boosted = MemoryAgent(
        store, embedder=vocab,
        memory_policy=MemoryPolicy.STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT,
    )
    ctx2 = boosted.retrieve("pottery", context={"t_h": 100.0})
    assert ctx2.episodes[0].id == "ep-relevant"  # experiment: topicality keeps it


# T3: embeddings interface


class RecordingEmbedder(VocabEmbedder):
    """VocabEmbedder that records every text it is asked to embed."""

    def __init__(self, vocab: list[str]) -> None:
        super().__init__(vocab)
        self.calls: list[str] = []

    def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        return super().__call__(text)


def test_t3_embedder_interfaces_deterministic_and_real():
    # DeterministicHashEmbedder: deterministic, unit-norm, e0 for empty input.
    emb = DeterministicHashEmbedder(dim=32, seed=7)
    assert emb("my dog's name is Bruno") == emb("my dog's name is Bruno")
    v = emb("my dog's name is Bruno")
    assert sum(x * x for x in v) == pytest.approx(1.0)
    assert emb("") == [1.0] + [0.0] * 31
    assert emb("Bruno") != v
    # Function form is byte-identical to the class form.
    assert deterministic_hash_embedder("x", dim=32, seed=7) == DeterministicHashEmbedder(dim=32, seed=7)("x")

    # RealSemanticEmbedder: injectable batch backend, no vector DB.
    calls: list[list[str]] = []

    def backend(texts):
        calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    real = RealSemanticEmbedder(backend)
    assert real("hello") == [1.0, 0.0]
    assert calls == [["hello"]]
    assert real.embed_many(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]
    assert calls[-1] == ["a", "b"]

    # Both implementations satisfy the callable interface.
    assert isinstance(emb, memory.Embedder)
    assert isinstance(real, memory.Embedder)


def test_t3_verbatim_rag_and_structured_share_same_semantic_backend():
    """Invariant 13: during comparison, VERBATIM_RAG and STRUCTURED_MEMORY
    use the SAME semantic backend — the injected embedder instance is shared
    by both policy paths; a policy change never swaps the embedder."""
    store = FakeStore()
    shared = RecordingEmbedder(["dog", "weather", "pottery"])
    agent_s = MemoryAgent(store, embedder=shared)  # STRUCTURED_MEMORY (default)
    run_day(store, 2, [("user", "My dog's name is Bruno."), ("assistant", "Nice to meet Bruno!")],
            judgement=0.6, agent=agent_s)
    for i in range(4):
        agent_s.record_turn("user", f"small talk {i} about the weather", 3 * 24 + i, "day-3")

    ctx_s = agent_s.retrieve("dog", context={"t_h": 100.0})
    assert ctx_s.episodes and "Bruno" in ctx_s.episodes[0].summary
    assert shared.calls, "structured path must use the injected embedder"

    agent_v = MemoryAgent(store, embedder=shared,
                          memory_policy=MemoryPolicy.VERBATIM_RAG)
    ctx_v = agent_v.retrieve("dog", context={"t_h": 100.0})
    assert agent_v._embed is shared and agent_s._embed is shared
    assert shared.calls, "verbatim RAG must call the SAME embedder instance"
    # Verbatim RAG ranks raw turns, not episodes.
    assert ctx_v.recent_turns and ctx_v.recent_turns[0].text == "My dog's name is Bruno."
    assert ctx_v.episodes == () and ctx_v.session_context == ()
    assert ctx_v.user_model is None
    assert ctx_v.evidence_anchors[0] == "My dog's name is Bruno."


def test_t3_raw_context_baseline_uses_budget_not_twelve():
    """RAW_CONTEXT: as much raw dialogue as the context budget permits —
    not merely the latest 12 turns."""
    store = FakeStore()
    agent = MemoryAgent(store, memory_policy=MemoryPolicy.RAW_CONTEXT)
    for i in range(20):
        store.add_message("user", f"message number {i} about the weather", i * 0.5, 0,
                          session_id="day-0")
    ctx = agent.retrieve("anything", context={"t_h": 100.0})
    assert len(ctx.recent_turns) > 12
    assert ctx.episodes == () and ctx.session_context == ()
    assert ctx.user_model is None
    assert memory._context_chars(ctx) <= MAX_CONTEXT_CHARS


# T4: summarization interface


def test_t4_summarizer_interfaces_deterministic_and_semantic():
    store = FakeStore()
    agent = MemoryAgent(store)
    # The default is the deterministic testing path; production uses SemanticSummaryExtractor.
    assert isinstance(agent._summarizer, DeterministicSummaryExtractor)

    messages = [
        {"id": 1, "role": "user", "content": "My dog's name is Bruno.", "t_h": 57.0},
        {"id": 2, "role": "assistant", "content": "Nice!", "t_h": 57.1},
    ]
    j = {"score": 0.6}
    det_class = DeterministicSummaryExtractor()("day-2", messages, j, 57.0, 71.0)
    det_fn = deterministic_summarizer("day-2", messages, j, 57.0, 71.0)
    assert det_class == det_fn

    # Semantic path: the LLM writes prose; structured fields stay factual.
    def fake_client(prompt: str) -> str:
        assert "My dog's name is Bruno." in prompt
        return "The user introduced their dog Bruno."

    sem = SemanticSummaryExtractor(fake_client)
    s = sem("day-2", messages, j, 57.0, 71.0)
    assert s.summary == "The user introduced their dog Bruno."
    assert s.source_turn_ids == (1, 2)                       # from messages, never the model
    assert s.user_facts == ("user's dog is named Bruno",)    # deterministic fields kept

    # A model output cannot inject source turns.
    s2 = SemanticSummaryExtractor(lambda p: "Some model text.")("day-2", messages, j, 57.0, 71.0)
    assert s2.source_turn_ids == (1, 2)
    # A broken client falls back to the deterministic summary.
    broken = SemanticSummaryExtractor(lambda p: (_ for _ in ()).throw(RuntimeError("offline")))
    assert broken("day-2", messages, j, 57.0, 71.0) == det_fn


def test_t4_semantic_summary_fact_never_authoritative_without_source_turns():
    """T5 provenance guard: a summary whose prose invents a fact (LLM
    hallucination) must never create an L4 assertion — assertions come only
    from facts re-extracted from raw source turns."""
    store = FakeStore()

    def hallucinating_client(prompt: str) -> str:
        return "The user owns a pet dragon."

    agent = MemoryAgent(
        store,
        policy=PromotionPolicy(importance_threshold=0.3),
        summarizer=SemanticSummaryExtractor(hallucinating_client),
    )
    run_day(store, 6, [("user", "I like jazz."), ("assistant", "Cool!")],
            judgement=0.5, agent=agent)
    # The session promoted, but only the real fact (jazz) reached L4.
    assert store.list_episodes()
    assert store.get_assertion("preference:like:jazz") is not None
    for a in store.list_assertions(status=None):
        assert "dragon" not in a.value
    assert store.get_assertion("user:dragon") is None


# T5: provenance chain L4 -> L3 -> L2 -> raw turns


def _walk_provenance_chain(store) -> None:
    """L4 assertion -> L3 episode -> L2 summary -> exact raw turns, for every
    assertion row (current AND superseded)."""
    messages_by_id = {
        m["id"]: m
        for m in store.messages_for_day(2) + store.messages_for_day(80)
    }
    summaries = {
        s.session_id: s
        for s in (store.load_session_summary("day-2"), store.load_session_summary("day-80"))
        if s is not None
    }
    assert summaries, "L2 summaries missing"
    rows = store.list_assertions(status="current") + store.list_assertions(status="superseded")
    assert rows, "no assertion rows to walk"
    for a in rows:
        assert a.source_memory_ids, "assertion without episode provenance"
        for ep_id in a.source_memory_ids:
            ep = store.get_episode(ep_id)
            assert ep is not None, f"L4 -> missing episode {ep_id}"
            assert ep.source_session_id in summaries, f"episode -> missing L2 {ep.source_session_id}"
            assert ep.source_turn_ids, "episode without turn provenance"
            for tid in ep.source_turn_ids:
                assert tid in messages_by_id, f"episode -> missing turn {tid}"
                assert messages_by_id[tid]["content"] in ep.verbatim_anchors


def test_t5_provenance_chain_full_walk():
    store = FakeStore()
    agent = MemoryAgent(store, policy=PromotionPolicy(importance_threshold=0.3))
    run_day(store, 2, [("user", "I love metal."), ("assistant", "Nice!")],
            judgement=0.5, agent=agent)
    run_day(store, 80, [("user", "I barely listen to metal anymore."), ("assistant", "Got it.")],
            judgement=0.5, agent=agent)
    _walk_provenance_chain(store)


def test_t5_provenance_chain_full_walk_sqlite(tmp_path):
    store = SQLiteStore(tmp_path / "provenance.db")
    try:
        agent = MemoryAgent(store, policy=PromotionPolicy(importance_threshold=0.3))
        run_day(store, 2, [("user", "I love metal."), ("assistant", "Nice!")],
                judgement=0.5, agent=agent)
        run_day(store, 80, [("user", "I barely listen to metal anymore."), ("assistant", "Got it.")],
                judgement=0.5, agent=agent)
        _walk_provenance_chain(store)
    finally:
        store.close()


# T6: preference revision (day 2 -> day 80)


def test_t6_preference_revision_metal_day2_day80():
    """Day 2 'I love metal.' -> day 80 'I barely listen to metal anymore.':
    L4 updates the CURRENT preference (same key supersedes in place) while
    the historical assertion and its provenance remain."""
    store = FakeStore()
    agent = MemoryAgent(store, policy=PromotionPolicy(importance_threshold=0.3))
    run_day(store, 2, [("user", "I love metal."), ("assistant", "Nice!")],
            judgement=0.5, agent=agent)
    run_day(store, 80, [("user", "I barely listen to metal anymore."), ("assistant", "Got it.")],
            judgement=0.5, agent=agent)

    # Current preference is the revision, same key, category unchanged.
    cur = store.get_assertion("preference:like:metal")
    assert cur is not None and cur.status == "current"
    assert cur.value == "user no longer likes metal"
    assert cur.source_memory_ids == ("ep-day-80-0",)
    assert store.get_assertion_category("preference:like:metal") is UserModelCategory.CURRENT_PREFERENCE

    # Historical preference remains: superseded, provenance intact.
    old = [a for a in store.list_assertions(status="superseded")
           if a.key == "preference:like:metal"]
    assert len(old) == 1
    assert old[0].value == "user likes metal"
    assert old[0].source_memory_ids == ("ep-day-2-0",)

    # The L4 projection shows only the current preference.
    prefs = [a.value for a in store.load_user_model().current_preferences]
    assert prefs == ["user no longer likes metal"]

    # Historical provenance remains walkable to the exact raw turns.
    old_ep = store.get_episode("ep-day-2-0")
    assert old_ep is not None and old_ep.verbatim_anchors == ("I love metal.",)
    new_ep = store.get_episode("ep-day-80-0")
    assert new_ep is not None and new_ep.verbatim_anchors == ("I barely listen to metal anymore.",)


def test_t6_preference_revision_metal_day2_day80_sqlite(tmp_path):
    store = SQLiteStore(tmp_path / "revision.db")
    try:
        agent = MemoryAgent(store, policy=PromotionPolicy(importance_threshold=0.3))
        run_day(store, 2, [("user", "I love metal."), ("assistant", "Nice!")],
                judgement=0.5, agent=agent)
        run_day(store, 80, [("user", "I barely listen to metal anymore."), ("assistant", "Got it.")],
                judgement=0.5, agent=agent)

        # Current preference is the revision; the old one is superseded.
        cur = store.get_assertion("preference:like:metal")
        assert cur is not None and cur.status == "current"
        assert cur.value == "user no longer likes metal"
        assert cur.source_memory_ids == ("ep-day-80-0",)
        old = [a for a in store.list_assertions(status="superseded")
               if a.key == "preference:like:metal"]
        assert len(old) == 1 and old[0].value == "user likes metal"
        assert old[0].source_memory_ids == ("ep-day-2-0",)
        _walk_provenance_chain(store)  # runs on both store generations
        if hasattr(store, "get_assertion_category"):
            prefs = [a.value for a in store.load_user_model().current_preferences]
            assert prefs == ["user no longer likes metal"]
            assert store.get_assertion_category("preference:like:metal") is UserModelCategory.CURRENT_PREFERENCE
        else:
            pytest.skip("A7 canonical-category store not merged yet (no category column)")
    finally:
        store.close()
