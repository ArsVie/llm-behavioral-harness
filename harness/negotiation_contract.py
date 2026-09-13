"""Availability event negotiation — the frozen G0 contract.

Shared constants, phase names, payload keys and emission shapes that A1-A4 bind
to; do not edit without re-freezing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# phases


class NegotiationPhase(str, Enum):
    """One negotiation's lifecycle phase."""

    INFORM = "inform"      # one-shot: she mentions the event, no verdict
    DECIDE = "decide"      # recurring: go / skip / delay(N)
    RESOLVED_GO = "resolved_go"          # terminal: into the activity
    RESOLVED_SKIP = "resolved_skip"      # terminal: abandoned by her
    RESOLVED_FORCED = "resolved_forced"  # terminal: backstop (missed it)


GO = "follow"          # go -> follow
SKIP = "abandon"       # skip -> abandon
DELAY = "defer"        # delay -> defer (+ N payload)


# triggers

#: How far AHEAD of an event's window the heads-up fires (default 15 min).
#: No verdict until the window opens; fires only while a conversation is open.
HEADS_UP_LEAD_MIN = 15.0
HEADS_UP_LEAD_H = HEADS_UP_LEAD_MIN / 60.0

#: Minutes of user silence that trigger the decide phase (default 10 min).
SHORT_AFK_MIN = 10.0
SHORT_AFK_H = SHORT_AFK_MIN / 60.0

#: Default defer turns for a vague "a bit longer" request.
DEFAULT_DEFER_TURNS = 2

#: User-away threshold is read from harness.tunables at the call sites.

#: Weight added toward "go" after each delay (steerable, linear default).
PULL_PER_DELAY = 0.15


# defer(N) payload shape

#: Verdict key holding the server-computed defer turns.
DEFER_TURNS_KEY = "defer_turns"

#: NL patterns mapped to defer turns: vague phrases -> default, explicit N -> clamped.
DEFER_N_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\bjust\s+(?:a\s+)?(second|sec|moment|minute|min)\b", 1),
    (r"\b(a\s+)?bit\s+longer\b", 2),
    (r"\bfew\s+more\b", 3),
    (r"\b(\d+)\s+more\s+(?:turns?|messages?|replies?)\b", 0),  # 0 = explicit
)
#: Explicit N clamp.
DEFER_N_MIN = 1
DEFER_N_MAX = 4


# skippable / unskippable

#: Source types treated as unskippable commitments; other types are skippable.
UNSKIPPABLE_SOURCE_TYPES: frozenset[str] = frozenset({"routine"})


def is_skippable(source_type: str) -> bool:
    """Whether an AgendaItem (by source_type) is a discretionary event."""
    return source_type not in UNSKIPPABLE_SOURCE_TYPES


# episode emission shape

#: MemoryKind category for negotiation outcomes (companion-side episodes).
EPISODE_CATEGORY = "companion_episode"

#: Episode tags for retrieving go/skip/delay outcomes later.
TAG_GO = "negotiation_go"
TAG_SKIP = "negotiation_skip"
TAG_FORCED = "negotiation_forced"
TAG_DELAY = "negotiation_delay"

#: Only consequential outcomes emit; a plain go with zero delays does not.
EMIT_GO_WITH_DELAYS = True      # go after >=1 delay emits
EMIT_GO_WITHOUT_DELAYS = False  # plain go does not
EMIT_SKIP = True
EMIT_FORCED = True


@dataclass(frozen=True)
class NegotiationEpisode:
    """The emission shape A1 hands to the memory hook (A3)."""

    item_id: str
    activity: str
    outcome: str                 # GO | SKIP | FORCED
    delay_count: int
    salience: float
    occurred_at_t_h: float
    summary: str                 # e.g. "kept choosing to stay with you
                                 # instead of the gym (3 delays), then went"
    source_session_id: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
