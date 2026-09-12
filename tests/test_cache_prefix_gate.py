"""Pre-run cache gate: every provider call must extend the previous one.

This is the check to run BEFORE a live run, and the one a new event kind,
steer, tool or card section has to pass. It does not inspect any particular
feature -- it drives a mixed sequence (reactive turns, an event pop-up
decision, a mid-turn user-message steer, a grounded proactive fire, a
conversation close) and asserts a single property over EVERY call the client
received, in order:

    the durable part of call N is a byte-prefix of the durable part of call N+1

"Durable" is everything except the trailing run of system messages -- the
state card and, on a decision call, the pop-up block. Those are rebuilt each
turn by design and cost only their own tokens; everything before them must
never move.

Why this is the property that matters: prefix caches match strictly from
token 0 and in fixed-size units, so a single byte inserted, removed or
reordered anywhere earlier discards the cache for everything after it. A
feature that appends at the tail is free. A feature that edits history, or
that truncates it from the front, silently costs a full re-prefill on every
subsequent turn -- with no error and no failing test, which is exactly how
the sliding-window read survived as long as it did.

If this test fails, the new code is writing into the middle of the context.
Move it to the tail, or -- if it genuinely must rewrite history -- make it a
compaction at an explicit boundary and record the event (see
``test_compaction_is_the_only_sanctioned_break``).
"""

from __future__ import annotations

import itertools

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
#: injective encoding of (system, [(role, content)]) -- two different message
#: lists can never render to the same string.
_FIELD = "\x01"
_RECORD = "\x00"


def _wire(system: str | None, messages: list[dict]) -> str:
    """The ordered bytes of a request, as a prefix cache sees them.

    Tool keys are part of the payload: a replayed decision carries its
    verdict in ``tool_calls`` and nothing in ``content``, so encoding only
    role and content would make two different exchanges look identical.
    """
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


def _durable(call: dict) -> str:
    """The reusable part: everything before the trailing volatile block.

    The trailing block is the run of system messages at the end -- the state
    card, plus the pop-up block on a decision call. System messages EARLIER in
    the list (delivered steers, projected past decisions) are durable history
    and stay in.
    """
    messages = call["messages"]
    end = len(messages)
    while end > 0 and messages[end - 1].get("role") == "system":
        end -= 1
    return _wire(call["system"], messages[:end])


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
    """Drive one day through every context-producing path we have.

    ADD NEW EVENT KINDS HERE. A steer, tool, card section or lifecycle hook
    that is not exercised by this sequence is not covered by the gate.

    Returns a coverage summary the caller asserts on, so the gate cannot go
    quietly vacuous: a change that stops pop-ups firing must fail here rather
    than make the run trivially pass.
    """
    # 1. plain reactive turns (this also triggers the day rollover, which
    #    regenerates the agenda -- so agenda fixtures go in AFTER it)
    for i in range(3):
        session.clock.advance_hours(0.05)
        session.on_message(f"reactive {i}")

    # 2. an agenda window that opens between the last boundary check and the
    #    next turn, so the event pop-up actually fires a decision
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
    # A replayed decision is a NATIVE tool exchange (2026-09-08), not a
    # prose system block.
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
    monkeypatch.setenv("HARNESS_DECISION_SOURCE", "model")
    monkeypatch.setenv("HARNESS_TOOL_MODE", "textual")


def test_every_call_extends_the_previous_one(tmp_path, decision_env):
    """The gate. One property, every call, in order."""
    store = make_store(tmp_path, "gate.db")
    profile = _persona()
    store.save_persona(profile)
    client = FakeClient(
        responses=[
            # Mainline replies and pop-up verdicts are drawn from the same
            # queue; a verdict-shaped reply parses as a verdict, anything
            # else is an ordinary turn.
            #
            # The verdict rides BEHIND prose because the textual parser is
            # tolerant of surrounding text, while a mainline turn that draws
            # this response must still have something sayable left after the
            # marker is stripped. A bare marker as a whole reply is machinery,
            # and `_reject_tool_markup` refuses to persist it as her message
            # (2026-09-08: DSML tool markup reached the live channel).
            f'reply {i}. tool_decide_event: {{"initiate": false, "reason": "later"}}'
            if i % 3 == 1
            else f"reply {i}."
            for i in range(60)
        ]
    )
    session = make_session(
        store, clock=VirtualClock(t_h=9.6), client=client,
        seed=SEED, persona_profile=profile,
    )
    try:
        coverage = _mixed_run(store, session, client)
    finally:
        store.close()

    # The gate must not go vacuous: assert the run really exercised the
    # paths it claims before asserting anything about their bytes.
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

    durables = [_durable(c) for c in calls]
    for index, (earlier, later) in enumerate(itertools.pairwise(durables)):
        assert later.startswith(earlier), (
            f"call {index + 1} is not an extension of call {index}: the "
            "context was edited, reordered or truncated before the tail"
        )


def test_the_volatile_block_is_actually_last(tmp_path, decision_env):
    """The card (and pop-up) must be the FINAL messages, not embedded.

    A card rendered anywhere but the tail invalidates every token after it,
    every turn -- the single most expensive mistake available here.
    """
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
        assert messages[-1]["role"] == "system", (
            "the last message is not the state card — the volatile block "
            "moved off the tail"
        )
        # ... and the card never rides inside the system message.
        assert "AFFECTIVE BEARING:" not in (call["system"] or "")
        assert "TEMPORAL FRAME:" not in (call["system"] or "")


def test_compaction_is_the_only_sanctioned_break(tmp_path):
    """History may shrink exactly once per boundary, and it must be logged.

    Compaction is the one operation allowed to break the chain, because it
    pays the re-prefill once instead of every turn. It must leave a
    ``context_compacted`` event so a prefix break is explainable after the
    fact rather than mysterious.
    """
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
