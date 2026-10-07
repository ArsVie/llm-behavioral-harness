"""What was on the model's context, priced and ordered.

Prices the request envelope that really went out (``llm_calls.repro_json``):
the system prompt and the message list from the persisted bytes, anchored to
the provider's ``prompt_tokens``. Tool schemas are not in the envelope, so
they are reconstructed from the current code (``harness.tools``).
"""

from __future__ import annotations

import calendar
import json
import re
from typing import Any

from harness.assembler import (
    AFFECTIVE_HEADER,
    BEHAVIORAL_HEADER,
    CURRENT_INTENT_HEADER,
    PROACTIVE_OPENING,
    TEMPORAL_HEADER,
    is_state_update,
)
from harness.cache_audit import shared_message_count
from harness.prompts import (
    CALL_END_OPENER,
    CALL_START_OPENER,
    ABOUT_YOU_HEADER,
    ACTIVITY_HEADER,
    AGENDA_HEADER,
    ARCS_HEADER,
    CLOSING_HEADER,
    FICTION_FRAME,
    MEMORIES_HEADER,
    MEMORY_EVIDENCE_HEADER,
    MOOD_BRIEF_HEADER,
    POPUP_MARKER_OPEN,
    PRESENCE_END_OPENER,
    PRESENCE_START_OPENER,
    STEER_TRUST_RULE,
    SYSTEM_CORE_PARAGRAPHS,
    TOOL_PROTOCOL,
    WRITING_HEADER,
)
from harness.steering import STEER_MARKER_LEAD
from observability import tokens

#: Paragraph marker for the split of the system prompt into priced blocks.
_PARAGRAPH_SEP = "\n\n"

#: The stable core's own paragraphs → their names, as the harness renders them
#: today — the one part of the assembly that is prose with no header to key on.
_CORE_PARAGRAPHS: dict[str, str] = {
    **{text: name for name, text in SYSTEM_CORE_PARAGRAPHS},
    FICTION_FRAME: "fiction frame",
}

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
_RETIRED_CORE_STARTERS: tuple[tuple[str, str], ...] = (
    ("You are a companion with an inner life of your own.", "core: an inner life"),
    ("Read the card before every reply", "card: read it every reply"),
    ("Show, do not announce", "card: show, don't announce"),
    ("Never name the internal state", "card: never name the state"),
    ("When the card and your character pull apart", "card: your character wins"),
    ("These days you are absorbed in", "interests"),
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
    if probe in _CORE_PARAGRAPHS:
        return _CORE_PARAGRAPHS[probe]
    for starter, name in _RETIRED_CORE_STARTERS:
        if probe.startswith(starter):
            return name
    # Both generations of the protocol open with these words.
    if probe == TOOL_PROTOCOL or probe.startswith("When a decision tool"):
        return "tool protocol"
    if probe in _STEER_TRUST_PARAGRAPHS or probe.startswith(_RETIRED_STEER_STARTERS):
        return "steer trust rule"
    for header, name in _SECTION_NAMES:
        if probe.startswith(header):
            return name
    if probe.startswith(_PROACTIVE_STARTER):
        return "proactive opening"
    if POPUP_MARKER_OPEN in probe:
        return "arriving event"
    if probe.startswith("You are "):
        return "persona voice"
    return "unlabelled"


def system_parts(system: str) -> list[dict[str, Any]]:
    """The system prompt as ordered priced blocks.

    A section spanning several paragraphs (a persona written in paragraphs)
    names each one "name · i/n", so no two rows read the same.
    """
    blocks = [b.strip() for b in (system or "").split(_PARAGRAPH_SEP)]
    blocks = [b for b in blocks if b]
    kinds = [_system_kind(block) for block in blocks]
    totals = {kind: kinds.count(kind) for kind in kinds}
    seen: dict[str, int] = {}
    names = []
    for kind in kinds:
        seen[kind] = seen.get(kind, 0) + 1
        names.append(kind if totals[kind] == 1 else f"{kind} · {seen[kind]}/{totals[kind]}")
    return [
        {
            "kind": name,
            "label": _clip(block.splitlines()[0] if block else "", 90),
            "tokens": tokens.estimate_text(block),
            "chars": len(block),
            "preview": _clip(block, 400),
            # The row shows a clipped line; the whole block rides along so
            # the drawer can show what the model actually reads.
            "text": block,
        }
        for block, name in zip(blocks, names, strict=True)
    ]


#: How a day's plan row opens: ``assembler.day_plan_header`` dates it with
#: the real weekday ("Lily's Saturday plan:", "Saturday's plan:" before
#: 2026-10-03), and an unanchored run keeps the bare agenda header.
_DAY_PLAN_OPENER = re.compile(
    rf"^(?:[^\n:]*'s )?(?:{'|'.join(calendar.day_name)})(?:'s)? plan:"
    rf"|^{re.escape(AGENDA_HEADER)}")

#: What a message in the request is, in the context bar's order: the key, the
#: row's own name, and the bar's plural.
MESSAGE_KINDS: tuple[tuple[str, str, str], ...] = (
    ("day_plan", "day plan", "day plans"),
    ("state_card", "state card", "state cards"),
    ("card_update", "card update", "card updates"),
    ("writing", "writing guidance", "writing guidance"),
    ("closing", "closing guidance", "closing guidance"),
    ("event", "arriving event", "arriving events"),
    ("voice_call", "voice call", "voice call start/end"),
    ("presence", "presence", "presence"),
    ("his", "user message", "his messages"),
    ("hers", "companion reply", "her replies"),
    ("tool_call", "tool call", "tool calls"),
    ("tool_result", "tool result", "tool results"),
)
_KIND_LABEL = {key: label for key, label, _ in MESSAGE_KINDS}


def _system_row_kind(content: Any) -> str:
    text = str(content or "").lstrip()
    if text.startswith(WRITING_HEADER):
        return "writing"
    if text.startswith(CLOSING_HEADER):
        return "closing"
    if STEER_MARKER_LEAD in text:
        return "event"
    if _DAY_PLAN_OPENER.match(text):
        return "day_plan"
    if text.startswith((CALL_START_OPENER, CALL_END_OPENER)):
        return "voice_call"
    if text.startswith((PRESENCE_START_OPENER, PRESENCE_END_OPENER)):
        return "presence"
    if is_state_update(text):
        return "card_update"
    return "state_card"


def message_kind(message: dict[str, Any]) -> str:
    """Which part of the request one message is (a ``MESSAGE_KINDS`` key)."""
    role = str(message.get("role") or "?")
    if role == "system":
        return _system_row_kind(message.get("content"))
    if role == "tool":
        return "tool_result"
    if role == "assistant":
        return "tool_call" if message.get("tool_calls") else "hers"
    return "his"


def system_row_label(content: Any) -> str:
    """What one system-role message is, named by the harness' own openers."""
    return _KIND_LABEL[_system_row_kind(content)]


def message_nodes(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """The ordered message surface, each node priced.

    Cache marks are not set here: they depend on numbers that only exist
    after pricing (``mark_provider_states`` / ``mark_shared_states``).
    """
    nodes: list[dict[str, Any]] = []
    for index, message in enumerate(envelope.get("messages") or []):
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        nodes.append({
            "index": index,
            "role": message.get("role"),
            "kind": message_kind(message),
            "label": _KIND_LABEL[message_kind(message)],
            "tokens": tokens.estimate_message(message),
            "chars": len(text or ""),
            "tool_calls": [tc.get("function", {}).get("name") or tc.get("name")
                           for tc in (message.get("tool_calls") or [])],
            # The full text rides along so the drawer can show the reply; the
            # preview is only for the row in the list.
            "content": text or "",
            "preview": _clip(text or "", 220),
        })
    return nodes


def mark_provider_states(nodes: list[dict[str, Any]], fresh_tokens: int) -> None:
    """Mark ``state`` per node from the provider's own split.

    Prefix caching serves a prefix, so the reported fresh tokens are the
    SUFFIX of the request: walk the priced messages from the newest one until
    the fresh amount is spent. The message the provider's block boundary cuts
    is ``partial`` — its earlier bytes came from cache and its tail was
    re-billed fresh; everything older is ``cached``.
    """
    remaining = max(int(fresh_tokens or 0), 0)
    for node in reversed(nodes):
        size = int(node.get("priced_tokens") or node.get("tokens") or 0)
        if remaining <= 0:
            node["state"] = "cached"
        elif size <= remaining:
            node["state"] = "fresh"
            remaining -= size
        else:
            node["state"] = "partial"
            remaining = 0


def mark_shared_states(nodes: list[dict[str, Any]], shared_count: int) -> None:
    """Byte-walk marking: the fallback when a call recorded no usage numbers.

    ``shared_count`` leading messages of the current request are
    byte-identical to the call before — "cacheable", not "was cached": with
    no provider split on the record, shared is the strongest claim available.
    """
    for node in nodes:
        node["state"] = "cached" if node["index"] < shared_count else "fresh"


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


def _composition(buckets: list[dict[str, Any]], nodes: list[dict[str, Any]],
                 provider: int | None) -> list[dict[str, Any]]:
    """The bar's segments: the system prompt, the tool schemas, then the
    conversation split by what each message is (``MESSAGE_KINDS`` order).

    Runs after pricing, so the conversation's segments sum to its bucket.
    """
    by_key = {bucket["key"]: bucket for bucket in buckets}
    segments = [
        {"key": key, "label": by_key[key]["label"],
         "tokens": by_key[key]["priced_tokens"], "count": by_key[key]["parts"],
         "share_pct": by_key[key]["share_pct"]}
        for key in ("system", "tools")
    ]
    for key, _, plural in MESSAGE_KINDS:
        members = [node for node in nodes if node["kind"] == key]
        if not members:
            continue
        priced = sum(node["priced_tokens"] for node in members)
        segments.append({"key": key, "label": plural, "tokens": priced,
                         "count": len(members), "share_pct": _share(priced, provider)})
    return segments


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
    # schema itself rides along the way a system block's text does. An entry
    # with no schema body (a name-only leftover in an old store) carries no
    # price and no body to show.
    for priced, schema in zip(priced_tools, tools or [], strict=True):
        priced["schema"] = schema if priced["tokens"] is not None else None
    nodes = message_nodes(envelope)
    buckets = _buckets(parts, priced_tools, nodes)
    anchor = _anchor_for(buckets, usage)
    _price_in_place(parts, nodes, buckets, anchor)
    pressure = _pressure(usage, anchor.provider_tokens, context_window)
    # The provider's own split decides the marks when the call recorded one:
    # prefix caching serves a prefix, so its fresh tokens are this stream's
    # suffix, on the provider's block grid. Without usage numbers the byte
    # walk against the call before is the only story available.
    if pressure["provider_prompt_tokens"]:
        mark_provider_states(nodes, pressure["fresh_tokens"])
        cache_source = "provider"
    else:
        mark_shared_states(nodes, shared_count)
        cache_source = "bytes"
    cached_messages = sum(1 for node in nodes if node["state"] == "cached")
    partial_message = next(
        (node["index"] for node in nodes if node["state"] == "partial"), None)
    return {
        "available": True,
        "context_window": context_window,
        "anchor": anchor.to_json(),
        "pressure": pressure,
        "buckets": buckets,
        "composition": _composition(buckets, nodes, anchor.provider_tokens),
        "surface": nodes,
        "system_parts": parts,
        "tools": priced_tools,
        "tools_source": tools_source,
        "prefix": {
            "shared_messages": cached_messages,
            "shared_chars": shared_chars,
            "system_stable": system_stable,
            "total_messages": len(nodes),
            # "provider": marks come from the call's own usage split;
            # "bytes": from the byte walk (no usage on the record).
            "source": cache_source,
            "partial_message": partial_message,
            "fresh_tokens": (pressure["fresh_tokens"]
                             if cache_source == "provider" else None),
        },
    }
