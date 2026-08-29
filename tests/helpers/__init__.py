"""Shared test helpers — the consolidated seam layer for the test suite.

Every helper here was extracted VERBATIM from per-file duplicates (the
thermo-nuclear review's code-judo move). Nothing in this package changes
test behavior: it only gives the copy-pasted builders/fakes/clocks one
canonical home so tests stop importing from each other.

Modules:
- ``store``       — ``make_store`` (SQLiteStore builder) + ``make_session``
  (Session factory with the superset signature across all per-file
  variants).
- ``agenda``      — agenda/row builders (``_rows``, ``_suppressed_codes``,
  ``ground_agenda``, ``agenda_item``, ``ground_item``).
- ``fakes``       — the hand-maintained store fakes: ``SeamStore`` (the A2
  seam-faithful in-memory store, ex test_proactive) and ``FakeStore`` (the
  A7 Iteration-2 in-memory store, ex test_memory).
- ``channel_fakes`` — Telegram-channel fakes (StubUpdate/FakeBot/
  FakeApplication, ex test_channel_telegram).
- ``clocks``      — ``ManualClock``/``GateSleeper``/``drain`` (ex
  test_telegram_helpers) and the anchor-mode ``AnchorManualClock`` (ex
  test_runtime_anchor).
- ``forbidden``   — the forbidden-token/numeric-leak battery (consolidated
  from test_snapshot + test_w2w3_time_aware; the patterns are CRITICAL
  invariants and are preserved exactly, only the location is shared).
- ``memory``      — the memory-pipeline day driver (``run_day`` +
  ``save_day_judgement``, ex test_memory).
- ``life``        — the life-lane day loop (``run_life_days``, the
  superset of test_life/_run_days and test_life_long_horizon/_run_days).
"""

from tests.helpers.store import make_session, make_store
from tests.helpers.agenda import (
    agenda_item,
    ground_agenda,
    ground_item,
    rows,
    suppressed_codes,
)
from tests.helpers.fakes import FakeStore, SeamStore
from tests.helpers.channel_fakes import FakeApplication, FakeBot, StubUpdate
from tests.helpers.clocks import AnchorManualClock, GateSleeper, ManualClock, drain
from tests.helpers.forbidden import (
    BATTERY_MESSAGES,
    BATTERY_VARIANTS,
    CLOCK_TIME_RE,
    DAY_INDEX_RE,
    FORBIDDEN_FLOAT_RE,
    FORBIDDEN_G_RE,
    FORBIDDEN_SUBSTRINGS,
    numeric_leak,
)
from tests.helpers.memory import run_day, save_day_judgement
from tests.helpers.life import run_life_days

__all__ = [
    "make_session",
    "make_store",
    "agenda_item",
    "ground_agenda",
    "ground_item",
    "rows",
    "suppressed_codes",
    "FakeStore",
    "SeamStore",
    "FakeApplication",
    "FakeBot",
    "StubUpdate",
    "GateSleeper",
    "ManualClock",
    "AnchorManualClock",
    "drain",
    "BATTERY_MESSAGES",
    "BATTERY_VARIANTS",
    "CLOCK_TIME_RE",
    "DAY_INDEX_RE",
    "FORBIDDEN_FLOAT_RE",
    "FORBIDDEN_G_RE",
    "FORBIDDEN_SUBSTRINGS",
    "numeric_leak",
    "run_day",
    "save_day_judgement",
    "run_life_days",
]
