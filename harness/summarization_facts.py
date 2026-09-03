"""Deterministic fact extraction from user turns (extracted from
``harness.summarization``, behaviour unchanged).

Conservative and regex-based by design: this is the layer whose output can
become an authoritative L4 assertion, so it must never guess. Every fact
carries its canonical ``UserModelCategory`` and the exact source excerpt,
and ``MemoryAgent`` re-extracts from the RAW messages rather than trusting
summary prose (provenance invariant, plan §5-A4 Task 5).

``harness.summarization`` re-exports every name here, so existing imports
are unaffected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from harness.domain import UserModelCategory

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


def _match_user_turn(text: str) -> tuple[str, str, str] | None:
    """First matching rule for one user turn, as ``(key, value, kind)``.

    Order is load-bearing and unchanged: negation and retraction are tried
    BEFORE the positive rules, so "I don't have a dog any more" never
    registers as owning a dog. A rule whose regex matches but whose captured
    subject is junk (or empties under ``_clean``) falls through to the next
    rule rather than swallowing the turn — which is why each positive rule
    re-checks its own capture instead of relying on the match alone.
    """
    m = _NEGATION_RE.search(text)
    if m and m.group(1).lower() not in _JUNK_NOUNS:
        subject = m.group(1).lower()
        return f"user:{subject}", f"user no longer has {subject}", "user_fact"
    # Retraction reuses the positive fact's key.
    m = _RETRACT_RE.search(text)
    if m:
        topic = _clean(m.group(1))
        if topic:
            return (f"preference:like:{topic}",
                    f"user no longer likes {topic}", "preference")
    m = _NAME_RE.search(text)
    if m and m.group(1).lower() not in _JUNK_NOUNS:
        noun, name = m.group(1).lower(), m.group(2)
        return (f"user:{noun}:name", f"user's {noun} is named {name}",
                "user_fact")
    m = _POSSESSIVE_RE.search(text)
    if m and m.group(1).lower() not in _JUNK_NOUNS:
        noun, rest = m.group(1).lower(), _clean(m.group(2))
        return f"user:{noun}", f"user's {noun} is {rest}", "user_fact"
    m = _HAVE_RE.search(text)
    if m and m.group(1).lower() not in _JUNK_NOUNS:
        noun = m.group(1).lower()
        name_m = _NAMED_RE.search(text)
        if name_m:
            # The name stays in the value for subject-word matching.
            return (f"user:{noun}",
                    f"user has a {noun} named {name_m.group(1)}", "user_fact")
        return f"user:{noun}", f"user has a {noun}", "user_fact"
    m = _LIKE_RE.search(text)
    if m:
        topic = _clean(m.group(1))
        if topic:
            return f"preference:like:{topic}", f"user likes {topic}", "preference"
    m = _DISLIKE_RE.search(text)
    if m:
        topic = _clean(m.group(1))
        if topic:
            return (f"preference:dislike:{topic}", f"user dislikes {topic}",
                    "preference")
    if _THANKS_RE.search(text):
        return ("relationship:gratitude", "user expressed gratitude",
                "relationship")
    return None


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
        matched = _match_user_turn(text)
        if matched is not None:
            key, value, kind = matched
            add(key, value, kind, int(msg.get("id", -1)), text)
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


