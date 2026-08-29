"""Forbidden-token / numeric-leak battery (CONSOLIDATED).

CRITICAL INVARIANT — this is the seam-guarding battery that raw engine
state never reaches the assembled prompt. The patterns below are preserved
EXACTLY as they were in test_snapshot.py (the primary battery) and
test_w2w3_time_aware.py (the anchored-prompt scan); only the LOCATION is
shared. Never weaken or remove a pattern here — add tokens, don't drop.
"""

from __future__ import annotations

import re

#: Substring check (lower-cased prompt). `g` is word-boundary only.
FORBIDDEN_SUBSTRINGS = (
    "phase_label",
    "cycle_day",
    "menstrual",
    "follicular",
    "ovulatory",
    "luteal",
    "mu",
    "eta",
)
FORBIDDEN_G_RE = re.compile(r"\bg\b")

#: G2 (W2+W3): raw engine numbers never reach the assembled prompt — no
#: bare t_h floats, no channel floats (unformatted ``digits.digits``).
#: The ONLY numeric content allowed is clock-shaped times (HH:MM — the
#: temporal line's 15:24, the agenda's 06:58) and the temporal line's
#: virtual day index ("day N").
FORBIDDEN_FLOAT_RE = re.compile(r"\d+\.\d+")
CLOCK_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
DAY_INDEX_RE = re.compile(r"day \d+")


def numeric_leak(prompt: str) -> list[str]:
    """Digits remaining after masking clock times and the temporal line's
    day index — the only numeric content allowed (G2)."""
    masked = CLOCK_TIME_RE.sub("T:T", prompt)
    masked = DAY_INDEX_RE.sub("day N", masked)
    return re.findall(r"\d+", masked)


#: Vocabulary used by the battery is deliberately free of the tokens
#: ("music", "must", "much", "beta", ... would trip the substring check).
BATTERY_MESSAGES = (
    "hello there",
    "My dog's name is Bruno.",
    "I like hiking on weekends.",
    "thanks for that",
    "see you later",
)

from engine.types import MoodVariant  # noqa: E402  (after the constants)

BATTERY_VARIANTS = (
    MoodVariant.DECOUPLED_OFFSETS,
    MoodVariant.DECOUPLED,
    MoodVariant.ORIGINAL,
)
