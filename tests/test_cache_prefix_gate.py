"""Pre-run cache gate: every provider call must extend the previous one.

The durable part of call N must be a byte-prefix of the durable part of call N+1.
"""

from __future__ import annotations

import itertools
import json

import pytest

from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import (
    AgendaItem,
    DailyAgenda,
    Interest,
    PersonaProfile,
    ProactiveIntent,
)
from harness.proactive import compose_hook
from harness.scheduler import REASON_SCHEDULE, REASON_VALIDITY_H
from tests.helpers.store import make_session, make_store

SEED = 4242

#: Separators that cannot occur in prompt text, so the joined form is an
#: injective encoding of (system, [(role, content)]).
_FIELD = "\x01"
_RECORD = "\x00"


def _wire(system: str | None, messages: list[dict]) -> str:
    """The ordered bytes of a request, as a prefix cache sees them."""
    import json as _json

    parts = [system or ""]
    for m in messages:
        extra = ""
        if m.get("tool_calls"):
            extra = _FIELD + _json.dumps(m["tool_calls"], sort_keys=True)
        elif m.get("tool_call_id"):
            extra = _FIELD + str(m["tool_call_id"])
        parts.append(f"{m.get('role', '')}{_FIELD}{m.get('content') or ''}{extra}")
    return _RECORD.join(parts)


def _durable(call: dict, kept: frozenset[str] = frozenset()) -> str:
    """The reusable part: everything before the trailing volatile block.

    ``kept`` are the pop-up blocks a decision was saved with: those stay in
    the stream (owner ruling 2026-10-03), so the next call must extend them.
    """
    # The writing row rides each request unstored: the next call drops it.
    messages = call["messages"]
    end = len(messages)
    while (end > 0 and messages[end - 1].get("role") == "system"
           and messages[end - 1].get("content") not in kept):
        end -= 1
    return _wire(call["system"], messages[:end])


def _saved_blocks(store) -> frozenset[str]:
    return frozenset(
        block
        for row in store.conn.execute(
            "SELECT popup_json FROM decision_records WHERE popup_json IS NOT NULL")
        for block in json.loads(row["popup_json"])
    )


def _persona() -> PersonaProfile:
    return PersonaProfile(
        name="Lily",
        core="You are Lily, sharp and unimpressed until proven otherwise.",
        interests=(Interest("alternative music", "music", 0.8),),
        routines=(),
    )


def _agenda_item(store, *, item_id="ag_1", start=9.5, end=10.5,
                 activity="pottery class") -> AgendaItem:
    item = AgendaItem(item_id, start, end, activity, "arc", "arc1", 0.8, "planned")
    store.save_agenda(0, DailyAgenda(0, (item,)))
    return item


def _intent(item: AgendaItem, intent_id: str, t_h: float) -> ProactiveIntent:
    return ProactiveIntent(
        id=intent_id,
        reason=REASON_SCHEDULE,
        source_type="agenda_item",
        source_id=item.id,
        hook=compose_hook(item, REASON_SCHEDULE),
        created_t_h=t_h,
        valid_until_t_h=t_h + REASON_VALIDITY_H[REASON_SCHEDULE],
        salience=0.8,
        evidence=f"agenda_item:{item.id}",
        opportunity_id=None,
    )


def _mixed_run(store, session, client) -> dict:
    """Drive one day through every context-producing path; add new event kinds here.
    Returns a coverage summary the caller asserts on."""
    # 1. plain reactive turns (also triggers the day rollover)
    for i in range(3):
        session.clock.advance_hours(0.05)
        session.on_message(f"reactive {i}")

    # 2. an agenda window opening before the next turn (fires the event pop-up)
    now = session.clock.now_h()
    item = _agenda_item(store, start=now + 0.01, end=now + 0.5)
    store.save_proactive_intent(_intent(item, "pi_1", t_h=now))

    session.clock.advance_hours(0.1)
    session.on_message("turn that drains the event pop-up")

    # 3. a user message arriving mid-turn (steer, delivered at the boundary)
    session.clock.advance_hours(0.05)
    if session.steering_enabled():
        session.enqueue_user_message_steer("did you see this", session.clock.now_h())
    session.on_message("turn that drains the steer")

    # 4. a grounded proactive fire (companion-initiated: no trailing user turn)
    session.clock.advance_hours(0.05)
    session.fire_proactive("pi_1")

    # 5. more reactive turns after the proactive, and a lifecycle check
    for i in range(3):
        session.clock.advance_hours(0.05)
        session.on_message(f"after proactive {i}")
    session.check_conversation_lifecycle(session.clock.now_h())

    messages = store.recent_messages(limit=200)
    # A replayed decision is a native tool exchange, not a prose system block.
    projected = sum(
        1
        for call in client.calls
        for m in call["messages"][:-1]
        if m.get("tool_calls")
    )
    return {
        "calls": len(client.calls),
        "decisions": [d["popup_kind"] for d in store.decisions_for_day(0)],
        "steers": len(
            store.conn.execute("SELECT id FROM steering_queue").fetchall()
        ),
        "proactive_messages": sum(m["proactive"] for m in messages),
        "projected_decisions": projected,
    }


@pytest.fixture()
def decision_env(monkeypatch):
    """Turn the decision layer on, and make pop-up verdicts deterministic."""
    monkeypatch.setenv("HARNESS_DECISIONS", "1")
    monkeypatch.setenv("HARNESS_DECISION_SOURCE", "model")
    monkeypatch.setenv("HARNESS_TOOL_MODE", "textual")


def test_every_call_extends_the_previous_one(tmp_path, decision_env):
    """The gate. One property, every call, in order."""
    store = make_store(tmp_path, "gate.db")
    profile = _persona()
    store.save_persona(profile)
    client = FakeClient(
        responses=[
            # Mainline replies and pop-up verdicts share one queue, so every
            # response answers every lane whatever slot it lands in.
            f"reply {i}.\n"
            'tool_decide_event: {"initiate": "yes", "reason": "heading over"}\n'
            'tool_decide_reply: {"reply": true, "reason": "sure"}\n'
            'tool_decide_proactive: {"initiate": true, "reason": "checking in"}'
            for i in range(60)
        ]
    )
    session = make_session(
        store, clock=VirtualClock(t_h=9.6), client=client,
        seed=SEED, persona_profile=profile,
    )
    try:
        coverage = _mixed_run(store, session, client)
        kept = _saved_blocks(store)
    finally:
        store.close()

    # The gate must not go vacuous.
    assert coverage["calls"] >= 8, coverage
    assert coverage["decisions"], "no pop-up decision fired — the decision lane is not covered"
    assert coverage["steers"] >= 1, "no steer was queued — the steer lane is not covered"
    assert coverage["proactive_messages"] >= 1, "no proactive turn — that lane is not covered"
    assert coverage["projected_decisions"] >= 1, (
        "no past decision was projected back into a later call — the "
        "decision-in-context lane is not covered"
    )

    calls = client.calls

    # The stable prefix must be literally the same string on every call --
    # mainline turns, proactive turns and decision aux calls alike.
    systems = {c["system"] for c in calls}
    assert len(systems) == 1, (
        f"the stable system prefix differs across calls ({len(systems)} "
        "variants) — something per-turn leaked into it"
    )

    # The pop-up blocks are durable too: a decided pop-up stays in the stream.
    assert kept, "no pop-up block was saved — the ruling is not covered"
    durables = [_durable(c, kept) for c in calls]
    for index, (earlier, later) in enumerate(itertools.pairwise(durables)):
        assert later.startswith(earlier), (
            f"call {index + 1} is not an extension of call {index}: the "
            "context was edited, reordered or truncated before the tail"
        )


def test_the_volatile_block_is_actually_last(tmp_path, decision_env):
    """Nothing synthetic trails the stream; the card never embeds."""
    store = make_store(tmp_path, "tail.db")
    profile = _persona()
    store.save_persona(profile)
    client = FakeClient(responses=[f"reply {i}." for i in range(40)])
    session = make_session(
        store, clock=VirtualClock(t_h=9.6), client=client,
        seed=SEED, persona_profile=profile,
    )
    try:
        for i in range(5):
            session.clock.advance_hours(0.05)
            session.on_message(f"turn {i}")
    finally:
        store.close()

    for call in client.calls:
        messages = call["messages"]
        assert messages, "empty payload"
        # Append-only: the last message is the newest real row, never a
        # re-appended block.
        assert messages[-1]["role"] in ("user", "assistant", "tool")
        # ... and the card never rides inside the stable system message.
        assert "AFFECTIVE BEARING:" not in (call["system"] or "")
        assert "TEMPORAL FRAME:" not in (call["system"] or "")


def test_compaction_is_the_only_sanctioned_break(tmp_path):
    """History may shrink exactly once per boundary, and it must be logged."""
    import harness.session as session_mod

    store = make_store(tmp_path, "compact.db")
    profile = _persona()
    store.save_persona(profile)
    client = FakeClient(responses=[f"reply {i}." for i in range(60)])
    session = make_session(
        store, clock=VirtualClock(t_h=9.6), client=client,
        seed=SEED, persona_profile=profile,
    )
    after, keep = (
        session_mod.CONTEXT_COMPACT_AFTER_MESSAGES,
        session_mod.CONTEXT_RETAIN_MESSAGES,
    )
    session_mod.CONTEXT_COMPACT_AFTER_MESSAGES = 6
    session_mod.CONTEXT_RETAIN_MESSAGES = 4
    try:
        for i in range(6):
            session.clock.advance_hours(0.05)
            session.on_message(f"day one turn {i}")
        before = [_durable(c) for c in client.calls]
        # Within the day the chain is unbroken.
        for earlier, later in itertools.pairwise(before):
            assert later.startswith(earlier)
        epoch_before = session.context_epoch_id()

        session.clock.advance_hours(24.0)
        session.ensure_day(session.clock.day())

        assert session.context_epoch_id() > epoch_before
        events = [e["event"] for e in store.events_since(0)]
        assert "context_compacted" in events, (
            "history shrank without a recorded compaction — a prefix break "
            "nobody can explain later"
        )
    finally:
        session_mod.CONTEXT_COMPACT_AFTER_MESSAGES = after
        session_mod.CONTEXT_RETAIN_MESSAGES = keep
        store.close()


def test_a_late_boundary_decision_sorts_after_his_message(tmp_path, decision_env):
    """No conversation open, a start boundary passed a while ago, then he
    writes: the pop-up is decided after his message is in the stream. Stamped
    at the boundary it sorted BEFORE his message, so the turn's own request no
    longer extended the pop-up request it was built on. The decision is
    stamped no earlier than the last row; the pop-up and the tool result
    still show the boundary time."""
    store = make_store(tmp_path, "late.db")
    profile = _persona()
    store.save_persona(profile)
    client = FakeClient(responses=[
        f"reply {i}.\n"
        'tool_decide_event: {"initiate": "yes", "action": "follow", '
        '"reason": "heading over"}\n'
        'tool_decide_reply: {"reply": true, "reason": "sure"}'
        for i in range(20)
    ])
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    try:
        session.on_message("morning")
        session.clock.advance_hours(6.0)        # he goes quiet: it closes
        session.check_conversation_lifecycle(session.clock.now_h())
        assert session.open_conversation_id() is None
        now = session.clock.now_h()
        _agenda_item(store, start=now + 0.2, end=now + 3.0)
        session.clock.advance_hours(0.5)        # the start passes unseen
        session.on_message("you there?")
        session.clock.advance_hours(0.05)
        session.on_message("hello?")
        event = [d for d in store.decisions_for_day(0)
                 if d["popup_kind"] == "tool_decide_event"]
        his = [m for m in store.recent_messages(limit=100) if m["role"] == "user"]
    finally:
        store.close()

    assert event, "no start decision fired — the case is not covered"
    assert float(event[0]["t_h"]) >= float(his[-2]["t_h"])
    assert abs(float(event[0]["inputs"]["time"]) - (now + 0.2)) < 1e-9
    results = [m for m in client.calls[-1]["messages"] if m.get("role") == "tool"]
    assert any("recorded at" in (m.get("content") or "") for m in results)
    durables = [_durable(c) for c in client.calls]
    for index, (earlier, later) in enumerate(itertools.pairwise(durables)):
        assert later.startswith(earlier), (
            f"call {index + 1} is not an extension of call {index}"
        )


def _gate_client(n: int = 80) -> FakeClient:
    return FakeClient(responses=[
        f"reply {i}.\n"
        'tool_decide_event: {"initiate": "yes", "reason": "heading over"}\n'
        'tool_decide_reply: {"reply": true, "reason": "sure"}\n'
        'tool_decide_proactive: {"initiate": true, "reason": "checking in"}'
        for i in range(n)
    ])


def _card_kind(m: dict) -> str | None:
    from harness.assembler import AFFECTIVE_HEADER, is_state_update

    if m.get("role") != "system":
        return None
    text = m.get("content") or ""
    if text.startswith(AFFECTIVE_HEADER):
        return "full"
    return "update" if is_state_update(text) else None


def test_a_cold_point_is_the_only_other_sanctioned_break(tmp_path, decision_env):
    """After 2 h or more with no model call the provider's cache is gone, and
    the stream sheds stale state cards and old tool pairs once. The break is
    logged, every lane and a restart read the same pruned stream, and the
    chain is append-only again from there."""
    store = make_store(tmp_path, "cold.db")
    profile = _persona()
    store.save_persona(profile)
    client = _gate_client()
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    try:
        _mixed_run(store, session, client)
        warm = len(client.calls)
        before = session._context_turns()
        saved = _saved_blocks(store)
        assert saved and any(m.get("content") in saved for m in before)
        assert sum(_card_kind(m) is not None for m in before) >= 2, (
            "one card row only: nothing for the prune to drop")
        assert any(m.get("tool_calls") for m in before)

        # overnight: the morning's first turn writes the day's full card, so
        # yesterday's cards are the stale ones
        session.clock.advance_hours(24.0)
        session.on_message("back")
        cold = client.calls[warm]
        for i in range(2):
            session.clock.advance_hours(0.05)
            session.on_message(f"after the gap {i}")

        events = [e for e in store.events_since(0) if e["event"] == "context_pruned"]
        stream = session._context_turns()
        reread = make_session(store, clock=VirtualClock(t_h=session.clock.now_h()),
                              client=_gate_client(), seed=SEED,
                              persona_profile=profile)._context_turns()
    finally:
        store.close()

    assert len(events) == 1, "a cold point must prune once, and log it"
    import json as _json
    detail = _json.loads(events[0]["detail"])
    assert detail["cards"] >= 1 and detail["decisions"] >= 1, detail
    # the pruned request: no card before the newest full card, no tool pair
    # before his last message; all of her and his messages still there
    msgs = cold["messages"]
    kinds = [_card_kind(m) for m in msgs]
    last_full = max(i for i, k in enumerate(kinds) if k == "full")
    assert not any(k for k in kinds[:last_full])
    last_user = max(i for i, m in enumerate(msgs) if m.get("role") == "user")
    assert not any(m.get("tool_calls") or m.get("role") == "tool" for m in msgs[:last_user])
    # ... and the pop-up blocks go with their pairs (owner ruling 2026-10-03)
    assert not any(m.get("content") in saved for m in msgs[:last_user])
    said = [m["content"] for m in before if m.get("role") in ("user", "assistant")
            and not m.get("tool_calls")]
    kept = [m["content"] for m in msgs
            if m.get("role") in ("user", "assistant") and not m.get("tool_calls")]
    assert kept[:len(said)] == said
    # append-only again after the cold point
    durables = [_durable(c) for c in client.calls[warm:]]
    for index, (earlier, later) in enumerate(itertools.pairwise(durables)):
        assert later.startswith(earlier), f"call {warm + index + 1} broke the chain"
    assert _wire(None, reread) == _wire(None, stream), (
        "a restart must read the stream the running session reads")


def test_cold_prune_can_be_turned_off(tmp_path, decision_env, monkeypatch):
    monkeypatch.setenv("HARNESS_COLD_PRUNE", "0")
    store = make_store(tmp_path, "cold-off.db")
    profile = _persona()
    store.save_persona(profile)
    client = _gate_client()
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    try:
        _mixed_run(store, session, client)
        session.clock.advance_hours(2.5)
        session.on_message("back")
        events = [e["event"] for e in store.events_since(0)]
    finally:
        store.close()
    assert "context_pruned" not in events
    durables = [_durable(c) for c in client.calls]
    for earlier, later in itertools.pairwise(durables):
        assert later.startswith(earlier)


class _PrefixGateClient(FakeClient):
    """The gate client, able to continue an assistant prefix (face picks)."""

    supports_prefix = True


def test_face_picks_extend_their_turn_and_leave_the_chain_whole(tmp_path, decision_env):
    """An untagged reply's face pick (harness.faces) is its turn's request
    plus one assistant prefix; the mainline chain around it is unbroken."""
    store = make_store(tmp_path, "faces-gate.db")
    profile = _persona()
    store.save_persona(profile)
    client = _PrefixGateClient(responses=_gate_client(120).responses)
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    try:
        _mixed_run(store, session, client)
    finally:
        store.close()
    calls = client.calls
    picks = [i for i, c in enumerate(calls)
             if c["messages"] and c["messages"][-1].get("prefix")]
    assert picks, "no face pick ran — the lane is not covered"
    for i in picks:
        turn = calls[i - 1] if not calls[i - 1]["messages"][-1].get("prefix") else calls[i - 2]
        assert calls[i]["system"] == turn["system"]
        assert calls[i]["messages"][:-1] == turn["messages"]
    main = [c for c in calls if not (c["messages"] and c["messages"][-1].get("prefix"))]
    assert len({c["system"] for c in main}) == 1
    durables = [_durable(c) for c in main]
    for index, (earlier, later) in enumerate(itertools.pairwise(durables)):
        assert later.startswith(earlier), f"call {index + 1} broke the chain"


def test_a_decided_pop_up_stays_in_the_stream(tmp_path, decision_env):
    """Owner ruling 2026-10-03: "They are saved, only dropped when the 2hr idle
    check is going to prune history". The block a decision was asked with
    sits right before its tool call, as sent; a restart reads it too."""
    store = make_store(tmp_path, "popup.db")
    profile = _persona()
    store.save_persona(profile)
    client = _gate_client()
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    try:
        _mixed_run(store, session, client)
        saved = _saved_blocks(store)
        stream = session._context_turns()
        reread = make_session(store, clock=VirtualClock(t_h=session.clock.now_h()),
                              client=_gate_client(), seed=SEED,
                              persona_profile=profile)._context_turns()
    finally:
        store.close()
    assert saved
    asked = [i for i, m in enumerate(stream) if m.get("content") in saved]
    assert asked
    for i in asked:
        nxt = stream[i + 1]
        assert nxt.get("tool_calls") or nxt.get("content") in saved
        # sent bytes: the block is the one a pop-up call appended, unchanged
        assert any(m.get("role") == "system" and m.get("content") == stream[i]["content"]
                   for c in client.calls for m in c["messages"][-2:])
    assert _wire(None, reread) == _wire(None, stream)
