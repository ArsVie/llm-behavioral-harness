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


def _fingerprint_dump(body: dict) -> tuple | None:
    """The identity of a dump body, as the stored envelope also spells it.

    A wire body is the envelope with its system hoisted to a leading
    ``role="system"`` message, so the two are comparable without keeping
    either in full: the message count, the system text and the last message
    pin a request exactly in practice.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    head, tail = messages[0], messages[-1]
    if not isinstance(head, dict) or not isinstance(tail, dict):
        return None
    return (len(messages) - 1, str(head.get("content") or ""),
            str(tail.get("role") or ""), str(tail.get("content") or ""))


def _fingerprint_envelope(envelope: dict) -> tuple | None:
    """The same identity, read off a persisted ``repro_json`` envelope."""
    messages = envelope.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    tail = messages[-1]
    if not isinstance(tail, dict):
        return None
    return (len(messages), str(envelope.get("system") or ""),
            str(tail.get("role") or ""), str(tail.get("content") or ""))


def correlate(run_path: Path, files: list[dict],
              envelopes: list[tuple[int, dict]]) -> None:
    """Stamp each dump in ``files`` with the ``call_id`` it belongs to.

    The wire directory outlives a reset while ``llm_calls`` ids restart, and
    several decide legs can share one virtual second, so neither position nor
    timestamp identifies a dump. The bytes do.
    """
    wanted = {}
    for call_id, envelope in envelopes:
        key = _fingerprint_envelope(envelope)
        if key is not None:
            wanted.setdefault(key, call_id)
    if not wanted:
        return
    root = wire_dir(run_path)
    for entry in files:
        entry["call_id"] = None
        try:
            import json

            body = json.loads((root / entry["name"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(body, dict):
            entry["call_id"] = wanted.get(_fingerprint_dump(body))
