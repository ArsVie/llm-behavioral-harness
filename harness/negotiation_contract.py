"""Availability event negotiation — G0 contract (2026-08-14, amended
2026-09-08).

Shared constants, phase names, payload keys and emission shapes that A1-A4
bind to. Do NOT edit without re-freezing (a new commit + steer). The
2026-09-08 amendment is recorded at the bottom of this docstring.

State machine (one negotiation per AgendaItem, active from
``start_t_h - HEADS_UP_LEAD_H``):

    start_t_h - HEADS_UP_LEAD_H, conversation OPEN
      |
      |-- no open conversation -> no heads-up; the start boundary carries
      |   the decision directly
      v
    INFORM  (once, idempotent) -- the "incoming event" steer: model emits a
            natural mention ("I've got gym soon") through the channel, so
            she can get ready for it and say so. NO verdict, she does not
            leave, and nothing decides until the window opens.
      |
      v  (start_t_h)
    DECIDE  (once; re-offered only on her own defer) -- fires at
            min(next companion turn, user-silence > SHORT_AFK_H), never
            before start_t_h. verdict in {go, skip, delay(N)}
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

The END boundary is NOT a phase and never calls the model. A window that
fully passes with the item still "planned" resolves deterministically in
``life.transition_past_windows`` (planned -> completed) — her day advances
whether or not anyone watched it. Asking anything at the end produced a
second verdict per event whose ``reason`` was rationale for a choice already
made, and that rationale was then captured as the item's "outcome".

Floor (never steered away):
  * The MODEL chooses go/skip/delay from feeling + conversation context,
    and MAY name its own N on a defer (``turns``, clamped to
    [DEFER_N_MIN, DEFER_N_MAX] — it asked, so it wins). The SERVER owns the
    rest of the mechanics: maps a vague "a bit longer" -> concrete N when
    the model named none, arms the AFK bomb, enforces the backstop, applies
    the converging pull.
  * The negotiation is HIS window to talk her out of going — the delay loop
    and the converging pull only have a subject while he is there. The
    DECISION is always hers and always model-authored: the AFK bomb fires a
    real decide leg rather than resolving on her behalf.
  * Inform fires exactly once per event (idempotency marker:
    rec.get("informed") is True -- responded-bool discipline, commit
    3005b9e; NEVER key presence).
  * No re-announcement: pending-event pressure is internal state, surfaced
    only on resolution (or if the model itself raises it again).
  * Converging pull-to-go: each delay raises the weight toward go.
  * SHORT_AFK_H (Decide trigger / time bomb) != USER_LEFT_THRESHOLD_H (the
    user-away threshold). Both measured from _last_user_turn_t_h; distinct by
    design.
  * No new tool: the model answers ONE tri-state field,
    ``initiate`` in {yes, no, defer}, which IS go/skip/delay. It normalizes
    onto the canonical internal ``{initiate: bool, action: follow|abandon|
    defer}`` that every consumer and every persisted verdict row speaks.
  * The reason rides in MAIN CONTEXT, as her own words in the recorded tool
    call. It is not audit-only: she can reference later in the conversation
    why she chose what she chose. It is never pasted to the channel as
    dialogue, and never repurposed as a record of what happened.
  * Conversation lifecycle, decision_records, AgendaItem start/end_t_h
    stay as-is. Deterministic given seed: virtual-clock driven, replayable.
  * REPLAY: a crossed boundary is stamped with its OWN instant, and the
    batch is ordered chronologically across items. A resume mid-day replays
    the morning in the order the morning happened, each pop-up carrying the
    time the event actually arrived — never a pile of stale questions
    stamped with the current hour.

Amendment (2026-09-08). Four changes against the 2026-08-14 freeze, each
because the frozen text described something the design did not want:

1. INFORM moved from ``start_t_h`` to ``start_t_h - HEADS_UP_LEAD_H`` and
   became the "incoming event" steer. Its own fallback mention said "coming
   up soon", which was already false when it fired at the boundary.
2. DECIDE is one decision, re-offered only on her defer, and never fires
   before ``start_t_h``.
3. The END boundary stopped being a decision.
4. ``initiate`` became the tri-state the model actually answers, and the
   model may name its own defer N.
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
#:
#: The heads-up is the "incoming event" steer: she learns something is about
#: to start and can get ready for it, and say so if he is there. It carries
#: NO verdict -- nothing is decided until the window actually opens. It fires
#: only while a conversation is open: a heads-up with nobody to hear it is a
#: model call for nothing.
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
