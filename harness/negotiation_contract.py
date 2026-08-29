"""Availability event negotiation — frozen contract (G0, 2026-08-14).

Shared constants, phase names, payload keys and emission shapes that A1-A4
bind to. Do NOT edit after G0 without re-freezing (a new commit + steer).

State machine (one negotiation per AgendaItem, active while the item is
"planned" and now >= item.start_t_h):

    boundary reached (start_t_h), conversation OPEN
      |
      |-- no open conversation -> skip Inform, go straight to Decide
      v
    INFORM  (once, idempotent) -- model emits a natural mention ("I've got
            gym soon") through the channel. NO verdict, she does not leave.
      |
      v
    DECIDE  (recurring) -- fires at min(next companion turn,
            user-silence > SHORT_AFK_H). verdict in {go, skip, delay(N)}
              go    -> graceful close of conversation, into the activity.
                        TERMINAL. AgendaItem.status = "completed"
              skip  -> abandon: status = "skipped", recorded. TERMINAL;
                        conversation continues.
              delay -> defer(N): stay. Re-arm BOTH triggers (N more turns
                        AND the AFK bomb) -> loop back to DECIDE
      |
      v
    BACKSTOP  now >= AgendaItem.end_t_h -> forced skip ("missed it
              entirely"), status = "skipped", recorded. Guarantees
              termination; defer can never loop past end_t_h.

Floor (never steered away):
  * The MODEL chooses go/skip/delay from feeling + conversation context.
    The SERVER owns mechanics: maps "a bit longer" -> concrete N, arms the
    AFK bomb, enforces the backstop, applies the converging pull.
  * Inform fires exactly once per event (idempotency marker:
    rec.get("informed") is True -- responded-bool discipline, commit
    3005b9e; NEVER key presence).
  * No re-announcement: pending-event pressure is internal state, surfaced
    only on resolution (or if the model itself raises it again).
  * Converging pull-to-go: each delay raises the weight toward go.
  * SHORT_AFK_H (Decide trigger / time bomb) != USER_LEFT_THRESHOLD_H (the
    user-away threshold). Both measured from _last_user_turn_t_h; distinct by
    design.
  * No new tool: tool_decide_event.action in {follow, abandon, defer}
    already IS go/skip/delay; defer gains an N payload; the runtime loops.
  * Conversation lifecycle, decision_records, AgendaItem start/end_t_h
    stay as-is. Deterministic given seed: virtual-clock driven, replayable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# --------------------------------------------------------------------------- #
# phases
# --------------------------------------------------------------------------- #


class NegotiationPhase(str, Enum):
    """One negotiation's lifecycle phase."""

    INFORM = "inform"      # one-shot: she mentions the event, no verdict
    DECIDE = "decide"      # recurring: go / skip / delay(N)
    RESOLVED_GO = "resolved_go"          # terminal: into the activity
    RESOLVED_SKIP = "resolved_skip"      # terminal: abandoned by her
    RESOLVED_FORCED = "resolved_forced"  # terminal: backstop (missed it)


#: Verdict actions map 1:1 to the existing tool_decide_event action enum.
GO = "follow"          # go   -> existing follow
SKIP = "abandon"       # skip -> existing abandon
DELAY = "defer"        # delay-> existing defer (+ N payload)


# --------------------------------------------------------------------------- #
# triggers
# --------------------------------------------------------------------------- #

#: Decide trigger / AFK time bomb: minutes of user silence since the last
#: user turn (steerable knob, default 10 min).
SHORT_AFK_MIN = 10.0
SHORT_AFK_H = SHORT_AFK_MIN / 60.0

#: Default N when the model says "a bit longer" (steerable; small).
DEFAULT_DEFER_TURNS = 2

#: The user-away threshold is defined ONCE in harness.tunables; the contract
#: must never re-hardcode it — the previous hardcoded 12.0 copy went stale
#: when the value changed. Distinct from SHORT_AFK by design; never collapse
#: the two (tunables is the single source; no import here by design — the
#: values are read at the call sites).

#: Converging pull schedule: per-delay weight added toward go (steerable;
#: linear default). The server presents this as rising pressure in the
#: decide request (mounting cost / eroding window) -- the model still
#: chooses; the pull only raises the weight.
PULL_PER_DELAY = 0.15


# --------------------------------------------------------------------------- #
# defer(N) payload shape (extends the existing tool_decide_event verdict)
# --------------------------------------------------------------------------- #

#: Key inside the decide verdict for the SERVER-FILLED N (the model never
#: emits arithmetic; the server maps "a bit longer" -> turns).
DEFER_TURNS_KEY = "defer_turns"

#: Natural-language patterns the server maps to N (deterministic, small):
#: "a bit longer" / "just a sec" / "a few more messages" -> DEFAULT_DEFER_TURNS
#: an explicit "N more turns/messages" -> N (clamped to 1..4).
DEFER_N_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\bjust\s+(?:a\s+)?(second|sec|moment|minute|min)\b", 1),
    (r"\b(a\s+)?bit\s+longer\b", 2),
    (r"\bfew\s+more\b", 3),
    (r"\b(\d+)\s+more\s+(?:turns?|messages?|replies?)\b", 0),  # 0 = explicit
)
#: Explicit N clamp.
DEFER_N_MIN = 1
DEFER_N_MAX = 4


# --------------------------------------------------------------------------- #
# skippable / unskippable
# --------------------------------------------------------------------------- #

#: AgendaItem source_type values treated as UNSKIPPABLE commitments
#: (class/work/routine -> Inform is a heads-up, Decide ~ deterministic go).
#: Skippable = source_type NOT in this set (arc/interest -> discretionary).
UNSKIPPABLE_SOURCE_TYPES: frozenset[str] = frozenset({"routine"})


def is_skippable(source_type: str) -> bool:
    """Whether an AgendaItem (by source_type) is a discretionary event."""
    return source_type not in UNSKIPPABLE_SOURCE_TYPES


# --------------------------------------------------------------------------- #
# episode emission shape (A3)
# --------------------------------------------------------------------------- #

#: MemoryKind category for negotiation outcomes (companion-side episodes).
EPISODE_CATEGORY = "companion_episode"

#: Episode tags so later conversations can retrieve go/skip/delay outcomes.
TAG_GO = "negotiation_go"
TAG_SKIP = "negotiation_skip"
TAG_FORCED = "negotiation_forced"
TAG_DELAY = "negotiation_delay"

#: Salience gate: only consequential outcomes emit. A plain go with zero
#: delays is NOT an episode; skip / forced-skip / go-after-any-delay are.
EMIT_GO_WITH_DELAYS = True      # go after >=1 delay emits
EMIT_GO_WITHOUT_DELAYS = False  # plain go does not
EMIT_SKIP = True
EMIT_FORCED = True


@dataclass(frozen=True)
class NegotiationEpisode:
    """The emission shape A1 hands to the memory hook (A3).

    The hook maps this onto the existing store.insert_episode seam
    (memory.py is must-not-touch; store.insert_episode is the seam).
    """

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
