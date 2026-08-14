"""Per-call context reconstruction + typed-header markdown export (WS1).

Rebuilds "what the model saw" for one ``llm_call`` (or a whole conversation)
from the persisted audit payload and renders it as markdown with the typed
headers from user L393:

    #System prompt / #User / #Tool / ##{tool name} / #Thinking / #Reply

Prompt persistence is VERIFIED, not rebuilt: audit-mode stores already
persist the exact per-call payload as ``llm_calls.repro_json`` (invariant 19;
``store.rebuild_call`` reconstructs the request envelope byte-for-byte).
This module only reads and renders. Hash-only rows (non-eval runs persist no
payload by design) raise a clear error for ``--call`` and are reported
honestly per turn inside conversation exports — never faked coverage.

CLI::

    python -m harness.audit --store <path> --call <id>
    python -m harness.audit --store <path> --conversation <id> [<id> ...]
    python -m harness.audit --store <path> --conversation <id> --out <file>

Reasoning (the future ``#Thinking`` content, WS3) is read from the call
row's ``meta`` or ``repro`` ``"reasoning"`` key when present; non-reasoning
runs store nothing and render nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from harness.prompts import (
    HEADER_CONVERSATION,
    HEADER_REPLY,
    HEADER_SYSTEM,
    HEADER_THINKING,
    HEADER_TOOL,
    HEADER_TOOL_CALL,
    HEADER_USER,
)
from harness.store import SQLiteStore

#: Error message for hash-only rows (invariant 19: no faked coverage).
_HASH_ONLY_MSG = (
    "llm_call {call_id} is a hash-only row (non-eval run): no persisted "
    "payload to reconstruct (invariant 19). Re-run with audit_mode=True "
    "to persist exact per-call repro payloads."
)

#: Tolerance for matching a conversation turn to its llm_call row by t_h
#: (both come from the same clock read; the comparison is exact in practice).
_T_H_EPS = 1e-9


def _reasoning_of(row: dict[str, Any]) -> str | None:
    """Reasoning text stored on the call row (``meta`` or ``repro``), or None."""
    for source in ("meta", "repro"):
        payload = row.get(source) or {}
        if isinstance(payload, dict) and payload.get("reasoning"):
            return str(payload["reasoning"])
    return None


def _message_headers(message: dict[str, Any]) -> list[str]:
    """Typed headers for one payload message (rendering only).

    user → ``#User``; tool → ``##{tool name}`` + ``#Tool`` (the name key is
    the tool-call name the decision layer records); assistant (and any
    unknown role) → ``#Reply`` (prior replies the model saw).
    """
    role = message.get("role", "")
    if role == "user":
        return [HEADER_USER]
    if role == "tool":
        name = message.get("name") or message.get("tool_name")
        if name:
            return [HEADER_TOOL_CALL.format(tool=name), HEADER_TOOL]
        return [HEADER_TOOL]
    return [HEADER_REPLY]


def render_call_extract(store: SQLiteStore, call_id: int) -> str:
    """Markdown extract of ONE call: 'what the model saw' + what it replied.

    Raises ``KeyError`` for an unknown call id and ``ValueError`` for
    hash-only rows (invariant 19).
    """
    row = store.get_llm_call(call_id)
    if row is None:
        raise KeyError(f"llm_call {call_id} not found")
    try:
        envelope = store.rebuild_call(call_id)
    except ValueError as exc:
        raise ValueError(_HASH_ONLY_MSG.format(call_id=call_id)) from exc

    lines: list[str] = []
    meta = row.get("meta") or {}
    model = envelope.get("model") or row.get("model") or "?"
    budget = envelope.get("max_tokens")
    lines.append(
        f"# llm_call {call_id} — day {row['day']} @ t_h {float(row['t_h']):.2f}"
    )
    details = f"- model: {model}"
    if budget is not None:
        details += f" · max_tokens: {budget}"
    temp = envelope.get("temperature")
    if temp is not None:
        details += f" · temperature: {temp}"
    if meta:
        details += f" · meta: {json.dumps(meta, sort_keys=True)}"
    lines.append(details)

    lines.append("")
    lines.append(HEADER_SYSTEM)
    lines.append(str(envelope["system"]))

    for message in envelope["messages"]:
        lines.append("")
        lines.extend(_message_headers(message))
        lines.append(str(message.get("content", "")))

    thinking = _reasoning_of(row)
    if thinking:
        lines.append("")
        lines.append(HEADER_THINKING)
        lines.append(thinking)

    lines.append("")
    lines.append(HEADER_REPLY)
    lines.append(row["response"] or "")
    return "\n".join(lines)


def _calls_by_moment(store: SQLiteStore) -> dict[tuple[int, float], list[int]]:
    """chat llm_call ids indexed by (day, t_h) — the assistant reply message
    of a turn is persisted at the same (day, t_h) as its call row."""
    rows = store.conn.execute(
        "SELECT id, day, t_h FROM llm_calls WHERE role = 'chat' ORDER BY id"
    ).fetchall()
    index: dict[tuple[int, float], list[int]] = {}
    for r in rows:
        index.setdefault((int(r["day"]), float(r["t_h"])), []).append(int(r["id"]))
    return index


def _call_id_for_turn(
    store: SQLiteStore,
    index: dict[tuple[int, float], list[int]],
    day: int,
    t_h: float,
    turn_text: str,
) -> int | None:
    """The llm_call row whose (day, t_h) matches the turn's reply message.

    The call's persisted ``response`` is the companion reply text itself, so
    a candidate whose response equals ``turn_text`` is the exact call (the
    reply message is persisted before its call row, so the first id at that
    moment is the tiebreaker). None when no chat call was logged at the
    turn's moment.
    """
    candidates: list[int] = []
    for (c_day, c_t_h), ids in index.items():
        if c_day == day and abs(c_t_h - t_h) <= _T_H_EPS:
            candidates.extend(ids)
    if not candidates:
        return None
    candidates.sort()
    for cid in candidates:
        row = store.get_llm_call(cid)
        if row is not None and (row.get("response") or "") == turn_text:
            return cid
    return candidates[0]


def render_conversation_export(store: SQLiteStore, conversation_id: str) -> str:
    """One FULL conversation as markdown with typed headers.

    Each user turn renders under ``#User``; each companion turn renders the
    ``#System prompt`` + ``#Thinking`` that produced it followed by the
    reply under ``#Reply``. Turns whose call row is hash-only (non-eval run)
    render an honest note instead of a faked system prompt (invariant 19).
    Raises ``KeyError`` for an unknown conversation id.
    """
    conv = store.load_conversation(conversation_id)
    if conv is None:
        raise KeyError(f"conversation {conversation_id} not found")

    index = _calls_by_moment(store)
    lines = [
        HEADER_CONVERSATION,
        f"# Conversation {conversation_id}",
        f"- opened: t_h {float(conv.opened_t_h):.2f} · opened by {conv.opened_by}",
    ]
    if conv.closed_t_h is not None:
        lines.append(f"- closed: t_h {float(conv.closed_t_h):.2f}"
                     f" · reason: {conv.close_reason}")
    lines.append("")

    for turn in conv.turns:
        if turn.speaker == "user":
            lines.append(HEADER_USER)
            lines.append(turn.text)
            lines.append("")
            continue
        # companion turn: attach the call payload that produced it
        day = int(turn.t_h // 24.0)
        call_id = _call_id_for_turn(store, index, day, turn.t_h, turn.text)
        if call_id is None:
            lines.append(HEADER_REPLY)
            lines.append(turn.text)
            lines.append("")
            lines.append("_no llm_call row logged for this turn_")
            lines.append("")
            continue
        row = store.get_llm_call(call_id)
        assert row is not None
        if row.get("repro") is None:
            lines.append(HEADER_REPLY)
            lines.append(turn.text)
            lines.append("")
            lines.append(f"_hash-only row (llm_call {call_id}); no persisted "
                         "payload to reconstruct (invariant 19)_")
            lines.append("")
            continue
        lines.append(HEADER_SYSTEM)
        lines.append(str(row["repro"].get("system", "")))
        thinking = _reasoning_of(row)
        if thinking:
            lines.append("")
            lines.append(HEADER_THINKING)
            lines.append(thinking)
        lines.append("")
        lines.append(HEADER_REPLY)
        lines.append(turn.text)
        lines.append("")

    return "\n".join(lines).rstrip()


def render_conversations_export(store: SQLiteStore, conversation_ids: list[str]) -> str:
    """Several full conversations in one markdown document."""
    parts = [render_conversation_export(store, cid) for cid in conversation_ids]
    return "\n\n---\n\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m harness.audit --store <path> --call <id>`` or
    ``--conversation <id> [<id> ...]`` (optionally ``--out <file>``)."""
    parser = argparse.ArgumentParser(
        prog="python -m harness.audit",
        description="Reconstruct 'what the model saw' from the store and "
        "render it with typed headers (user L393).",
    )
    parser.add_argument("--store", required=True, help="path to the SQLite store")
    parser.add_argument("--call", type=int, default=None,
                        help="llm_call id to render (hash-only rows raise)")
    parser.add_argument("--conversation", nargs="+", default=None,
                        help="conversation id(s) to render into one document")
    parser.add_argument("--out", default=None,
                        help="write the markdown to this file instead of stdout")
    args = parser.parse_args(argv)
    if (args.call is None) == (args.conversation is None):
        parser.error("pass exactly one of --call <id> or --conversation <id> [...]")

    store = SQLiteStore(Path(args.store))
    try:
        if args.call is not None:
            text = render_call_extract(store, args.call)
        else:
            text = render_conversations_export(store, args.conversation)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
