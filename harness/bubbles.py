"""Bubble splitting — model-driven, gated by HARNESS_BUBBLES.

When the flag is on the system prompt tells the model it may split a reply
into several short chat messages (bubbles) separated by a blank line.
Both a single newline and a blank line (double newline) are treated as one
separator — the model's natural blank-line style counts (WS-B parser fix).

No model call is changed when the flag is off: parity holds.
"""

from __future__ import annotations

import os
import re

# Instruction appended to the system prompt when bubbling is enabled.
# Plain English, no numbers, no jargon.
BUBBLE_INSTRUCTION = (
    "Formatting: you may split a reply into several short chat messages "
    "(bubbles) where a human would naturally hit send. "
    "Separate bubbles with a blank line — an empty line between them. "
    "Only split at natural sentence boundaries; never mid-sentence."
)

_BUBBLE_ENV = "HARNESS_BUBBLES"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def bubbles_enabled() -> bool:
    return _env_bool(_BUBBLE_ENV, False)


def parse_bubbles(text: str) -> list[str]:
    """Split model text into non-empty bubbles.

    A bubble boundary is one or more blank-line runs — any run of newlines
    (with optional whitespace in between) counts as ONE separator. This makes
    both a single newline and a blank line valid (WS-B ruling: \\n and \\n\\n
    are the same separator; runs collapse).

    Leading/trailing whitespace and empty pieces are dropped.
    Returns at least one element (the trimmed text) when the stripped text
    is non-empty; empty input returns [].
    """
    if text.strip() == "":
        return []
    # Any run of newlines (with optional whitespace on blank lines) is one
    # separator — both \n and \n\n count as one boundary (WS-B ruling).
    raw = re.split(r"(?:\n\s*)+", text)
    bubbles = [p.strip() for p in raw if p.strip() != ""]
    if not bubbles:
        return [text.strip()]
    return bubbles
