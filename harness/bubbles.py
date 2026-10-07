"""Bubble splitting — model-driven, gated by ``HARNESS_BUBBLES``.

With the flag on, the system prompt tells the model it may split a reply into
several short messages separated by a blank line; with it off, no model call
changes (parity holds).
"""

from __future__ import annotations

import os
import re
from harness.env import env_bool as _env_bool

# Instruction appended to the system prompt when bubbling is enabled.
BUBBLE_INSTRUCTION = (
    "You can answer in more than one message. A blank line is one send. "
    "Break where a thought lands, never mid-sentence."
)

_BUBBLE_ENV = "HARNESS_BUBBLES"

#: Env flag for backend bubble streaming: each bubble sends as soon as its
#: boundary parses. Default OFF (byte parity).
_STREAM_ENV = "HARNESS_BUBBLE_STREAM"

#: Pause before each message after the first of one reply, as if she were
#: typing it: base + per-char * len, clamped. Each knob is env-overridable
#: (HARNESS_BUBBLE_PAUSE_BASE_S / _PER_CHAR_S / _MIN_S / _MAX_S);
#: HARNESS_BUBBLE_PACING=0 turns the pauses off.
PAUSE_BASE_S = 0.5
PAUSE_PER_CHAR_S = 0.05
PAUSE_MIN_S = 0.8
PAUSE_MAX_S = 7.0


def _env_float(name: str, default: float) -> float:
    """A non-negative float from the environment; unset/blank/bad -> default."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def typing_pause_s(text: str) -> float:
    """Seconds to wait (typing shown) before sending ``text`` when it is not
    the first message of a reply. Grows with its length, clamped to
    [min, max]; 0.0 when HARNESS_BUBBLE_PACING is off."""
    if not _env_bool("HARNESS_BUBBLE_PACING", True):
        return 0.0
    base = _env_float("HARNESS_BUBBLE_PAUSE_BASE_S", PAUSE_BASE_S)
    per_char = _env_float("HARNESS_BUBBLE_PAUSE_PER_CHAR_S", PAUSE_PER_CHAR_S)
    lo = _env_float("HARNESS_BUBBLE_PAUSE_MIN_S", PAUSE_MIN_S)
    hi = max(lo, _env_float("HARNESS_BUBBLE_PAUSE_MAX_S", PAUSE_MAX_S))
    return max(lo, min(base + per_char * len(text.strip()), hi))


def bubbles_enabled() -> bool:
    return _env_bool(_BUBBLE_ENV, False)


def bubble_stream_enabled() -> bool:
    """True when backend bubble streaming is on (requires bubbles on)."""
    return _env_bool(_STREAM_ENV, False) and bubbles_enabled()


class BubbleStreamer:
    """Incremental bubble parser: feed text chunks, get complete bubbles.

    Only sentence-complete pieces are released early; :meth:`flush` releases
    whatever remains when the reply is complete.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> list[str]:
        """Append ``chunk``; return newly-completed bubbles (maybe empty)."""
        self._buf += chunk
        return self._release(final=False)

    def flush(self) -> list[str]:
        """Release all remaining text as bubbles (reply complete)."""
        return self._release(final=True)

    def _release(self, *, final: bool) -> list[str]:
        out: list[str] = []
        while True:
            boundary = re.search(r"\n\s*\n|\n", self._buf)
            if boundary is None:
                break
            head, rest = self._buf[: boundary.start()], self._buf[boundary.end():]
            head = head.strip()
            if not head:
                self._buf = rest.lstrip()
                continue
            if not rest.strip():
                # A boundary at the very end: a sentence-complete head still
                # releases, an incomplete one holds (the newline may be a wrap).
                if not final and not _sentence_complete(head):
                    break
                self._buf = rest
                out.append(head)
                continue
            if final or _sentence_complete(head):
                self._buf = rest.lstrip()
                out.append(head)
                continue
            # Boundary without sentence end and more text coming: the
            # newline may be a wrap — hold for the next chunk.
            break
        if final:
            # Tail after the last boundary (or the whole buffer when no
            # boundary exists): flush releases it, matching parse_bubbles.
            tail = self._buf.strip()
            self._buf = ""
            if tail:
                out.append(tail)
        return out


def _sentence_complete(text: str) -> bool:
    """True when ``text`` ends with sentence punctuation (trailing closers skipped)."""
    stripped = text.rstrip()
    while stripped and stripped[-1] in "\"'”’」』）)]}…—-":
        stripped = stripped[:-1].rstrip()
    return bool(stripped) and stripped[-1] in ".?!。！？।"


def parse_bubbles(text: str) -> list[str]:
    """Split model text into non-empty bubbles.

    Any run of newlines (with optional whitespace) is ONE separator. Leading and
    trailing whitespace and empty pieces are dropped; empty input returns [].
    """
    return [p.strip() for p in re.split(r"(?:\n\s*)+", text) if p.strip()]
