"""Run inspector: one read-only view over a live or archived run DB.

Views: timeline (default), checks, negotiations, decisions, steers, agenda,
cache, memory, all. ``checks`` exits 1 when any ERROR finding stands, so it
works as a post-run gate as well as an inspection tool. The DB is opened
READ-ONLY (``mode=ro``), so it is safe to point at a database a live bot is
currently writing; raw SQL on purpose, so old or half-written runs still read.

Virtual hours are an engine coordinate; every line also renders the real
local clock when the run persisted a time anchor.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.pricing import DEFAULT_MODEL_PRICING, MODELS, price_for
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VIEWS = (
    "timeline", "checks", "negotiations", "decisions", "steers",
    "agenda", "cache", "memory", "all",
)

#: Severities, worst first. Only ERROR fails the ``checks`` gate.
ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"
_RANK = {ERROR: 0, WARN: 1, INFO: 2}

#: A prompt whose predecessor is a proven byte-prefix should cache at least
#: this fraction of its tokens.
CACHE_FLOOR = 0.6

#: state_events that are per-turn bookkeeping rather than run history.
_NOISY_EVENTS = frozenset({"popup_boundary_check", "assistant_reply"})


# ---------------------------------------------------------------- plumbing


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open ``path`` read-only; raises when the file does not exist."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such run database: {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        r[0] for r in conn.execute(
            "select name from sqlite_master where type='table'"
        )
    )


def rows(conn: sqlite3.Connection, table: str, sql: str = "",
         params: tuple = ()) -> list[sqlite3.Row]:
    """Query ``table`` when it exists, else return nothing. Old and partial
    runs are missing tables; an inspector must read them anyway."""
    if table not in _tables(conn):
        return []
    return list(conn.execute(f"select * from {table} {sql}", params))


def _loads(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


class Anchor:
    """The run's virtual-hour -> real-local-clock mapping."""

    def __init__(self, epoch0_s: float, t_h0: float, tz: str) -> None:
        self.epoch0_s = epoch0_s
        self.t_h0 = t_h0
        try:
            self.tz = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):   # unknown tz in the row
            self.tz = UTC

    def real(self, t_h: float) -> datetime:
        epoch = self.epoch0_s + (t_h - self.t_h0) * 3600.0
        return datetime.fromtimestamp(epoch, self.tz)

    def stamp(self, t_h: float) -> str:
        return self.real(t_h).strftime("%a %H:%M:%S")


def load_anchor(conn: sqlite3.Connection) -> Anchor | None:
    kv = {r["key"]: r["value"] for r in rows(conn, "kv_store")}
    try:
        return Anchor(
            float(kv["anchor.epoch0_s"]),
            float(kv["anchor.t_h0"]),
            kv.get("anchor.tz", "UTC"),
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class Ctx:
    """Everything a view needs: the connection, the anchor, the filters."""

    conn: sqlite3.Connection
    anchor: Anchor | None
    day: int | None = None
    t_from: float | None = None
    t_to: float | None = None
    width: int = 96

    def when(self, t_h: float | None) -> str:
        if t_h is None:
            return " " * 22
        if self.anchor is None:
            return f"t={t_h:8.3f}         "
        return f"t={t_h:8.3f} {self.anchor.stamp(t_h)}"

    def keep(self, t_h: float | None, day: int | None = None) -> bool:
        if self.day is not None and day is not None and day != self.day:
            return False
        if t_h is None:
            return True
        if self.t_from is not None and t_h < self.t_from:
            return False
        return not (self.t_to is not None and t_h > self.t_to)

    def clip(self, text: str | None) -> str:
        flat = " ".join((text or "").split())
        if self.width <= 0 or len(flat) <= self.width:
            return flat
        return flat[: self.width - 1] + "…"


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    detail: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = f"  [{self.severity:5}] {self.code}: {self.message}"
        return "\n".join([head] + [f"           - {d}" for d in self.detail])


def _head(title: str) -> str:
    return f"\n{'=' * 78}\n{title}\n{'=' * 78}"


# ---------------------------------------------------------------- timeline


def timeline(ctx: Ctx) -> list[str]:
    """Every recorded happening in one chronological stream.

    Ordering is by virtual instant, so a decision or steer stamped at its
    ORIGINAL boundary sorts there; the delivery instant is printed alongside
    rather than substituted.
    """
    conn = ctx.conn
    out: list[tuple[float, int, str]] = []

    def add(t_h: float | None, rank: int, text: str) -> None:
        if t_h is None or not ctx.keep(t_h):
            return
        out.append((t_h, rank, text))

    for r in rows(conn, "conversations"):
        add(r["opened_t_h"], 0,
            f"CONV   opened  {r['id']} by={r['opened_by']}")
        if r["closed_t_h"] is not None:
            add(r["closed_t_h"], 9,
                f"CONV   closed  {r['id']} reason={r['close_reason']}")

    for r in rows(conn, "messages"):
        if not ctx.keep(r["t_h"], r["day"]):
            continue
        tag = {"user": "USER  ", "assistant": "LILY  ",
               "system": "SYSTEM"}.get(r["role"], r["role"][:6].upper())
        flag = " (proactive)" if r["proactive"] else ""
        add(r["t_h"], 4,
            f"{tag} #{r['id']}{flag} {ctx.clip(r['content'])}")

    for r in rows(conn, "steering_queue"):
        pay = _loads(r["payload_json"]) or {}
        what = pay.get("event") or pay.get("message") or ""
        add(r["t_h"], 2,
            f"STEER  {r['kind']} #{r['id']} state={pay.get('state', '-')} "
            f"status={r['status']} attempts={r['attempts']} "
            f"[{ctx.clip(what)}]")
        if r["delivered_t_h"] is not None and abs(
            float(r["delivered_t_h"]) - float(r["t_h"])
        ) > 1e-6:
            add(r["delivered_t_h"], 3,
                f"STEER  delivered #{r['id']} "
                f"(boundary t={float(r['t_h']):.3f}) turn={r['seen_turn_id']}")

    for r in rows(conn, "decision_records"):
        verdict = _loads(r["verdict_json"]) or {}
        keys = ", ".join(f"{k}={verdict[k]}" for k in sorted(verdict))
        late = ""
        if r["delivered_t_h"] is not None and abs(
            float(r["delivered_t_h"]) - float(r["t_h"])
        ) > 1e-6:
            late = f" (ran at t={float(r['delivered_t_h']):.3f})"
        add(r["t_h"], 5,
            f"DECIDE {r['popup_kind']} #{r['id']} state={r['state_label']} "
            f"src={r['source']}{late} {{{ctx.clip(keys)}}}")

    for r in rows(conn, "state_events"):
        if r["event"] in _NOISY_EVENTS:
            continue
        add(r["t_h"], 6,
            f"EVENT  {r['event']} {ctx.clip(r['detail'])}")

    for r in rows(conn, "agenda_items"):
        add(r["start_t_h"], 1,
            f"AGENDA start   {r['id']} [{r['activity']}] status={r['status']}")
        add(r["end_t_h"], 8,
            f"AGENDA end     {r['id']} status={r['status']}")

    for r in rows(conn, "llm_calls"):
        add(r["t_h"], 7,
            f"CALL   #{r['id']} lane={r['lane']} "
            f"prompt={r['prompt_tokens']} cached={r['cached_tokens']} "
            f"completion={r['completion_tokens']}")

    out.sort(key=lambda x: (x[0], x[1]))
    return [_head("TIMELINE")] + [
        f"{ctx.when(t)}  {text}" for t, _, text in out
    ]


# ------------------------------------------------------------ negotiations


def _negotiation_snapshots(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Every persisted ``negotiation_state`` snapshot, per item, in order."""
    per: dict[str, list[dict]] = {}
    for r in rows(conn, "state_events",
                  "where event='negotiation_state' order by id"):
        snap = _loads(r["detail"])
        if not isinstance(snap, dict):
            continue
        snap["_t_h"] = r["t_h"]
        per.setdefault(str(snap.get("item_id")), []).append(snap)
    return per


def negotiations(ctx: Ctx) -> list[str]:
    """Per-item availability negotiation: who was told, who decided, and
    whether the model or the backstop ended it."""
    lines = [_head("NEGOTIATIONS")]
    per = _negotiation_snapshots(ctx.conn)
    if not per:
        return lines + ["  (none)"]
    decisions = rows(ctx.conn, "decision_records", "order by id")
    for item_id, snaps in per.items():
        last = snaps[-1]
        legs = [
            d for d in decisions
            if d["event_id"] == item_id
            and d["popup_kind"] == "tool_decide_event"
        ]
        window = f"{last.get('start_t_h')}..{last.get('end_t_h')}"
        lines.append(
            f"\n  {item_id}  [{last.get('activity')}]  window {window}"
        )
        lines.append(
            f"    phase={last.get('phase')} informed={last.get('informed')} "
            f"delays={last.get('delay_count')} "
            f"outcome={last.get('resolved_action')} "
            f"at={last.get('resolved_t_h')}"
        )
        lines.append(
            f"    afk_deadline={last.get('afk_deadline_t_h')} "
            f"last_decide_at={last.get('last_decide_at_t_h')} "
            f"turns_to_decide={last.get('turns_to_decide')}"
        )
        for d in legs:
            verdict = _loads(d["verdict_json"]) or {}
            lines.append(
                f"    leg  {ctx.when(d['t_h'])}  state={d['state_label']:8} "
                f"src={d['source']:8} {ctx.clip(json.dumps(verdict))}"
            )
        for snap in snaps:
            lines.append(
                f"    snap {ctx.when(snap['_t_h'])}  phase={snap.get('phase')}"
            )
    return lines


# --------------------------------------------------------------- decisions


def decisions(ctx: Ctx) -> list[str]:
    """The decision lane: every verdict plus the parse-failure rate."""
    lines = [_head("DECISIONS")]
    recs = [r for r in rows(ctx.conn, "decision_records", "order by id")
            if ctx.keep(r["t_h"], r["day"])]
    fails = [r for r in rows(ctx.conn, "state_events",
                             "where event='decision_parse_failed' order by id")
             if ctx.keep(r["t_h"], r["day"])]

    by_kind: dict[str, list[int]] = {}
    for r in recs:
        by_kind.setdefault(r["popup_kind"], [0, 0])[0] += 1
    for r in fails:
        d = _loads(r["detail"]) or {}
        by_kind.setdefault(str(d.get("popup_kind")), [0, 0])[1] += 1

    lines.append("  kind                        verdicts  parse-fails  rate")
    for kind, (ok, bad) in sorted(by_kind.items()):
        total = ok + bad
        rate = (bad / total * 100.0) if total else 0.0
        lines.append(f"  {kind:28} {ok:8} {bad:12} {rate:5.1f}%")

    lines.append("")
    for r in recs:
        verdict = _loads(r["verdict_json"]) or {}
        lines.append(
            f"  {ctx.when(r['t_h'])}  #{r['id']:3} {r['popup_kind']:20} "
            f"{r['state_label']!s:9} src={r['source']:8} "
            f"{ctx.clip(json.dumps(verdict, ensure_ascii=False))}"
        )
    if fails:
        lines.append("\n  parse failures (the model replied in prose):")
        for r in fails:
            d = _loads(r["detail"]) or {}
            lines.append(
                f"  {ctx.when(r['t_h'])}  {d.get('decision_id')} "
                f"{d.get('popup_kind')} mode={d.get('parse_failure_mode')} "
                f"{ctx.clip(str(d.get('raw_excerpt')))}"
            )
    return lines


# ------------------------------------------------------------------ steers


def steers(ctx: Ctx) -> list[str]:
    lines = [_head("STEERING QUEUE")]
    for r in rows(ctx.conn, "steering_queue", "order by id"):
        if not ctx.keep(r["t_h"], r["day"]):
            continue
        pay = _loads(r["payload_json"]) or {}
        lines.append(
            f"  #{r['id']:3} {ctx.when(r['t_h'])}  {r['kind']:24} "
            f"state={pay.get('state')!s:12} status={r['status']:10} "
            f"attempts={r['attempts']} delivered={r['delivered_t_h']}"
        )
        lines.append(
            f"        event={pay.get('event')!r} "
            f"{ctx.clip(pay.get('message'))}"
        )
    return lines


# ------------------------------------------------------------------ agenda


def agenda(ctx: Ctx) -> list[str]:
    lines = [_head("AGENDA")]
    for r in rows(ctx.conn, "agenda_items", "order by day, start_t_h"):
        if ctx.day is not None and r["day"] != ctx.day:
            continue
        lines.append(
            f"  day {r['day']}  {r['start_t_h']:6.2f}-{r['end_t_h']:6.2f}  "
            f"{r['status']:10} sal={r['salience']:.3f} "
            f"{r['source_type']:8} {r['id']:22} {r['activity']}"
        )
    arcs = rows(ctx.conn, "life_arcs", "order by id")
    if arcs:
        lines.append("\n  life arcs:")
        for r in arcs:
            lines.append(
                f"    {r['id']:8} {r['status']:8} progress={r['progress']:.3f} "
                f"{r['name']} -> {r['next_intention']}"
            )
    return lines


# ------------------------------------------------------------------- cache


def _envelope(row: sqlite3.Row) -> dict | None:
    env = _loads(row["repro_json"])
    return env if isinstance(env, dict) else None


def _prefix_share(prev: dict, cur: dict) -> tuple[int, int, bool]:
    """Characters of ``cur`` provably identical to a prefix of ``prev``.

    ``prev`` minus its trailing (last) message is what an append-only stream
    re-sends. Returns ``(shared_chars, total_chars, system_stable)``.
    """
    pm = prev.get("messages") or []
    cm = cur.get("messages") or []
    body = pm[:-1] if pm else []
    shared = 0
    for i, msg in enumerate(body):
        if i >= len(cm) or cm[i] != msg:
            break
        shared += len(json.dumps(msg, sort_keys=True, ensure_ascii=False))
    total = sum(
        len(json.dumps(m, sort_keys=True, ensure_ascii=False)) for m in cm
    )
    sys_prev, sys_cur = prev.get("system") or "", cur.get("system") or ""
    return shared, total, sys_prev == sys_cur


def cache(ctx: Ctx) -> list[str]:
    """Prefix-cache accounting per call; the shared prefix is measured from
    the persisted envelopes rather than assumed."""
    lines = [_head("PREFIX CACHE")]
    calls = rows(ctx.conn, "llm_calls", "order by id")
    if not calls:
        return lines + ["  (no llm_calls rows)"]

    lanes: dict[str, list[int]] = {}
    for r in calls:
        acc = lanes.setdefault(str(r["lane"]), [0, 0, 0, 0])
        acc[0] += 1
        acc[1] += r["prompt_tokens"] or 0
        acc[2] += r["cached_tokens"] or 0
        acc[3] += r["completion_tokens"] or 0
    lines.append("  lane          calls   prompt   cached  completion  hit%")
    for lane, (n, p, c, comp) in sorted(lanes.items()):
        lines.append(
            f"  {lane:12} {n:6} {p:8} {c:8} {comp:11} "
            f"{(c / p * 100.0) if p else 0.0:5.1f}%"
        )

    lines.append(
        "\n  call  prompt  cached   hit%   shared-prefix  system  verdict"
    )
    prev_env: dict | None = None
    for r in calls:
        env = _envelope(r)
        prompt = r["prompt_tokens"] or 0
        cached = r["cached_tokens"] or 0
        hit = (cached / prompt * 100.0) if prompt else 0.0
        share_s, stable = "        -", "  -"
        verdict = ""
        if env is not None and prev_env is not None:
            shared, total, sys_stable = _prefix_share(prev_env, env)
            frac = (shared / total) if total else 0.0
            share_s = f"{frac * 100:8.1f}%"
            stable = " ok" if sys_stable else " !!"
            if not sys_stable:
                verdict = "stable prefix CHANGED"
            elif frac >= CACHE_FLOOR and hit / 100.0 < CACHE_FLOOR:
                verdict = "prefix shared but not cached"
        elif env is None:
            share_s, verdict = "  no repro", "envelope not persisted"
        lines.append(
            f"  #{r['id']:3} {prompt:7} {cached:7} {hit:5.1f}% "
            f"{share_s} {stable}   {verdict}"
        )
        if env is not None:
            prev_env = env
    return lines


# ------------------------------------------------------------------ memory


def memory(ctx: Ctx) -> list[str]:
    lines = [_head("MEMORY")]
    for r in rows(ctx.conn, "memory_episodes", "order by occurred_at_t_h"):
        anchors = _loads(r["verbatim_anchors_json"]) or []
        lines.append(
            f"  {ctx.when(r['occurred_at_t_h'])}  {r['category']:18} "
            f"imp={r['importance']:.2f}  {ctx.clip(r['summary'])}"
        )
        lines.append(f"        id={r['id']} tags={_loads(r['tags_json'])}")
        for a in anchors:
            lines.append(f"        anchor: {ctx.clip(a)}")
    facts = rows(ctx.conn, "user_model_assertions", "order by seq")
    if facts:
        lines.append("\n  user model:")
        for r in facts:
            lines.append(
                f"    {r['key']:20} conf={r['confidence']:.2f} "
                f"{r['status']:8} {r['category']:10} {r['value']}"
            )
    return lines


# ------------------------------------------------------------------ checks


def _message_stream(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return rows(conn, "messages", "order by id")


def _check_decision_lane(ctx: Ctx) -> list[Finding]:
    found: list[Finding] = []
    fails = rows(ctx.conn, "state_events",
                 "where event='decision_parse_failed'")
    recs = rows(ctx.conn, "decision_records")
    total = len(fails) + len(recs)
    if fails:
        rate = len(fails) / total * 100.0 if total else 0.0
        sev = ERROR if rate >= 25.0 else WARN
        by_id: dict[str, int] = {}
        for r in fails:
            d = _loads(r["detail"]) or {}
            by_id[str(d.get("decision_id"))] = by_id.get(
                str(d.get("decision_id")), 0) + 1
        found.append(Finding(
            sev, "decision-prose-reply",
            f"{len(fails)} of {total} decision calls came back as prose "
            f"instead of a tool call ({rate:.0f}%)",
            [f"{k}: {v} attempt(s)" for k, v in sorted(by_id.items())],
        ))
    abandoned = [r for r in rows(ctx.conn, "steering_queue")
                 if r["status"] == "abandoned"]
    if abandoned:
        found.append(Finding(
            ERROR, "steer-abandoned",
            f"{len(abandoned)} event boundary steer(s) exhausted their "
            "retries and were dropped without a decision",
            [f"#{r['id']} t={r['t_h']:.3f} {r['kind']} "
             f"attempts={r['attempts']} "
             f"{(_loads(r['payload_json']) or {}).get('event')}"
             for r in abandoned],
        ))
    placeholder = [
        r for r in rows(ctx.conn, "steering_queue")
        if (_loads(r["payload_json"]) or {}).get("event") == "?"
    ]
    if placeholder:
        found.append(Finding(
            WARN, "steer-placeholder-event",
            f"{len(placeholder)} steer(s) carry the literal event name '?' "
            "— they were enqueued with no active event to name",
            [f"#{r['id']} t={r['t_h']:.3f} {r['kind']}"
             for r in placeholder[:6]],
        ))
    return found


def _check_negotiations(ctx: Ctx) -> list[Finding]:
    found: list[Finding] = []
    for item_id, snaps in _negotiation_snapshots(ctx.conn).items():
        last = snaps[-1]
        if last.get("resolved_action") != "forced":
            continue
        afk = last.get("afk_deadline_t_h")
        end_t = last.get("end_t_h")
        resolved = last.get("resolved_t_h")
        if (
            afk is not None and end_t is not None and resolved is not None
            and afk < end_t and afk < resolved
        ):
            found.append(Finding(
                ERROR, "afk-decide-never-ran",
                f"{item_id}: the AFK decide leg was due at t={afk:.3f}, "
                f"inside the window, but nothing woke the runtime — the "
                f"backstop force-skipped it at t={resolved:.3f}",
                [(f"window {last.get('start_t_h')}..{end_t}, "
                  f"delays={last.get('delay_count')}, "
                  f"informed={last.get('informed')}")],
            ))
        if last.get("informed") is True and not last.get("delay_count"):
            legs = [
                d for d in rows(ctx.conn, "decision_records")
                if d["event_id"] == item_id
                and d["state_label"] == "decide"
                and d["source"] == "model"
            ]
            if not legs:
                found.append(Finding(
                    ERROR, "informed-never-decided",
                    f"{item_id}: she was told the event was coming and then "
                    "never got to decide on it — no model decide leg ran",
                ))
    return found


def _check_stream(ctx: Ctx) -> list[Finding]:
    found: list[Finding] = []
    stream = _message_stream(ctx.conn)
    adjacent = [
        (a["id"], b["id"]) for a, b in itertools.pairwise(stream)
        if a["role"] == "system" and b["role"] == "system"
    ]
    if adjacent:
        found.append(Finding(
            ERROR, "adjacent-system-messages",
            f"{len(adjacent)} pair(s) of consecutive system messages in the "
            "persisted stream (the 2026-09-08 markup-leak shape)",
            [f"#{a} then #{b}" for a, b in adjacent[:6]],
        ))
    convo = [r for r in stream if r["role"] in ("user", "assistant")]
    unanswered = [
        (a["id"], b["id"]) for a, b in itertools.pairwise(convo)
        if a["role"] == "user" and b["role"] == "user"
    ]
    if unanswered:
        found.append(Finding(
            ERROR, "unanswered-user-turn",
            f"{len(unanswered)} user turn(s) with no reply between them and "
            "the next user turn",
            [f"#{a} never answered (next user turn #{b})"
             for a, b in unanswered[:6]],
        ))
    dupes = [
        (a["id"], b["id"]) for a, b in itertools.pairwise(convo)
        if a["role"] == b["role"] == "user"
        and (a["content"] or "").strip() == (b["content"] or "").strip()
    ]
    if dupes:
        found.append(Finding(
            WARN, "duplicate-user-turn",
            f"{len(dupes)} identical consecutive user turn(s) — a resend or "
            "manual row surgery left the same text in context twice",
            [f"#{a} == #{b}" for a, b in dupes[:6]],
        ))
    return found


def _check_agenda(ctx: Ctx) -> list[Finding]:
    found: list[Finding] = []
    items = rows(ctx.conn, "agenda_items")
    if not items:
        return found
    clock = max(
        [float(r["t_h"]) for r in rows(ctx.conn, "state_events")] or [0.0]
    )
    stale = [r for r in items
             if r["status"] == "planned" and float(r["end_t_h"]) < clock]
    if stale:
        found.append(Finding(
            WARN, "agenda-window-passed-unresolved",
            f"{len(stale)} agenda item(s) are still 'planned' after their "
            "window closed",
            [f"{r['id']} {r['start_t_h']:.2f}-{r['end_t_h']:.2f} "
             f"{r['activity']}" for r in stale[:6]],
        ))

    calls = rows(ctx.conn, "llm_calls", "order by id desc")
    tail = ""
    for r in calls:
        env = _envelope(r)
        msgs = (env or {}).get("messages") or []
        if msgs and msgs[-1].get("role") == "system":
            tail = msgs[-1].get("content") or ""
            break
    done_block = _card_section(tail, "Done earlier")
    if done_block:
        misreported = [
            r for r in items
            if r["status"] in ("skipped", "abandoned")
            and str(r["activity"]) in done_block
        ]
        if misreported:
            found.append(Finding(
                ERROR, "card-reports-skipped-as-done",
                "the per-turn card lists item(s) she did NOT do under "
                "'Done earlier'",
                [f"{r['id']} status={r['status']} {r['activity']}"
                 for r in misreported],
            ))
    return found


#: The per-turn card's TEMPORAL FRAME buckets, in render order.
_CARD_SECTIONS = ("Done earlier", "Did not happen", "Happening now",
                  "Later today")


def _card_section(card: str, name: str) -> str:
    """One labelled block of a rendered state card, or "" when absent.

    The block runs from its own header to the next section header or blank
    line.
    """
    marker = f"{name}:"
    if marker not in card:
        return ""
    rest = card.split(marker, 1)[1]
    cuts = [rest.index(f"{s}:") for s in _CARD_SECTIONS if f"{s}:" in rest]
    if "\n\n" in rest:
        cuts.append(rest.index("\n\n"))
    return rest[: min(cuts)] if cuts else rest


def _check_metering(ctx: Ctx) -> list[Finding]:
    found: list[Finding] = []
    calls = rows(ctx.conn, "llm_calls")
    recs = rows(ctx.conn, "decision_records")
    if not calls:
        return [Finding(WARN, "no-call-metering",
                        "no llm_calls rows: nothing about token use or cost "
                        "was recorded for this run")]
    # The decision lane is the same model on the same lane as the mainline;
    # the row's role (the pop-up kind) tells them apart in the ledger.
    metered = {str(r["role"]) for r in calls}
    instants = [float(r["t_h"]) for r in calls if r["t_h"] is not None]
    model_recs = [r for r in recs if r["source"] == "model"]
    unmet: list[Any] = []
    for rec in model_recs:
        if str(rec["popup_kind"]) in metered:
            continue
        # A native pop-up rides INSIDE the mainline call, metered under
        # ``role='chat'`` at the same instant; a textual verdict is its own row.
        native = ("transport" in rec.keys()
                  and str(rec["transport"] or "").startswith("native"))
        if native and any(abs(t - float(rec["t_h"])) < 1e-9 for t in instants):
            continue
        unmet.append(rec)
    if unmet:
        kinds = sorted({str(r["popup_kind"]) for r in unmet})
        found.append(Finding(
            ERROR, "decision-calls-unmetered",
            f"{len(unmet)} of {len(model_recs)} model decision call(s) have "
            f"neither a ledger row of their own nor a mainline call at their "
            f"instant ({kinds}) — those tokens and that cost are not recorded",
            [f"llm_calls roles present: {sorted(metered)}",
             f"unmetered decision ids: "
             f"{[r['id'] for r in unmet][:12]}"],
        ))
    # ``raw_cost`` can be an absent column on old runs; a null raw_cost is
    # normal — spend is reconstructed from the token counts and pricing below.
    if all(("raw_cost" not in r.keys()) or (r["raw_cost"] is None) for r in calls):
        if all(price_for(_model_of(r)) is None for r in calls):
            found.append(Finding(
                WARN, "no-cost-recorded",
                "no call names a model with rates and raw_cost is null on "
                "every call: spend cannot be reconstructed from this run",
            ))
    unknown = sorted({_model_of(r) for r in calls
                      if _model_of(r) and _model_of(r) not in MODELS})
    if unknown:
        found.append(Finding(
            WARN, "cost-rates-unknown",
            f"{len(unknown)} model id(s) are missing from harness.pricing, so "
            f"their cost comes from the fallback tier: {unknown}",
            [f"fallback rates: {DEFAULT_MODEL_PRICING} per 1M tokens"],
        ))
    return found


def _model_of(row: Any) -> str:
    """The row's model id, or "" when the column is absent (old runs)."""
    if "model" not in row.keys():
        return ""
    return str(row["model"] or "").strip()
    return found


def _check_cache(ctx: Ctx) -> list[Finding]:
    found: list[Finding] = []
    calls = rows(ctx.conn, "llm_calls", "order by id")
    prev_env: dict | None = None
    drift: list[str] = []
    wasted: list[str] = []
    for r in calls:
        env = _envelope(r)
        if env is None:
            continue
        if prev_env is not None:
            shared, total, sys_stable = _prefix_share(prev_env, env)
            frac = (shared / total) if total else 0.0
            prompt = r["prompt_tokens"] or 0
            hit = ((r["cached_tokens"] or 0) / prompt) if prompt else 0.0
            if not sys_stable:
                drift.append(f"#{r['id']}")
            if frac >= CACHE_FLOOR and hit < CACHE_FLOOR:
                wasted.append(
                    f"#{r['id']} shared={frac * 100:.0f}% "
                    f"cached={hit * 100:.0f}% ({prompt} prompt tokens)"
                )
        prev_env = env
    if drift:
        found.append(Finding(
            ERROR, "stable-prefix-changed",
            "the persona/system prefix is not byte-identical across calls",
            drift[:8],
        ))
    if wasted:
        found.append(Finding(
            ERROR, "prefix-shared-but-not-cached",
            f"{len(wasted)} call(s) re-sent a proven byte-identical prefix "
            "that the provider did not report as cached — either caching "
            "stops after the system field or the proxy under-reports it",
            wasted[:8],
        ))
    return found


def _check_memory(ctx: Ctx) -> list[Finding]:
    found: list[Finding] = []
    eps = rows(ctx.conn, "memory_episodes")
    machine = [r for r in eps
               if "negotiation_forced" in (r["tags_json"] or "")]
    if machine:
        found.append(Finding(
            WARN, "machine-reason-stored-as-memory",
            f"{len(machine)} memory episode(s) hold a server-generated "
            "verdict reason as their summary — machine rationale is audit "
            "data, and it now reaches the prompt as recalled experience",
            [f"{r['id']}: {r['summary']}" for r in machine[:6]],
        ))
    thin = [r for r in eps
            if r["category"] == "user_fact" and len(r["summary"].split()) <= 5]
    if thin:
        found.append(Finding(
            WARN, "thin-user-fact",
            f"{len(thin)} extracted user fact(s) are five words or fewer — "
            "keyword-shaped rather than a stated fact",
            [f"{r['id']}: {r['summary']!r}" for r in thin[:6]],
        ))
    return found


CHECKS = (
    _check_decision_lane,
    _check_negotiations,
    _check_stream,
    _check_agenda,
    _check_metering,
    _check_cache,
    _check_memory,
)


def collect_findings(ctx: Ctx) -> list[Finding]:
    """Every invariant finding, sorted worst-first.

    The single source for the ``checks`` view and for any other reader.
    """
    found: list[Finding] = []
    for check in CHECKS:
        found.extend(check(ctx))
    found.sort(key=lambda f: (_RANK[f.severity], f.code))
    return found


def run_checks(ctx: Ctx) -> tuple[list[str], int]:
    """Every invariant check. Returns ``(lines, exit_code)``; the code is 1
    when an ERROR finding stands, so this doubles as a post-run gate."""
    found = collect_findings(ctx)
    lines = [_head("CHECKS")]
    if not found:
        return lines + ["  all clear"], 0
    counts: dict[str, int] = {}
    for f in found:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    lines.append(
        "  " + ", ".join(f"{counts.get(s, 0)} {s}"
                         for s in (ERROR, WARN, INFO) if counts.get(s))
    )
    lines.append("")
    lines.extend(f.render() for f in found)
    return lines, (1 if counts.get(ERROR) else 0)


# --------------------------------------------------------------------- CLI


def render(ctx: Ctx, view: str) -> tuple[str, int]:
    if view == "checks":
        lines, code = run_checks(ctx)
        return "\n".join(lines), code
    views = {
        "timeline": timeline, "negotiations": negotiations,
        "decisions": decisions, "steers": steers, "agenda": agenda,
        "cache": cache, "memory": memory,
    }
    if view == "all":
        out: list[str] = []
        for name in ("agenda", "timeline", "negotiations", "decisions",
                     "steers", "cache", "memory"):
            out.extend(views[name](ctx))
        lines, code = run_checks(ctx)
        out.extend(lines)
        return "\n".join(out), code
    return "\n".join(views[view](ctx)), 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m harness.trace",
        description="Read-only inspection of a harness run database: what "
                    "happened, and which invariants it broke.",
    )
    parser.add_argument("view", nargs="?", default="timeline", choices=VIEWS)
    parser.add_argument("--db", "--store", dest="db", required=True,
                        help="path to the run's SQLite database")
    parser.add_argument("--day", type=int, default=None,
                        help="restrict to one day index")
    parser.add_argument("--from", dest="t_from", type=float, default=None,
                        help="earliest virtual hour to include")
    parser.add_argument("--to", dest="t_to", type=float, default=None,
                        help="latest virtual hour to include")
    parser.add_argument("--width", type=int, default=96,
                        help="text clip width; 0 for no clipping")
    parser.add_argument("--out", default=None,
                        help="write to this file instead of stdout")
    args = parser.parse_args(argv)

    try:
        conn = open_db(args.db)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        ctx = Ctx(
            conn=conn, anchor=load_anchor(conn), day=args.day,
            t_from=args.t_from, t_to=args.t_to, width=args.width,
        )
        text, code = render(ctx, args.view)
    finally:
        conn.close()

    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
