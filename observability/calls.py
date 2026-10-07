"""Read model over ``llm_calls``: usage, prefix cache and context provenance.

Every number here is either the provider's own (the persisted usage columns)
or the harness' own (``harness.spend``'s formula, ``harness.tools``' schemas).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.cache_audit import divergence, envelope_of, provider_prefix_share
from harness.spend import GroupStats, aggregate, aggregate_by
from harness.tools import TOOL_SCHEMAS
from harness.trace import (CACHE_FLOOR, CACHE_TTL_H, _is_fork,
                           _transient_slots, rebuild_note, stream_rebuilds)
from observability import context as context_mod
from observability import wire
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


def usage_payload(rows_in: list[dict[str, Any]], anchor=None) -> dict[str, Any]:
    """Totals plus per-lane and per-role spend (same math as harness.spend).

    ``anchor`` dates each call, for models priced by time of day.

    Also reports the prompt-denominated hit share and counts rows whose ledger
    split does not sum to its prompt total.
    """
    if not rows_in:
        return {"totals": stats_json(GroupStats()), "by_lane": [], "by_role": []}
    when = None if anchor is None else (lambda row: anchor.real(float(row["t_h"])))
    by_lane = aggregate_by(rows_in, lambda row: str(row.get("lane") or "?"), when=when)
    by_role = aggregate_by(rows_in, lambda row: str(row.get("role") or "?"), when=when)
    totals = stats_json(aggregate(rows_in, when=when))
    prompt = totals["prompt_tokens"]
    totals["prompt_hit_pct"] = (round(totals["cached_tokens"] / prompt * 100.0, 1)
                                if prompt else None)
    totals["ledger_mismatch_calls"] = sum(1 for row in rows_in if not _split_sums(row))
    return {
        "totals": totals,
        "by_lane": [{"key": key, **stats_json(value)} for key, value in by_lane.items()],
        "by_role": [{"key": key, **stats_json(value)} for key, value in by_role.items()],
    }


def _annotate(rows_in: list[dict[str, Any]],
              conn: Any | None = None) -> list[dict[str, Any]]:
    """Add the measured prefix share, drift flag and cache verdict per call.

    The same chain as ``harness.trace``'s cache check, so a note here and a
    finding there name the same calls: a mainline call is compared with the
    mainline call before it, and an ``aux_`` fork — not a stream extension —
    is compared with the mainline it forked from but never
    becomes the predecessor. With ``conn`` the note cites a logged stream
    rebuild (cold prune / compaction) when one falls between the two calls.
    """
    rebuilds = stream_rebuilds(conn) if conn is not None else []
    previous: dict[str, Any] | None = None
    previous_t: float | None = None
    previous_role = ""
    previous_prompt = 0
    for row in rows_in:
        envelope = envelope_of(row)
        fork = _is_fork(row)
        fraction = 0.0
        system_stable: bool | None = None
        verdict = ""
        if envelope is None:
            verdict = "no persisted envelope"
        elif previous is not None:
            fraction, system_stable, break_at = provider_prefix_share(previous, envelope)
            prompt = int(row.get("prompt_tokens") or 0)
            hit = (int(row.get("cached_tokens") or 0) / prompt) if prompt else 0.0
            kept = len(previous.get("messages") or []) - _transient_slots(previous_role)
            at = divergence(previous, envelope)
            missed = bool(prompt) and fraction >= CACHE_FLOOR and hit < CACHE_FLOOR
            if not system_stable or break_at == "system":
                verdict = "stable prefix CHANGED"
            elif break_at == "tools":
                verdict = "tool menu differs from the previous call"
            elif not fork and at < kept:
                # An already-sent message was edited, dropped or reordered.
                verdict = f"history rewritten at message {at}"
                note = rebuild_note(rebuilds, previous_t or 0.0,
                                    float(row.get("t_h") or 0.0))
                if note:
                    # Sanctioned: the prune/compaction is the rewrite.
                    verdict = f"{verdict} — {note}"
            elif missed:
                idle = float(row.get("t_h") or 0.0) - (previous_t or 0.0)
                if idle >= CACHE_TTL_H:
                    verdict = f"cache expired after {idle:.1f} h idle"
                else:
                    # A fork is not a stream extension, so the cache check
                    # does not hold it to the append-only rule; a miss here is
                    # the provider's (the gateway can route a fork to a backend
                    # without the prefix: #52 missed 5 s after #51 hit 86%).
                    verdict = "fork, not cached" if fork else "prefix shared but not cached"
            elif (prompt and previous_prompt and not fork and at >= kept
                  and hit >= CACHE_FLOOR
                  and previous_role == "chat"
                  and str(row.get("role") or "") == "chat"):
                # The warm law holds exactly for chat-to-chat full carries:
                # cached is 128 * floor(prev prompt / 128). A full block or
                # more below it means an older backend re-billed tokens that
                # were already sent. Decide rounds drop their trailing steer,
                # so their banked floor can sit a block or two lower on
                # purpose — not flagged here.
                expected = 128 * (previous_prompt // 128)
                cached = int(row.get("cached_tokens") or 0)
                if cached <= expected - 128:
                    verdict = (f"re-billed {expected - cached:,} tokens past "
                               f"the block floor")
        row["prefix_share_pct"] = round(fraction * 100.0, 1) if previous is not None else None
        row["system_stable"] = system_stable
        row["verdict"] = verdict
        if envelope is not None and not fork:
            previous = envelope
            previous_t = float(row.get("t_h") or 0.0)
            previous_role = str(row.get("role") or "")
            previous_prompt = int(row.get("prompt_tokens") or 0)
    return rows_in


def call_rows(conn: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
    """llm_calls rows, oldest first, with cache annotations per consecutive pair."""
    sql = "select * from llm_calls order by id"
    if limit is not None:
        sql = (f"select * from (select * from llm_calls order by id desc limit {int(limit)})"
               " order by id")
    return _annotate(rows(conn, sql), conn)


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


def tool_schemas_for(row: dict[str, Any] | None,
                     *, run_path: Any | None = None) -> tuple[list[dict[str, Any]], str]:
    """The tool menu one call sent, and where the schemas come from.

    The call's own wire dump comes first: it holds the request bytes exactly
    as they went out, ``tools`` included. Without a dump — an old run, or a
    store copied without its ``wire/`` directory — the schemas come from this
    build's menu (``harness.tools``), which every canonical call rides.
    """
    if row is None:
        return [], "no call to read"
    if run_path is not None:
        found, tools = wire.tools_for_call(Path(run_path), int(row.get("id") or 0),
                                           envelope_of(row))
        if found:
            if tools:
                return tools, f"the call's wire dump — {len(tools)} schemas as sent"
            return [], "the call's wire dump — no tool menu on it"
    return list(TOOL_SCHEMAS), (f"this build's menu — {len(TOOL_SCHEMAS)} schemas "
                                "from harness/tools.py; no wire dump for this call")


def usage_of(row: dict[str, Any]) -> dict[str, Any]:
    """The provider numbers one call reported, in the store's column names."""
    return {
        "prompt_tokens": int(row.get("prompt_tokens") or 0),
        "cached_tokens": int(row.get("cached_tokens") or 0),
        "cache_miss_tokens": int(row.get("cache_miss_tokens") or 0),
        "completion_tokens": int(row.get("completion_tokens") or 0),
    }


def call_by_id(conn: Any, call_id: int | None) -> dict[str, Any] | None:
    """One call row by id, or the newest call when ``call_id`` is None.

    Annotated like the ledger's rows: the prefix share and the drift flag are
    measured against the call before, so that call is fetched too.
    """
    found = (rows(conn, "select * from llm_calls where id = ?", (call_id,))
             if call_id is not None
             else rows(conn, "select * from llm_calls order by id desc limit 1"))
    if not found:
        return None
    row = found[0]
    # _annotate measures against the last call that PERSISTED an envelope,
    # which is what the ledger's full walk also carries forward.
    before = rows(conn, "select * from llm_calls where id < ? and repro_json is not null "
                        "order by id desc limit 1", (int(row.get("id") or 0),))
    return _annotate([*before, row], conn)[-1]


def _previous_envelope(conn: Any, call_id: int) -> dict[str, Any] | None:
    found = rows(conn, "select repro_json from llm_calls where id < ? order by id desc limit 1",
                 (call_id,))
    return envelope_of(found[0]) if found else None


def context_payload(conn: Any, anchor: Any, *, call_id: int | None,
                    context_window: int,
                    run_path: Any | None = None) -> dict[str, Any]:
    """Context composition for one call (the newest by default)."""
    row = call_by_id(conn, call_id)
    if row is None:
        return {"available": False, "reason": "no model call recorded yet",
                "context_window": context_window}
    tools, tools_source = tool_schemas_for(row, run_path=run_path)
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


def decision_for_call(conn: Any, row: dict[str, Any]) -> dict[str, Any] | None:
    """The verdict a decide leg answered with, or None.

    ``llm_calls.response`` is EMPTY for a decide leg: the answer left as a
    tool call, and only the function's NAME survives in ``meta``. The verdict
    itself is a ``decision_records`` row, and the two tables share no id.

    ``t_h`` does not join them either: the three legs that open a live day all
    ran at one virtual instant while their records carry each event's own hour
    (7.92, 11.0, 17.0 against a call t_h of 19.40). What does hold is order
    within a kind — the model is asked each pop-up once, and answers in the
    order it was asked — so model-sourced records of a kind pair with that
    kind's calls by position. A default or replayed verdict costs no call, so
    only ``source='model'`` rows take part; when the two counts disagree the
    pairing is refused rather than guessed at.
    """
    kind = str(_meta_of(row).get("popup_kind") or "")
    if not kind:
        return None
    calls = [
        int(entry["id"])
        for entry in rows(conn, "select id, meta from llm_calls order by id")
        if _meta_of(entry).get("popup_kind") == kind
    ]
    records = rows(
        conn,
        "select * from decision_records where popup_kind = ? and source = 'model' "
        "order by id", (kind,))
    if len(calls) != len(records) or int(row.get("id") or 0) not in calls:
        return None
    record = records[calls.index(int(row["id"]))]
    verdict = record.get("verdict_json")
    if isinstance(verdict, str):
        try:
            verdict = json.loads(verdict)
        except ValueError:
            verdict = None
    return {
        "kind": kind,
        "verdict": verdict if isinstance(verdict, dict) else None,
        "event_label": record.get("event_label") or "",
        "state_label": record.get("state_label") or "",
        "transport": record.get("transport") or "",
        "reask": bool(record.get("reask")),
    }


def call_detail(conn: Any, anchor: Any, call_id: int,
                *, run_path: Any | None = None) -> dict[str, Any] | None:
    """The full request/response of one call, for the inspector panel."""
    row = call_by_id(conn, call_id)
    if row is None:
        return None
    meta = _meta_of(row)
    tools, tools_source = tool_schemas_for(row, run_path=run_path)
    return {
        "call": call_payload(row, anchor),
        "envelope": envelope_of(row),
        "envelope_available": envelope_of(row) is not None,
        "tools": tools,
        "tools_source": tools_source,
        "response": row.get("response") or "",
        "decision": decision_for_call(conn, row),
        "reasoning": meta.get("reasoning") or "",
        "meta": meta,
    }
