"""Prefix-cache accounting over persisted request envelopes.

One implementation for the offline inspector (``harness.trace``) and the
run viewer (``observability``): what a call's envelope is, how much of it
repeats the previous call in the order the provider banks it, and where the
stream stopped repeating.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any


def _size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, ensure_ascii=False))


def envelope_of(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any] | None:
    """The persisted request envelope, or None for a hash-only row.

    The metered auxiliary lane (``metering.MeteredClient``) nests the client
    kwargs - the system prompt and the tool menu among them - under
    ``kwargs``; they are hoisted to the top level (additively: ``kwargs``
    stays, and a top-level key wins).
    """
    try:
        blob = row["repro_json"]
    except (KeyError, IndexError):
        return None
    if not blob:
        return None
    try:
        loaded = json.loads(blob)
    except (ValueError, TypeError):
        return None
    if not isinstance(loaded, dict):
        return None
    kwargs = loaded.get("kwargs")
    if isinstance(kwargs, dict):
        loaded = {**kwargs, **loaded}
    return loaded


def shared_message_count(prev: dict[str, Any] | None,
                         cur: dict[str, Any]) -> tuple[int, int, bool]:
    """Leading byte-identical messages between two envelopes.

    The walk stops at the first message the current request no longer
    repeats in place. The previous request is taken WHOLE: its last message
    counts as shared when the current request carries it, which is the
    append-only invariant the mainline keeps — a consumed steer or a pruned
    block stops the walk at its own index, so nothing is dropped up front.
    Returns ``(shared_message_count, shared_chars, system_stable)``.
    """
    prev_messages = list((prev or {}).get("messages") or [])
    cur_messages = list(cur.get("messages") or [])
    shared_count = 0
    shared_chars = 0
    for index, message in enumerate(prev_messages):
        if index >= len(cur_messages) or cur_messages[index] != message:
            break
        shared_count += 1
        shared_chars += _size(message)
    system_stable = (prev or {}).get("system") == cur.get("system")
    return shared_count, shared_chars, bool(system_stable)


def provider_prefix_share(prev: dict[str, Any] | None,
                          cur: dict[str, Any]) -> tuple[float, bool, str]:
    """Leading byte-identical fraction in the order the provider renders it.

    A provider banks a prompt as one token stream: the stable system block,
    then the tool schemas when the call carries any, then the conversation. A
    system drift breaks it first; a changed menu breaks it AT THE SCHEMAS, so
    everything after is billed fresh no matter how identical the conversation
    is. Returns ``(fraction, system_stable, break_at)`` with ``break_at`` in
    ``"system" | "tools" | "messages" | ""``.

    Tool identity comes from what the envelope records: a literal ``tools``
    list. Anything else is a gap — it proves nothing either way, and the walk
    treats it as matching. The message walk matches
    :func:`shared_message_count`: the previous request is taken whole, and its
    last message breaks the prefix only when the current request does not
    carry it.
    """
    prev = prev or {}
    prev_messages = list(prev.get("messages") or [])
    cur_messages = list(cur.get("messages") or [])
    body = prev_messages

    def menu(env: dict[str, Any]) -> str | None:
        """The schemas the envelope itself holds; None is a gap."""
        tools = env.get("tools")
        if isinstance(tools, list) and tools:
            return json.dumps(tools, sort_keys=True, ensure_ascii=False)
        return None

    cur_system = cur.get("system")
    total_chars = sum(_size(m) for m in cur_messages)
    if cur_system:
        total_chars += _size(cur_system)
    cur_tools = cur.get("tools")
    cur_tools = cur_tools if isinstance(cur_tools, list) else None
    if cur_tools:
        total_chars += _size(cur_tools)
    system_stable = prev.get("system") == cur_system
    if not total_chars:
        return 0.0, bool(system_stable), ""
    if not system_stable:
        return 0.0, False, "system"

    shared_chars = _size(cur_system) if cur_system else 0
    index = 0
    while index < len(cur_messages) and cur_messages[index].get("role") == "system":
        if index >= len(body) or body[index] != cur_messages[index]:
            return shared_chars / total_chars, True, "system"
        shared_chars += _size(cur_messages[index])
        index += 1
    prev_menu, cur_menu = menu(prev), menu(cur)
    if prev_menu is not None and cur_menu is not None and prev_menu != cur_menu:
        return shared_chars / total_chars, True, "tools"
    if cur_tools:
        shared_chars += _size(cur_tools)
    for offset in range(index, len(body)):
        if offset >= len(cur_messages) or cur_messages[offset] != body[offset]:
            return shared_chars / total_chars, True, "messages"
        shared_chars += _size(body[offset])
    return shared_chars / total_chars, True, ""


def divergence(prev: dict[str, Any], cur: dict[str, Any]) -> int:
    """Index of the first message where ``cur`` stops repeating ``prev``.

    ``len(prev["messages"])`` means ``cur`` repeats all of it and appends.
    """
    pm = prev.get("messages") or []
    cm = cur.get("messages") or []
    i = 0
    while i < min(len(pm), len(cm)) and pm[i] == cm[i]:
        i += 1
    return i
