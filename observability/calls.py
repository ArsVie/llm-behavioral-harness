"""Read model over ``llm_calls``: usage, prefix cache and context provenance.

Every number here is either the provider's own (the persisted usage columns)
or the harness' own (``harness.spend``'s formula, ``harness.tools``' schemas).
"""

from __future__ import annotations

import json
from typing import Any

from harness.spend import GroupStats, aggregate, aggregate_by
from harness.tools import TOOL_PAYLOAD, TOOL_SCHEMAS, tools_identity
from harness.trace import CACHE_FLOOR

from observability import context as context_mod
from observability.db import rows, rows as db_rows

#: Fallback context window when the model's capacity is not known locally.
DEFAULT_CONTEXT_WINDOW: int = 1_000_000

#: This build's constant tool menu, hashed: a call whose recorded tools_hash
#: matches this is reconstructable here; a hash that matches nothing means the
#: call's schemas are not in this build.
_MENU_HASH: str | None = tools_identity(TOOL_PAYLOAD)[0]


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

    Also reports the prompt-denominated hit share and counts rows whose ledger
    split does not sum to its prompt total.
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
    """The persisted request envelope, or None for a hash-only row.

    The metered auxiliary lane nests the client kwargs - the system prompt among
    them - under ``kwargs``; the aux system is hoisted to the top level.
    """
    blob = row.get("repro_json")
    if not blob:
        return None
    try:
        loaded = json.loads(blob)
    except (ValueError, TypeError):
        return None
    if not isinstance(loaded, dict):
        return None
    if "system" not in loaded and isinstance(loaded.get("kwargs"), dict):
        aux_system = loaded["kwargs"].get("system")
        if isinstance(aux_system, str):
            loaded["system"] = aux_system
    return loaded


def _prefix_share(previous: dict[str, Any],
                  current: dict[str, Any]) -> tuple[float, bool, str]:
    """Provider-order shared prefix: fraction, system stability, first break.

    Breaks live at ``"system" | "tools" | "messages"``: the schemas sit
    between the system block and the conversation, so a tool-menu difference
    kills more of the prefix than a late message ever could. See
    :func:`observability.context.provider_prefix_share`.
    """
    return context_mod.provider_prefix_share(previous, current)


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
            fraction, system_stable, break_at = _prefix_share(previous, envelope)
            prompt = int(row.get("prompt_tokens") or 0)
            hit = (int(row.get("cached_tokens") or 0) / prompt) if prompt else 0.0
            if not system_stable or break_at == "system":
                verdict = "stable prefix CHANGED"
            elif break_at == "tools":
                verdict = "tool menu differs from the previous call"
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


def tool_schemas_for(row: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str]:
    """The tool menu one call sent, with the provenance of the claim.

    The call's OWN record comes first: ``tool_names``/``tools_hash`` ride the
    envelope (the stored request body drops ``tools``), and the schemas
    rebuild from ``harness.tools`` only when the recorded hash proves the
    payload is this build's menu — the menu changed over the trial (the
    pre-reset live run sent calendar/memory/remind on chat calls), and
    rebuilding today's schemas onto such a call would label it with tools it
    never carried. Those calls keep their recorded names, nothing more.

    A row whose record kept no menu — older stores, or a mainline row written
    by a build whose recorder stubbed these keys — falls back to the leg's
    rule, named as the inference it is: ONE constant menu rides every
    CANONICAL mainline call (``session._chat`` passes the whole
    ``TOOL_PAYLOAD`` to ``_generate``; the verbatim wire dumps carry all
    three actuation functions on every chat request, round trips included).
    The auxiliary legs mirror that same menu since 2026-09-20 (their records
    carry it; a fork without the schemas broke the shared prefix), so an
    empty aux panel is HISTORY — it belongs to the pre-fix fork, not to
    today's design.
    """
    env = envelope_of(row) if row else None
    names = [str(name) for name in (env.get("tool_names") or [])] if env else []
    if names:
        if env.get("tools_hash") == _MENU_HASH:
            whole = list(TOOL_SCHEMAS)
            return whole, f"recorded on the call — {len(whole)} rebuilt from harness.tools"
        return [{"name": name} for name in names], (
            f"recorded on the call — {len(names)} names; their schemas are "
            "not in this build")
    kind = str((row or {}).get("role") or "")
    if kind.startswith("aux_"):
        # Modern aux calls mirror the mainline menu and record it (the fork
        # used to drop the schemas, breaking the shared prefix at them). An
        # empty panel here is history, and it says which history.
        if env is not None and ("tools_hash" in env or "tool_names" in env):
            return [], "none — the mirrored mainline request carried no menu"
        return [], ("none — this call predates the fork fix: aux legs ran "
                    "tool-less before 2026-09-20, and mirror the mainline "
                    "menu on every call since")
    if kind == "chat" or kind.startswith("tool_decide"):
        chosen = list(TOOL_SCHEMAS)
        note = ("the call's record kept no menu" if env is not None
                else "no envelope on the row")
        return chosen, (f"the same {len(chosen)} on every mainline call"
                        f" — rebuilt from this build; {note}")
    return [], "unknown leg — not reconstructable"


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

    Annotated like the ledger's rows. The prefix share and the drift flag are
    MEASURED against the call before, so a row fetched on its own carried
    neither, and the inspector reported "first call" and a null share for
    every call it opened.
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
    return _annotate([*before, row])[-1]


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
    tools, tools_source = tool_schemas_for(row)
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
    itself is a ``decision_records`` row, and the two tables share no id. The
    panel used to render "(empty)" for these calls, which reads as a dropped
    response when the call in fact answered.

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
        for entry in db_rows(
            conn, "select id, meta from llm_calls order by id")
        if _meta_of(entry).get("popup_kind") == kind
    ]
    records = db_rows(
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


def call_detail(conn: Any, anchor: Any, call_id: int) -> dict[str, Any] | None:
    """The full request/response of one call, for the inspector panel."""
    row = call_by_id(conn, call_id)
    if row is None:
        return None
    meta = _meta_of(row)
    tools, tools_source = tool_schemas_for(row)
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
