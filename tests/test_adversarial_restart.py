"""A9 adversarial wave — RESTART attack class (cases R-1..R-9 + case 40).

Restart = re-open the same DB file with a fresh Store/Session/Schedule, then resume."""

from __future__ import annotations

import asyncio

from engine.types import ADJ_SLOPE, MoodVariant, PersonaParams, TimingParams
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda, EpisodicMemory, MemoryKind
from harness.gates import context_gate
from harness.interests import build_catalog
from harness.judge import ScriptedJudge
from harness.memory import MemoryAgent
from harness.persona import build_persona
from harness.proactive import IntentResolver
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import (
    REASON_CHECK_IN,
    REASON_SCHEDULE,
    REASON_SHARED_INTEREST,
    ProactiveSchedule,
    day_initiative,
    day_scores,
    initiative_factor,
    plan_proactive_events,
)
from harness.session import Session
from harness.store import SQLiteStore
from tests.helpers import make_store

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345
LIFE_SEED = 12345

#: 0.5 s per virtual hour
SLOW = TimeScale(seconds_per_virtual_hour=0.5)




def _session(store, *, clock=None, profile=None, replies=None):
    return Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=FakeClient(responses=replies or ["ok!"]),
        clock=clock or VirtualClock(),
        judge=ScriptedJudge(score=0.5).judge_day,
        persona_profile=profile,
    )


def _ground_item(item_id: str, start_t_h: float, end_t_h: float,
                 activity: str = "pottery", salience: float = 0.8) -> AgendaItem:
    return AgendaItem(item_id, start_t_h, end_t_h, activity, "arc", "arc1",
                      salience, "planned")


def _run_runtime(store, session, schedule, channel, *, max_hours, clock_start_h=None,
                 scale=SLOW):
    """Run the real AsyncRuntime for a bounded horizon (injectable sleeper)."""
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    if clock_start_h is not None and session.clock.now_h() < clock_start_h:
        session.clock.advance_hours(clock_start_h - session.clock.now_h())
    runtime = AsyncRuntime(
        session, schedule, channel,
        store=store, timing=TIMING, seed=SEED,
        time_scale=scale, max_virtual_hours=max_hours,
        resolver=IntentResolver(store),
        sleeper=record,
    )
    asyncio.run(runtime.run())
    runtime.delays = delays
    return runtime


def _rows(store):
    return {float(r["t_h"]): r for r in store.schedule_events_for_seed(SEED)}


def _suppressed_codes(store):
    return {
        e["detail"]
        for e in store.events_since(0)
        if e["event"] == "proactive_suppressed"
    }


# R-1 .. R-6: restart timing attacks


def test_r1_restart_exactly_at_event_time_fires_once(tmp_path):
    """R-1: restart with the clock at exactly H — the event fires once and the
    fired row persists."""
    store = make_store(tmp_path, "r1.db")
    sched = ProactiveSchedule.plan_and_persist(2, SEED, PERSONA, TIMING, store)
    H = next(float(h) for h in sched.event_hours if h < 20.0)
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("g1", H - 1.0, H + 1.0),
    )))

    # restart: fresh store instance (same file) + restored schedule, clock at H
    store2 = make_store(tmp_path, "r1.db")
    restored = ProactiveSchedule.restore(SEED, store2)
    assert restored.next_pending(H) == H, "event invisible at now == H"
    session = _session(store2)
    channel = FakeChannel()
    _run_runtime(store2, session, restored, channel, max_hours=H + 0.5,
                 clock_start_h=H)

    sent = [m for m in channel.sent if m.proactive]
    assert len(sent) == 1, f"expected exactly one fire, got {len(sent)}"
    assert sent[0].reason == REASON_SCHEDULE
    rows = _rows(store2)
    assert rows[H]["status"] == "fired"
    assert rows[H]["fired_t_h"] == H
    fired = store2.list_proactive_intents(status="fired")
    assert len(fired) == 1
    assert store2.resolve_intent_source(fired[0]) is not None
    store2.close()


def test_r2_restart_ten_minutes_after_event_fires(tmp_path):
    """R-2: restart at H+10min, inside the 3h validity window — the overdue row
    fires, never stranded."""
    store = make_store(tmp_path, "r2.db")
    sched = ProactiveSchedule.plan_and_persist(2, SEED, PERSONA, TIMING, store)
    H = next(float(h) for h in sched.event_hours if h < 20.0)
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("g1", H - 1.0, H + 1.0),
    )))

    store2 = make_store(tmp_path, "r2.db")
    restored = ProactiveSchedule.restore(SEED, store2)
    restart_h = H + 10.0 / 60.0
    assert restored.next_pending(restart_h) == H, "overdue row stranded"
    session = _session(store2)
    channel = FakeChannel()
    _run_runtime(store2, session, restored, channel, max_hours=H + 0.5,
                 clock_start_h=restart_h)

    assert len([m for m in channel.sent if m.proactive]) == 1
    assert _rows(store2)[H]["status"] == "fired"
    store2.close()


def test_r3_restart_beyond_validity_expires_without_ghost(tmp_path):
    """R-3: restart at H+4h, past the 3h validity — the row expires, no message,
    no ghost firing on later polls."""
    store = make_store(tmp_path, "r3.db")
    sched = ProactiveSchedule.plan_and_persist(3, SEED, PERSONA, TIMING, store)
    H = next(float(h) for h in sched.event_hours if h < 20.0)
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("g1", H - 1.0, H + 1.0),
    )))

    store2 = make_store(tmp_path, "r3.db")
    restored = ProactiveSchedule.restore(SEED, store2)
    late = H + 4.0
    assert restored.next_pending(late) == H  # overdue row surfaced
    session = _session(store2)
    channel = FakeChannel()
    _run_runtime(store2, session, restored, channel, max_hours=H + 4.5,
                 clock_start_h=late)

    assert channel.sent == [], "expired event must not produce a message"
    assert _rows(store2)[H]["status"] == "expired"
    assert "expired" in _suppressed_codes(store2)
    # a later poll does not re-surface the expired row
    after = ProactiveSchedule.restore(SEED, store2)
    assert after.next_pending(H + 4.5) != H
    store2.close()


def test_r4_restart_during_quiet_hours_expires_by_policy_no_retry(tmp_path):
    """R-4: restart during quiet hours for an expired row — expires by policy, no
    retry; no new plan may land in quiet hours."""
    store = make_store(tmp_path, "r4.db")
    store.save_schedule_events(SEED, [
        {"t_h": 14.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("g1", 13.0, 15.0),
    )))

    # context gate at 03:00 is quiet_hours by itself
    assert context_gate(27.0, 1, store=store, timing=TIMING,
                        last_fired_t_h=None).code == "quiet_hours"

    store2 = make_store(tmp_path, "r4.db")
    restored = ProactiveSchedule.restore(SEED, store2)
    session = _session(store2)
    channel = FakeChannel()
    _run_runtime(store2, session, restored, channel, max_hours=27.5,
                 clock_start_h=27.0)

    assert channel.sent == []
    assert _rows(store2)[14.0]["status"] == "expired"
    after = ProactiveSchedule.restore(SEED, store2)
    assert after.next_pending(27.5) is None or after.next_pending(27.5) != 14.0
    # no new plan lands in quiet hours
    plan = ProactiveSchedule.plan(3, SEED, PERSONA, TIMING)
    for h in plan.event_hours:
        local = h % 24.0
        assert not (23.0 <= local or local < 8.0), f"event at quiet hour {local}"
    store2.close()


def test_r4b_quiet_hours_does_not_consume_still_valid_event(tmp_path):
    """R-4b: a still-valid overdue event recovered during quiet hours is deferred,
    never consumed as fired."""
    store = make_store(tmp_path, "r4b.db")
    store.save_schedule_events(SEED, [
        {"t_h": 23.5, "day": 0, "reason": REASON_CHECK_IN},  # valid until 11:30 d+1
    ])
    # ground a check-in anchor: a recent user turn + an episode
    store.add_message("user", "long talk about the trip", 22.0, 0,
                      proactive=False, session_id="day-0")
    store.add_message("assistant", "sounds lovely", 22.1, 0,
                      proactive=False, session_id="day-0")
    from harness.domain import EpisodicMemory, MemoryKind
    store.insert_episode(EpisodicMemory(
        "ep_anchor", "we talked about the trip", MemoryKind.SHARED_EPISODE,
        22.0, 22.1, 0.6, 0, None, None, "day-0", (1,), ("long talk about the trip",), ("trip",),
    ))

    store2 = make_store(tmp_path, "r4b.db")
    restored = ProactiveSchedule.restore(SEED, store2)
    session = _session(store2)
    channel = FakeChannel()
    _run_runtime(store2, session, restored, channel, max_hours=27.5,
                 clock_start_h=27.0)

    assert channel.sent == [], "no message during quiet hours"
    row = _rows(store2)[23.5]
    # still valid (until 11:30), not consumed as fired-without-delivery
    assert row["status"] in ("pending", "expired"), (
        f"still-valid overdue event consumed as {row['status']} during "
        "quiet hours — deferred firing is lost"
    )
    store2.close()


def test_r5_two_missed_events_evaluated_independently(tmp_path):
    """R-5: restart at 14:00 — the expired row expires, the still-valid row fires
    with its own grounded source."""
    store = make_store(tmp_path, "r5.db")
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
        {"t_h": 12.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("gA", 9.5, 10.5, activity="morning pottery"),
        _ground_item("gB", 11.5, 12.5, activity="evening run"),
    )))

    store2 = make_store(tmp_path, "r5.db")
    restored = ProactiveSchedule.restore(SEED, store2)
    assert restored.next_pending(14.0) == 10.0  # oldest overdue first
    session = _session(store2)
    channel = FakeChannel()
    _run_runtime(store2, session, restored, channel, max_hours=14.5,
                 clock_start_h=14.0)

    rows = _rows(store2)
    assert rows[10.0]["status"] == "expired"
    assert rows[12.0]["status"] == "fired"
    sent = [m for m in channel.sent if m.proactive]
    assert len(sent) == 1
    fired = store2.list_proactive_intents(status="fired")
    assert len(fired) == 1
    assert fired[0].source_id == "gB", "H2's message must use H2's own source"
    assert store2.resolve_intent_source(fired[0]) is not None
    store2.close()


def test_r5b_two_missed_events_both_valid(tmp_path):
    """R-5b: restart at 13:00 with both rows still valid — the first fires, the
    second is subject to cooldown/daily-cap, neither is stranded."""
    store = make_store(tmp_path, "r5b.db")
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
        {"t_h": 12.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("gA", 9.5, 10.5),
        _ground_item("gB", 11.5, 12.5),
    )))

    store2 = make_store(tmp_path, "r5b.db")
    restored = ProactiveSchedule.restore(SEED, store2)
    session = _session(store2)
    channel = FakeChannel()
    _run_runtime(store2, session, restored, channel, max_hours=13.5,
                 clock_start_h=13.0)

    rows = _rows(store2)
    assert rows[10.0]["status"] == "fired"  # oldest valid fires first
    assert rows[12.0]["status"] == "fired"  # consumed (fired or suppressed)
    assert len([m for m in channel.sent if m.proactive]) == 1
    codes = _suppressed_codes(store2)
    assert codes <= {"cooldown", "daily_cap", "quiet_hours"}
    store2.close()


def test_r6_restart_at_midnight_day_boundary(tmp_path):
    """R-6: restart at midnight — the day-0 row stays visible, the day-1 cap
    starts fresh, and day 0's score feeds day 1's hazard."""
    store = make_store(tmp_path, "r6.db")
    # set up a finalized day 0 so the judgement feeds day 1's hazard
    s0 = _session(store)
    s0.clock.advance_hours(19.0)
    s0.on_message("good evening")
    s0.clock.advance_to_day(1)
    s0.ensure_day(1)
    assert store.load_judgement(0) is not None
    store.save_schedule_events(SEED, [
        {"t_h": 23.5, "day": 0, "reason": REASON_SCHEDULE},   # valid until 02:30 d+1
        {"t_h": 33.0, "day": 1, "reason": REASON_SCHEDULE},   # 09:00 d+1 (past the 08:00 ramp)
    ])
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("gN", 22.5, 24.5),
    )))
    store.save_agenda(1, DailyAgenda(1, (
        _ground_item("gM", 32.0, 34.0),
    )))

    store2 = make_store(tmp_path, "r6.db")
    restored = ProactiveSchedule.restore(SEED, store2)
    assert restored.next_pending(24.0) == 23.5, "day-0 row invisible at midnight"
    session = _session(store2)
    channel = FakeChannel()
    _run_runtime(store2, session, restored, channel, max_hours=34.0,
                 clock_start_h=24.0)

    rows = _rows(store2)
    assert rows[23.5]["status"] == "fired"  # evaluated (suppressed: quiet hours)
    assert rows[33.0]["status"] == "fired"
    # the day-0 row fired at midnight took no day-1 cap slot
    assert store2.proactive_count(1) == 1, "only the day-1 09:00 message counts"
    # day 0's score is still the one feeding day 1's hazard
    from harness.scheduler import day_initiative

    scores = day_scores(store2, 1, TIMING)
    j0 = store2.load_judgement(0)
    a = 1.0 + ADJ_SLOPE * float(j0["score"])
    a = max(0.7, min(1.3, a))
    init = day_initiative(store2, 1, TIMING)
    expected = (a * initiative_factor(init) - 1.0) / ADJ_SLOPE
    assert abs(scores[0] - expected) < 1e-9, (
        "day-0 judgement did not reach day-1's hazard"
    )
    assert a != 1.0, "score=0.5 must move the adjustment away from neutral"
    store2.close()


# R-7 .. R-9: agenda / memory / judge restart attacks


def test_r7_restart_after_agenda_generation_before_completion(tmp_path):
    """R-7: kill mid-item and resume — same current item, agenda not regenerated,
    completing the row marks it exactly once."""
    store = make_store(tmp_path, "r7.db")
    profile = build_persona(LIFE_SEED, graph=build_catalog())
    store.save_persona(profile)
    session = _session(store, profile=profile)
    session.clock.advance_to_day(5)
    session.ensure_day(5)
    agenda_pre = store.load_agenda(5)
    assert agenda_pre is not None and agenda_pre.items
    ids_pre = [it.id for it in agenda_pre.items]
    planned = [it for it in agenda_pre.items if it.status == "planned"]
    assert planned, "day 5 agenda must contain planned items"
    item = planned[0]
    t_h = item.start_t_h + (item.end_t_h - item.start_t_h) / 2.0  # in progress
    ca_pre = session._current_activity(5, t_h)
    assert ca_pre is not None and ca_pre.item is not None
    assert ca_pre.item.id == item.id
    idle_h = 5 * 24.0 + 15.0
    if not any(it.start_t_h <= idle_h < it.end_t_h for it in agenda_pre.items):
        # no current activity outside every window
        assert session._current_activity(5, idle_h) is None

    # kill + resume at the same moment
    store2 = make_store(tmp_path, "r7.db")
    session2 = _session(store2, profile=profile)
    session2.clock.advance_to_day(5)
    session2.ensure_day(5)  # agenda exists, not regenerated
    agenda_post = store2.load_agenda(5)
    assert [it.id for it in agenda_post.items] == ids_pre, (
        "agenda regenerated after restart (duplicate items)"
    )
    ca_post = session2._current_activity(5, t_h)
    assert ca_post is not None and ca_post.item.id == ca_pre.item.id
    assert ca_post.description == ca_pre.description

    # complete the original row once
    store2.update_agenda_item_status(ca_post.item.id, "completed")
    rows = store2.list_agenda_items(day=5)
    matches = [it for it in rows if it.id == ca_post.item.id]
    assert len(matches) == 1 and matches[0].status == "completed"
    store.close()
    store2.close()


def test_r8_restart_after_memory_write_no_loss_no_dupes(tmp_path):
    """R-8: kill after record_turn or after promotion — turns and episodes
    survive, and close/promote are idempotent."""
    from harness.memory import MemoryAgent

    path = tmp_path / "r8.db"
    # (a) kill after record_turn only
    s1 = SQLiteStore(path)
    a1 = MemoryAgent(s1)
    a1.record_turn("user", "I have a cat named Luna", 50.0, "day-2")
    a1.record_turn("assistant", "aww", 50.1, "day-2")
    s1.close()

    s2 = SQLiteStore(path)
    a2 = MemoryAgent(s2)
    assert len(s2.turns_for_session("day-2")) == 2, "L1 lost after restart"
    summary = a2.close_session("day-2", ended_at_t_h=72.0)
    assert summary.source_turn_ids
    promoted = a2.promote(summary)
    assert promoted, "fact session should promote"
    # (b) kill after promotion
    s3 = SQLiteStore(path)
    a3 = MemoryAgent(s3)
    ctx = a3.retrieve("cat", context={"t_h": 200.0})
    luna = [e for e in ctx.episodes if "Luna" in e.summary]
    assert luna, "episode did not survive restart"
    assert all(e.verbatim_anchors for e in luna), "episode lost its anchors"
    # idempotent re-close + re-promote
    n_summaries = len(s3.conn.execute(
        "SELECT session_id FROM memory_session_summaries WHERE session_id='day-2'"
    ).fetchall())
    n_eps = len(s3.list_episodes())
    a3.close_session("day-2", ended_at_t_h=72.0)
    a3.promote(summary)
    assert len(s3.conn.execute(
        "SELECT session_id FROM memory_session_summaries WHERE session_id='day-2'"
    ).fetchall()) == n_summaries == 1
    assert len(s3.list_episodes()) == n_eps, "promote duplicated episodes"
    s3.close()


def test_r9a_restart_before_judge_finalization_neutral_fallback(tmp_path):
    """R-9(a): resume with no judgement — no crash, no scores=None, neutral A=1
    fallback."""
    store = make_store(tmp_path, "r9a.db")
    s0 = _session(store)
    s0.clock.advance_hours(19.0)
    s0.on_message("hello")  # day 0 runs but is not finalized
    scores = day_scores(store, 1, TIMING)
    assert scores is not None and len(scores) == 2
    assert float(scores[0]) == 0.0, "no judgement ⇒ neutral A=1, score 0"
    # live-style planning with the concrete array sees no None
    events = plan_proactive_events(2, SEED, PERSONA, TIMING, scores=scores)
    assert len(events) > 0
    ProactiveSchedule.plan_and_persist(2, SEED, PERSONA, TIMING, store, scores=scores)
    store.close()


def test_r9b_restart_after_judge_finalization_score_feeds_hazard(tmp_path):
    """R-9(b): a finalized day's score feeds the next day's hazard; the judge
    never re-runs for a finalized day."""
    store = make_store(tmp_path, "r9b.db")
    s0 = _session(store)
    s0.clock.advance_hours(19.0)
    s0.on_message("a warm evening")
    s0.clock.advance_to_day(1)
    s0.ensure_day(1)
    j0 = store.load_judgement(0)
    assert j0 is not None and float(j0["score"]) == 0.5
    s0.finalize_current()  # finalizes the CURRENT day (1); idempotent re-run
    s0.finalize_current()
    rows = [(r["day"], r["score"]) for r in
            store.conn.execute("SELECT day, score FROM judgements ORDER BY day")]
    # day 0 was judged on its transcript (0.5); day 1 has no interaction (0.0)
    assert rows == [(0, 0.5), (1, 0.0)], (
        "judge re-ran for an already-finalized day (duplicate judgement)"
    )
    finalized = [e for e in store.events_since(0)
                 if e["event"] == "day_finalized" and e["day"] == 1]
    assert len(finalized) == 1

    scores = day_scores(store, 1, TIMING)
    a = 1.0 + ADJ_SLOPE * 0.5
    a = max(0.7, min(1.3, a))
    init = initiative_factor(day_initiative(store, 1, TIMING))
    assert abs(scores[0] - (a * init - 1.0) / ADJ_SLOPE) < 1e-9, (
        "day-0 score × day-1 initiative did not reach the hazard"
    )
    store.close()


# Case 40: finalize_day crash window


def test_case40_finalize_crash_window_no_lost_memory_or_life(tmp_path):
    """CASE 40: process death between the conversation-close persist and its
    memory tail — the tail is recovered on resume."""
    profile = build_persona(LIFE_SEED, graph=build_catalog())

    def crashed_run(path):
        store = SQLiteStore(path)
        store.save_persona(profile)
        session = _session(store, profile=profile)
        session.clock.advance_hours(19.0)
        session.on_message("I have a cat named Luna")

        def boom(*a, **k):
            raise RuntimeError("process died between close persist and memory tail")

        session._close_conversation_memory = boom
        conv = session._conversation
        assert conv is not None
        try:
            # 23.0 = a quiet-hours boundary close inside the day
            session._close_conversation(conv, 23.0, "quiet_hours")
        except RuntimeError:
            pass  # the simulated process death
        closed_conv = store.load_conversation("conv-0")
        assert closed_conv is not None and closed_conv.close_reason == "quiet_hours"
        store.close()
        # resume with a fresh Store/Session: the recovery re-runs the tail
        store2 = SQLiteStore(path)
        s2 = _session(store2, profile=profile)
        s2.clock.advance_to_day(1)
        s2.ensure_day(1)
        return store2

    def control_run(path):
        store = SQLiteStore(path)
        store.save_persona(profile)
        session = _session(store, profile=profile)
        session.clock.advance_hours(19.0)
        session.on_message("I have a cat named Luna")
        session.clock.advance_to_day(1)
        session.ensure_day(1)  # clean finalize of day 0
        store.close()
        store2 = SQLiteStore(path)
        s2 = _session(store2, profile=profile)
        s2.clock.advance_to_day(1)
        s2.ensure_day(1)
        return store2

    crashed = crashed_run(tmp_path / "crash.db")
    control = control_run(tmp_path / "control.db")

    # no double-advance in the crash case
    assert len(crashed.conn.execute("SELECT day FROM judgements").fetchall()) == 1

    # Conversation + memory state matches the control or is recoverable.
    # (memory sessions key off conversations.)
    ctrl_conv = control.load_open_conversation()
    assert ctrl_conv is not None and ctrl_conv.close_reason is None, (
        "control conversation must still be open"
    )
    assert crashed.load_conversation("conv-0") is not None, (
        "closed conversation lost on resume"
    )
    assert crashed.load_session_summary("day-1000") is not None, (
        "conversation memory tail lost on resume after the close crash"
    )
    # L1 episodes key off the conversation boundary; the open control
    # has none, so the crashed run shows the recovered memory.
    crash_eps = {(e.id, e.summary) for e in crashed.list_episodes()}
    assert crash_eps, (
        "crashed run lost its recovered episodes"
    )
    assert crashed.get_assertion("user:cat") is not None, (
        "L4 assertion for the crashed conversation lost on resume"
    )
    ctrl_arcs = {(a.id, a.progress, a.status) for a in control.list_life_arcs()}
    crash_arcs = {(a.id, a.progress, a.status) for a in crashed.list_life_arcs()}
    assert crash_arcs == ctrl_arcs, (
        "life step for the crashed day silently skipped on resume (divergence)"
    )
    crashed.close()
    control.close()


def test_case40_finalize_no_double_advance_on_resume(tmp_path):
    """CASE 40 guard leg: a clean finalize followed by resume does not
    double-advance."""
    profile = build_persona(LIFE_SEED, graph=build_catalog())
    store = make_store(tmp_path, "c40.db")
    store.save_persona(profile)
    session = _session(store, profile=profile)
    session.clock.advance_hours(19.0)
    session.on_message("I have a cat named Luna")
    session.clock.advance_to_day(1)
    session.ensure_day(1)
    arcs_pre = {(a.id, a.progress, a.status) for a in store.list_life_arcs()}
    eps_pre = {(e.id, e.summary) for e in store.list_episodes()}
    n_assertions = len(store.list_assertions())

    store2 = make_store(tmp_path, "c40.db")
    s2 = _session(store2, profile=profile)
    s2.clock.advance_to_day(1)
    s2.ensure_day(1)
    assert len(store2.conn.execute("SELECT day FROM judgements").fetchall()) == 1
    assert {(e.id, e.summary) for e in store2.list_episodes()} == eps_pre
    # the exchange lives in open conversation conv-0, which survives
    # the clean finalize + resume without rewind.
    conv = store2.load_open_conversation()
    assert conv is not None and len(conv.turns) == 2
    assert len(store2.list_assertions()) == n_assertions
    assert {(a.id, a.progress, a.status) for a in store2.list_life_arcs()} == arcs_pre
    store.close()
    store2.close()


# R-10 / V-1: restart across quiet boundary + message provenance

FAST = TimeScale(seconds_per_virtual_hour=0.02)

#: 12:00 local, outside the check-in windows (8-11, 19-22)
NOW_H = 300.0


def test_r10_restart_across_quiet_boundary_delivers_still_valid_event(tmp_path):
    """R-10: a 12h-validity shared-interest event at 23:30 survives three
    restarts and is delivered exactly once at 09:00."""
    from harness.bootstrap import ensure_companion_initialized
    from harness.domain import EpisodicMemory, MemoryKind, UserProfile

    store = make_store(tmp_path, "r10.db")
    store.save_schedule_events(SEED, [
        {"t_h": 23.5, "day": 0, "reason": REASON_SHARED_INTEREST},
    ])
    ensure_companion_initialized(
        store, seed=SEED, user=UserProfile(name="u", interests=("pottery",))
    )
    # register the episode's source session
    store.open_session("day-0", 22.0)
    store.close_session("day-0", 22.7)
    store.insert_episode(EpisodicMemory(
        "ep_si", "user talked about pottery class", MemoryKind.SHARED_EPISODE,
        22.5, 22.6, 0.8, 0, None, None,
        "day-0", (1,), ("pottery class is fun",), ("pottery",),
    ))

    # phase 1: run to 23:00 — the event is still in the future, untouched
    s1 = make_store(tmp_path, "r10.db")
    _run_runtime(s1, _session(s1), ProactiveSchedule.restore(SEED, s1),
                 FakeChannel(), max_hours=23.0, clock_start_h=None)
    assert _rows(s1)[23.5]["status"] == "pending", (
        "event consumed before its own time"
    )

    # phase 2: restart at 03:00 (quiet) — deferred, no message during it
    s2 = make_store(tmp_path, "r10.db")
    chan2 = FakeChannel()
    _run_runtime(s2, _session(s2), ProactiveSchedule.restore(SEED, s2),
                 chan2, max_hours=34.0, clock_start_h=26.0)
    assert chan2.sent == [], "message sent during quiet hours"
    row2 = _rows(s2)[23.5]
    assert row2["status"] in ("pending", "fired"), (
        f"still-valid event consumed as {row2['status']!r} during quiet "
        "hours — the deferral must not consume it (r4b)"
    )

    # phase 3: restart at 09:00 — fully awake — the event fires exactly once
    s3 = make_store(tmp_path, "r10.db")
    chan3 = FakeChannel()
    _run_runtime(s3, _session(s3), ProactiveSchedule.restore(SEED, s3),
                 chan3, max_hours=34.5, clock_start_h=33.0)
    fired = [m for m in chan3.sent if m.proactive]
    assert len(fired) == 1, f"expected exactly one delivery, got {len(fired)}"
    row3 = _rows(s3)[23.5]
    assert row3["status"] == "fired"
    assert "expired" not in _suppressed_codes(s3)
    # the delivered message carries the validated intent id, resolvable to a source
    last = s3.recent_messages()[-1]
    assert last["intent_id"], "delivered message missing intent provenance"
    intent = s3.load_proactive_intent(last["intent_id"])
    assert intent is not None
    assert s3.resolve_intent_source(intent) is not None
    s1.close()
    s2.close()
    s3.close()


def test_v1_every_proactive_message_carries_real_intent_id(tmp_path):
    """Every proactive message row carries the intent_id of a real stored intent
    with a resolvable source; reactive rows keep intent_id None."""
    store = make_store(tmp_path, "v1.db")
    sched = ProactiveSchedule.plan_and_persist(2, SEED, PERSONA, TIMING, store)
    h = next(float(x) for x in sched.event_hours if x < 20.0)
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("g1", h - 0.5, h + 0.5),
    )))
    session = _session(store)
    session.on_message("hi there")  # one reactive row for the contrast leg
    channel = FakeChannel()
    _run_runtime(store, session, sched, channel, max_hours=h + 2.0,
                 clock_start_h=None, scale=FAST)

    assert len([m for m in channel.sent if m.proactive]) == 1
    rows = store.recent_messages(limit=100)
    proactive = [m for m in rows if m["proactive"]]
    reactive = [m for m in rows if not m["proactive"]]
    assert len(proactive) == 1
    assert reactive, "precondition: reactive message exists"
    for row in proactive:
        assert row["intent_id"], "proactive message without intent provenance"
        intent = store.load_proactive_intent(row["intent_id"])
        assert intent is not None, (
            f"message intent_id {row['intent_id']!r} is not a stored intent"
        )
        assert store.resolve_intent_source(intent) is not None, (
            f"message intent {row['intent_id']!r} points at a missing source"
        )
        assert intent.id in {
            i.id for i in store.list_proactive_intents(status="fired")
        }
        assert intent.reason in ("schedule", "event", "callback",
                                 "shared_interest", "check_in")
    for row in reactive:
        assert row["intent_id"] is None, (
            "reactive message polluted with intent provenance"
        )
    store.close()


def test_v1b_intent_provenance_survives_restart(tmp_path):
    """A restart keeps the provenance chain intact: message.intent_id unchanged,
    the intent row stored as fired, its source still resolvable."""
    store = make_store(tmp_path, "v1b.db")
    sched = ProactiveSchedule.plan_and_persist(2, SEED, PERSONA, TIMING, store)
    h = next(float(x) for x in sched.event_hours if x < 20.0)
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("g1", h - 0.5, h + 0.5),
    )))
    _run_runtime(store, _session(store), sched, FakeChannel(),
                 max_hours=h + 2.0, clock_start_h=None, scale=FAST)
    before = {
        m["id"]: m["intent_id"] for m in store.recent_messages(limit=100)
        if m["proactive"]
    }
    assert before, "precondition: a proactive message fired"
    store.close()

    store2 = make_store(tmp_path, "v1b.db")
    after = {
        m["id"]: m["intent_id"] for m in store2.recent_messages(limit=100)
        if m["proactive"]
    }
    assert after == before, "message intent provenance changed across restart"
    for intent_id in after.values():
        intent = store2.load_proactive_intent(intent_id)
        assert intent is not None, "intent row lost across restart"
        assert intent.id in {
            i.id for i in store2.list_proactive_intents(status="fired")
        }
        assert store2.resolve_intent_source(intent) is not None, (
            "intent source unresolvable across restart"
        )
    store2.close()


def test_v1c_callback_provenance_required_end_to_end(tmp_path):
    """A callback whose source session is deleted is suppressed (no_source); the
    stale intent is never attached to a message."""
    store = make_store(tmp_path, "v1c.db")
    store.open_session("day-12", 288.0)
    agent = MemoryAgent(store)
    agent.record_turn("user", "remind me to send the playlist", 290.0, "day-12")
    agent.record_turn("assistant", "sure", 290.1, "day-12")
    agent.close_session("day-12", ended_at_t_h=291.0)
    tid = store.turns_for_session("day-12")[0]["id"]
    store.insert_episode(EpisodicMemory(
        "ep_cb", "user asked to be reminded to send the playlist",
        MemoryKind.CALLBACK, 290.0, 290.5, 0.8, 0, None, None,
        "day-12", (tid,), ("remind me to send the playlist",), ("callback",),
    ))
    assert store.session_exists("day-12"), "precondition: source session exists"
    intent = IntentResolver(store).resolve(NOW_H)
    assert intent is not None and intent.source_type == "callback"
    assert store.resolve_intent_source(intent) is not None
    store.save_proactive_intent(intent)
    store.save_schedule_events(SEED, [
        {"t_h": NOW_H, "day": 12, "reason": "callback"},
    ])
    # break the provenance chain: the witnessing session disappears
    store.conn.execute("DELETE FROM memory_sessions WHERE session_id = 'day-12'")
    store.conn.commit()
    assert not store.session_exists("day-12")

    store2 = make_store(tmp_path, "v1c.db")
    channel = FakeChannel()
    _run_runtime(store2, _session(store2),
                 ProactiveSchedule.restore(SEED, store2), channel,
                 max_hours=NOW_H + 1.0, clock_start_h=NOW_H, scale=FAST)
    assert channel.sent == [], (
        "callback with broken session provenance produced a message"
    )
    assert "no_source" in _suppressed_codes(store2)
    # no message row carries the stale intent id
    for m in store2.recent_messages(limit=100):
        assert m["intent_id"] is None, "suppressed intent attached to a message"
    store.close()
    store2.close()
