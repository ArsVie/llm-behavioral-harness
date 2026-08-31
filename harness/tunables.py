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

#: User silence after which the user is treated as away and the
#: conversation goes dormant. 0.25 vh = 15 real minutes.
USER_AWAY_THRESHOLD_H = 0.25

#: True silence after which the open conversation is closed. ~6 h.
USER_LEFT_THRESHOLD_H = 6.0

#: Grace period after the wind-down draw fires. ~5 min.
WIND_DOWN_GRACE_H = 0.0833

#: closing_tendency draw is off. Conversations end on user_left
#: or quiet hours.
CLOSING_TENDENCY_ENABLED = False

#: Hard turn cap; None disables the cap entirely.
MAX_TURNS: int | None = None
