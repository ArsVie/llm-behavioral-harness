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
from dataclasses import replace
from typing import Callable, Protocol, runtime_checkable

from harness.domain import AffectMetadata, SessionSummary

__all__ = [
    "Summarizer",
    "DeterministicSummaryExtractor",
    "SemanticSummaryExtractor",
    "deterministic_summarizer",
]

# Deterministic fact extraction lives in harness.summarization_facts; every
# name is re-exported here so existing imports keep working unchanged.
from harness.summarization_facts import (  # noqa: E402
    Callback,
    _callbacks,
    _extract_facts,
    _Fact,
)

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


def _judgement_score(judgement: dict | None) -> float | None:
    """The judge's score as a float, or None when there isn't a usable one.

    The judge is a noisy sensor: a missing judgement, a missing score, or a
    score that will not parse all mean "no affect signal from the judge",
    never a crash and never a fabricated 0.0.
    """
    if judgement is None:
        return None
    raw = judgement.get("score")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _companion_events(messages: list[dict]) -> tuple[str, ...]:
    """Companion turns worth remembering, truncated to 120 chars.

    A turn qualifies if it was proactive (she chose to send it) or if it
    reads as her reporting her own life — the "I will / I'll / I started /
    I finished" forms that carry an event the user may refer back to.
    """
    pattern = r"\b(i will|i'll|i started|i finished)\b"
    return tuple(
        str(m.get("content", ""))[:120]
        for m in messages
        if m.get("role") == "assistant"
        and (
            m.get("proactive")
            or re.search(pattern, str(m.get("content", "")), re.IGNORECASE)
        )
    )


def _summary_prose(session_id: str, turns: int, started_at_t_h: float,
                   ended_at_t_h: float, user_facts: tuple,
                   preferences: tuple, observations: list) -> str:
    """The one-paragraph session summary; each clause appears only when it
    has something to say, so an empty session reads as a bare header rather
    than a list of empty headings."""
    parts = [
        f"Session {session_id}: {turns} turn(s) between "
        f"{started_at_t_h:.1f}h and {ended_at_t_h:.1f}h.",
    ]
    if user_facts:
        parts.append("User shared: " + "; ".join(user_facts) + ".")
    if preferences:
        parts.append("Preferences: " + "; ".join(preferences) + ".")
    if observations:
        mean = sum(o.intensity for o in observations) / len(observations)
        peak = str(any(o.emotional_peak for o in observations)).lower()
        parts.append(f"Affect: peak={peak}, mean intensity={mean:.2f}.")
    return " ".join(parts)


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
    observations = _affect_observations(messages, _judgement_score(judgement))

    user_facts = tuple(f.value for f in facts if f.kind == "user_fact")
    preferences = tuple(f.value for f in facts if f.kind == "preference")
    relationships = tuple(f.value for f in facts if f.kind == "relationship")
    callback_excerpts = tuple(cb[0] for cb in cbs)
    companion_events = _companion_events(messages)
    peak = any(o.emotional_peak for o in observations)
    return SessionSummary(
        session_id=session_id,
        started_at_t_h=float(started_at_t_h),
        ended_at_t_h=float(ended_at_t_h),
        summary=_summary_prose(
            session_id, len(messages), started_at_t_h, ended_at_t_h,
            user_facts, preferences, observations,
        ),
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
