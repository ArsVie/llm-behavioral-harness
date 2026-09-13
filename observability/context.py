"""What was on the model's context, priced and ordered.

Prices the request envelope that really went out (``llm_calls.repro_json``):
the system prompt and the message list from the persisted bytes, anchored to
the provider's ``prompt_tokens``. Tool schemas are not in the envelope, so
they are reconstructed from the current code (``harness.tools``).
"""

from __future__ import annotations

import json
from typing import Any

from observability import tokens

#: Paragraph marker for the split of the system prompt into priced blocks.
_PARAGRAPH_SEP = "\n\n"


def _clip(text: str, limit: int = 160) -> str:
    blob = text.replace("\n", " ").strip()
    return blob if len(blob) <= limit else blob[: limit - 1] + "…"


def _system_kind(paragraph: str) -> str:
    """A coarse label for one system-prompt block (display only)."""
    probe = paragraph.lower()
    if "state card" in probe:
        return "state card"
    if "decision tool" in probe:
        return "decision tools"
    if probe.startswith("you are"):
        return "persona & voice"
    if "plan:" in probe:
        return "internal card"
    return "prompt"


def system_parts(system: str) -> list[dict[str, Any]]:
    """The system prompt as ordered priced blocks."""
    blocks = [b.strip() for b in (system or "").split(_PARAGRAPH_SEP)]
    return [
        {
            "kind": _system_kind(block),
            "label": _clip(block.splitlines()[0] if block else "", 90),
            "tokens": tokens.estimate_text(block),
            "chars": len(block),
            "preview": _clip(block, 400),
        }
        for block in blocks
        if block
    ]


def _message_label(message: dict[str, Any], index: int) -> str:
    role = str(message.get("role") or "?")
    if role == "system":
        return "internal card"
    if role == "tool":
        return "tool result"
    if role == "assistant" and message.get("tool_calls"):
        return "tool call"
    if role == "assistant":
        return "companion reply"
    return "user message"


def shared_message_count(prev: dict[str, Any] | None,
                         cur: dict[str, Any]) -> tuple[int, int, bool]:
    """Leading byte-identical messages between two envelopes.

    Mirrors ``harness.trace._prefix_share``: the previous envelope's trailing
    block is the per-turn card, so it is dropped before comparison. Returns
    ``(shared_message_count, shared_chars, system_stable)``.
    """
    prev_messages = list((prev or {}).get("messages") or [])
    cur_messages = list(cur.get("messages") or [])
    body = prev_messages[:-1] if prev_messages else []
    shared_count = 0
    shared_chars = 0
    for index, message in enumerate(body):
        if index >= len(cur_messages) or cur_messages[index] != message:
            break
        shared_count += 1
        shared_chars += len(json.dumps(message, sort_keys=True, ensure_ascii=False))
    system_stable = (prev or {}).get("system") == cur.get("system")
    return shared_count, shared_chars, bool(system_stable)


def message_nodes(envelope: dict[str, Any], shared_count: int) -> list[dict[str, Any]]:
    """The ordered message surface, each node priced and cache-marked."""
    nodes: list[dict[str, Any]] = []
    for index, message in enumerate(envelope.get("messages") or []):
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        nodes.append({
            "index": index,
            "role": message.get("role"),
            "label": _message_label(message, index),
            "tokens": tokens.estimate_message(message),
            "chars": len(text or ""),
            "cached": index < shared_count,
            "tool_calls": [tc.get("function", {}).get("name") or tc.get("name")
                           for tc in (message.get("tool_calls") or [])],
            # The full text rides along so the drawer can show the reply; the
            # preview is only for the row in the list.
            "content": text or "",
            "preview": _clip(text or "", 220),
        })
    return nodes


def _buckets(parts: list[dict[str, Any]], priced_tools: list[dict[str, Any]],
             nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The three composition buckets, unpriced (heuristic totals only)."""
    return [
        {"key": "system", "label": "System prompt",
         "tokens": sum(p["tokens"] for p in parts),
         "chars": sum(p["chars"] for p in parts), "parts": len(parts)},
        {"key": "tools", "label": "Tool schemas",
         "tokens": sum(t["tokens"] for t in priced_tools),
         "chars": sum(t["chars"] for t in priced_tools), "parts": len(priced_tools)},
        {"key": "messages", "label": "Conversation",
         "tokens": sum(n["tokens"] for n in nodes),
         "chars": sum(n["chars"] for n in nodes), "parts": len(nodes)},
    ]


def _anchor_for(buckets: list[dict[str, Any]], usage: dict[str, Any]) -> tokens.Anchor:
    """The provider anchor for the persisted surface (the tool bucket excluded).

    The tool schemas are code-reconstructed, and the provider's prompt total
    already covers them, so folding their price into the anchor would scale
    every other bucket by a share the provider never charged separately.
    """
    provider = int(usage.get("prompt_tokens") or 0) or None
    persisted = sum(b["tokens"] for b in buckets if b["key"] != "tools")
    return tokens.reconcile(persisted, provider)


def _share(priced: int, provider: int | None) -> float | None:
    """A bucket's share of the billed prompt, or None without a provider anchor."""
    if not provider:
        return None
    return round(priced / provider * 100.0, 1)


def _price_in_place(parts: list[dict[str, Any]], nodes: list[dict[str, Any]],
                    buckets: list[dict[str, Any]], anchor: tokens.Anchor) -> None:
    """Write ``priced_tokens``/``share_pct`` onto parts, nodes and buckets.

    A bucket's price is the sum of its priced parts, so the bar, the legend
    and the node list can never disagree by a rounding token.
    """
    for part, price in zip(parts, tokens.price([p["tokens"] for p in parts], anchor),
                           strict=True):
        part["priced_tokens"] = price
    for node, price in zip(nodes, tokens.price([n["tokens"] for n in nodes], anchor),
                           strict=True):
        node["priced_tokens"] = price
    priced_by_key = {
        "system": sum(part["priced_tokens"] for part in parts),
        "messages": sum(node["priced_tokens"] for node in nodes),
    }
    provider = anchor.provider_tokens
    for bucket in buckets:
        priced = priced_by_key.get(bucket["key"])
        bucket["priced_tokens"] = bucket["tokens"] if priced is None else priced
        bucket["share_pct"] = _share(bucket["priced_tokens"], provider)


def _pressure(usage: dict[str, Any], provider: int | None,
              context_window: int) -> dict[str, Any]:
    """The headline figures: what was billed, cached, fresh and full."""
    cached_tokens = int(usage.get("cached_tokens") or 0)
    miss_tokens = int(usage.get("cache_miss_tokens") or 0)
    # A miss count that does not sum to the prompt total: the fresh share
    # is the remainder and the mismatch is reported for the front-end.
    if provider is None or cached_tokens + miss_tokens == provider:
        ledger_ok = True
        fresh_tokens = miss_tokens
    else:
        ledger_ok = False
        fresh_tokens = max(provider - cached_tokens, 0)
    return {
        "provider_prompt_tokens": provider,
        "cached_tokens": cached_tokens,
        "cache_miss_tokens": miss_tokens,
        "fresh_tokens": fresh_tokens,
        "ledger_ok": ledger_ok,
        "cache_hit_pct": (round(cached_tokens / provider * 100.0, 1)
                          if provider else None),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "saturation_pct": (round(provider / context_window * 100.0, 2)
                           if provider and context_window else None),
    }


def build_context(*, envelope: dict[str, Any] | None,
                  tools: list[dict[str, Any]] | None,
                  tools_source: str,
                  previous: dict[str, Any] | None,
                  context_window: int,
                  usage: dict[str, Any]) -> dict[str, Any]:
    """The full context-composition payload for the front-end.

    ``usage`` carries the call's provider numbers (``prompt_tokens``,
    ``cached_tokens``, ``cache_miss_tokens``, ``completion_tokens``).
    """
    if envelope is None:
        return {"available": False,
                "reason": "no persisted request envelope for this call "
                          "(non-audit store, invariant 19)",
                "context_window": context_window}

    shared_count, shared_chars, system_stable = shared_message_count(previous, envelope)
    parts = system_parts(str(envelope.get("system") or ""))
    priced_tools = tokens.estimate_tools(tools)
    nodes = message_nodes(envelope, shared_count)
    buckets = _buckets(parts, priced_tools, nodes)
    anchor = _anchor_for(buckets, usage)
    _price_in_place(parts, nodes, buckets, anchor)
    return {
        "available": True,
        "context_window": context_window,
        "anchor": anchor.to_json(),
        "pressure": _pressure(usage, anchor.provider_tokens, context_window),
        "buckets": buckets,
        "surface": nodes,
        "system_parts": parts,
        "tools": priced_tools,
        "tools_source": tools_source,
        "prefix": {
            "shared_messages": shared_count,
            "shared_chars": shared_chars,
            "system_stable": system_stable,
            "total_messages": len(nodes),
        },
    }
