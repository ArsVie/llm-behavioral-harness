"""A9 adversarial wave — RESTART attack class (plan §9, cases R-1..R-9 + case 40).

Every test attacks the INTEGRATED system (session + store + scheduler +
runtime + memory + life). Restart = persist → close → re-open the SAME DB
file with a fresh Store/Session/Schedule, then resume. Assertions target
continuity invariants: no lost state, no stranded overdue events, no
duplicated firings, no divergence.

Case 40 (A1-flagged): process death between save_judgement and the
memory/life steps inside session.finalize_day — on resume the day's
L2/L3/L4 close_session/promote/update_user_model + life step must not be
silently lost.
"""

from __future__ import annotations

import asyncio

from engine.types import ADJ_SLOPE, MoodVariant, PersonaParams, TimingParams
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda, EpisodicMemory, MemoryKind
from harness.gates import content_gate, context_gate
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

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345
LIFE_SEED = 12345

#: 0.5 s per virtual hour — robust against the rollover-vs-firing race
#: (same rationale as tests/test_runtime.py SLOW).
SLOW = TimeScale(seconds_per_virtual_hour=0.5)


def _store(tmp_path, name: str) -> SQLiteStore:
    return SQLiteStore(tmp_path / name)


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


# --------------------------------------------------------------------------- #
# R-1 .. R-6: restart timing attacks
# --------------------------------------------------------------------------- #


def test_r1_restart_exactly_at_event_time_fires_once(tmp_path):
    """R-1: event planned at hour H, killed at H−0.01, restarted with clock at
    exactly H. The event must be VISIBLE at now == H (regression on the old
    strict `h > t_h` bug), pass gates, resolve to a grounded source, fire
    exactly once, and the fired row must persist."""
    store = _store(tmp_path, "r1.db")
    sched = ProactiveSchedule.plan_and_persist(2, SEED, PERSONA, TIMING, store)
    H = next(float(h) for h in sched.event_hours if h < 20.0)
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("g1", H - 1.0, H + 1.0),
    )))

    # restart: fresh store instance (same file) + restored schedule, clock at H
    store2 = _store(tmp_path, "r1.db")
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
    """R-2: killed at H−0.01, restarted at H+10min (within the 3h validity
    window). The overdue pending row must be EVALUATED (not dropped): the
    grounded source still exists ⇒ fires with a grounded intent; row ends
    'fired', never silently stranded."""
    store = _store(tmp_path, "r2.db")
    sched = ProactiveSchedule.plan_and_persist(2, SEED, PERSONA, TIMING, store)
    H = next(float(h) for h in sched.event_hours if h < 20.0)
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("g1", H - 1.0, H + 1.0),
    )))

    store2 = _store(tmp_path, "r2.db")
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
    """R-3: restart at H+4h for a schedule reason (validity 3h). The gate must
    return 'expired' → row marked expired, NO message, and no ghost firing on
    later polls (invariant: stranded overdue events = 0)."""
    store = _store(tmp_path, "r3.db")
    sched = ProactiveSchedule.plan_and_persist(3, SEED, PERSONA, TIMING, store)
    H = next(float(h) for h in sched.event_hours if h < 20.0)
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("g1", H - 1.0, H + 1.0),
    )))

    store2 = _store(tmp_path, "r3.db")
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
    # no ghost: a later poll never re-surfaces the expired row
    after = ProactiveSchedule.restore(SEED, store2)
    assert after.next_pending(H + 4.5) != H
    store2.close()


def test_r4_restart_during_quiet_hours_expires_by_policy_no_retry(tmp_path):
    """R-4: planned event at 14:00 (validity 3h), restart at 03:00 next day
    (envelope == 0). The overdue row is past its validity window ⇒ expired per
    policy: no message during quiet hours, no infinite retry loop, row not
    stranded. Also: no NEW plan may land in quiet hours."""
    store = _store(tmp_path, "r4.db")
    store.save_schedule_events(SEED, [
        {"t_h": 14.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("g1", 13.0, 15.0),
    )))

    # context gate at 03:00 is quiet_hours by itself
    assert context_gate(27.0, 1, store=store, timing=TIMING,
                        last_fired_t_h=None).code == "quiet_hours"

    store2 = _store(tmp_path, "r4.db")
    restored = ProactiveSchedule.restore(SEED, store2)
    session = _session(store2)
    channel = FakeChannel()
    _run_runtime(store2, session, restored, channel, max_hours=27.5,
                 clock_start_h=27.0)

    assert channel.sent == []
    assert _rows(store2)[14.0]["status"] == "expired"
    after = ProactiveSchedule.restore(SEED, store2)
    assert after.next_pending(27.5) is None or after.next_pending(27.5) != 14.0
    # no new plan lands in quiet hours (envelope == 0 by construction)
    plan = ProactiveSchedule.plan(3, SEED, PERSONA, TIMING)
    for h in plan.event_hours:
        local = h % 24.0
        assert not (23.0 <= local or local < 8.0), f"event at quiet hour {local}"
    store2.close()


def test_r4b_quiet_hours_does_not_consume_still_valid_event(tmp_path):
    """R-4 (deferral leg): a still-VALID overdue event (check_in reason, 12h
    validity) recovered during quiet hours must NOT be silently consumed as
    fired — it must be deferred to the next awake instant or expired per
    policy. Consuming it without delivery loses a message the store still
    grounds."""
    store = _store(tmp_path, "r4b.db")
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

    store2 = _store(tmp_path, "r4b.db")
    restored = ProactiveSchedule.restore(SEED, store2)
    session = _session(store2)
    channel = FakeChannel()
    _run_runtime(store2, session, restored, channel, max_hours=27.5,
                 clock_start_h=27.0)

    assert channel.sent == [], "no message during quiet hours"
    row = _rows(store2)[23.5]
    # still valid (until 11:30) → must not be consumed as fired-without-delivery
    assert row["status"] in ("pending", "expired"), (
        f"still-valid overdue event consumed as {row['status']} during "
        "quiet hours — deferred firing is lost"
    )
    store2.close()


def test_r5_two_missed_events_evaluated_independently(tmp_path):
    """R-5: events at 10:00 (valid until 13:00) and 12:00 (valid until 15:00),
    restart at 14:00 → H1 expired, H2 still valid. Each row evaluated
    independently: expired → expire (no message), valid → fire with ITS OWN
    grounded intent; no double-fire, no cross-reason contamination."""
    store = _store(tmp_path, "r5.db")
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
        {"t_h": 12.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("gA", 9.5, 10.5, activity="morning pottery"),
        _ground_item("gB", 11.5, 12.5, activity="evening run"),
    )))

    store2 = _store(tmp_path, "r5.db")
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
    """R-5 variant: restart at 13:00 with both rows still inside their
    validity windows. Both are evaluated; the first fires, the second is
    subject to cooldown/daily-cap — neither is stranded."""
    store = _store(tmp_path, "r5b.db")
    store.save_schedule_events(SEED, [
        {"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE},
        {"t_h": 12.0, "day": 0, "reason": REASON_SCHEDULE},
    ])
    store.save_agenda(0, DailyAgenda(0, (
        _ground_item("gA", 9.5, 10.5),
        _ground_item("gB", 11.5, 12.5),
    )))

    store2 = _store(tmp_path, "r5b.db")
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
    """R-6: pending event at 23:30 day 0 (validity into day 1) + planned
    08:00 day 1; restart at midnight. The day-0 row must stay visible across
    the boundary (day attribution does not expire it), the day-1 daily cap
    starts fresh, day 0's judge score is available for day 1's hazard, and the
    day-0 event is never double-counted against day 1's cap."""
    store = _store(tmp_path, "r6.db")
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

    store2 = _store(tmp_path, "r6.db")
    restored = ProactiveSchedule.restore(SEED, store2)
    assert restored.next_pending(24.0) == 23.5, "day-0 row invisible at midnight"
    session = _session(store2)
    channel = FakeChannel()
    _run_runtime(store2, session, restored, channel, max_hours=34.0,
                 clock_start_h=24.0)

    rows = _rows(store2)
    assert rows[23.5]["status"] == "fired"  # evaluated (suppressed: quiet hours)
    assert rows[33.0]["status"] == "fired"
    # the day-0 row fired at midnight consumed NO day-1 cap slot
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


# --------------------------------------------------------------------------- #
# R-7 .. R-9: agenda / memory / judge restart attacks
# --------------------------------------------------------------------------- #


def test_r7_restart_after_agenda_generation_before_completion(tmp_path):
    """R-7: day D's agenda generated, current activity = pottery item in
    progress; kill at 15:00 before completion. On resume: CurrentActivity is
    the SAME item (not None, not fabricated), the agenda is NOT regenerated
    (no duplicate items), and completing the item later marks the original
    row completed exactly once."""
    store = _store(tmp_path, "r7.db")
    profile = build_persona(LIFE_SEED, graph=build_catalog())
    store.save_persona(profile)
    session = _session(store, profile=profile)
    session.clock.advance_to_day(5)
    session.ensure_day(5)
    agenda_pre = store.load_agenda(5)
    assert agenda_pre is not None and agenda_pre.items
    ids_pre = [it.id for it in agenda_pre.items]
    t_h = 5 * 24.0 + 15.0
    ca_pre = session._current_activity(5, t_h)
    assert ca_pre is not None and ca_pre.item is not None

    # kill + resume at the same moment
    store2 = _store(tmp_path, "r7.db")
    session2 = _session(store2, profile=profile)
    session2.clock.advance_to_day(5)
    session2.ensure_day(5)  # agenda exists → must NOT regenerate
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
    """R-8: turn recorded (L1) → session closed (L2) → episode promoted (L3),
    killed (a) after record_turn only and (b) after promotion. On restart: L1
    survives with the same session_id; the episode survives and is
    retrievable; close_session/promote are IDEMPOTENT — re-closing or
    re-promoting never duplicates summaries/episodes."""
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
    """R-9(a): resume with no previous judgement — the scheduler must not
    crash and must NOT use scores=None in live planning (plan §7.4): the
    neutral fallback (A=1) is a real array, no fabricated score."""
    store = _store(tmp_path, "r9a.db")
    s0 = _session(store)
    s0.clock.advance_hours(19.0)
    s0.on_message("hello")  # day 0 runs but is never finalized
    assert store.load_previous_judgement(1) is None
    scores = day_scores(store, 1, TIMING)
    assert scores is not None and len(scores) == 2
    assert float(scores[0]) == 0.0, "no judgement ⇒ neutral A=1, score 0"
    # live-style planning with the concrete array works and never sees None
    events = plan_proactive_events(2, SEED, PERSONA, TIMING, scores=scores)
    assert len(events) > 0
    ProactiveSchedule.plan_and_persist(2, SEED, PERSONA, TIMING, store, scores=scores)
    store.close()


def test_r9b_restart_after_judge_finalization_score_feeds_hazard(tmp_path):
    """R-9(b): day D finalized; D's real score feeds D+1's hazard
    (A(score)·I term) and the judge never re-runs for a finalized day
    (idempotent finalization: one judgement row, one day_finalized event)."""
    store = _store(tmp_path, "r9b.db")
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


# --------------------------------------------------------------------------- #
# Case 40 (A1-flagged): finalize_day crash window
# --------------------------------------------------------------------------- #


def test_case40_finalize_crash_window_no_lost_memory_or_life(tmp_path):
    """CASE 40 (it3 B2 adaptation): process death between the conversation
    close persist and its memory tail (plan §5.1: L1->L2->L3->L4 now runs at
    the CONVERSATION boundary, not the day finalize). On resume the recovery
    must complete the tail — the close is idempotent and nothing is silently
    lost. Comparison is against an identical NON-crashed run (same seed) so
    the assertion is deterministic."""
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
            # 23.0 = a natural quiet-hours boundary close (the runtime's
            # closes land inside the day, never on the 24.0 instant)
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

    # judgement-guarded: no double-advance (the one thing that must hold even
    # in the crash case)
    assert len(crashed.conn.execute("SELECT day FROM judgements").fetchall()) == 1

    # Conversation + memory state must match the control (completed), or be
    # cleanly recoverable. A silent gap is the failure this case pins.
    # (it3 B2: memory sessions now key off conversations — plan §5.1 — so
    # the recoverable unit is the conversation and its memory tail.)
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
    # it3 B2: L1 episode formation keys off the CONVERSATION boundary too,
    # so the control (conversation still open) has no episodes yet — the
    # crashed run must show the RECOVERED memory instead.
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
    """CASE 40 (guard leg): a CLEAN finalize followed by resume must not
    double-advance — one judgement, one summary, one episode set, one life
    step; the resumed session reproduces the same persistent state."""
    profile = build_persona(LIFE_SEED, graph=build_catalog())
    store = _store(tmp_path, "c40.db")
    store.save_persona(profile)
    session = _session(store, profile=profile)
    session.clock.advance_hours(19.0)
    session.on_message("I have a cat named Luna")
    session.clock.advance_to_day(1)
    session.ensure_day(1)
    arcs_pre = {(a.id, a.progress, a.status) for a in store.list_life_arcs()}
    eps_pre = {(e.id, e.summary) for e in store.list_episodes()}
    n_assertions = len(store.list_assertions())

    store2 = _store(tmp_path, "c40.db")
    s2 = _session(store2, profile=profile)
    s2.clock.advance_to_day(1)
    s2.ensure_day(1)
    assert len(store2.conn.execute("SELECT day FROM judgements").fetchall()) == 1
    assert {(e.id, e.summary) for e in store2.list_episodes()} == eps_pre
    # it3 B2: the exchange lives in an open conversation (conv-0) that must
    # survive the clean finalize + resume without rewind.
    conv = store2.load_open_conversation()
    assert conv is not None and len(conv.turns) == 2
    assert len(store2.list_assertions()) == n_assertions
    assert {(a.id, a.progress, a.status) for a in store2.list_life_arcs()} == arcs_pre
    store.close()
    store2.close()


# --------------------------------------------------------------------------- #
# R-10 / V-1: Iteration-2 restart-across-quiet-boundary + message provenance
# (plan §5-A9 R1/V1, invariants 6/17 + r4b deferral semantics)
# --------------------------------------------------------------------------- #

FAST = TimeScale(seconds_per_virtual_hour=0.02)

#: 12:00 local — outside the check-in windows (8-11, 19-22), so the
#: callback candidate wins the rank cleanly.
NOW_H = 300.0


def test_r10_restart_across_quiet_boundary_delivers_still_valid_event(tmp_path):
    """A shared-interest event at 23:30 (quiet hours, 12h validity — outlives
    the window) survives THREE restarts: (1) run to 23:00 leaves it pending
    and unconsumed; (2) restart at 03:00 defers it through the quiet window
    (never consumed, never expired, no message during quiet); (3) restart at
    09:00 — the first fully-awake instant — delivers it EXACTLY ONCE with a
    grounded intent. r4b semantics hold across every boundary."""
    from harness.bootstrap import ensure_companion_initialized
    from harness.domain import EpisodicMemory, MemoryKind, UserProfile

    store = _store(tmp_path, "r10.db")
    store.save_schedule_events(SEED, [
        {"t_h": 23.5, "day": 0, "reason": REASON_SHARED_INTEREST},
    ])
    ensure_companion_initialized(
        store, seed=SEED, user=UserProfile(name="u", interests=("pottery",))
    )
    # g8b: the episode's source session must exist — register it
    store.open_session("day-0", 22.0)
    store.close_session("day-0", 22.7)
    store.insert_episode(EpisodicMemory(
        "ep_si", "user talked about pottery class", MemoryKind.SHARED_EPISODE,
        22.5, 22.6, 0.8, 0, None, None,
        "day-0", (1,), ("pottery class is fun",), ("pottery",),
    ))

    # phase 1: run to 23:00 — the event is still in the future, untouched
    s1 = _store(tmp_path, "r10.db")
    _run_runtime(s1, _session(s1), ProactiveSchedule.restore(SEED, s1),
                 FakeChannel(), max_hours=23.0, clock_start_h=None)
    assert _rows(s1)[23.5]["status"] == "pending", (
        "event consumed before its own time"
    )

    # phase 2: restart at 03:00 (quiet) — deferred, never consumed/expired,
    # no message during the quiet window
    s2 = _store(tmp_path, "r10.db")
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
    s3 = _store(tmp_path, "r10.db")
    chan3 = FakeChannel()
    _run_runtime(s3, _session(s3), ProactiveSchedule.restore(SEED, s3),
                 chan3, max_hours=34.5, clock_start_h=33.0)
    fired = [m for m in chan3.sent if m.proactive]
    assert len(fired) == 1, f"expected exactly one delivery, got {len(fired)}"
    row3 = _rows(s3)[23.5]
    assert row3["status"] == "fired"
    assert "expired" not in _suppressed_codes(s3)
    # the delivered message carries the exact validated intent id, resolvable
    # to a real source (invariant 6)
    last = s3.recent_messages()[-1]
    assert last["intent_id"], "delivered message missing intent provenance"
    intent = s3.load_proactive_intent(last["intent_id"])
    assert intent is not None
    assert s3.resolve_intent_source(intent) is not None
    s1.close()
    s2.close()
    s3.close()


def test_v1_every_proactive_message_carries_real_intent_id(tmp_path):
    """Invariant 6 at the persisted-row level, end to end through the REAL
    AsyncRuntime + Session + SQLiteStore: every proactive message row carries
    the intent_id of a REAL stored intent whose source exists (never a
    reason label, never a missing id); reactive rows keep intent_id None."""
    store = _store(tmp_path, "v1.db")
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
    """After a fire, a full restart (fresh store over the same file) keeps
    the provenance chain intact: message.intent_id unchanged, the intent row
    still stored with status fired, and its source still resolvable."""
    store = _store(tmp_path, "v1b.db")
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

    store2 = _store(tmp_path, "v1b.db")
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
    """The g8b semantics end to end: a callback memory grounded in a REAL
    session (open_session row + source turns) resolves to an intent; once the
    source session record is deleted the runtime must suppress (no_source)
    and NEVER attach the stale intent to a message — no record of the
    promise, no claim."""
    store = _store(tmp_path, "v1c.db")
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

    store2 = _store(tmp_path, "v1c.db")
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
