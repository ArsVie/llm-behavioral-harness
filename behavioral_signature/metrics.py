"""The eight behavioral-signature metrics.

Every metric is a pure, deterministic function of a ``LogRecord``: no
randomness, no wall clock, fixed formatting. Metric names align with the
behavioral channels the harness already names (``harness.behavior``'s
initiative / reactivity / persistence / warmth), but here they are MEASURED
from the conversation-log surface rather than derived from engine state —
prescription and measurement are two sides of the same contract and must
never diverge.

Operational definitions (one line each; ranges in brackets):

* contact_frequency [>=0, turns/hour] — total turns divided by the log's
  elapsed span (first→last turn, chosen time source); 0.0 when fewer than
  two timeable turns or a zero span.
* initiative [0..1] — fraction of companion turns that OPEN an exchange,
  i.e. are not immediately preceded by a user turn (first turn of the log
  counts as opened); 0.0 with no companion turns.
* warmth [0..1] — fraction of companion turns containing at least one
  warm-affect token from the fixed WARM_TOKENS lexicon; 0.0 with no
  companion turns.
* verbosity [>=0, words] — mean whitespace-separated word count per
  companion turn; 0.0 with no companion turns.
* latency [>=0, hours] — median companion reply delay: for each companion
  turn immediately preceded by a user turn, delay = time(companion) −
  time(user), clamped at 0; 0.0 when no measurable reply pairs.
* topic_selection [0..1] — fraction of companion turns that SHIFT topic:
  content words (stopword-filtered tokens) share none with the preceding
  user turn; a companion turn with no preceding user turn counts as a
  shift; 0.0 with no companion turns.
* persistence [0..1] — fraction of companion turns after the first that
  re-tread an earlier thread: share at least one content word with ANY
  earlier companion turn; 0.0 with fewer than two companion turns.
* reactivity [0..1] — fraction of user turns immediately followed by a
  companion turn (user turns that got an answer); 0.0 with no user turns.

Content words for topic_selection / persistence / warmth are produced by
``_content_tokens``: lowercase, apostrophes removed, split on non-alphanumerics,
drop 1-char tokens and the fixed STOPWORDS set. Both lexicons are frozen
constants — part of the contract; changing them changes the signature.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Sequence, Union

from .log import LogRecord, LogTurn, time_kind, _TIME_KIND_DATETIME, _TIME_KIND_T_H

# --------------------------------------------------------------------------- #
# frozen lexical constants (contract)
# --------------------------------------------------------------------------- #

WARM_TOKENS = frozenset(
    {
        "glad", "good", "happy", "love", "loving", "warm", "warmth",
        "nice", "soft", "gentle", "kind", "kindness", "smile", "smiling",
        "care", "caring", "sweet", "hug", "miss", "enjoy", "enjoying",
        "beautiful", "proud", "hope", "hoping", "welcome", "comfort",
        "comforting", "lovely", "fond", "tender", "grateful",
    }
)

STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "if", "then", "else", "elif",
        "for", "of", "to", "in", "on", "at", "by", "with", "from", "up",
        "down", "out", "off", "over", "under", "again", "once", "here",
        "there", "when", "where", "why", "how", "all", "any", "both",
        "each", "few", "more", "most", "other", "some", "such", "no", "nor",
        "not", "only", "own", "same", "so", "than", "too", "very", "just",
        "can", "will", "would", "could", "should", "shall", "may", "might",
        "must", "i", "me", "my", "myself", "we", "our", "ours", "us", "you",
        "your", "yours", "yourself", "he", "him", "his", "she", "her",
        "hers", "it", "its", "they", "them", "their", "im", "idk", "ive",
        "ill", "youre", "youve", "youd", "dont", "cant", "wont", "didnt",
        "is", "am", "are", "was", "were", "be", "been", "being", "have",
        "has", "had", "do", "does", "did", "doing", "got", "get", "gets",
        "getting", "what", "which", "who", "whom", "this", "that", "these",
        "those", "about", "into", "through", "during", "before", "after",
        "above", "below", "between", "along", "around", "as", "while",
        "until", "upon", "like", "oh", "yeah", "okay", "ok", "hmm", "hey",
    }
)


def _content_tokens(text: str) -> set:
    return {
        tok
        for tok in re.split(r"[^a-z0-9]+", text.lower().replace("'", ""))
        if len(tok) >= 2 and tok not in STOPWORDS
    }


def _turn_time(turn: LogTurn, kind: str) -> Union[datetime, float, None]:
    return turn.timestamp if kind == _TIME_KIND_DATETIME else turn.t_h


def _diff_hours(a, b, kind: str) -> float:
    if kind == _TIME_KIND_DATETIME:
        return (a - b).total_seconds() / 3600.0
    return float(a) - float(b)


# --------------------------------------------------------------------------- #
# the eight metrics
# --------------------------------------------------------------------------- #


def contact_frequency(record: LogRecord) -> float:
    """Turns per hour across the log's span (first → last turn)."""
    kind = time_kind(record)
    times = [_turn_time(t, kind) for t in record.turns]
    times = [x for x in times if x is not None]
    if len(times) < 2:
        return 0.0
    span_h = _diff_hours(max(times), min(times), kind)
    if span_h <= 0:
        return 0.0
    return len(times) / span_h


def initiative(record: LogRecord) -> float:
    """Fraction of companion turns that open an exchange."""
    comp_idx = [i for i, t in enumerate(record.turns) if t.speaker == "companion"]
    if not comp_idx:
        return 0.0
    opened = 0
    for i in comp_idx:
        if i == 0 or record.turns[i - 1].speaker != "user":
            opened += 1
    return opened / len(comp_idx)


def warmth(record: LogRecord) -> float:
    """Fraction of companion turns with at least one WARM_TOKENS token."""
    comp = [t for t in record.turns if t.speaker == "companion"]
    if not comp:
        return 0.0
    warm = sum(1 for t in comp if _content_tokens(t.text) & WARM_TOKENS)
    return warm / len(comp)


def verbosity(record: LogRecord) -> float:
    """Mean words (whitespace-separated) per companion turn."""
    comp = [t for t in record.turns if t.speaker == "companion"]
    if not comp:
        return 0.0
    return sum(len(t.text.split()) for t in comp) / len(comp)


def latency(record: LogRecord) -> float:
    """Median companion reply delay in hours (clamped at 0)."""
    kind = time_kind(record)
    delays = []
    for i, t in enumerate(record.turns):
        if t.speaker != "companion" or i == 0:
            continue
        prev = record.turns[i - 1]
        if prev.speaker != "user":
            continue
        a = _turn_time(t, kind)
        b = _turn_time(prev, kind)
        if a is None or b is None:
            continue
        delays.append(max(_diff_hours(a, b, kind), 0.0))
    if not delays:
        return 0.0
    s = sorted(delays)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def topic_selection(record: LogRecord) -> float:
    """Fraction of companion turns whose content shares no word with the
    preceding user turn (a topic shift); no preceding user turn → shift."""
    comp_idx = [i for i, t in enumerate(record.turns) if t.speaker == "companion"]
    if not comp_idx:
        return 0.0
    shifts = 0
    for i in comp_idx:
        prev_user = None
        for j in range(i - 1, -1, -1):
            if record.turns[j].speaker == "user":
                prev_user = j
                break
        if prev_user is None:
            shifts += 1
            continue
        if not (_content_tokens(record.turns[i].text) & _content_tokens(record.turns[prev_user].text)):
            shifts += 1
    return shifts / len(comp_idx)


def persistence(record: LogRecord) -> float:
    """Fraction of companion turns after the first that share a content word
    with ANY earlier companion turn (thread re-tread)."""
    comp_idx = [i for i, t in enumerate(record.turns) if t.speaker == "companion"]
    if len(comp_idx) < 2:
        return 0.0
    seen: set = set()
    persistent = 0
    for i in comp_idx:
        toks = _content_tokens(record.turns[i].text)
        if toks & seen:
            persistent += 1
        seen |= toks
    # the first companion turn has no earlier companion turn to re-tread:
    # it is excluded from the denominator.
    return persistent / (len(comp_idx) - 1)


def reactivity(record: LogRecord) -> float:
    """Fraction of user turns immediately followed by a companion turn."""
    n_user = 0
    n_answered = 0
    for i, t in enumerate(record.turns):
        if t.speaker == "user":
            n_user += 1
            if i + 1 < len(record.turns) and record.turns[i + 1].speaker == "companion":
                n_answered += 1
    if n_user == 0:
        return 0.0
    return n_answered / n_user


# --------------------------------------------------------------------------- #
# signature assembly
# --------------------------------------------------------------------------- #

METRIC_NAMES = (
    "contact_frequency",
    "initiative",
    "warmth",
    "verbosity",
    "latency",
    "topic_selection",
    "persistence",
    "reactivity",
)

Signature = dict  # dict[str, float], keys exactly METRIC_NAMES

_METRIC_FUNCS = {
    "contact_frequency": contact_frequency,
    "initiative": initiative,
    "warmth": warmth,
    "verbosity": verbosity,
    "latency": latency,
    "topic_selection": topic_selection,
    "persistence": persistence,
    "reactivity": reactivity,
}


def compute_signature(record: Union[LogRecord, Sequence[LogTurn]]) -> Signature:
    """Compute the 8-metric signature dict from a log.

    Accepts a ``LogRecord`` or a flat sequence of ``LogTurn`` (wrapped into a
    LogRecord). Values are rounded to 6 decimal places — fixed formatting,
    fully deterministic.
    """
    if not isinstance(record, LogRecord):
        record = LogRecord(conversation_id="", turns=tuple(record))
    return {name: round(fn(record), 6) for name, fn in _METRIC_FUNCS.items()}


def signature_to_json(sig: Signature) -> str:
    """Byte-deterministic serialization of a signature (sorted keys)."""
    return json.dumps(sig, sort_keys=True, indent=2) + "\n"
