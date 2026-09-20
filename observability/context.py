"""What was on the model's context, priced and ordered.

Prices the request envelope that really went out (``llm_calls.repro_json``):
the system prompt and the message list from the persisted bytes, anchored to
the provider's ``prompt_tokens``. Tool schemas are not in the envelope, so
they are reconstructed from the current code (``harness.tools``).
"""

from __future__ import annotations

import json
from typing import Any

from harness.assembler import (
    AFFECTIVE_HEADER,
    BEHAVIORAL_HEADER,
    CURRENT_INTENT_HEADER,
    PROACTIVE_OPENING,
    TEMPORAL_HEADER,
)
from harness.persona import INTEREST_SENTENCE_PREFIX
from harness.prompts import (
    ABOUT_YOU_HEADER,
    ACTIVITY_HEADER,
    AGENDA_HEADER,
    ARCS_HEADER,
    CLOSING_HEADER,
    MEMORIES_HEADER,
    MEMORY_EVIDENCE_HEADER,
    MOOD_BRIEF_HEADER,
    POPUP_MARKER_OPEN,
    STEER_TRUST_RULE,
    SYSTEM_CORE,
    TOOL_PROTOCOL,
)

from observability import tokens

#: Paragraph marker for the split of the system prompt into priced blocks.
_PARAGRAPH_SEP = "\n\n"

#: The stable core's own paragraphs, as the harness renders them today — the
#: one part of the assembly that is prose with no header to key on.
_CORE_PARAGRAPHS: frozenset[str] = frozenset(SYSTEM_CORE.split(_PARAGRAPH_SEP))

#: The steer trust rule, when the harness has one enabled ("" today).
_STEER_TRUST_PARAGRAPHS: frozenset[str] = (
    frozenset({STEER_TRUST_RULE}) if STEER_TRUST_RULE else frozenset()
)

#: The proactive block's opener ("You are reaching out first."), off the
#: template itself so a reworded template renames its block with it.
_PROACTIVE_STARTER = PROACTIVE_OPENING.split("{hook}", 1)[0].strip()

#: The labelled sections: the harness' own header constants → the name the
#: page shows. The headers are imported, never copied, so a renamed section
#: renames here; the names are this page's vocabulary.
_SECTION_NAMES: tuple[tuple[str, str], ...] = (
    (TEMPORAL_HEADER, "temporal frame"),
    (AFFECTIVE_HEADER, "affective bearing"),
    (BEHAVIORAL_HEADER, "behavioral bearing"),
    (CURRENT_INTENT_HEADER, "current intent"),
    (AGENDA_HEADER, "agenda"),
    (ACTIVITY_HEADER, "activity"),
    (ARCS_HEADER, "life arcs"),
    (MEMORIES_HEADER, "memories"),
    (MEMORY_EVIDENCE_HEADER, "memory evidence"),
    (ABOUT_YOU_HEADER, "user model"),
    (CLOSING_HEADER, "closing guidance"),
    (MOOD_BRIEF_HEADER, "mood brief"),
    # Availability is its own card section (it varies on the energy clock);
    # older runs carried it inside AFFECTIVE BEARING.
    ("Availability:", "availability"),
)

#: Retired wordings the archived runs still carry: the stable core and the
#: tool protocol were rewritten mid-trial, and these opening words are the
#: only handle left on those blocks.
_RETIRED_CORE_STARTERS: tuple[str, ...] = (
    "You are a companion with an inner life of your own.",
    "Read the card before every reply",
    "Show, do not announce",
    "Never name the internal state",
    "When the card and your character pull apart",
)
_RETIRED_STEER_STARTERS: tuple[str, ...] = (
    "Sometimes the harness delivers a real arriving event",
)


def _clip(text: str, limit: int = 160) -> str:
    blob = text.replace("\n", " ").strip()
    return blob if len(blob) <= limit else blob[: limit - 1] + "…"


def _system_kind(paragraph: str) -> str:
    """Which part of the assembled prompt one block is (display only).

    Blocks split on paragraph breaks, so one section spans several of them
    (the stable core is two paragraphs; the interest sentence is a paragraph
    of the day block). The name comes from the harness' OWN vocabulary —
    today's constants by exact match, the labelled headers as openers, the
    retired wordings archived runs carry — never from a mention inside the
    prose: the old mention-scan named two different blocks "prompt" and
    labeled a paragraph about the card as if it were the card. A block none
    of the vocabulary claims stays unlabelled rather than guessed at.
    """
    probe = paragraph.strip()
    if probe in _CORE_PARAGRAPHS or probe.startswith(_RETIRED_CORE_STARTERS):
        return "system core"
    # Both generations of the protocol open with these words.
    if probe == TOOL_PROTOCOL or probe.startswith("When a decision tool"):
        return "tool protocol"
    if probe in _STEER_TRUST_PARAGRAPHS or probe.startswith(_RETIRED_STEER_STARTERS):
        return "steer trust rule"
    for header, name in _SECTION_NAMES:
        if probe.startswith(header):
            return name
    if probe.startswith(INTEREST_SENTENCE_PREFIX):
        return "interests"
    if probe.startswith(_PROACTIVE_STARTER):
        return "proactive opening"
    if POPUP_MARKER_OPEN in probe:
        return "arriving event"
    if probe.startswith("You are "):
        return "persona voice"
    return "unlabelled"


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
            # The row shows a clipped line; the whole block rides along so
            # the drawer can show what the model actually reads.
            "text": block,
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


def provider_prefix_share(prev: dict[str, Any] | None,
                          cur: dict[str, Any]) -> tuple[float, bool, str]:
    """Leading byte-identical fraction in the order the provider renders it.

    A provider banks a prompt as one token stream: the stable system block,
    then the tool schemas when the call carries any, then the conversation. A
    system drift breaks it first; a tools/no-tools (or changed menu) pair
    breaks it AT THE SCHEMAS, so everything after is billed fresh no matter
    how identical the conversation is. Returns ``(fraction, system_stable,
    break_at)`` with ``break_at`` in ``"system" | "tools" | "messages" | ""``.

    Tool identity comes from what the envelope records (a literal ``tools``,
    or the ``tools_hash``/``tool_names`` pair). A menu difference — or a menu
    against a PROVEN empty (aux-shaped record: the fork ran tool-less by
    construction) — breaks the prefix at the schemas. An empty MAINLINE
    record is a gap, not a claim: old builds wrote None for chat legs that
    did ride the menu, so it is treated as matching. Same conventions as
    :func:`shared_message_count`: the previous envelope's trailing card block
    is dropped before the message walk.
    """
    prev = prev or {}
    prev_messages = list(prev.get("messages") or [])
    cur_messages = list(cur.get("messages") or [])
    body = prev_messages[:-1] if prev_messages else []

    def size(value: Any) -> int:
        return len(json.dumps(value, sort_keys=True, ensure_ascii=False))

    def menu_class(env: dict[str, Any]) -> tuple:
        """(class, value): ``menu`` carries schemas; ``none`` is a PROVEN
        empty menu (aux-shaped record — the fork ran tool-less by job);
        ``gap`` is an empty mainline record, which proves nothing either way."""
        tools = env.get("tools")
        if isinstance(tools, list) and tools:
            return ("menu", json.dumps(tools, sort_keys=True, ensure_ascii=False))
        if env.get("tools_hash") or env.get("tool_names"):
            return ("menu", (env.get("tools_hash"), tuple(env.get("tool_names") or ())))
        return ("none",) if "kwargs" in env else ("gap",)

    cur_system = cur.get("system")
    total_chars = sum(size(m) for m in cur_messages)
    if cur_system:
        total_chars += size(cur_system)
    cur_tools = cur.get("tools")
    cur_tools = cur_tools if isinstance(cur_tools, list) else None
    if cur_tools:
        total_chars += size(cur_tools)
    system_stable = prev.get("system") == cur_system
    if not total_chars:
        return 0.0, bool(system_stable), ""
    if not system_stable:
        return 0.0, False, "system"

    shared_chars = size(cur_system) if cur_system else 0
    index = 0
    while index < len(cur_messages) and cur_messages[index].get("role") == "system":
        if index >= len(body) or body[index] != cur_messages[index]:
            return shared_chars / total_chars, True, "system"
        shared_chars += size(cur_messages[index])
        index += 1
    prev_menu, cur_menu = menu_class(prev), menu_class(cur)
    if prev_menu[0] == "menu" and cur_menu[0] == "menu":
        if prev_menu[1] != cur_menu[1]:
            return shared_chars / total_chars, True, "tools"
    elif prev_menu[0] == "menu" or cur_menu[0] == "menu":
        # One side carried schemas; "none" proves the other did not (an aux
        # record is tool-less by construction), a gap proves nothing.
        other = cur_menu if prev_menu[0] == "menu" else prev_menu
        if other[0] == "none":
            return shared_chars / total_chars, True, "tools"
    if cur_tools:
        shared_chars += size(cur_tools)
    for offset in range(index, len(body)):
        if offset >= len(cur_messages) or cur_messages[offset] != body[offset]:
            return shared_chars / total_chars, True, "messages"
        shared_chars += size(body[offset])
    return shared_chars / total_chars, True, ""


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


def _sum_priced(values: list[int | None]) -> int | None:
    """Sum that keeps an unpriced part visible: one None makes the total None."""
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _buckets(parts: list[dict[str, Any]], priced_tools: list[dict[str, Any]],
             nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The three composition buckets, unpriced (heuristic totals only).

    A bucket holding an unpriced part carries no total: a partial sum dressed
    as a total is the same lie as a fabricated one.
    """
    return [
        {"key": "system", "label": "System prompt",
         "tokens": _sum_priced([p["tokens"] for p in parts]),
         "chars": sum(p["chars"] for p in parts), "parts": len(parts)},
        {"key": "tools", "label": "Tool schemas",
         "tokens": _sum_priced([t["tokens"] for t in priced_tools]),
         "chars": _sum_priced([t["chars"] for t in priced_tools]),
         "parts": len(priced_tools)},
        {"key": "messages", "label": "Conversation",
         "tokens": _sum_priced([n["tokens"] for n in nodes]),
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


def _share(priced: int | None, provider: int | None) -> float | None:
    """A bucket's share of the billed prompt, or None without a price anchor."""
    if not provider or priced is None:
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
    # A row shows the tool's price; the drawer shows what was priced, so the
    # schema itself rides along the way a system block's text does. Whatever
    # could not be priced has no body to show either (a recorded name whose
    # schemas this build does not hold), so it stays schema-less.
    for priced, schema in zip(priced_tools, tools or [], strict=True):
        priced["schema"] = schema if priced["tokens"] is not None else None
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
