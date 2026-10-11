"""The /chat page's read model: the live conversation, as he and she wrote it.

Read-only, like the rest of observability. The page's writes (what he types,
the call button) go to the bot's web channel (``harness.channels.web``);
``proxy`` forwards them, so the page talks to one origin.

Interface:
    history(conn, anchor, after=0, after_event=0, limit=HISTORY_MAX)
        -> {"messages": [...], "latest": id}
        his and her lines plus the call marks (started / ended / missed),
        oldest first. The lines between a call's start and end carry its id
        (``call``): the page folds them into the call's card. The state card
        and every other system row stay out.
        With ``after`` / ``after_event`` only newer rows of each source; a
        mark's ``src`` names the table its id belongs to ("m"essages /
        state_``e``vents).
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
from harness.prompts import (CALL_END_ROW, CALL_HUNG_UP_RINGING_ROW,
                             CALL_START_ROW)
from observability.calls import stamp
from observability.db import repo_root

#: Rows the page loads when it opens (it then only asks for newer ones).
HISTORY_MAX = 300

#: The harness' call rows in the stream (``harness.prompts``): the page turns
#: them into the call cards. The openers are derived from the row templates,
#: so a wording change moves the match with it.
_CALL_START_OPENER = CALL_START_ROW.split("{time}")[0]
_CALL_END_OPENER = CALL_END_ROW.split("{start}")[0]
_HUNG_UP_OPENER = CALL_HUNG_UP_RINGING_ROW.split("{time}")[0]

#: ``Call ended, 21:46–21:51 (5 min)`` — the times and duration the row shows.
_CALL_END_TIMES = re.compile(re.escape(_CALL_END_OPENER)
                             + r"(\d{1,2}:\d{2})\u2013(\d{1,2}:\d{2}) \((\d+) min\)")

#: state_events that mean his call never connected.
_MISSED_EVENTS = ("call_decided", "call_decision_failed", "call_stale")

#: Mark order inside one virtual instant: a mark opens before the lines it
#: explains and closes after them (his and her lines sit between).
_MARK_RANK = {"call_started": 0, "missed_call": 0, "call_ended": 2}

#: An unpaired call start folds the lines after it for at most this many
#: virtual hours: a live call keeps folding, a crashed one cannot swallow
#: the evening.
_OPEN_CALL_HOURS = 3.0

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


def _mark(anchor: Any, src: str, row_id: int, t_h: Any, kind: str, *,
          sub: str | None = None, start: str | None = None,
          end: str | None = None, minutes: int | None = None,
          call: int | None = None) -> dict[str, Any]:
    """One call mark as the page reads it (``src`` names its table).

    ``call`` is the id of the call's start row: the lines between start and
    end carry the same id, and the page folds them into one card. None on a
    missed call.
    """
    # t_h stays raw: the merge in history() sorts marks against messages on
    # it, and a rounded display value would push a same-instant mark after
    # the line it explains (event and message share the runtime's float).
    return {"src": src, "id": int(row_id), "kind": kind, "who": None,
            "t_h": float(t_h or 0.0), "at": stamp(anchor, t_h),
            "sub": sub, "start": start, "end": end, "minutes": minutes,
            "call": call}


def _call_windows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The calls as start…end windows, oldest first.

    Pairing runs over every call row, whatever the cursor, so an end finds
    its start even when the start row is older than ``after``. A start with
    no end is open: its fold is bounded by the next call row and
    ``_OPEN_CALL_HOURS``. An end with no start stays a window of its own —
    nothing to fold, still a card.
    """
    rows = conn.execute(
        "SELECT id, content, t_h FROM messages WHERE role = 'system' AND "
        "(content LIKE ? OR content LIKE ? OR content LIKE ?) ORDER BY id",
        (_CALL_START_OPENER + "%", _CALL_END_OPENER + "%",
         _HUNG_UP_OPENER + "%")).fetchall()
    windows: list[dict[str, Any]] = []
    open_w: dict[str, Any] | None = None
    for row_id, content, t_h in rows:
        text = content or ""
        if text.startswith(_CALL_START_OPENER):
            if open_w is not None:
                windows.append(open_w)          # the earlier start never ended
            open_w = {"start_id": int(row_id), "start_t_h": float(t_h or 0.0),
                      "end_id": None, "end_t_h": None, "minutes": None,
                      "start": None, "end": None}
        elif text.startswith(_CALL_END_OPENER):
            times = _CALL_END_TIMES.match(text)
            window: dict[str, Any] = open_w if open_w is not None else {
                "start_id": None, "start_t_h": None, "end_id": None,
                "end_t_h": None, "minutes": None, "start": None, "end": None}
            window.update(
                end_id=int(row_id), end_t_h=float(t_h or 0.0),
                minutes=int(times.group(3)) if times else None,
                start=times.group(1) if times else None,
                end=times.group(2) if times else None)
            windows.append(window)
            open_w = None
        elif open_w is not None:
            open_w["next_id"] = int(row_id)      # a hung-up bounds an open call
    if open_w is not None:
        windows.append(open_w)
    call_ids = [int(r[0]) for r in rows]
    for w in windows:
        if w["end_id"] is None:
            later = [i for i in call_ids if i > w["start_id"]]
            w["next_id"] = later[0] if later else None
    return windows


def _window_for(windows: list[dict[str, Any]], row_id: int,
                t_h: Any) -> dict[str, Any] | None:
    """The call window a line sits inside, or None."""
    hit: dict[str, Any] | None = None
    for w in windows:
        if w["start_id"] is None or row_id <= w["start_id"]:
            continue
        if w["end_id"] is not None:
            if row_id < w["end_id"]:
                hit = w
            continue
        if w.get("next_id") is not None and row_id >= w["next_id"]:
            continue
        if (t_h or 0.0) - (w["start_t_h"] or 0.0) > _OPEN_CALL_HOURS:
            continue
        hit = w
    return hit


def _call_marks(conn: sqlite3.Connection, anchor: Any, *,
                windows: list[dict[str, Any]], after: int, after_event: int,
                limit: int) -> list[dict[str, Any]]:
    """The conversation's call marks: started, ended and missed.

    Start and end ride the harness' own system rows, paired into windows;
    the lines between them fold into the page's card under the window's
    ``call`` id. A ring-out leaves no row of its own, so its mark comes from
    the ``state_events`` log instead — one source per mark, never both.
    """
    marks: list[dict[str, Any]] = []
    for w in windows:
        if w["start_id"] is not None and w["start_id"] > after:
            marks.append(_mark(anchor, "m", w["start_id"], w["start_t_h"],
                               "call_started", call=w["start_id"]))
        if w["end_id"] is not None and w["end_id"] > after:
            marks.append(_mark(anchor, "m", w["end_id"], w["end_t_h"],
                               "call_ended", call=w["start_id"],
                               start=w["start"], end=w["end"],
                               minutes=w["minutes"]))
    for row_id, t_h in conn.execute(
            "SELECT id, t_h FROM messages WHERE role = 'system' AND id > ? "
            "AND content LIKE ? ORDER BY id DESC LIMIT ?",
            (after, _HUNG_UP_OPENER + "%", limit)).fetchall():
        marks.append(_mark(anchor, "m", row_id, t_h, "missed_call",
                           sub="hung up while ringing"))
    for row_id, event, detail, t_h in conn.execute(
            "SELECT id, event, detail, t_h FROM state_events WHERE id > ? "
            "AND event IN (?, ?, ?) ORDER BY id DESC LIMIT ?",
            (after_event, *_MISSED_EVENTS, limit)).fetchall():
        if event == "call_decided" and not (detail or "").endswith("answer=False"):
            continue  # a pick-up: the call-start row already marks it
        marks.append(_mark(anchor, "e", row_id, t_h, "missed_call"))
    return marks


def history(conn: sqlite3.Connection, anchor: Any, *, after: int = 0,
            after_event: int = 0, limit: int = HISTORY_MAX) -> dict[str, Any]:
    windows = _call_windows(conn)
    rows = conn.execute(
        "SELECT id, role, content, t_h, proactive, meta FROM messages "
        "WHERE role IN ('user', 'assistant') AND id > ? "
        "ORDER BY id DESC LIMIT ?", (after, limit)).fetchall()
    keyed: list[tuple[tuple[float, int, str, int], dict[str, Any]]] = []
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
        entry = {"src": "m", "id": r[0], "who": "him" if r[1] == "user" else "her",
                 "text": text, "face": face, "faces": faces, "blush": blush,
                 "scene": scene,
                 "at": stamp(anchor, r[3]), "proactive": bool(r[4]),
                 "photos": images.of({"meta": r[5]})}
        window = _window_for(windows, r[0], r[3])
        if window is not None:
            entry["call"] = window["start_id"]  # the page folds it into the card
        keyed.append(((float(r[3] or 0.0), 1, "m", int(r[0])), entry))
    for mark in _call_marks(conn, anchor, windows=windows, after=after,
                            after_event=after_event, limit=limit):
        key = (float(mark["t_h"] or 0.0), _MARK_RANK.get(mark["kind"], 1),
               mark["src"], mark["id"])
        keyed.append((key, mark))
    keyed.sort(key=lambda pair: pair[0])
    latest = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0
    return {"messages": [entry for _, entry in keyed], "latest": int(latest)}


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
