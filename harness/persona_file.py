"""The authored persona core, loaded from a file named by config.

Why this exists
---------------
The companion's voice — who she is, how she talks, what she calls him — is
authored prose, not something the engine can derive. It used to have nowhere
to live. ``harness.persona._build_core`` writes a generated sentence over the
sampled portfolio ("You are Nova, a warm and attentive companion with your
own days and rhythms..."), and ``assembler`` prefers the stored core over its
own ``DEFAULT_PERSONA_CORE``, so the authored voice could only ever get into
a prompt by hand-editing ``persona.core`` in the database.

That is exactly what happened, and on 2026-09-08 a DB reset erased it. Two
runs then talked to the owner as "a warm and attentive companion", closing
every turn with a question, and nothing in the tree recorded that anything
was missing — because nothing in the tree ever held the persona.

The contract
------------
* ``HARNESS_PERSONA_FILE`` names a file whose CONTENT IS THE PROMPT. It is
  read verbatim apart from stripped whitespace, an optional YAML frontmatter
  block, and markdown headings (a persona kept as ``SOUL.md`` should not
  inject "# SOUL.md — Lily Agent Personality" into a system prompt).
* Unset, empty, missing, unreadable or blank all fall back to the built-in
  Nova default. A persona file is an override, never a requirement, and a
  typo in the path must not take the companion down.
* The file is AUTHORITATIVE on every start, not just a cold one
  (:func:`harness.bootstrap.ensure_companion_initialized` recomposes the
  stored core from it). Editing the file and restarting is the whole
  workflow: no reset, no hand-patching, and no way for a reset to silently
  revert the voice.
* The authored prose does NOT replace the drawn interest sentence — the two
  are composed, authored voice first, so she still knows what this cycle's
  portfolio made her care about.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Config key naming the persona file. Unset -> the Nova default.
PERSONA_FILE_ENV = "HARNESS_PERSONA_FILE"

#: Upper bound on the authored core. The whole assembled system prompt is
#: capped at ``assembler.MAX_PROMPT_CHARS`` (12000) and the persona is one
#: section of it; a file past this is a mistake (a whole design doc pasted
#: in), so it is refused rather than silently eating the budget.
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
    # Collapse the blank runs that stripping headings leaves behind, but keep
    # paragraph breaks: the authored voice is written in paragraphs.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def load_authored_core(path: Path | None = None, *, logger=None) -> str | None:
    """The authored persona core, or None to use the built-in default.

    Never raises. A configured-but-broken file logs and falls back, because a
    bad path must not be the difference between a companion and no companion.
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
