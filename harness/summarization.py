"""L2 summarization interfaces (Iteration-2 A4, plan §5-A4 Task 4).

Two implementations of one callable contract (``Summarizer``):

* ``DeterministicSummaryExtractor`` — the heuristic regex/judge-sign
  extractor (the historic ``deterministic_summarizer``). Fully deterministic,
  no LLM required. This is the TESTING path: useful for tests and offline
  runs, but NOT presented as the research-quality production path.
* ``SemanticSummaryExtractor`` — the research-quality LLM-backed extractor
  for the real eval/live condition. An injectable OpenAI-compatible client
  produces the prose summary; structured fields and — critically — the
  provenance (``source_turn_ids``) always come from the REAL messages,
  never from the model.

Callable contract::

    (session_id, messages, judgement, started_at_t_h, ended_at_t_h)
        -> SessionSummary

Provenance invariant (plan §5-A4 Task 5): no summarization-generated user
fact becomes authoritative without source turns. ``MemoryAgent`` creates L4
assertions only from facts re-extracted from the RAW messages of a session
whose summary carries ``source_turn_ids`` — never from summary prose. The
deterministic extractor is also the source of the fact->``UserModelCategory``
assignment consumed by the L4 layer (plan §5-A4 Task 1): the canonical enum
is consumed HERE, and the store persists the category next to the assertion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Protocol, runtime_checkable

from harness.domain import AffectMetadata, SessionSummary, UserModelCategory

__all__ = [
    "Summarizer",
    "DeterministicSummaryExtractor",
    "SemanticSummaryExtractor",
    "deterministic_summarizer",
]

# Deterministic fact extraction.


@dataclass(frozen=True)
class _Fact:
    """One structured fact extracted from a user turn (provenanced).

    ``category`` is the CANONICAL L4 category (``UserModelCategory``) — the
    enum is consumed here, never inferred from store conventions or string
    prefixes. ``key`` stays a stable, human-readable provenance identifier
    under the documented legacy prefixes (``user:`` / ``preference:`` /
    ``relationship:``); the canonical category rides alongside it and is
    persisted directly on the assertion row by the store.
    """

    key: str          # stable assertion key, e.g. "user:dog:name"
    value: str        # human-readable fact, e.g. "user's dog is named Bruno"
    kind: str         # "user_fact" | "preference" | "relationship"
    category: UserModelCategory
    turn_id: int      # message id of the source turn
    anchor: str       # exact excerpt from the source turn


_FACT_CATEGORY: dict[str, UserModelCategory] = {
    "user_fact": UserModelCategory.IDENTITY,
    "preference": UserModelCategory.CURRENT_PREFERENCE,
    "relationship": UserModelCategory.RELATIONSHIP_PATTERN,
}

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
# Retraction emits the positive fact's key ("preference:like:metal").
_RETRACT_RE = re.compile(
    r"\bi\s+(?:barely|hardly|rarely|no longer|don'?t|do not|not really)\s+"
    r"(?:listen to|care about|enjoy|like|love|watch|read|play)\s+"
    r"(.+?)(?:\s+anymore)?[.!?]?$",
    re.IGNORECASE,
)
_LIKE_RE = re.compile(r"\bi\s+(?:love|like|enjoy)\s+(.+?)[.!?]?$", re.IGNORECASE)
_DISLIKE_RE = re.compile(r"\bi\s+(?:hate|dislike)\s+(.+?)[.!?]?$", re.IGNORECASE)
_THANKS_RE = re.compile(r"\bthank", re.IGNORECASE)
_CALLBACK_RE = re.compile(r"\b(?:remind me|remember to|don'?t forget|next time)\b", re.IGNORECASE)

_JUNK_NOUNS = frozenset({"day", "week", "mood", "life", "head", "heart", "time"})

Callback = tuple[str, int, str]  # (excerpt, turn_id, full_text)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().strip(".!?;,。")).strip()


def _extract_facts(messages: list[dict]) -> list[_Fact]:
    """Conservative, deterministic fact extraction over USER turns only.

    Returns facts deduplicated by (key, value) — first occurrence wins, so
    repeated disclosures never create duplicate assertions downstream. Every
    fact carries its canonical ``UserModelCategory`` (``_FACT_CATEGORY``).
    """
    facts: list[_Fact] = []
    seen: set[tuple[str, str]] = set()

    def add(key: str, value: str, kind: str, turn_id: int, anchor: str) -> None:
        value = _clean(value)
        if not value or (key, value) in seen:
            return
        seen.add((key, value))
        facts.append(
            _Fact(
                key=key,
                value=value,
                kind=kind,
                category=_FACT_CATEGORY[kind],
                turn_id=turn_id,
                anchor=anchor,
            )
        )

    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = str(msg.get("content", ""))
        tid = int(msg.get("id", -1))
        # Negation is checked before the positive rules.
        m = _NEGATION_RE.search(text)
        if m and m.group(1).lower() not in _JUNK_NOUNS:
            subject = m.group(1).lower()
            add(f"user:{subject}", f"user no longer has {subject}",
                "user_fact", tid, text)
            continue
        # Retraction reuses the positive fact's key.
        m = _RETRACT_RE.search(text)
        if m:
            topic = _clean(m.group(1))
            if topic:
                add(f"preference:like:{topic}", f"user no longer likes {topic}",
                    "preference", tid, text)
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
                # The name stays in the value for subject-word matching.
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


# Affect observations.


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
    v = 0.0 if score is None else max(-1.0, min(1.0, float(score)))
    arousal = max(0.0, min(1.0, 0.3 + 0.2 * min(1, exclaim) + 0.2 * min(1, question) - 0.1 * min(1, ellipsis)))
    intensity = max(0.0, min(1.0, 0.7 * abs(v) + 0.3 * arousal + 0.2 * min(1.0, exclaim)))
    peak = abs(v) >= 0.7 or exclaim >= 2
    return AffectMetadata(
        user_valence=v,
        user_arousal=arousal,
        companion_valence=0.5,
        intensity=intensity,
        conflict=0.5,
        comfort=max(0.0, min(1.0, 0.5 + 0.4 * v)),
        vulnerability=max(0.0, min(1.0, 0.5 + 0.3 * intensity)),
        relationship_relevance=max(0.0, min(1.0, 0.3 + 0.4 * intensity)),
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


# Deterministic session summarizer.


@runtime_checkable
class Summarizer(Protocol):
    """Callable contract shared by every L2 summary extractor."""

    def __call__(
        self,
        session_id: str,
        messages: list[dict],
        judgement: dict | None,
        started_at_t_h: float,
        ended_at_t_h: float,
    ) -> SessionSummary:
        ...


_TOKENS_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    {
        "the", "and", "you", "your", "that", "this", "with", "have", "was",
        "are", "for", "not", "but", "all", "can", "out", "get", "just",
        "like", "about", "really", "what", "when", "where", "how", "why",
        "there", "here", "from", "they", "them", "she", "him", "her", "will",
        "would", "could", "should", "into", "over", "than", "then", "very",
    }
)


def _topics(messages: list[dict], n: int = 5) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for msg in messages:
        for tok in _TOKENS_RE.findall(str(msg.get("content", "")).lower()):
            if len(tok) >= 4 and tok not in _STOP:
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
    return max(0.0, min(1.0, signal + 0.15 * affect_signal + 0.10 * engagement))


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


class DeterministicSummaryExtractor:
    """Heuristic L2 extractor — the TESTING path (deterministic, no LLM).

    Identical behavior to the module function ``deterministic_summarizer``;
    the class form exists so the testing path and the research-quality path
    share one callable interface. This is deliberately NOT presented as the
    research-quality production path (plan §5-A4 Task 4).
    """

    def __call__(
        self,
        session_id: str,
        messages: list[dict],
        judgement: dict | None,
        started_at_t_h: float,
        ended_at_t_h: float,
    ) -> SessionSummary:
        return deterministic_summarizer(
            session_id, messages, judgement, started_at_t_h, ended_at_t_h
        )


class SemanticSummaryExtractor:
    """LLM-backed L2 extractor — the research-quality path.

    Constructor takes an injectable client ``Callable[[str], str]``
    (prompt -> completion text; an OpenAI-compatible completion call). The
    model writes the prose summary; the deterministic extractor supplies the
    structured fields (topics, facts, affect, importance).

    PROVENANCE GUARD (plan §5-A4 Task 5): ``source_turn_ids`` and all
    fact-derived fields come from the REAL messages — the model's output can
    only replace the free-text ``summary``. A model never invents source
    turns, and no model-generated fact can become an L4 assertion (L4 facts
    are re-extracted from raw messages by ``MemoryAgent``).

    On client failure or empty output the deterministic summary is returned
    unchanged (degradation, never fabrication).
    """

    def __init__(
        self,
        client: Callable[[str], str],
        *,
        fallback: Callable[..., SessionSummary] | None = None,
        prompt_template: str | None = None,
    ) -> None:
        self._client = client
        self._fallback = fallback or deterministic_summarizer
        self._prompt_template = prompt_template or (
            "Summarize this companion-user conversation session in one "
            "paragraph (2-4 sentences). Mention the user's disclosures, "
            "preferences and emotional tone. Conversation:\n\n{turns}"
        )

    @staticmethod
    def _render_turns(messages: list[dict], max_chars: int = 4000) -> str:
        lines = []
        used = 0
        for m in messages:
            line = f"{m.get('role', '?')}: {m.get('content', '')}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def __call__(
        self,
        session_id: str,
        messages: list[dict],
        judgement: dict | None,
        started_at_t_h: float,
        ended_at_t_h: float,
    ) -> SessionSummary:
        base = self._fallback(session_id, messages, judgement, started_at_t_h, ended_at_t_h)
        prompt = self._prompt_template.format(turns=self._render_turns(messages))
        try:
            text = self._client(prompt)
        except Exception:  # noqa: BLE001
            return base
        text = (text or "").strip()
        if not text:
            return base
        # The model replaces only the prose.
        return replace(base, summary=text[:2000])
