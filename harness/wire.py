"""Verbatim wire dump: the exact payload sent to the model, per call.

``save()`` is called by the client right before a request goes out and writes
the body it is about to send to ``<wire dir>/NNNNN_<stamp>.json``, plus one
line per dump to ``index.jsonl``. The body values are verbatim; indentation is
ours so two dumps diff cleanly line by line. Nothing here may ever raise into
a call path.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_DIR: Path | None = None
_SEQ = 0


def configure(path: str | os.PathLike | None) -> None:
    """Point the dump at ``path`` (env ``HARNESS_WIRE_DIR`` overrides)."""
    global _DIR, _SEQ
    if path is None:
        _DIR = None
        return
    d = Path(path)
    try:
        d.mkdir(parents=True, exist_ok=True)
        names = sorted(p.name for p in d.glob("[0-9]*.json"))
        _SEQ = int(names[-1].split("_", 1)[0]) if names else 0
    except OSError:
        _DIR = None
        return
    _DIR = d


def _dir() -> Path | None:
    env = os.environ.get("HARNESS_WIRE_DIR")
    if env:
        return Path(env)
    return _DIR


def save(payload: dict) -> None:
    """Dump one request body. Never raises; no dump without a configured dir."""
    global _SEQ
    try:
        d = _dir()
        if d is None:
            return
        d.mkdir(parents=True, exist_ok=True)
        _SEQ += 1
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        name = f"{_SEQ:05d}_{stamp}.json"
        d.joinpath(name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tools = payload.get("tools") or []
        names = [((t.get("function") or t).get("name") or "") for t in tools]
        with d.joinpath("index.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "file": name, "ts": time.time(),
                "model": payload.get("model"),
                "messages": len(payload.get("messages") or []),
                "tools": names,
            }) + "\n")
    except Exception:  # noqa: BLE001 - a dump must never break a call
        pass
