"""The authored persona core, loaded from a file named by config.

The file is authoritative on every start; unset, empty, missing, unreadable or
blank all fall back to the built-in default.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Config key naming the persona file. Unset -> the Nova default.
PERSONA_FILE_ENV = "HARNESS_PERSONA_FILE"

#: Upper bound on the authored core. The whole assembled prompt is capped at
#: ``assembler.MAX_PROMPT_CHARS``; a file past this is refused, not truncated.
MAX_CORE_CHARS = 4000

#: YAML frontmatter block at the very start of the file.
_FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)

#: Markdown ATX heading lines ("# Title", "## Section").
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s.*$", re.MULTILINE)


def persona_file_path() -> Path | None:
    """The configured persona file, or None when none is configured."""
    raw = (os.environ.get(PERSONA_FILE_ENV) or "").strip()
    return Path(raw).expanduser() if raw else None


def clean_core(text: str) -> str:
    """Reduce raw file content to the prose that belongs in a prompt."""
    body = _FRONTMATTER.sub("", text or "")
    body = _HEADING.sub("", body)
    # Collapse the blank runs left by heading stripping; keep paragraph breaks.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def load_authored_core(path: Path | None = None, *, logger=None) -> str | None:
    """The authored persona core, or None to use the built-in default.

    Never raises: a configured-but-unreadable, blank or oversized file logs and
    falls back.
    """
    target = path if path is not None else persona_file_path()
    if target is None:
        return None
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        if logger is not None:
            logger(f"persona file: cannot read {target} ({exc}); using the default")
        return None
    core = clean_core(raw)
    if not core:
        if logger is not None:
            logger(f"persona file: {target} has no prose; using the default")
        return None
    if len(core) > MAX_CORE_CHARS:
        if logger is not None:
            logger(
                f"persona file: {target} is {len(core)} chars "
                f"(cap {MAX_CORE_CHARS}); using the default"
            )
        return None
    return core
