"""ZifaMem-style L1/L2/L3/L4 memory pipeline for the companion harness (A4).

Tiers
-----
* L1 — recent turns: exact text persisted through the store's message table
  (``add_message``). Never summarized at write time.
* L2 — session summaries: one structured ``SessionSummary`` per completed
  session (a session is one calendar day, ``session_id="day-<N>"``). The
  default extractor is fully deterministic (regex fact extraction, judge
  score sign/magnitude for affect) and injectable; the research-quality
  path is the LLM-backed ``SemanticSummaryExtractor`` (see
  ``harness.summarization``).
* L3 — episodic memories: explicit promotion of important sessions
  (``PromotionPolicy(importance_threshold=0.5, promote_emotional_peaks=True)``).
  Every episode carries exact verbatim anchors, turn ids, and affect metadata.
* L4 — consolidated user model: assertions ``{key, value, confidence,
  updated_at, source_memory_ids, status}``. Compatible evidence strengthens the
  current assertion; contradictory evidence supersedes it (provenance kept).
  Each assertion carries its CANONICAL ``UserModelCategory`` (plan §5-A4
  Task 1): facts are categorized from the enum in ``harness.summarization``
  and the category is passed to the store explicitly — never inferred from
  keys or store conventions.

Memory policy (plan §5-A4 Task 2, invariants 11/12)
----------------------------------------------------
``MemoryAgent`` takes and respects a ``MemoryPolicy``:

* ``STRUCTURED_MEMORY`` (default) — the research-faithful condition: the
  retrieval score is EXACTLY ``0.35*sem + 0.30*strength + 0.35*importance``.
  No topicality boost is applied, ever.
* ``STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT`` — the separately named
  experimental variant (``MemoryPolicy.is_experimental`` is True): the same
  formula PLUS the documented topicality boost (``TOPICALITY_BOOST * sem``
  for episodes with a semantic match).
* ``RAW_CONTEXT`` — honest baseline: as much raw dialogue as the context
  budget permits (not merely the latest 12 turns).
* ``VERBATIM_RAG`` — honest baseline: raw conversation chunks retrieved by
  semantic similarity, using the SAME embedder instance as the structured
  path (invariant 13). No L3 summaries masquerade as raw RAG.

Embeddings and summarization interfaces (plan §5-A4 Tasks 3/4)
--------------------------------------------------------------
``harness.embeddings``: ``DeterministicHashEmbedder`` (tests / deterministic
CI) and ``RealSemanticEmbedder`` (real eval/live condition), one callable
contract, no vector DB — brute-force cosine over stored vectors.
``harness.summarization``: ``DeterministicSummaryExtractor`` (heuristic
TESTING path, never presented as the research-quality path) and
``SemanticSummaryExtractor`` (LLM-backed research-quality path).

Invariants
----------
* No provenance -> no truth: nothing is promoted and no assertion is created
  without source turn ids; verbatim anchors are exact excerpts from turns.
* Affect is metadata ON memories — there is no separate emotional store.
* The retrieval reranker's BASE score is exactly ``0.35*sem + 0.30*strength
  + 0.35*importance`` (``score_memory``); the topicality boost applies ONLY
  under ``MemoryPolicy.STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT``. Strength
  is recalculated at retrieval from age, access count, importance and stored
  affect metadata. No standalone emotional-intensity weight.
* All stochastic-free by construction: the default embedder is a deterministic
  seeded hash embedder; the default summarizer consumes no RNG. No real-clock
  reads anywhere — every timestamp is passed in as ``t_h``.

Store contract (duck-typed, documented in the plan §15 store seam)
------------------------------------------------------------------
``add_message(role, content, t_h, day, *, session_id=None) -> int`` (the
session kwarg is optional; detected by signature), ``messages_for_day`` /
``messages_for_session``, ``recent_messages(limit)``, ``load_judgement(day)``,
``save_session_summary`` / ``load_session_summary``, ``insert_episode`` /
``get_episode`` / ``list_episodes`` / ``touch_episode``, ``save_embedding`` /
``load_embeddings``, ``upsert_assertion(assertion, *, category=None)``
(canonical L4 category kwarg; detected by signature) /
``list_assertions`` / ``get_assertion`` / ``get_assertion_category``,
``supersede_assertion``, ``load_user_model`` (buckets current assertions by
their STORED canonical category; keys are never parsed for semantics).
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, replace
from typing import Callable

from harness.domain import (
    AffectMetadata,
    EpisodicMemory,
    MemoryContext,
    MemoryKind,
    MemoryPolicy,
    SessionSummary,
    Turn,
    UserModel,
    UserModelAssertion,
    UserModelCategory,
)
from harness.embeddings import (
    DeterministicHashEmbedder,
    Embedder,
    RealSemanticEmbedder,
    cosine as _cosine,
)
from harness.summarization import (
    DeterministicSummaryExtractor,
    SemanticSummaryExtractor,
    Summarizer,
    _NEGATION_VALUE_RE,
    _STOP,
    _TOKENS_RE,
    _affect_observation,
    _callbacks,
    _clean,
    _extract_facts,
    deterministic_summarizer,
)

# Backward-compatible re-exports (tests and callers import these names from
# harness.memory; the implementations now live in their own modules).
__all__ = [
    "MemoryAgent",
    "PromotionPolicy",
    "deterministic_hash_embedder",
    "deterministic_summarizer",
    "score_memory",
    "episodic_strength",
    "raw_history",
    "simple_retrieval",
    "Embedder",
    "DeterministicHashEmbedder",
    "RealSemanticEmbedder",
    "Summarizer",
    "DeterministicSummaryExtractor",
    "SemanticSummaryExtractor",
    "SEM_WEIGHT",
    "STRENGTH_WEIGHT",
    "IMPORTANCE_WEIGHT",
    "TOPICALITY_BOOST",
    "MAX_CONTEXT_CHARS",
]


def deterministic_hash_embedder(text: str, *, dim: int = 64, seed: int = 0) -> list[float]:
    """Deterministic seeded hash embedder mapping text to a unit vector.

    Function form of ``DeterministicHashEmbedder`` (kept for backward
    compatibility; the class is the canonical interface).
    """
    return DeterministicHashEmbedder(dim=dim, seed=seed)(text)


# ---------------------------------------------------------------------------
# Weights and budgets (documented; tests assert against these constants)
# ---------------------------------------------------------------------------

SEM_WEIGHT = 0.35
STRENGTH_WEIGHT = 0.30
IMPORTANCE_WEIGHT = 0.35
"""Retrieval reranker formula: score(q,j) = 0.35*sem + 0.30*strength
+ 0.35*importance (``score_memory`` — the research-faithful formula,
verbatim). ``MemoryAgent.retrieve`` under ``STRUCTURED_MEMORY`` applies
NOTHING on top of this."""

TOPICALITY_BOOST = 0.9
"""Topicality boost — applied ONLY under the separately named experimental
policy ``MemoryPolicy.STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT`` (invariant
12): ``retrieve`` ranking adds ``TOPICALITY_BOOST * sem`` for episodes with
a semantic match (sem > 0), so a weak-but-topical memory (the ONLY topical
match) is not crowded out of the top-N by irrelevant high-salience memories.
Under the research-faithful ``STRUCTURED_MEMORY`` condition the boost is
NEVER applied (invariant 11). Calibrated against the deterministic hash
embedder: the strongest irrelevant distractor in the M-3 adversarial
scenario (sem ~ 0.39) still loses to the relevant pottery memory
(sem ~ 0.73) with 0.9 > 0.808."""

MAX_CONTEXT_CHARS = 8000
"""Hard budget on the total text payload of a returned ``MemoryContext``."""

L1_SLICE_LIMIT = 12
"""L1 slice size — matches the assembler's 12-turn recent-transcript horizon."""

L2_CONTEXT_BOUND = 3
"""Maximum number of session summaries attached to one retrieval."""

ANCHOR_CHAR_BUDGET = 1200
"""Maximum total characters of verbatim evidence anchors in one retrieval."""

EPISODE_LIST_LIMIT = 500
"""Upper bound for the episode scan per retrieval (brute-force, no vector DB)."""

RAW_CONTEXT_WINDOW = 1000
"""RAW_CONTEXT baseline window: as much raw dialogue as the store returns;
the 8000-char context budget then decides what fits (not merely 12 turns)."""

VERBATIM_RAG_WINDOW = 400
"""VERBATIM_RAG baseline window: raw turns scanned for semantic similarity
(bounded scan; no vector DB)."""

# Strength recomputation at retrieval time.
STRENGTH_IMPORTANCE_W = 0.40
STRENGTH_RECENCY_W = 0.30
STRENGTH_ACCESS_W = 0.20
STRENGTH_AFFECT_W = 0.10
STRENGTH_HALF_LIFE_H = 336.0  # 14 days

# L4 consolidation.
ASSERTION_INITIAL_CONFIDENCE = 0.6
ASSERTION_CONTRADICTION_CONFIDENCE = 0.7
ASSERTION_STRENGTHEN_STEP = 0.15
ASSERTION_MAX_CONFIDENCE = 0.99


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# L3/L4 — episodic strength, reranker, promotion policy
# ---------------------------------------------------------------------------


def episodic_strength(episode: EpisodicMemory, now_t_h: float) -> float:
    """Episodic strength, recalculated at retrieval time.

    Combines importance, recency of use (exponential decay with a 14-day
    half-life over age since last access/creation), access count, and stored
    affect metadata. Affect enters ONLY here — there is no separate
    emotional-intensity weight in the reranker (double counting avoided).
    """
    anchor = (
        episode.last_accessed_t_h
        if episode.last_accessed_t_h is not None
        else episode.created_at_t_h
    )
    age_h = max(0.0, float(now_t_h) - float(anchor))
    recency = 0.5 ** (age_h / STRENGTH_HALF_LIFE_H)
    access = min(1.0, episode.access_count / 5.0)
    affect = 0.4
    if episode.affect is not None:
        affect = _clip(
            0.5 * episode.affect.intensity
            + 0.5 * (1.0 if episode.affect.emotional_peak else 0.0)
        )
    return _clip(
        STRENGTH_IMPORTANCE_W * episode.importance
        + STRENGTH_RECENCY_W * recency
        + STRENGTH_ACCESS_W * access
        + STRENGTH_AFFECT_W * affect
    )


def score_memory(
    query_vec: list[float],
    episode_vec: list[float],
    episode: EpisodicMemory,
    strength: float,
) -> float:
    """The 0.35/0.30/0.35 reranker, verbatim:

    ``score(q,j) = 0.35*sem(q,j) + 0.30*strength_j + 0.35*importance_j``
    """
    return (
        SEM_WEIGHT * _cosine(query_vec, episode_vec)
        + STRENGTH_WEIGHT * strength
        + IMPORTANCE_WEIGHT * episode.importance
    )


@dataclass(frozen=True)
class PromotionPolicy:
    """L2 -> L3 promotion gate (configurable, evaluated defaults)."""

    importance_threshold: float = 0.5
    promote_emotional_peaks: bool = True


# ---------------------------------------------------------------------------
# Baselines (Memory Gate item 10 — the policy baselines)
# ---------------------------------------------------------------------------


def raw_history(store, *, limit: int = L1_SLICE_LIMIT) -> tuple[Turn, ...]:
    """RAW_HISTORY baseline: the recent transcript, nothing else.

    Identical to the L1 slice the assembler already consumes. The
    policy-based ``RAW_CONTEXT`` condition in ``MemoryAgent.retrieve`` is
    the budget-filling variant of this baseline.
    """
    rows = store.recent_messages(limit=limit)
    return tuple(Turn(role=r["role"], text=r["content"], t_h=float(r["t_h"])) for r in rows)


def simple_retrieval(
    query: str,
    episodes: list[EpisodicMemory],
    embeddings: dict[str, list[float]],
    *,
    embedder: Callable[[str], list[float]] | None = None,
    limit: int = 8,
) -> list[EpisodicMemory]:
    """SIMPLE_RAG baseline: semantic relevance only (cosine over embeddings).

    Ignores strength and importance entirely — the contrast to the full
    0.35/0.30/0.35 reranker used by ``MemoryAgent.retrieve`` under
    ``STRUCTURED_MEMORY``. (The policy-based ``VERBATIM_RAG`` condition
    ranks RAW TURNS with the same embedder instead of L3 episodes.)
    """
    embed = embedder or deterministic_hash_embedder
    qv = embed(query)
    scored = [
        (_cosine(qv, embeddings.get(ep.id, [0.0] * len(qv))), ep) for ep in episodes
    ]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [ep for _, ep in scored[:limit]]


# ---------------------------------------------------------------------------
# MemoryAgent
# ---------------------------------------------------------------------------


class MemoryAgent:
    """ZifaMem-style L1/L2/L3/L4 pipeline over the store seam.

    Seam-exact public API (plan §15): ``record_turn``, ``close_session``,
    ``promote``, ``update_user_model``, ``retrieve``.

    ``memory_policy`` selects the conditioning condition (plan §5-A4 Task 2):
    ``STRUCTURED_MEMORY`` (default, research-faithful), the experimental
    topicality variant, or one of the honest baselines (``RAW_CONTEXT`` /
    ``VERBATIM_RAG``). Baselines use the SAME injectable embedder as the
    structured path — a policy change never swaps the semantic backend.
    """

    def __init__(
        self,
        store,
        *,
        embedder: Embedder | None = None,
        policy: PromotionPolicy | None = None,
        rng=None,
        summarizer: Summarizer | None = None,
        memory_policy: MemoryPolicy = MemoryPolicy.STRUCTURED_MEMORY,
    ) -> None:
        self.store = store
        self._embed: Embedder = embedder or DeterministicHashEmbedder()
        self._policy = policy if policy is not None else PromotionPolicy()
        self._summarizer: Summarizer = summarizer or DeterministicSummaryExtractor()
        self._rng = rng  # reserved for future stochastic extensions; A4 is deterministic
        self.memory_policy = memory_policy
        try:
            self._add_message_accepts_session = (
                "session_id" in inspect.signature(store.add_message).parameters
            )
        except (TypeError, AttributeError):
            self._add_message_accepts_session = False
        # A9 M-1b provenance leg: stores whose supersede_assertion accepts
        # provenance kwargs let the negation evidence be persisted on the
        # superseded row; minimal stores fall back to a bare status flip.
        try:
            self._supersede_accepts_provenance = (
                "source_memory_ids"
                in inspect.signature(store.supersede_assertion).parameters
            )
        except (TypeError, AttributeError):
            self._supersede_accepts_provenance = False
        # Canonical L4 category kwarg (plan §5-A4 Task 1, invariant 10): the
        # store persists the UserModelCategory directly on the assertion row.
        # Stores without the kwarg fall back to their legacy key-prefix
        # bucketing (detected once at construction, never re-derived).
        try:
            self._upsert_accepts_category = (
                "category" in inspect.signature(store.upsert_assertion).parameters
            )
        except (TypeError, AttributeError):
            self._upsert_accepts_category = False

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _day_of(session_id: str) -> int:
        m = re.fullmatch(r"day-(\d+)", session_id)
        if not m:
            raise ValueError(f"session_id must be 'day-<N>', got {session_id!r}")
        return int(m.group(1))

    def _session_messages(self, session_id: str) -> list[dict]:
        if hasattr(self.store, "messages_for_session"):
            msgs = self.store.messages_for_session(session_id)
            if msgs:
                return msgs
        return self.store.messages_for_day(self._day_of(session_id))

    def _session_score(self, session_id: str) -> float | None:
        try:
            j = self.store.load_judgement(self._day_of(session_id))
        except (AttributeError, ValueError):
            return None
        if j is None:
            return None
        raw = j.get("score")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _affect_for_episode(
        self, turn_id: int, messages: list[dict], score: float | None
    ) -> AffectMetadata | None:
        for m in messages:
            if int(m.get("id", -1)) == turn_id:
                return _affect_observation(m, score)
        return None

    def _latest_t_h(self) -> float:
        best = 0.0
        for r in self.store.recent_messages(limit=1):
            best = max(best, float(r["t_h"]))
        for ep in self.store.list_episodes(limit=EPISODE_LIST_LIMIT):
            best = max(best, ep.created_at_t_h)
        for a in self.store.list_assertions():
            best = max(best, a.updated_at_t_h)
        return best

    # -- L1 -----------------------------------------------------------------

    def record_turn(self, role: str, text: str, t_h: float, session_id: str) -> None:
        """Persist one turn verbatim as L1 (exact text, never summarized now).

        Uses the store's message table; the session id is passed through when
        the store supports the ``session_id`` kwarg (detected once at
        construction), otherwise L1 rows are linked by day.
        """
        day = self._day_of(session_id)
        if self._add_message_accepts_session:
            self.store.add_message(role, text, t_h, day, session_id=session_id)
        else:
            self.store.add_message(role, text, t_h, day)

    # -- L2 -----------------------------------------------------------------

    def close_session(self, session_id: str, *, ended_at_t_h: float) -> SessionSummary:
        """Close a session: build + persist the structured L2 summary.

        The default extractor is deterministic (no LLM required); the
        research-quality ``SemanticSummaryExtractor`` may be injected. The
        summary is persisted through the store seam before being returned.
        """
        messages = self._session_messages(session_id)
        judgement = None
        try:
            judgement = self.store.load_judgement(self._day_of(session_id))
        except (AttributeError, ValueError):
            judgement = None
        started = min((float(m["t_h"]) for m in messages), default=float(self._day_of(session_id) * 24.0))
        summary = self._summarizer(session_id, messages, judgement, started, ended_at_t_h)
        self.store.save_session_summary(summary)
        return summary

    # -- L2 -> L3 -----------------------------------------------------------

    def _policy_should_promote(self, summary: SessionSummary) -> bool:
        if summary.importance >= self._policy.importance_threshold:
            return True
        return self._policy.promote_emotional_peaks and summary.emotional_peak

    @staticmethod
    def _tags_for_text(text: str, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        tags = [t for t in _TOKENS_RE.findall(text.lower()) if len(t) >= 3 and t not in _STOP]
        for t in extra:
            if t not in tags:
                tags.append(t)
        return tuple(tags)

    @staticmethod
    def _episode_text(episode: EpisodicMemory) -> str:
        return " ".join(
            [episode.summary, " ".join(episode.tags), " ".join(episode.verbatim_anchors)]
        )

    def promote(self, summary: SessionSummary) -> list[EpisodicMemory]:
        """Promote a completed session to L3 episodes, per the policy.

        Promotion = importance >= threshold OR emotional peak. Every episode
        carries exact verbatim anchors, source turn ids, affect metadata and
        a deterministic id (``ep-<session>-<n>``), so calling promote twice
        is idempotent (already-stored ids are not re-inserted).

        Refuses to promote unprovenanced content: a summary without source
        turn ids raises ``ValueError``.
        """
        if not summary.source_turn_ids:
            raise ValueError("refusing to promote unprovenanced summary (no source turns)")
        if not self._policy_should_promote(summary):
            return []

        messages = self._session_messages(summary.session_id)
        score = self._session_score(summary.session_id)
        facts = _extract_facts(messages)
        cbs = _callbacks(messages)
        by_id = {int(m.get("id", -1)): m for m in messages}

        episodes: list[EpisodicMemory] = []
        idx = 0

        def make_episode(
            text: str,
            category: MemoryKind,
            turn_ids: tuple[int, ...],
            anchors: tuple[str, ...],
            affect: AffectMetadata | None,
            occurred: float,
            extra_tags: tuple[str, ...] = (),
        ) -> EpisodicMemory:
            nonlocal idx
            ep = EpisodicMemory(
                id=f"ep-{summary.session_id}-{idx}",
                summary=text,
                category=category,
                occurred_at_t_h=float(occurred),
                created_at_t_h=summary.ended_at_t_h,
                importance=summary.importance,
                access_count=0,
                last_accessed_t_h=None,
                affect=affect,
                source_session_id=summary.session_id,
                source_turn_ids=tuple(turn_ids),
                verbatim_anchors=tuple(anchors),
                tags=self._tags_for_text(text, extra_tags),
            )
            idx += 1
            return ep

        for f in facts:
            category = {
                "user_fact": MemoryKind.USER_FACT,
                "preference": MemoryKind.USER_PREFERENCE,
                "relationship": MemoryKind.RELATIONSHIP_EVENT,
            }[f.kind]
            msg = by_id.get(f.turn_id)
            occurred = float(msg["t_h"]) if msg else summary.ended_at_t_h
            affect = self._affect_for_episode(f.turn_id, messages, score)
            episodes.append(
                make_episode(
                    f.value, category, (f.turn_id,), (f.anchor,), affect, occurred,
                    extra_tags=(f.kind,),
                )
            )
        for excerpt, tid, full in cbs:
            msg = by_id.get(tid)
            occurred = float(msg["t_h"]) if msg else summary.ended_at_t_h
            affect = self._affect_for_episode(tid, messages, score)
            episodes.append(
                make_episode(
                    f"callback: {excerpt}",
                    MemoryKind.CALLBACK,
                    (tid,),
                    (excerpt,),
                    affect,
                    occurred,
                    extra_tags=("callback",),
                )
            )

        # Fallbacks for summaries promoted without extractable facts: use the
        # summary's own fields (never fabricate anchors — they stay empty).
        if not episodes:
            for fact_str in summary.user_facts:
                episodes.append(
                    make_episode(
                        fact_str,
                        MemoryKind.USER_FACT,
                        summary.source_turn_ids,
                        (),
                        None,
                        summary.ended_at_t_h,
                        extra_tags=("user_fact",),
                    )
                )
        if not episodes and summary.emotional_peak:
            peak_text = (
                "user shared an emotionally intense moment"
            )
            episodes.append(
                make_episode(
                    peak_text,
                    MemoryKind.SHARED_EPISODE,
                    summary.source_turn_ids,
                    (),
                    next((o for o in summary.affect_observations if o.emotional_peak), None),
                    summary.ended_at_t_h,
                    extra_tags=("emotional_peak",),
                )
            )

        episodes = episodes[:6]
        for ep in episodes:
            if self.store.get_episode(ep.id) is None:
                self.store.insert_episode(ep)
                self.store.save_embedding(ep.id, self._embed(self._episode_text(ep)))
        return episodes

    # -- L4 -----------------------------------------------------------------

    def update_user_model(self, summary: SessionSummary) -> list[UserModelAssertion]:
        """Consolidate the session's facts into the L4 user model.

        * Compatible evidence (same key, same normalized value) STRENGTHENS
          the current assertion: confidence up, source ids appended — no
          duplicate facts.
        * Contradictory evidence (same key, different value) SUPERSEDES: the
          store flips the old current assertion to "superseded" (provenance
          kept) and the new one becomes "current".
        * Every assertion carries its canonical ``UserModelCategory``
          (``_Fact.category``) and is persisted with it explicitly — the
          store never infers categories from keys.
        * Nothing is ever deleted.
        * Assertions are created ONLY from promoted sessions — no source
          episodes, no assertion (no summarization hallucination becomes L4
          truth without source evidence).
        """
        if not summary.source_turn_ids:
            raise ValueError("refusing to update user model from unprovenanced summary")
        episodes = self.promote(summary)  # idempotent
        ep_by_turn: dict[int, str] = {}
        for ep in episodes:
            for tid in ep.source_turn_ids:
                ep_by_turn.setdefault(tid, ep.id)
        messages = self._session_messages(summary.session_id)
        facts = _extract_facts(messages)

        updated: list[UserModelAssertion] = []
        for f in facts:
            sources = tuple(
                ep_by_turn[tid] for tid in (f.turn_id,) if tid in ep_by_turn
            )
            if not sources:
                continue  # provenance required
            existing = self.store.get_assertion(f.key)
            if existing is None:
                assertion = UserModelAssertion(
                    key=f.key,
                    value=f.value,
                    confidence=ASSERTION_INITIAL_CONFIDENCE,
                    updated_at_t_h=summary.ended_at_t_h,
                    source_memory_ids=sources,
                    status="current",
                )
            elif _clean(existing.value).lower() == _clean(f.value).lower():
                assertion = UserModelAssertion(
                    key=f.key,
                    value=existing.value,
                    confidence=min(
                        ASSERTION_MAX_CONFIDENCE,
                        existing.confidence + ASSERTION_STRENGTHEN_STEP,
                    ),
                    updated_at_t_h=summary.ended_at_t_h,
                    source_memory_ids=existing.source_memory_ids + sources,
                    status="current",
                )
            else:
                assertion = UserModelAssertion(
                    key=f.key,
                    value=f.value,
                    confidence=ASSERTION_CONTRADICTION_CONFIDENCE,
                    updated_at_t_h=summary.ended_at_t_h,
                    source_memory_ids=sources,
                    status="current",
                )
            if self._upsert_accepts_category:
                self.store.upsert_assertion(assertion, category=f.category)
            else:
                self.store.upsert_assertion(assertion)
            updated.append(assertion)

        # M-1b gate fix (A9 Gate 3): negation facts supersede EVERY current
        # assertion whose value mentions the negated subject — a name-based
        # contradiction ("I don't have Luna anymore") kills "user has a cat
        # named Luna" even though the keys differ ("user:luna" vs "user:cat").
        for f in facts:
            m = _NEGATION_VALUE_RE.match(f.value)
            if not m:
                continue
            subject = m.group(1)
            extra = tuple(
                ep_by_turn[tid] for tid in (f.turn_id,) if tid in ep_by_turn
            )
            for a in self.store.list_assertions(status="current"):
                if a.key == f.key:
                    continue  # same-key supersede already handled by upsert
                if re.search(rf"\b{re.escape(subject)}\b", a.value, re.IGNORECASE):
                    superseded = replace(
                        a,
                        status="superseded",
                        updated_at_t_h=summary.ended_at_t_h,
                        source_memory_ids=a.source_memory_ids + extra,
                    )
                    if self._supersede_accepts_provenance:
                        # Persist the merged provenance (original episodes +
                        # negation episode) so the superseded row keeps BOTH
                        # sources after a restart — not just the return value.
                        self.store.supersede_assertion(
                            a.key,
                            source_memory_ids=superseded.source_memory_ids,
                            updated_at_t_h=superseded.updated_at_t_h,
                        )
                    else:
                        self.store.supersede_assertion(a.key)
                    updated.append(superseded)
        return updated

    # -- retrieval ----------------------------------------------------------

    def retrieve(
        self,
        query: str,
        *,
        context: dict | None = None,
        limit: int = 8,
    ) -> MemoryContext:
        """Retrieve a bounded, budgeted memory context for ``query``.

        The condition is selected by ``self.memory_policy`` (plan §5-A4
        Task 2):

        * ``STRUCTURED_MEMORY`` — L1 slice + bounded L2 + top L3 episodes +
          L4 projection + verbatim anchors, ranked by the research-faithful
          formula ``score(q,j) = 0.35*sem + 0.30*strength + 0.35*importance``
          (``score_memory``) with strength recalculated at retrieval time.
          NO topicality boost, ever.
        * ``STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT`` — the same formula PLUS
          ``TOPICALITY_BOOST * sem`` for episodes with a semantic match
          (``is_experimental`` condition).
        * ``RAW_CONTEXT`` — as much raw dialogue as the context budget
          permits (not merely the latest 12 turns); no L2/L3/L4.
        * ``VERBATIM_RAG`` — raw conversation chunks ranked by semantic
          similarity with the SAME embedder instance used by the structured
          path; no L3 summaries masquerade as raw RAG.

        ``context`` may carry ``{"t_h": now}``; without it the latest stored
        timestamp is used. Total payload is hard-capped at
        ``MAX_CONTEXT_CHARS``.
        """
        ctx = context or {}
        now = ctx.get("t_h")
        if now is None:
            now = self._latest_t_h()
        policy = self.memory_policy
        if policy is MemoryPolicy.RAW_CONTEXT:
            return self._retrieve_raw_context()
        if policy is MemoryPolicy.VERBATIM_RAG:
            return self._retrieve_verbatim_rag(query, limit=limit)
        topicality = policy is MemoryPolicy.STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT
        return self._retrieve_structured(query, now, limit=limit, topicality=topicality)

    def _retrieve_raw_context(self) -> MemoryContext:
        """RAW_CONTEXT baseline: raw dialogue up to the hard budget."""
        rows = self.store.recent_messages(limit=RAW_CONTEXT_WINDOW)
        turns = tuple(
            Turn(role=r["role"], text=r["content"], t_h=float(r["t_h"])) for r in rows
        )
        return _trim_to_budget(
            MemoryContext(
                recent_turns=turns,
                session_context=(),
                episodes=(),
                user_model=None,
                evidence_anchors=(),
            )
        )

    def _retrieve_verbatim_rag(self, query: str, *, limit: int = 8) -> MemoryContext:
        """VERBATIM_RAG baseline: raw turns ranked by semantic similarity.

        Uses ``self._embed`` — the SAME semantic backend as the structured
        path (invariant 13). Episodes/L2/L4 are not consulted: this is an
        honest raw-RAG baseline, not a summary retrieval in disguise.
        """
        qv = self._embed(query)
        scored: list[tuple[float, dict]] = []
        for r in self.store.recent_messages(limit=VERBATIM_RAG_WINDOW):
            sem = _cosine(qv, self._embed(str(r["content"])))
            scored.append((sem, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[:limit]
        turns = tuple(
            Turn(role=r["role"], text=r["content"], t_h=float(r["t_h"]))
            for _, r in top
        )
        anchors = tuple(str(r["content"]) for _, r in top)
        return _trim_to_budget(
            MemoryContext(
                recent_turns=turns,
                session_context=(),
                episodes=(),
                user_model=None,
                evidence_anchors=anchors,
            )
        )

    def _retrieve_structured(
        self,
        query: str,
        now: float,
        *,
        limit: int = 8,
        topicality: bool = False,
    ) -> MemoryContext:
        """Structured L1/L2/L3/L4 retrieval with the faithful reranker.

        ``topicality=False`` (``STRUCTURED_MEMORY``) applies the formula
        EXACTLY; ``topicality=True`` (the separately named experiment) adds
        ``TOPICALITY_BOOST * sem`` for episodes with a semantic match.
        """
        qv = self._embed(query)
        embeddings = dict(self.store.load_embeddings())

        scored: list[tuple[float, EpisodicMemory]] = []
        for ep in self.store.list_episodes(limit=EPISODE_LIST_LIMIT):
            strength = episodic_strength(ep, now)
            vec = embeddings.get(ep.id, [])
            sem = _cosine(qv, vec)
            score = score_memory(qv, vec, ep, strength)
            if topicality and sem > 0.0:
                score += TOPICALITY_BOOST * sem
            scored.append((score, ep))
        scored.sort(key=lambda t: t[0], reverse=True)
        top = [ep for _, ep in scored[:limit]]
        for ep in top:
            self.store.touch_episode(ep.id, now)

        recent = self.store.recent_messages(limit=L1_SLICE_LIMIT)
        turns = tuple(
            Turn(role=r["role"], text=r["content"], t_h=float(r["t_h"])) for r in recent
        )

        summaries: list[SessionSummary] = []
        seen: set[str] = set()
        for ep in top:
            sid = ep.source_session_id
            if sid not in seen:
                seen.add(sid)
                s = self.store.load_session_summary(sid)
                if s is not None:
                    summaries.append(s)

        user_model: UserModel | None = self.store.load_user_model()

        anchors: list[str] = []
        anchor_chars = 0
        for ep in top:
            for a in ep.verbatim_anchors:
                if anchor_chars + len(a) > ANCHOR_CHAR_BUDGET:
                    continue
                anchors.append(a)
                anchor_chars += len(a)

        result = MemoryContext(
            recent_turns=turns,
            session_context=tuple(summaries[:L2_CONTEXT_BOUND]),
            episodes=tuple(top),
            user_model=user_model,
            evidence_anchors=tuple(anchors),
        )
        return _trim_to_budget(result)


def _context_chars(ctx: MemoryContext) -> int:
    total = 0
    for t in ctx.recent_turns:
        total += len(t.text)
    for s in ctx.session_context:
        total += len(s.summary)
        total += sum(len(x) for x in s.topics)
        total += sum(len(x) for x in s.user_facts)
    for e in ctx.episodes:
        total += len(e.summary) + sum(len(a) for a in e.verbatim_anchors)
    if ctx.user_model is not None:
        total += len(ctx.user_model.identity)
        for bucket in (
            ctx.user_model.stable_preferences,
            ctx.user_model.current_preferences,
            ctx.user_model.boundaries,
            ctx.user_model.vulnerabilities,
            ctx.user_model.recurring_interests,
            ctx.user_model.relationship_patterns,
            ctx.user_model.important_entities,
        ):
            for a in bucket:
                total += len(a.key) + len(a.value)
    for a in ctx.evidence_anchors:
        total += len(a)
    return total


def _trim_to_budget(ctx: MemoryContext) -> MemoryContext:
    """Deterministically trim lower-priority content until under budget.

    Order: drop oldest L1 turns, then lowest-ranked episodes, then L2
    summaries, then anchors. Text is never truncated — exact evidence is
    dropped whole rather than mangled.
    """
    while _context_chars(ctx) > MAX_CONTEXT_CHARS:
        if ctx.recent_turns:
            ctx = replace(ctx, recent_turns=ctx.recent_turns[1:])
        elif ctx.episodes:
            ctx = replace(ctx, episodes=ctx.episodes[:-1])
        elif ctx.session_context:
            ctx = replace(ctx, session_context=ctx.session_context[:-1])
        elif ctx.evidence_anchors:
            ctx = replace(ctx, evidence_anchors=ctx.evidence_anchors[:-1])
        else:
            break
    return ctx
