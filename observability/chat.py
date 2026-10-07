"""The /chat page's read model: the live conversation, as he and she wrote it.

Read-only, like the rest of observability. The page's writes (what he types,
the call button) go to the bot's web channel (``harness.channels.web``);
``proxy`` forwards them, so the page talks to one origin.

Interface:
    history(conn, anchor, after=0, limit=HISTORY_MAX) -> {"messages": [...], "latest": id}
        user and assistant rows only (never the system rows: the state card,
        the writing block), oldest first. With ``after`` only newer rows.
    proxy(method, path, body) -> (status, bytes)   to the bot's /web/* API
    chat_db() -> Path          HARNESS_CHAT_DB, else the live run's database
    photo(name) -> Path | None a photo he sent (``images/`` next to chat_db)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from harness import images
from harness.faces import mod_blush, mod_scene, paragraph_faces, split_face_mods
from observability.calls import stamp
from observability.db import repo_root

#: Rows the page loads when it opens (it then only asks for newer ones).
HISTORY_MAX = 300

#: The bot's web channel (``harness.channels.web``).
WEB_API = "http://127.0.0.1:{port}"


def chat_db() -> Path:
    raw = os.environ.get("HARNESS_CHAT_DB", "").strip()
    return Path(raw) if raw else repo_root() / "results/live-companion/companion.db"


def photo(name: str) -> Path | None:
    """The stored photo behind a reference (:mod:`harness.images`), or None."""
    if not re.fullmatch(r"[0-9a-f]{64}\.jpg", name):
        return None
    target = chat_db().parent / "images" / name
    return target if target.is_file() else None


def history(conn: sqlite3.Connection, anchor: Any, *, after: int = 0,
            limit: int = HISTORY_MAX) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id, role, content, t_h, proactive, meta FROM messages "
        "WHERE role IN ('user', 'assistant') AND id > ? "
        "ORDER BY id DESC LIMIT ?", (after, limit)).fetchall()
    messages = []
    for r in reversed(rows):
        # her rows open with her face tag (harness.faces), and a later
        # paragraph can carry its own: the page shows the text and plays each
        # paragraph's face instead of guessing one
        faces: list[str | None] = []
        face, text, blush, scene = None, r[2], None, None
        if r[1] == "assistant":
            parts = paragraph_faces(r[2] or "")
            faces = [f for f, _ in parts]
            face, mods, _ = split_face_mods(r[2] or "")
            blush = mod_blush(mods)
            blush = blush.split(":", 1)[1] if blush else None
            scene = mod_scene(mods)
            text = "\n\n".join(t for _, t in parts)
        messages.append({"id": r[0], "who": "him" if r[1] == "user" else "her",
                         "text": text, "face": face, "faces": faces, "blush": blush,
                         "scene": scene,
                         "at": stamp(anchor, r[3]), "proactive": bool(r[4]),
                         "photos": images.of({"meta": r[5]})})
    latest = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0
    return {"messages": messages, "latest": int(latest)}


def proxy(method: str, path: str, body: bytes | None = None,
          *, timeout: float = 5.0) -> tuple[int, bytes]:
    """Forward one request to the bot's web API. 502 when it is not up."""
    from harness.channels.web import web_port

    req = urllib.request.Request(
        WEB_API.format(port=web_port()) + path, data=body, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return 502, json.dumps({"error": f"the bot is not reachable ({exc})"}).encode()
