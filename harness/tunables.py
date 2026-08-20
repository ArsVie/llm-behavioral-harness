"""Single source of truth for conversation-lifecycle tunables.

Both the runtime code AND the tests import from here, so changing a value never
means editing a test — no more drift (the stale ``12.0`` copy in
``negotiation_contract`` was exactly that failure mode). These live in code, not
an external file, on purpose: they shape replayable behavior, so a replay is
pinned to the code version rather than a mutable file. If we ever want to tune
these without a code edit, snapshot the values into the replay record first.

Clock note: at the default ``seconds_per_virtual_hour = 3600`` (runtime.py) one
virtual hour == one real hour, so ``0.25`` vh == 15 real minutes.
"""

from __future__ import annotations

#: Presence signal: user silence after which the user is treated as "away"
#: and the conversation goes dormant (conversation stays OPEN). 0.25 vh = 15
#: real min. This gates skip-inform / double-text / proactive-landing (see
#: BACKLOG: user_left as a presence signal). It does NOT close the
#: conversation — that is the long backstop below.
USER_AWAY_THRESHOLD_H = 0.25

#: Long abandoned-chat backstop: true silence after which the open
#: conversation is checkpoint-closed ("user_left"). ~6 h. The 15-min
#: "away" threshold above marks presence; this one tears down.
USER_LEFT_THRESHOLD_H = 6.0

#: Two-phase wind-down grace: an unanswered wind-down closes this long after the
#: draw fired. MUST stay < USER_LEFT_THRESHOLD_H (6 h backstop) or the away
#: backstop would pre-empt it. ~5 min. Provisional; part of the deferred
#: closing_tendency redesign.
WIND_DOWN_GRACE_H = 0.0833

#: closing_tendency draw: OFF for now (feature-flagged). With it off a
#: conversation ends only on user_left (long backstop) or quiet hours — never a
#: mid-conversation taper draw. Redesign pending (flat prob gives std≈mean;
#: a fatigue curve lands length near a target). See BACKLOG.
CLOSING_TENDENCY_ENABLED = False

#: Hard turn cap: OFF. Conversations are not capped by turn count — "running out
#: of room" is a context/compaction concern, not a turn count (was 12). None
#: disables the cap entirely.
MAX_TURNS: int | None = None
