"""ZifaMem-style L1/L2/L3/L4 memory pipeline for the companion harness (A5).

Tiers
-----
* L1 — recent turns: exact text persisted through the store's message table
  (``add_message``). Never summarized at write time.
* L2 — session summaries: one deterministic structured ``SessionSummary`` per
  completed session (a session is one calendar day, ``session_id="day-<N>"``).
  The default summarizer is fully deterministic (regex fact extraction, judge
  score sign/magnitude for affect) and injectable.
* L3 — episodic memories: explicit promotion of important sessions
  (``PromotionPolicy(importance_threshold=0.5, promote_emotional_peaks=True)``).
  Every episode carries exact verbatim anchors, turn ids, and affect metadata.
* L4 — consolidated user model: assertions ``{key, value, confidence,
  updated_at, source_memory_ids, status}``. Compatible evidence strengthens the
  current assertion; contradictory evidence supersedes it (provenance kept).

Invariants
----------
* No provenance -> no truth: nothing is promoted and no assertion is created
  without source turn ids; verbatim anchors are exact excerpts from turns.
* Affect is metadata ON memories — there is no separate emotional store.
* Retrieval score is EXACTLY ``0.35*sem + 0.30*strength + 0.35*importance``;
  strength is recalculated at retrieval from age, access count, importance and
  stored affect metadata. No standalone emotional-intensity weight.
* All stochastic-free by construction: the default embedder is a deterministic
  seeded hash embedder; the default summarizer consumes no RNG. No real-clock
  reads anywhere — every timestamp is passed in as ``t_h``.
* Semantics without a vector DB: brute-force cosine over stored embedding
  vectors; the embedder is injectable (a local service would be wired only by
  experiments).

Store contract (duck-typed, documented in the plan §15 store seam)
------------------------------------------------------------------
``add_message(role, content, t_h, day, *, session_id=None) -> int`` (the
session kwarg is optional; detected by signature), ``messages_for_day`` /
``messages_for_session``, ``recent_messages(limit)``, ``load_judgement(day)``,
``save_session_summary`` / ``load_session_summary``, ``insert_episode`` /
``get_episode`` / ``list_episodes`` / ``touch_episode``, ``save_embedding`` /
``load_embeddings``, ``upsert_assertion`` (flips same-key current ->
superseded) / ``list_assertions`` / ``get_assertion``, ``load_user_model``.

``load_user_model`` buckets current assertions by key prefix (convention the
store layer follows): ``user:`` -> identity, ``preference:`` ->
current_preferences, ``boundary:`` -> boundaries, ``vulnerability:`` ->
vulnerabilities, ``interest:`` -> recurring_interests, ``relationship:`` ->
relationship_patterns, ``entity:`` -> important_entities.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import re
from dataclasses import dataclass, replace
from typing import Callable

from harness.domain import (
    AffectMetadata,
    EpisodicMemory,
    MemoryContext,
    MemoryKind,
    SessionSummary,
    Turn,
    UserModel,
    UserModelAssertion,
)

# ---------------------------------------------------------------------------
# Weights and budgets (documented; tests assert against these constants)
# ---------------------------------------------------------------------------

SEM_WEIGHT = 0.35
STRENGTH_WEIGHT = 0.30
IMPORTANCE_WEIGHT = 0.35
"""Retrieval reranker: score(q,j) = 0.35*sem + 0.30*strength + 0.35*importance."""

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

# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "the", "and", "you", "your", "that", "this", "with", "have", "was",
        "are", "for", "not", "but", "all", "can", "out", "get", "just",
        "like", "about", "really", "what", "when", "where", "how", "why",
        "there", "here", "from", "they", "them", "she", "him", "her", "will",
        "would", "could", "should", "into", "over", "than", "then", "very",
    }
)


def deterministic_hash_embedder(text: str, *, dim: int = 64, seed: int = 0) -> list[float]:
    """Deterministic seeded hash embedder mapping text to a unit vector.

    Feature hashing with signed accumulators over lower-cased alphanumeric
    tokens (SHA-256 based, so results are stable across processes — never
    Python's randomized ``hash()``). Empty text maps to ``e_0``. This is the
    default embedder for tests and offline runs; a real local service would
    be injected by experiments only.
    """
    vec = [0.0] * dim
    for tok in _TOKEN_RE.findall(text.lower()):
        digest = hashlib.sha256(f"{seed}:{tok}".encode("utf-8")).digest()
        idx = int.from_bytes(digest[:8], "little") % dim
        sign = 1.0 if digest[8] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dim = max(len(a), len(b))
    va = a + [0.0] * (dim - len(a))
    vb = b + [0.0] * (dim - len(b))
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Deterministic fact extraction (L2 input and L4 keys)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Fact:
    """One structured fact extracted from a user turn (provenanced)."""

    key: str          # stable L4 assertion key, e.g. "user:dog:name"
    value: str        # human-readable fact, e.g. "user's dog is named Bruno"
    kind: str         # "user_fact" | "preference" | "relationship"
    turn_id: int      # message id of the source turn
    anchor: str       # exact excerpt from the source turn


_NAME_RE = re.compile(r"\bmy\s+([a-z]+)'s\s+name\s+is\s+([A-Za-z][A-Za-z0-9]*)", re.IGNORECASE)
_POSSESSIVE_RE = re.compile(r"\bmy\s+([a-z]+)\s+(?:is|are)\s+(.+?)[.!?]?$", re.IGNORECASE)
_HAVE_RE = re.compile(r"\bi\s+have\s+(?:a|an)\s+([a-z]+)\b", re.IGNORECASE)
_NAMED_RE = re.compile(r"\bnamed\s+([A-Za-z][A-Za-z0-9]*)", re.IGNORECASE)
_NEGATION_RE = re.compile(
    r"\b(?:don'?t|do not|no longer|not anymore|never)\s+have\s+"
    r"(?:a|an|the|my)?\s*([A-Za-z][A-Za-z0-9]*)",
    re.IGNORECASE,
)
#: value of a negation fact, subject captured ("user no longer has luna")
_NEGATION_VALUE_RE = re.compile(r"^user no longer has ([a-z0-9]+)$")
_LIKE_RE = re.compile(r"\bi\s+(?:love|like|enjoy)\s+(.+?)[.!?]?$", re.IGNORECASE)
_DISLIKE_RE = re.compile(r"\bi\s+(?:hate|dislike)\s+(.+?)[.!?]?$", re.IGNORECASE)
_THANKS_RE = re.compile(r"\bthank", re.IGNORECASE)
_CALLBACK_RE = re.compile(r"\b(?:remind me|remember to|don'?t forget|next time)\b", re.IGNORECASE)

_JUNK_NOUNS = frozenset({"day", "week", "mood", "life", "head", "heart", "time"})

Callback = tuple[str, int, str]  # (excerpt, turn_id, full_text)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().strip(".!?,;")).strip()


def _extract_facts(messages: list[dict]) -> list[_Fact]:
    """Conservative, deterministic fact extraction over USER turns only.

    Returns facts deduplicated by (key, value) — first occurrence wins, so
    repeated disclosures never create duplicate assertions downstream.
    """
    facts: list[_Fact] = []
    seen: set[tuple[str, str]] = set()

    def add(key: str, value: str, kind: str, turn_id: int, anchor: str) -> None:
        value = _clean(value)
        if not value or (key, value) in seen:
            return
        seen.add((key, value))
        facts.append(_Fact(key=key, value=value, kind=kind, turn_id=turn_id, anchor=anchor))

    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = str(msg.get("content", ""))
        tid = int(msg.get("id", -1))
        # Negation FIRST: "I don't have Luna anymore" must not fall through to
        # the positive rules (M-1b — without this, the stale positive fact
        # survives and reaches the prompt).
        m = _NEGATION_RE.search(text)
        if m and m.group(1).lower() not in _JUNK_NOUNS:
            subject = m.group(1).lower()
            add(f"user:{subject}", f"user no longer has {subject}",
                "user_fact", tid, text)
            continue
        m = _NAME_RE.search(text)
        if m and m.group(1).lower() not in _JUNK_NOUNS:
            noun, name = m.group(1).lower(), m.group(2)
            add(f"user:{noun}:name", f"user's {noun} is named {name}", "user_fact", tid, text)
            continue
        m = _POSSESSIVE_RE.search(text)
        if m and m.group(1).lower() not in _JUNK_NOUNS:
            noun, rest = m.group(1).lower(), _clean(m.group(2))
            add(f"user:{noun}", f"user's {noun} is {rest}", "user_fact", tid, text)
            continue
        m = _HAVE_RE.search(text)
        if m and m.group(1).lower() not in _JUNK_NOUNS:
            noun = m.group(1).lower()
            name_m = _NAMED_RE.search(text)
            if name_m:
                # keep the name INSIDE the value so a later name-based
                # negation ("I don't have Luna anymore") can supersede this
                # key via subject-word matching
                add(f"user:{noun}",
                    f"user has a {noun} named {name_m.group(1)}",
                    "user_fact", tid, text)
            else:
                add(f"user:{noun}", f"user has a {noun}", "user_fact", tid, text)
            continue
        m = _LIKE_RE.search(text)
        if m:
            topic = _clean(m.group(1))
            if topic:
                add(f"preference:like:{topic}", f"user likes {topic}", "preference", tid, text)
                continue
        m = _DISLIKE_RE.search(text)
        if m:
            topic = _clean(m.group(1))
            if topic:
                add(f"preference:dislike:{topic}", f"user dislikes {topic}", "preference", tid, text)
                continue
        if _THANKS_RE.search(text):
            add("relationship:gratitude", "user expressed gratitude", "relationship", tid, text)
    return facts


def _callbacks(messages: list[dict]) -> list[Callback]:
    """User turns that ask for a future reminder — exact excerpt kept."""
    out: list[Callback] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = str(msg.get("content", ""))
        if _CALLBACK_RE.search(text):
            out.append((text[:80], int(msg.get("id", -1)), text))
    return out


# ---------------------------------------------------------------------------
# Affect observations (metadata on memories, derived deterministically)
# ---------------------------------------------------------------------------


def _affect_observation(msg: dict, score: float | None) -> AffectMetadata:
    """Deterministic affect metadata for one user turn.

    Valence comes from the day's judge score sign/magnitude (the judge
    consumes no RNG); arousal and intensity come from surface text signals
    (exclamation/questions/ellipsis). This is metadata ON the memory — there
    is no separate emotional store.
    """
    text = str(msg.get("content", ""))
    exclaim = text.count("!")
    question = text.count("?")
    ellipsis = text.count("...") + text.count("…")
    v = 0.0 if score is None else _clip(float(score), -1.0, 1.0)
    arousal = _clip(0.3 + 0.2 * min(1, exclaim) + 0.2 * min(1, question) - 0.1 * min(1, ellipsis))
    intensity = _clip(0.7 * abs(v) + 0.3 * arousal + 0.2 * min(1.0, exclaim))
    peak = abs(v) >= 0.7 or exclaim >= 2
    return AffectMetadata(
        user_valence=v,
        user_arousal=arousal,
        companion_valence=0.5,
        intensity=intensity,
        conflict=0.5,
        comfort=_clip(0.5 + 0.4 * v),
        vulnerability=_clip(0.5 + 0.3 * intensity),
        relationship_relevance=_clip(0.3 + 0.4 * intensity),
        emotional_peak=peak,
    )


def _affect_observations(messages: list[dict], score: float | None) -> tuple[AffectMetadata, ...]:
    """Per-user-turn observations that carry a measurable signal."""
    obs = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        o = _affect_observation(msg, score)
        if o.intensity >= 0.2 or o.emotional_peak:
            obs.append(o)
    return tuple(obs)


# ---------------------------------------------------------------------------
# L2 — deterministic session summarizer (default; injectable)
# ---------------------------------------------------------------------------

Summarizer = Callable[[str, list[dict], dict | None, float, float], SessionSummary]
# (session_id, messages, judgement, started_at_t_h, ended_at_t_h) -> SessionSummary


def _topics(messages: list[dict], n: int = 5) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for msg in messages:
        for tok in _TOKEN_RE.findall(str(msg.get("content", "")).lower()):
            if len(tok) >= 4 and tok not in _STOPWORDS:
                counts[tok] = counts.get(tok, 0) + 1
    ranked = sorted(counts, key=lambda t: (-counts[t], t))
    return tuple(ranked[:n])


def _session_importance(
    facts: list[_Fact],
    callbacks: list[Callback],
    observations: tuple[AffectMetadata, ...],
    n_messages: int,
) -> float:
    """Deterministic importance: disclosure signal + affect + engagement."""
    signal = 0.0
    if any(f.kind == "user_fact" for f in facts):
        signal += 0.50
    if any(f.kind == "preference" for f in facts):
        signal += 0.25
    if any(f.kind == "relationship" for f in facts):
        signal += 0.15
    if callbacks:
        signal += 0.10
    affect_signal = (
        sum(o.intensity for o in observations) / len(observations) if observations else 0.0
    )
    engagement = min(1.0, n_messages / 12.0)
    return _clip(signal + 0.15 * affect_signal + 0.10 * engagement)


def deterministic_summarizer(
    session_id: str,
    messages: list[dict],
    judgement: dict | None,
    started_at_t_h: float,
    ended_at_t_h: float,
) -> SessionSummary:
    """Default L2 summarizer: fully deterministic, never requires an LLM.

    * topics      — content-word frequency across all turns
    * user_facts  — conservative regex extraction (name/possessive/have)
    * preferences — like/dislike patterns
    * callbacks   — reminder-style requests (exact excerpts)
    * affect      — judge score sign/magnitude + surface text signals
    * importance  — disclosure + affect + engagement (see ``_session_importance``)
    """
    facts = _extract_facts(messages)
    cbs = _callbacks(messages)
    score = None
    if judgement is not None:
        raw = judgement.get("score")
        if raw is not None:
            try:
                score = float(raw)
            except (TypeError, ValueError):
                score = None
    observations = _affect_observations(messages, score)

    user_facts = tuple(f.value for f in facts if f.kind == "user_fact")
    preferences = tuple(f.value for f in facts if f.kind == "preference")
    relationships = tuple(f.value for f in facts if f.kind == "relationship")
    callback_excerpts = tuple(cb[0] for cb in cbs)
    companion_events = tuple(
        str(m.get("content", ""))[:120]
        for m in messages
        if m.get("role") == "assistant"
        and (m.get("proactive") or re.search(r"\b(i will|i'll|i started|i finished)\b", str(m.get("content", "")), re.IGNORECASE))
    )

    summary_parts = [
        f"Session {session_id}: {len(messages)} turn(s) between "
        f"{started_at_t_h:.1f}h and {ended_at_t_h:.1f}h.",
    ]
    if user_facts:
        summary_parts.append("User shared: " + "; ".join(user_facts) + ".")
    if preferences:
        summary_parts.append("Preferences: " + "; ".join(preferences) + ".")
    if observations:
        summary_parts.append(
            "Affect: peak=" + str(any(o.emotional_peak for o in observations)).lower()
            + ", mean intensity=" + f"{sum(o.intensity for o in observations) / len(observations):.2f}."
        )

    peak = any(o.emotional_peak for o in observations)
    return SessionSummary(
        session_id=session_id,
        started_at_t_h=float(started_at_t_h),
        ended_at_t_h=float(ended_at_t_h),
        summary=" ".join(summary_parts),
        topics=_topics(messages),
        user_facts=user_facts,
        preference_updates=preferences,
        companion_events=companion_events,
        relationship_events=relationships,
        callbacks=callback_excerpts,
        affect_observations=observations,
        emotional_peak=peak,
        importance=_session_importance(facts, cbs, observations, len(messages)),
        source_turn_ids=tuple(int(m["id"]) for m in messages if "id" in m),
    )


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
# Baselines (Memory Gate item 10 — the three policy baselines)
# ---------------------------------------------------------------------------


def raw_history(store, *, limit: int = L1_SLICE_LIMIT) -> tuple[Turn, ...]:
    """RAW_HISTORY baseline: the recent transcript, nothing else.

    Identical to the L1 slice the assembler already consumes.
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
    0.35/0.30/0.35 reranker used by ``MemoryAgent.retrieve``.
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

Embedder = Callable[[str], list[float]]


class MemoryAgent:
    """ZifaMem-style L1/L2/L3/L4 pipeline over the store seam.

    Seam-exact public API (plan §15): ``record_turn``, ``close_session``,
    ``promote``, ``update_user_model``, ``retrieve``.
    """

    def __init__(
        self,
        store,
        *,
        embedder: Embedder | None = None,
        policy: PromotionPolicy | None = None,
        rng=None,
        summarizer: Summarizer | None = None,
    ) -> None:
        self.store = store
        self._embed: Embedder = embedder or deterministic_hash_embedder
        self._policy = policy if policy is not None else PromotionPolicy()
        self._summarizer: Summarizer = summarizer or deterministic_summarizer
        self._rng = rng  # reserved for future stochastic extensions; A5 is deterministic
        try:
            self._add_message_accepts_session = (
                "session_id" in inspect.signature(store.add_message).parameters
            )
        except (TypeError, AttributeError):
            self._add_message_accepts_session = False

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

        The default summarizer is deterministic (no LLM required); an
        injectable summarizer may decorate later. The summary is persisted
        through the store seam before being returned.
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
        tags = [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 3 and t not in _STOPWORDS]
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
                    self.store.supersede_assertion(a.key)
                    updated.append(
                        replace(
                            a,
                            status="superseded",
                            updated_at_t_h=summary.ended_at_t_h,
                            source_memory_ids=a.source_memory_ids + extra,
                        )
                    )
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

        Ranking: ``score(q,j) = 0.35*sem + 0.30*strength + 0.35*importance``
        with strength recalculated at retrieval time. Returns the L1 slice
        (recent turns), a bounded L2 slice (summaries of the sessions that
        produced the top episodes), the top L3 episodes, the L4 projection,
        and exact verbatim evidence anchors. ``context`` may carry
        ``{"t_h": now}``; without it the latest stored timestamp is used.
        Total payload is hard-capped at ``MAX_CONTEXT_CHARS``.
        """
        ctx = context or {}
        now = ctx.get("t_h")
        if now is None:
            now = self._latest_t_h()
        qv = self._embed(query)
        embeddings = dict(self.store.load_embeddings())

        scored: list[tuple[float, EpisodicMemory]] = []
        for ep in self.store.list_episodes(limit=EPISODE_LIST_LIMIT):
            strength = episodic_strength(ep, now)
            score = score_memory(qv, embeddings.get(ep.id, []), ep, strength)
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
