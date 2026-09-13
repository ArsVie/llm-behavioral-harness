"""Read model over ``llm_calls``: usage, prefix cache and context provenance.

Every number here is either the provider's own (the persisted usage columns)
or the harness' own (``harness.spend``'s formula, ``harness.tools``' schemas).
"""

from __future__ import annotations

import json
from typing import Any

from harness.spend import GroupStats, aggregate, aggregate_by
from harness.tools import TOOL_SCHEMAS
from harness.trace import CACHE_FLOOR

from observability import context as context_mod
from observability.db import rows

#: Fallback context window when the model's capacity is not known locally.
DEFAULT_CONTEXT_WINDOW: int = 1_000_000


def stats_json(stats: GroupStats) -> dict[str, Any]:
    """One ``GroupStats`` as JSON, including the cache-hit rate and savings."""
    hit = stats.cache_hit_rate
    return {
        "calls": stats.calls,
        "prompt_tokens": stats.prompt_tokens,
        "cached_tokens": stats.cached_tokens,
        "cache_miss_tokens": stats.cache_miss_tokens,
        "completion_tokens": stats.completion_tokens,
        "total_tokens": stats.total_tokens,
        "cache_hit_pct": None if hit is None else round(hit * 100.0, 1),
        "cost_usd": round(stats.cost_usd, 6),
        "uncached_cost_usd": round(stats.uncached_cost_usd, 6),
        "savings_usd": round(stats.savings_usd, 6),
        "raw_cost_usd": round(stats.raw_cost_usd, 6),
        "unpriced_calls": stats.unpriced_calls,
    }


def _split_sums(row: dict[str, Any]) -> bool:
    """Whether a row's cached + miss buckets add up to its prompt total."""
    prompt = row.get("prompt_tokens") or 0
    if not prompt:
        return True
    return (row.get("cached_tokens") or 0) + (row.get("cache_miss_tokens") or 0) == prompt


def usage_payload(rows_in: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals plus per-lane and per-role spend (same math as harness.spend).

    Also reports the prompt-denominated hit share and how many rows carry a
    ledger whose split does not sum to its prompt total. Rows written before
    the ``cache_creation_input_tokens: 0`` reconciliation (see
    ``harness.client._parse_cache_split``) stored ``cache_miss_tokens = 0``
    and would otherwise read as a 100% cache hit forever.
    """
    if not rows_in:
        return {"totals": stats_json(GroupStats()), "by_lane": [], "by_role": []}
    by_lane = aggregate_by(rows_in, lambda row: str(row.get("lane") or "?"))
    by_role = aggregate_by(rows_in, lambda row: str(row.get("role") or "?"))
    totals = stats_json(aggregate(rows_in))
    prompt = totals["prompt_tokens"]
    totals["prompt_hit_pct"] = (round(totals["cached_tokens"] / prompt * 100.0, 1)
                                if prompt else None)
    totals["ledger_mismatch_calls"] = sum(1 for row in rows_in if not _split_sums(row))
    return {
        "totals": totals,
        "by_lane": [{"key": key, **stats_json(value)} for key, value in by_lane.items()],
        "by_role": [{"key": key, **stats_json(value)} for key, value in by_role.items()],
    }


def envelope_of(row: dict[str, Any]) -> dict[str, Any] | None:
    """The persisted request envelope, or None for a hash-only row."""
    blob = row.get("repro_json")
    if not blob:
        return None
    try:
        loaded = json.loads(blob)
    except (ValueError, TypeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _prefix_share(previous: dict[str, Any], current: dict[str, Any]) -> tuple[float, bool]:
    """Byte-proven shared prefix fraction and whether the system stayed put.

    Mirrors ``harness.trace._prefix_share``: the previous envelope's trailing
    block is the per-turn card, so it is excluded before comparison, and the
    shared bytes are measured against the WHOLE current request (what share of
    this prompt the provider could have served from cache).
    """
    _count, shared_chars, system_stable = context_mod.shared_message_count(previous, current)
    total_chars = sum(
        len(json.dumps(m, sort_keys=True, ensure_ascii=False))
        for m in (current.get("messages") or [])
    )
    fraction = (shared_chars / total_chars) if total_chars else 0.0
    return fraction, bool(system_stable)


def _annotate(rows_in: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add the measured prefix share, drift flag and cache verdict per call."""
    previous: dict[str, Any] | None = None
    for row in rows_in:
        envelope = envelope_of(row)
        fraction = 0.0
        system_stable: bool | None = None
        verdict = ""
        if envelope is None:
            verdict = "no persisted envelope"
        elif previous is not None:
            fraction, system_stable = _prefix_share(previous, envelope)
            prompt = int(row.get("prompt_tokens") or 0)
            hit = (int(row.get("cached_tokens") or 0) / prompt) if prompt else 0.0
            if not system_stable:
                verdict = "stable prefix CHANGED"
            elif fraction >= CACHE_FLOOR and hit < CACHE_FLOOR:
                verdict = "prefix shared but not cached"
        row["prefix_share_pct"] = round(fraction * 100.0, 1) if previous is not None else None
        row["system_stable"] = system_stable
        row["verdict"] = verdict
        if envelope is not None:
            previous = envelope
    return rows_in


def call_rows(conn: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
    """llm_calls rows, oldest first, with cache annotations per consecutive pair."""
    sql = "select * from llm_calls order by id"
    if limit is not None:
        sql = (f"select * from (select * from llm_calls order by id desc limit {int(limit)})"
               " order by id")
    return _annotate(rows(conn, sql))


def _head(text: Any, limit: int = 180) -> str:
    blob = " ".join(str(text or "").split())
    return blob if len(blob) <= limit else blob[: limit - 1] + "…"


def _meta_of(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def call_payload(row: dict[str, Any], anchor: Any) -> dict[str, Any]:
    """One call as the front-end reads it."""
    prompt = int(row.get("prompt_tokens") or 0)
    cached = int(row.get("cached_tokens") or 0)
    meta = _meta_of(row)
    return {
        "id": int(row.get("id") or 0),
        "day": int(row.get("day") or 0),
        "t_h": round(float(row.get("t_h") or 0.0), 4),
        "real": stamp(anchor, row.get("t_h")),
        "role": row.get("role"),
        "lane": row.get("lane"),
        "model": row.get("model"),
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "cache_miss_tokens": int(row.get("cache_miss_tokens") or 0),
        "completion_tokens": int(row.get("completion_tokens") or 0),
        "total_tokens": int(row.get("total_tokens") or 0),
        "cache_hit_pct": round(cached / prompt * 100.0, 1) if prompt else None,
        "prefix_share_pct": row.get("prefix_share_pct"),
        "system_stable": row.get("system_stable"),
        "verdict": row.get("verdict") or "",
        "has_envelope": row.get("repro_json") is not None,
        "tool_calls": list(meta.get("tool_calls") or []),
        "reasoning_chars": len(str(meta.get("reasoning") or "")),
        "response_head": _head(row.get("response")),
    }


def stamp(anchor: Any, t_h: Any) -> str | None:
    """Local wall clock for a virtual hour, or None on an unanchored run."""
    if anchor is None or t_h is None:
        return None
    try:
        return anchor.real(float(t_h)).isoformat(timespec="seconds")
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def tool_schemas_for(role: str | None) -> tuple[list[dict[str, Any]], str]:
    """The tool schemas a call of ``role`` sent, with their provenance.

    The store keeps the request body WITHOUT ``tools``, so these are rebuilt
    from the current code. The mainline reply sends no tools at all, and the
    decide legs send the pinned schema named by their popup kind (falling back
    to the full set, exactly as ``harness.session`` does).
    """
    kind = str(role or "")
    if kind == "chat":
        # "none" alone read as "the harness never sends schemas". It does —
        # on the decide legs. Say where they go, so an empty tool panel on a
        # chat call is not mistaken for a harness-wide omission.
        return [], ("none on this call — the mainline reply is prose by design; "
                    "the decide legs carry the schema")
    if kind.startswith("tool_decide"):
        wanted = [t for t in TOOL_SCHEMAS if t.get("name") == kind]
        chosen = wanted or list(TOOL_SCHEMAS)
        return chosen, (f"rebuilt from harness.tools — {len(chosen)} function"
                        " in the OpenAI shape; no tool_choice field is sent")
    if kind.startswith("aux_"):
        # The four auxiliary callers ask for JSON text, never a tool, so an
        # empty panel here is the call being what it says it is.
        return [], ("auxiliary call — no tools offered; it asks for JSON text")
    return [], "unknown role — tool payload not reconstructable"


def usage_of(row: dict[str, Any]) -> dict[str, Any]:
    """The provider numbers one call reported, in the store's column names."""
    return {
        "prompt_tokens": int(row.get("prompt_tokens") or 0),
        "cached_tokens": int(row.get("cached_tokens") or 0),
        "cache_miss_tokens": int(row.get("cache_miss_tokens") or 0),
        "completion_tokens": int(row.get("completion_tokens") or 0),
    }


def call_by_id(conn: Any, call_id: int | None) -> dict[str, Any] | None:
    """One call row by id, or the newest call when ``call_id`` is None."""
    if call_id is None:
        found = rows(conn, "select * from llm_calls order by id desc limit 1")
        return found[0] if found else None
    found = rows(conn, "select * from llm_calls where id = ?", (call_id,))
    return found[0] if found else None


def _previous_envelope(conn: Any, call_id: int) -> dict[str, Any] | None:
    found = rows(conn, "select repro_json from llm_calls where id < ? order by id desc limit 1",
                 (call_id,))
    return envelope_of(found[0]) if found else None


def context_payload(conn: Any, anchor: Any, *, call_id: int | None,
                    context_window: int) -> dict[str, Any]:
    """Context composition for one call (the newest by default)."""
    row = call_by_id(conn, call_id)
    if row is None:
        return {"available": False, "reason": "no model call recorded yet",
                "context_window": context_window}
    tools, tools_source = tool_schemas_for(row.get("role"))
    payload = context_mod.build_context(
        envelope=envelope_of(row),
        tools=tools,
        tools_source=tools_source,
        previous=_previous_envelope(conn, int(row["id"])),
        context_window=context_window,
        usage=usage_of(row),
    )
    payload["call"] = call_payload(row, anchor)
    return payload


def call_detail(conn: Any, anchor: Any, call_id: int) -> dict[str, Any] | None:
    """The full request/response of one call, for the inspector panel."""
    row = call_by_id(conn, call_id)
    if row is None:
        return None
    meta = _meta_of(row)
    tools, tools_source = tool_schemas_for(row.get("role"))
    return {
        "call": call_payload(row, anchor),
        "envelope": envelope_of(row),
        "envelope_available": envelope_of(row) is not None,
        "tools": tools,
        "tools_source": tools_source,
        "response": row.get("response") or "",
        "reasoning": meta.get("reasoning") or "",
        "meta": meta,
    }
