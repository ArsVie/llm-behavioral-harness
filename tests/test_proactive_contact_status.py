"""A consumed proactive slot is a contact only when something reached him.

Live 2026-09-25: her three declines were stored as ``fired`` and every
suppressed slot restarted the cooldown.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from harness.domain import ProactiveIntent
from harness.runtime import AsyncRuntime
from harness.scheduler import ProactiveSchedule
from harness.store import SQLiteStore

SEED = 7


def _intent() -> ProactiveIntent:
    return ProactiveIntent(
        id="pi_1", reason="schedule", source_type="agenda_item",
        source_id="ag_0", hook="Agenda: pottery class (10:00–11:00)",
        created_t_h=10.0, valid_until_t_h=13.0, salience=0.5,
        evidence="agenda_item:ag_0",
    )


def _fire(store, result) -> None:
    store.save_schedule_events(SEED, [{"t_h": 10.0, "day": 0, "reason": ""}])
    store.save_proactive_intent(_intent())
    sent = []

    async def _generate(gen):
        return await gen()

    async def _exact(intent):
        if result.declined:  # what the session's decline path does
            store.update_proactive_intent_status(intent.id, "suppressed")
        return result

    async def _send(res, **kw):
        sent.append(res)

    runtime = SimpleNamespace(
        store=store, seed=SEED, survive_turn_failures=True,
        schedule=ProactiveSchedule(event_hours=[10.0]),
        _generate_with_typing=_generate, _fire_exact_intent=_exact,
        _send_turn_outputs=_send,
    )
    asyncio.run(AsyncRuntime._fire_or_record_failure(runtime, _intent(), 10.0, 10.2, 0))
    assert sent == [result]


def _status(store) -> tuple[str, str]:
    slot = store.schedule_events_for_seed(SEED)[0]["status"]
    intent = store.conn.execute(
        "SELECT status FROM proactive_intents WHERE id = 'pi_1'").fetchone()
    return slot, intent["status"]


def test_a_decline_is_not_a_contact(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    _fire(store, SimpleNamespace(reply="", declined=True))
    assert _status(store) == ("suppressed", "suppressed")
    assert store.last_proactive_t_h(SEED) is None
    store.close()


def test_the_session_flags_its_decline(tmp_path):
    from harness.client import FakeClient
    from harness.clock import VirtualClock
    from harness.tools import DecisionConfig
    from tests.test_proactive_fire_decision import _intent as fd_intent
    from tests.test_proactive_fire_decision import _session

    store = SQLiteStore(tmp_path / "d.db")
    client = FakeClient(responses=[
        {"content": "",
         "tool_calls": [{"id": "c1", "name": "tool_decide_proactive",
                         "arguments_json": "{\"initiate\": false, \"reason\": \"later\"}"}]},
    ])
    session = _session(store, client, VirtualClock(t_h=10.0), decision=DecisionConfig())
    store.save_proactive_intent(fd_intent())
    assert session.fire_proactive("pi_1").declined is True
    store.close()


def test_a_sent_reach_out_is_a_contact(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    _fire(store, SimpleNamespace(reply="hey", declined=False))
    assert _status(store) == ("fired", "fired")
    assert store.last_proactive_t_h(SEED) == 10.2
    store.close()


def test_a_slot_announced_in_a_negotiation_is_not_offered(tmp_path):
    from harness.domain import AgendaItem, DailyAgenda
    from harness.proactive import IntentResolver

    store = SQLiteStore(tmp_path / "n.db")
    store.save_agenda(0, DailyAgenda(0, (AgendaItem(
        "ag_0_i_pottery", 11.0, 12.0, "trim the foot ring", "arc", "arc1",
        0.8, "planned"),)))
    resolver = IntentResolver(store)
    assert resolver.resolve(11.2) is not None
    store.log_event(0, 11.0, "negotiation_inform", "item=ag_0_i_pottery")
    assert store.negotiation_informed_items() == {"ag_0_i_pottery"}
    assert resolver.resolve(11.2) is None
    store.close()
