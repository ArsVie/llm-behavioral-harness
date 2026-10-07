"""L1/L2/L3/L4 memory pipeline for the companion harness, over the store seam.

L1 = recent turns (never summarized at write time), L2 = session summaries,
L3 = episodic memories with verbatim anchors, L4 = consolidated user model.
No real-clock reads: every timestamp is passed in as ``t_h``.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, replace

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
)
from harness.embeddings import (
    DeterministicHashEmbedder,
    Embedder,
    cosine as _cosine,
)
from harness.summarization import (
    Summarizer,
    _STOP,
    _TOKENS_RE,
    _affect_observation,
    deterministic_summarizer,
)
from harness.summarization_facts import (
    _NEGATION_VALUE_RE,
    _callbacks,
    _clean,
    _extract_facts,
)

#: kv key for the memory fork's summary of a day, in her words.
DAY_SUMMARY_KV = "memory.day_summary.{day}"

__all__ = [
    "MemoryAgent",
    "PromotionPolicy",
    "deterministic_hash_embedder",
    "deterministic_summarizer",
    "score_memory",
    "episodic_strength",
    "Embedder",
    "DeterministicHashEmbedder",
    "Summarizer",
    "SEM_WEIGHT",
    "STRENGTH_WEIGHT",
    "IMPORTANCE_WEIGHT",
    "TOPICALITY_BOOST",
    "MAX_CONTEXT_CHARS",
]


def deterministic_hash_embedder(text: str, *, dim: int = 64, seed: int = 0) -> list[float]:
    """Deterministic seeded hash embedder mapping text to a unit vector.

    Function form of ``DeterministicHashEmbedder``, the canonical interface.
    """
    return DeterministicHashEmbedder(dim=dim, seed=seed)(text)


def recall_embedder(text: str, *, dim: int = 1024, seed: int = 0) -> list[float]:
    """The live memory lane's embedder: character unigrams and bigrams with
    signed feature hashing (SHA-256, stable across processes) in 1024 dims.

    Stored embeddings were written with exactly this function; changing it
    (dims, features, hash) silently breaks retrieval over an existing store.
    """
    vec = [0.0] * dim
    feats: list[str] = []
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        feats.append("w:" + tok)
        if len(tok) >= 3:
            for i in range(len(tok) - 1):
                feats.append("b:" + tok[i:i + 2])
    for f in feats:
        d = hashlib.sha256(f"{seed}:{f}".encode("utf-8")).digest()
        idx = int.from_bytes(d[:4], "little") % dim
        sign = 1.0 if d[4] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# Weights and budgets

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


def episodic_strength(episode: EpisodicMemory, now_t_h: float) -> float:
    """Episodic strength, recalculated at retrieval time.

    Combines importance, recency of use (exponential decay with a 14-day
    half-life over age since last access/creation), access count and stored
    affect metadata. Affect enters ONLY here — it carries no separate weight
    in the reranker.
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
# MemoryAgent


#: Fact kind -> the episode category it is stored under.
_FACT_MEMORY_KIND = {
    "user_fact": MemoryKind.USER_FACT,
    "preference": MemoryKind.USER_PREFERENCE,
    "relationship": MemoryKind.RELATIONSHIP_EVENT,
}

#: An extracted fact shorter than this reads as a keyword blob, not memory;
#: the extractor's fallback path drops it.
MIN_FACT_WORDS = 6


def _states_a_fact(text: str) -> bool:
    """True when ``text`` reads as a stated fact rather than a keyword."""
    return len(str(text).split()) >= MIN_FACT_WORDS


class MemoryAgent:
    """L1/L2/L3/L4 memory pipeline over the store seam.

    Public API: ``record_turn``, ``close_session``, ``promote``,
    ``update_user_model``, ``retrieve``.

    ``memory_policy`` selects the conditioning condition — ``STRUCTURED_MEMORY``
    (default), the experimental topicality variant, or one of the baselines
    (``RAW_CONTEXT`` / ``VERBATIM_RAG``). Baselines use the SAME injectable
    embedder as the structured path; a policy change never swaps the semantic
    backend.
    """

    def __init__(
        self,
        store,
        *,
        embedder: Embedder | None = None,
        policy: PromotionPolicy | None = None,
        summarizer: Summarizer | None = None,
        memory_policy: MemoryPolicy = MemoryPolicy.STRUCTURED_MEMORY,
    ) -> None:
        self.store = store
        self._embed: Embedder = embedder or DeterministicHashEmbedder()
        self._policy = policy if policy is not None else PromotionPolicy()
        self._summarizer: Summarizer = summarizer or deterministic_summarizer
        self.memory_policy = memory_policy

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _day_of(session_id: str) -> int:
        m = re.fullmatch(r"day-(\d+)", session_id)
        if not m:
            raise ValueError(f"session_id must be 'day-<N>', got {session_id!r}")
        return int(m.group(1))

    def _session_messages(self, session_id: str) -> list[dict]:
        return (self.store.messages_for_session(session_id)
                or self.store.messages_for_day(self._day_of(session_id)))

    def _session_score(self, session_id: str) -> float | None:
        try:
            return self.store.load_judgement_score(self._day_of(session_id))
        except (AttributeError, ValueError):
            return None

    def _save_episode(self, ep: EpisodicMemory) -> None:
        """Insert ``ep`` with its embedding unless it is already stored."""
        if self.store.get_episode(ep.id) is None:
            self.store.insert_episode(ep)
            self.store.save_embedding(ep.id, self._embed(self._episode_text(ep)))

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
        """Persist one turn verbatim as L1 (never summarized at write time)."""
        self.store.add_message(role, text, t_h, self._day_of(session_id),
                               session_id=session_id)

    # -- L2 -----------------------------------------------------------------

    def close_session(self, session_id: str, *, ended_at_t_h: float) -> SessionSummary:
        """Close a session: build + persist the structured L2 summary.

        The default extractor is deterministic; another ``Summarizer`` may be
        injected. Persisted through the store seam before being returned.
        """
        day = self._day_of(session_id)
        messages = self._session_messages(session_id)
        judgement = self.store.load_judgement(day)
        started = min((float(m["t_h"]) for m in messages), default=float(day * 24.0))
        summary = self._summarizer(session_id, messages, judgement, started, ended_at_t_h)
        if messages and hasattr(self.store, "get_kv"):
            told = self.store.get_kv(DAY_SUMMARY_KV.format(day=messages[0].get("day")))
            if told:  # the memory fork already told this day in her words
                summary = replace(summary, summary=told)
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

        Promotion = importance >= threshold OR emotional peak. Episodes carry
        exact verbatim anchors, source turn ids, affect metadata and
        deterministic ids (``ep-<session>-<n>``), so promoting twice is
        idempotent. A summary without source turn ids raises ``ValueError``.
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
            msg = by_id.get(f.turn_id)
            occurred = float(msg["t_h"]) if msg else summary.ended_at_t_h
            affect = self._affect_for_episode(f.turn_id, messages, score)
            episodes.append(
                make_episode(
                    f.value, _FACT_MEMORY_KIND[f.kind], (f.turn_id,), (f.anchor,),
                    affect, occurred, extra_tags=(f.kind,),
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

        if not episodes:
            for fact_str in summary.user_facts:
                if not _states_a_fact(fact_str):
                    continue
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
            episodes.append(
                make_episode(
                    "user shared an emotionally intense moment",
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
            self._save_episode(ep)
        return episodes

    # -- L4 -----------------------------------------------------------------

    def update_user_model(self, summary: SessionSummary) -> list[UserModelAssertion]:
        """Consolidate the session's facts into the L4 user model.

        Compatible evidence (same key, same normalized value) STRENGTHENS the
        current assertion — confidence up, source ids appended, no duplicate
        facts; contradictory evidence (same key, different value) SUPERSEDES
        it: the store flips the old row to "superseded" (provenance kept) and
        the new one becomes "current". Every assertion is persisted with its
        canonical ``UserModelCategory`` — the store never infers categories
        from keys. Nothing is ever deleted, and assertions are created ONLY
        from promoted sessions, so no unprovenanced claim becomes L4 truth.
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
            assertion = self._merged_assertion(
                f, self.store.get_assertion(f.key), sources,
                summary.ended_at_t_h,
            )
            self.store.upsert_assertion(assertion, category=f.category)
            updated.append(assertion)
        updated.extend(
            self._supersede_negated(facts, ep_by_turn, summary.ended_at_t_h)
        )
        return updated

    def consolidate(self, remembered, *, day: int, messages: list[dict],
                    ended_at_t_h: float) -> list[UserModelAssertion]:
        """Write what the end-of-day memory fork kept (``memory_fork.parse``):
        one episode per item, anchored on his quoted words and their turn,
        then the user-model assertions those episodes source.

        Episode ids are ``ep-day<d>-mem-<n>``, so a day consolidated twice
        (a restart replaying finalize) writes nothing new.
        """
        by_id = {int(m.get("id", -1)): m for m in messages}
        score = None
        try:
            score = self.store.load_judgement_score(day)
        except AttributeError:
            pass
        items = [(f.value, _FACT_MEMORY_KIND[f.kind], f.turn_id, f.anchor,
                  (f.kind,)) for f in remembered.facts]
        items += [(f"callback: {sentence or excerpt}", MemoryKind.CALLBACK, tid,
                   excerpt, ("callback",))
                  for excerpt, tid, sentence in remembered.callbacks]
        items += [(sentence, MemoryKind.SHARED_EPISODE, tid, quote, ("moment",))
                  for sentence, tid, quote in remembered.moments]
        ep_by_turn: dict[int, str] = {}
        for n, (text, category, tid, anchor, extra) in enumerate(items):
            msg = by_id.get(tid, {})
            ep = EpisodicMemory(
                id=f"ep-day{day}-mem-{n}",
                summary=text,
                category=category,
                occurred_at_t_h=float(msg.get("t_h", ended_at_t_h)),
                created_at_t_h=ended_at_t_h,
                importance=float(remembered.weights.get(tid, 0.5)),
                access_count=0,
                last_accessed_t_h=None,
                affect=self._affect_for_episode(tid, messages, score),
                source_session_id=str(msg.get("session_id") or f"day-{day}"),
                source_turn_ids=(tid,),
                verbatim_anchors=(anchor,),
                tags=self._tags_for_text(text, extra),
            )
            ep_by_turn.setdefault(tid, ep.id)
            self._save_episode(ep)
        if remembered.summary:
            self._tell_day(day, remembered.summary, messages)
        updated: list[UserModelAssertion] = []
        for f in remembered.facts:
            existing = self.store.get_assertion(f.key)
            source = ep_by_turn[f.turn_id]
            if existing is not None and source in existing.source_memory_ids:
                continue  # this day already counted (replay)
            assertion = self._merged_assertion(f, existing, (source,), ended_at_t_h)
            self.store.upsert_assertion(assertion, category=f.category)
            updated.append(assertion)
        return updated

    def _tell_day(self, day: int, text: str, messages: list[dict]) -> None:
        """Her summary of the day replaces the session summaries' placeholder
        prose, now and when a session of that day closes later."""
        if hasattr(self.store, "set_kv"):
            self.store.set_kv(DAY_SUMMARY_KV.format(day=day), text)
        for sid in dict.fromkeys(str(m["session_id"]) for m in messages
                                 if m.get("session_id")):
            saved = self.store.load_session_summary(sid)
            if saved is not None:
                self.store.save_session_summary(replace(saved, summary=text))

    @staticmethod
    def _merged_assertion(fact, existing, sources: tuple,
                          ended_at_t_h: float) -> UserModelAssertion:
        """One fact merged against what the model already believes.

        Three cases, distinguished by the confidence: a NEW key starts at the
        initial confidence; COMPATIBLE evidence (same normalized value) keeps
        the stored wording, raises confidence and appends provenance;
        CONTRADICTORY evidence takes the new value at the contradiction
        confidence.
        """
        if existing is None:
            return UserModelAssertion(
                key=fact.key,
                value=fact.value,
                confidence=ASSERTION_INITIAL_CONFIDENCE,
                updated_at_t_h=ended_at_t_h,
                source_memory_ids=sources,
                status="current",
            )
        if _clean(existing.value).lower() == _clean(fact.value).lower():
            return UserModelAssertion(
                key=fact.key,
                value=existing.value,
                confidence=min(
                    ASSERTION_MAX_CONFIDENCE,
                    existing.confidence + ASSERTION_STRENGTHEN_STEP,
                ),
                updated_at_t_h=ended_at_t_h,
                source_memory_ids=existing.source_memory_ids + sources,
                status="current",
            )
        return UserModelAssertion(
            key=fact.key,
            value=fact.value,
            confidence=ASSERTION_CONTRADICTION_CONFIDENCE,
            updated_at_t_h=ended_at_t_h,
            source_memory_ids=sources,
            status="current",
        )

    def _supersede_negated(self, facts, ep_by_turn: dict,
                           ended_at_t_h: float) -> list[UserModelAssertion]:
        """Retire assertions a negation fact contradicts by subject.

        "I don't have a dog any more" must also retire "user's dog is named
        Rex" — a different key that merely MENTIONS the subject, which the
        same-key upsert can never reach. Nothing is deleted: the row flips to
        "superseded" with the negation's provenance merged in.
        """
        retired: list[UserModelAssertion] = []
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
                    continue  # same-key supersede handled by the upsert above
                if not re.search(rf"\b{re.escape(subject)}\b", a.value,
                                 re.IGNORECASE):
                    continue
                superseded = replace(
                    a,
                    status="superseded",
                    updated_at_t_h=ended_at_t_h,
                    source_memory_ids=a.source_memory_ids + extra,
                )
                self.store.supersede_assertion(
                    a.key,
                    source_memory_ids=superseded.source_memory_ids,
                    updated_at_t_h=superseded.updated_at_t_h,
                )
                retired.append(superseded)
        return retired

    # -- retrieval ----------------------------------------------------------

    def retrieve(
        self,
        query: str,
        *,
        context: dict | None = None,
        limit: int = 8,
    ) -> MemoryContext:
        """Retrieve a bounded, budgeted memory context for ``query``.

        The condition is selected by ``self.memory_policy``:

        * ``STRUCTURED_MEMORY`` — L1 slice + bounded L2 + top L3 episodes +
          L4 projection + verbatim anchors, ranked by ``score(q,j) =
          0.35*sem + 0.30*strength + 0.35*importance`` (``score_memory``) with
          strength recalculated at retrieval time. NO topicality boost, ever.
        * ``STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT`` — the same formula PLUS
          ``TOPICALITY_BOOST * sem`` for episodes with a semantic match.
        * ``RAW_CONTEXT`` — as much raw dialogue as the context budget permits
          (not merely the latest 12 turns); no L2/L3/L4.
        * ``VERBATIM_RAG`` — raw turns ranked by semantic similarity with the
          SAME embedder instance used by the structured path.

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
        turns = tuple(_turn(r) for r in rows)
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
        path. Episodes/L2/L4 are not consulted.
        """
        qv = self._embed(query)
        scored: list[tuple[float, dict]] = []
        for r in self.store.recent_messages(limit=VERBATIM_RAG_WINDOW):
            sem = _cosine(qv, self._embed(str(r["content"])))
            scored.append((sem, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[:limit]
        turns = tuple(_turn(r) for _, r in top)
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
        EXACTLY; ``topicality=True`` (the experiment) adds
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
        turns = tuple(_turn(r) for r in recent)

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


def _turn(row: dict) -> Turn:
    return Turn(role=row["role"], text=row["content"], t_h=float(row["t_h"]))


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
    dropped whole.
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
