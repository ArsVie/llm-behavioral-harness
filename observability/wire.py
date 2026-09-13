"""Verbatim wire dumps: list and read a run's ``wire/`` directory.

``harness.wire`` writes one JSON body per model call; this module only finds
and reads them. Names are validated against the writer's pattern, so no
request can reach outside the wire directory.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The names ``harness.wire`` writes: ``NNNNN_YYYYMMDD-HHMMSS.json``.
_NAME_RE = re.compile(r"^[0-9]{5}_[0-9]{8}-[0-9]{6}\.json$")


def wire_dir(run_path: Path) -> Path:
    """The wire directory that belongs to a run database."""
    return Path(run_path).parent / "wire"


def wire_listing(run_path: Path) -> dict:
    """Every dump of a run, oldest first (the numeric prefix orders them)."""
    root = wire_dir(run_path)
    files: list[dict] = []
    try:
        for path in sorted(root.iterdir()):
            if _NAME_RE.match(path.name) and path.is_file():
                stat = path.stat()
                files.append({"name": path.name, "size": stat.st_size,
                              "mtime": stat.st_mtime})
    except OSError:
        files = []
    return {"dir": str(root), "files": files}


def wire_read(run_path: Path, name: str) -> str | None:
    """The raw dump text, or None when the name is unknown or unsafe."""
    if not _NAME_RE.match(name or ""):
        return None
    try:
        return (wire_dir(run_path) / name).read_text(encoding="utf-8")
    except OSError:
        return None
