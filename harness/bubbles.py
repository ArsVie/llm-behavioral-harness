"""Bubble splitting — model-driven, gated by HARNESS_BUBBLES.

When the flag is on the system prompt tells the model it may split a reply
into several short chat messages (bubbles) separated by a blank line.
Both a single newline and a blank line (double newline) are treated as one
separator — the model's natural blank-line style counts (WS-B parser fix).

No model call is changed when the flag is off: parity holds.
"""

from __future__ import annotations

import re
from harness.env import env_bool as _env_bool

# Instruction appended to the system prompt when bubbling is enabled.
# Plain English, no numbers, no jargon.
BUBBLE_INSTRUCTION = (
    "You can answer in more than one message. A blank line is one send. "
    "Break where a thought lands — never mid-sentence."
)

_BUBBLE_ENV = "HARNESS_BUBBLES"

#: Env flag for backend bubble streaming: when on (and bubbles are on),
#: the runtime sends each bubble as soon as its boundary parses instead
#: of waiting for the full reply. Default OFF — byte parity.
_STREAM_ENV = "HARNESS_BUBBLE_STREAM"




def bubbles_enabled() -> bool:
    return _env_bool(_BUBBLE_ENV, False)


def bubble_stream_enabled() -> bool:
    """True when backend bubble streaming is on (requires bubbles on).

    Streaming sends bubble k as soon as its boundary parses instead of
    waiting for the full reply. OFF by default: byte parity with the
    paced multi-send path.
    """
    return _env_bool(_STREAM_ENV, False) and bubbles_enabled()


class BubbleStreamer:
    """Incremental bubble parser: feed text chunks, get complete bubbles.

    A boundary is any run of newlines (with optional whitespace between),
    identical to :func:`parse_bubbles` — both ``\\n`` and ``\\n\\n`` count
    as one boundary. Only sentence-complete bubbles are released: a
    boundary followed by more text releases the piece before it when that
    piece ends with sentence punctuation (``.?!`` incl. CJK ``。！？``,
    possibly followed by closing quotes/brackets); otherwise the piece is
    held until the next chunk (a bare newline may be a line wrap, not a
    send). :meth:`flush` releases whatever text remains, split on every
    boundary — the reply is complete, so held pieces go out as-is.
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
                # A boundary at the very end carries no evidence of what
                # follows. A sentence-complete head still releases: a
                # newline is always a separator (parse_bubbles splits on any
                # newline run), so this piece's bubble membership is already
                # decided — releasing early is never premature. An
                # incomplete head holds instead: the newline may be a wrap,
                # so the piece waits for a later chunk or for flush() to
                # finalize the reply.
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
    """True when ``text`` ends with sentence punctuation.

    Trailing closers (quotes, brackets, ellipsis) are skipped so
    ``... उसके बाद।"`` still counts as complete.
    """
    stripped = text.rstrip()
    while stripped and stripped[-1] in "\"'”’」』）)]}…—-":
        stripped = stripped[:-1].rstrip()
    return bool(stripped) and stripped[-1] in ".?!。！？।"


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
    # At least one piece survives: the empty-input guard above means `text`
    # holds a non-whitespace character, and whichever piece contains it
    # strips to something non-empty. No fallback branch is reachable here.
    return [p.strip() for p in raw if p.strip() != ""]
