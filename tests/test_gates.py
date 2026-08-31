"""Gate tests (wave 2, seam A-4; A7) — the shared contract A3 relies on.

Since A7 the content gate is REAL: ``content_gate(intent, store)`` verifies
groundedness against the store (source exists, not deleted/superseded,
timely, hook actually attached to the source) instead of checking a reason
taxonomy. context_gate is unchanged; both use the REAL defaults and the
seam-faithful SeamStore (A2 store ops have not landed in this repo yet).
"""

from engine.circadian import envelope
from engine.types import TimingParams
from harness.domain import AgendaItem, DailyAgenda
from harness.gates import GateDecision, content_gate, context_gate
from harness.proactive import IntentResolver, compose_hook
from harness.scheduler import REASON_SCHEDULE, REASON_VALIDITY_H
from tests.helpers import SeamStore, agenda_item

TIMING = TimingParams()

AGENDA_HOUR = 14.0


def _store():
    return SeamStore()




def _seed_proactives(store, day: int, n: int) -> None:
    for i in range(n):
        store.add_message("assistant", f"p{i}", t_h=float(i), day=day, proactive=True)


def _grounded_intent(store, *, now_h=None, item=None):
    """A real intent from the resolver over a seeded agenda item."""
    now_h = AGENDA_HOUR if now_h is None else now_h
    item = item if item is not None else agenda_item(start=now_h, end=now_h + 1.0)
    store.save_agenda(0, DailyAgenda(0, (item,)))
    intent = IntentResolver(store).resolve(now_h + 0.1)
    assert intent is not None
    return intent


# ---- content_gate (groundedness) ----


def test_content_gate_ok_when_fully_grounded():
    store = _store()
    intent = _grounded_intent(store)
    decision = content_gate(intent, store, now_h=intent.created_t_h)
    assert decision.allowed is True
    assert decision.code == "ok"


def test_content_gate_none_intent():
    store = _store()
    decision = content_gate(None, store, now_h=AGENDA_HOUR)
    assert decision.allowed is False
    assert decision.code == "no_valid_reason"


def test_content_gate_expired_past_validity():
    store = _store()
    intent = _grounded_intent(store)
    decision = content_gate(
        intent, store, now_h=intent.valid_until_t_h + 0.01
    )
    assert decision.allowed is False
    assert decision.code == "expired"


def test_content_gate_ok_at_validity_boundary():
    store = _store()
    intent = _grounded_intent(store)
    decision = content_gate(intent, store, now_h=intent.valid_until_t_h)
    assert decision.allowed is True
    assert decision.code == "ok"


def test_content_gate_no_source_when_item_deleted():
    store = _store()
    intent = _grounded_intent(store)
    # The agenda item is gone before firing.
    store._agenda_items.clear()
    decision = content_gate(intent, store, now_h=intent.created_t_h)
    assert decision.allowed is False
    assert decision.code == "no_source"


def test_content_gate_no_source_when_source_type_unknown():
    store = _store()
    intent = _grounded_intent(store)
    # The store cannot resolve the altered source_type.
    from dataclasses import replace
    forged = replace(intent, source_type="hallucinated_source")
    decision = content_gate(forged, store, now_h=intent.created_t_h)
    assert decision.allowed is False
    assert decision.code == "no_source"


def test_content_gate_source_superseded_skippedagenda_item():
    store = _store()
    intent = _grounded_intent(store)
    store.update_agenda_item_status(intent.source_id, "skipped")
    decision = content_gate(intent, store, now_h=intent.created_t_h)
    assert decision.allowed is False
    assert decision.code == "source_superseded"


def test_content_gate_source_superseded_abandoned_arc():
    store = _store()
    # A LifeArc-sourced intent.
    from dataclasses import replace
    from harness.domain import LifeArc
    from harness.proactive import compose_hook
    arc = LifeArc(
        id="arc_pottery", name="pottery", interest="pottery",
        started_day=0, progress=0.3, status="active", next_intention="glaze",
    )
    store.upsert_life_arc(arc)
    base = _grounded_intent(store)
    intent = replace(
        base, source_type="arc", source_id=arc.id,
        hook=compose_hook(arc, REASON_SCHEDULE),
    )
    assert content_gate(intent, store, now_h=intent.created_t_h).allowed
    store.update_life_arc_status(arc.id, "abandoned")
    decision = content_gate(intent, store, now_h=intent.created_t_h)
    assert decision.allowed is False
    assert decision.code == "source_superseded"


def test_content_gate_hook_mismatch():
    store = _store()
    intent = _grounded_intent(store)
    from dataclasses import replace
    forged = replace(intent, hook="Contact reason: schedule")
    decision = content_gate(forged, store, now_h=intent.created_t_h)
    assert decision.allowed is False
    assert decision.code == "hook_mismatch"


def test_content_gate_hook_rederives_from_source():
    store = _store()
    item = agenda_item(start=AGENDA_HOUR, end=AGENDA_HOUR + 1.0)
    intent = _grounded_intent(store, item=item)
    assert compose_hook(item, REASON_SCHEDULE) == intent.hook
    # The hook is re-derived from the source.
    source = store.resolve_intent_source(intent)
    assert compose_hook(source, intent.reason) == intent.hook


def test_content_gate_episode_sources_pass_status_checks():
    store = _store()
    from harness.domain import EpisodicMemory, MemoryKind

    ep = EpisodicMemory(
        id="ep_g", summary="shared moment", category=MemoryKind.CALLBACK,
        occurred_at_t_h=0.0, created_at_t_h=0.0, importance=0.6,
        access_count=0, last_accessed_t_h=None, affect=None,
        source_session_id="s1", source_turn_ids=(1,),
        verbatim_anchors=("exact source text",), tags=("t",),
    )
    store.insert_episode(ep)
    intent = IntentResolver(store).resolve(AGENDA_HOUR)
    assert intent is not None
    decision = content_gate(intent, store, now_h=AGENDA_HOUR)
    assert decision.allowed is True
    assert decision.code == "ok"


def test_content_gate_timeliness_is_separate_from_source():
    # Intent long past its window is expired although the source exists.
    store = _store()
    intent = _grounded_intent(store)
    decision = content_gate(intent, store, now_h=intent.valid_until_t_h + 48.0)
    assert decision.allowed is False
    assert decision.code == "expired"


# ---- context_gate ----


def test_context_gate_ok_when_all_hold():
    store = _store()
    decision = context_gate(
        14.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is True
    assert decision.code == "ok"
    store.close()


def test_envelope_sanity_default_timing():
    # Quiet hours cross midnight: 2.0 is quiet, 14.0 is awake.
    assert envelope(2.0, TIMING) == 0.0
    assert envelope(14.0, TIMING) > 1e-9


def test_context_gate_quiet_hours():
    store = _store()
    decision = context_gate(
        2.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is False
    assert decision.code == "quiet_hours"
    store.close()


def test_context_gate_quiet_hours_after_midnight_rollover():
    # 26.0 is day 1 at 02:00 local, still quiet.
    store = _store()
    decision = context_gate(
        26.0, day=1, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.code == "quiet_hours"
    store.close()


def test_context_gate_cooldown_via_last_fired():
    store = _store()
    min_gap_h = TIMING.min_gap_min / 60.0
    # Just under the gap: cooldown.
    blocked = context_gate(
        14.0, day=0, store=store, timing=TIMING,
        last_fired_t_h=14.0 - min_gap_h + 0.01,
    )
    assert blocked.allowed is False
    assert blocked.code == "cooldown"
    # Exactly at the gap: allowed.
    ok = context_gate(
        14.0, day=0, store=store, timing=TIMING,
        last_fired_t_h=14.0 - min_gap_h,
    )
    assert ok.allowed and ok.code == "ok"
    store.close()


def test_context_gate_no_last_fired_passes_cooldown():
    store = _store()
    decision = context_gate(
        14.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is True
    store.close()


def test_context_gate_daily_cap():
    store = _store()
    _seed_proactives(store, day=0, n=TIMING.daily_cap)
    blocked = context_gate(
        14.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert blocked.allowed is False
    assert blocked.code == "daily_cap"
    store.close()


def test_context_gate_daily_cap_under_limit():
    store = _store()
    _seed_proactives(store, day=0, n=TIMING.daily_cap - 1)
    decision = context_gate(
        14.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is True
    assert decision.code == "ok"
    store.close()


def test_context_gate_daily_cap_is_per_day():
    store = _store()
    _seed_proactives(store, day=0, n=TIMING.daily_cap)
    # Day 1 has no proactive messages yet: cap does not block.
    decision = context_gate(
        14.0 + 24.0, day=1, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is True
    store.close()


def test_context_gate_reactive_messages_do_not_count():
    store = _store()
    store.add_message("user", "hi", t_h=1.0, day=0, proactive=False)
    decision = context_gate(
        14.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is True
    store.close()


def test_context_gate_first_failing_code_wins():
    store = _store()
    _seed_proactives(store, day=0, n=TIMING.daily_cap)
    min_gap_h = TIMING.min_gap_min / 60.0
    # All three failing: quiet_hours is reported first.
    quiet = context_gate(
        2.0, day=0, store=store, timing=TIMING,
        last_fired_t_h=2.0 - min_gap_h + 0.01,
    )
    assert quiet.code == "quiet_hours"
    # Awake with cooldown + cap failing: cooldown is reported first.
    cooldown = context_gate(
        14.0, day=0, store=store, timing=TIMING,
        last_fired_t_h=14.0 - min_gap_h + 0.01,
    )
    assert cooldown.code == "cooldown"
    store.close()


def test_gate_decision_is_frozen_dataclass():
    decision = GateDecision(allowed=True, code="ok")
    assert decision.allowed and decision.code == "ok"
    try:
        decision.allowed = False
    except Exception as exc:  # noqa: BLE001 - frozen dataclass raises
        assert isinstance(exc, (AttributeError, TypeError))
    else:
        raise AssertionError("GateDecision must be immutable")
