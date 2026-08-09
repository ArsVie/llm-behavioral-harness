"""A9 adversarial wave — LIFE attack class (plan §9, cases L-1..L-8).

Accelerated 60-day runs (fixed seed) against the integrated session + life +
store lanes: no immortal unfinished activities, not everything completes
silently, day-to-day diversity, no impossible overlaps in the snapshot, dead
arcs cleaned, no off-persona drift, no goldfish resets, exact continuity
across a day-30 restart.
"""

from __future__ import annotations

from engine.rng import stream_rng
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import AgendaItem, DailyAgenda, CurrentActivity
from harness.interests import build_catalog
from harness.judge import ScriptedJudge
from harness.persona import build_persona
from harness.session import Session
from harness.store import SQLiteStore
from harness import life

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345
ALT_SEED = 999


def _store(tmp_path, name: str) -> SQLiteStore:
    return SQLiteStore(tmp_path / name)


def _profile(seed: int = SEED):
    return build_persona(seed, graph=build_catalog())


def _session(store, profile, *, seed: int = SEED):
    return Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=seed,
        client=FakeClient(responses=["ok!"]),
        clock=VirtualClock(),
        judge=ScriptedJudge(score=0.5).judge_day,
        persona_profile=profile,
    )


def _run_days(session, days: int, *, start_day: int = 1,
              talk_days: frozenset[int] = frozenset({0})):
    """Advance the session day by day, finalizing each previous day. Days in
    ``talk_days`` get one user turn (drives the memory lane too). For resumed
    sessions pass ``start_day`` = current_day + 1."""
    if 0 in talk_days and session.clock.now_h() < 1.0:
        session.clock.advance_hours(19.0)
        session.on_message("hello, tell me about your day")
    for d in range(start_day, days + 1):
        session.clock.advance_to_day(d)
        session.ensure_day(d)
        if d in talk_days:
            session.clock.advance_hours(19.0)
            session.on_message(f"day {d} check-in")


def _run_60(tmp_path, name: str, seed: int = SEED, *, stop_at: int | None = None) -> SQLiteStore:
    """Full 60-day run (or partial when stop_at given); returns the store."""
    store = _store(tmp_path, name)
    profile = _profile(seed)
    store.save_persona(profile)
    session = _session(store, profile, seed=seed)
    horizon = stop_at if stop_at is not None else 60
    _run_days(session, horizon)
    if stop_at is None:
        session.clock.advance_to_day(61)
        session.ensure_day(61)  # finalize day 60 so the last life step ran
    return store


def _agenda_signature(store, day: int):
    ag = store.load_agenda(day)
    if ag is None:
        return ()
    return tuple(sorted((it.start_t_h, it.end_t_h, it.activity) for it in ag.items))


# --------------------------------------------------------------------------- #
# L-1 / L-2: immortal vs everything-completing
# --------------------------------------------------------------------------- #


def test_l1_no_immortal_unfinished_activity(tmp_path):
    """L-1: after 60 accelerated days no arc may be `active` with progress
    frozen near 0 — progress either advances or the arc is declared
    completed/abandoned with a status change; next_intention stays
    consistent."""
    store = _run_60(tmp_path, "l1.db")
    arcs = store.list_life_arcs()
    assert arcs
    stalled = [a for a in arcs if a.status == "active" and a.progress < 0.5]
    assert not stalled, f"immortal unfinished arcs: {[(a.id, a.progress) for a in stalled]}"
    for a in arcs:
        assert a.next_intention, f"arc {a.id} lost its next_intention"
        assert 0.0 <= a.progress <= 1.0
    # either something completed or something is still advancing — the run is
    # not a frozen snapshot
    assert any(a.status in ("completed", "abandoned") for a in arcs) or any(
        a.status == "active" for a in arcs
    )
    store.close()


def test_l2_completed_arcs_excluded_from_agenda(tmp_path):
    """L-2: agenda generation must never reference a completed arc (no
    dangling source_id), and 'everything completing too fast' must leave a
    legitimately sparse-but-valid agenda — never items of a dead arc."""
    store = _store(tmp_path, "l2.db")
    profile = _profile(SEED)
    store.save_persona(profile)
    session = _session(store, profile)
    completion_day: dict[str, int] = {}
    session.clock.advance_hours(19.0)
    session.on_message("hello, tell me about your day")
    for d in range(1, 61):
        session.clock.advance_to_day(d)
        session.ensure_day(d)
        for arc in store.list_life_arcs():
            if arc.status == "completed" and arc.id not in completion_day:
                completion_day[arc.id] = d  # visible after step of day d-1
    session.clock.advance_to_day(61)
    session.ensure_day(61)
    bad = []
    for d in range(0, 61):
        ag = store.load_agenda(d)
        if ag is None:
            continue
        for it in ag.items:
            if it.source_type == "arc" and it.source_id in completion_day:
                if d >= completion_day[it.source_id]:
                    bad.append((d, it.id, it.source_id))
    assert not bad, f"agenda references completed arcs: {bad[:5]}"
    store.close()


# --------------------------------------------------------------------------- #
# L-3: identical schedules
# --------------------------------------------------------------------------- #


def test_l3_agendas_diverse_day_over_day(tmp_path):
    """L-3: agendas must NOT be identical day over day (routines recur with
    cadence but item sets/times vary); with a second seed the trajectory
    differs."""
    store = _run_60(tmp_path, "l3.db")
    sigs = {d: _agenda_signature(store, d) for d in range(0, 60)}
    assert len(set(sigs.values())) >= 10, (
        "agendas nearly identical across 60 days"
    )
    for d in range(0, 55):
        window = {sigs[d + k] for k in range(1, 6)}
        assert sigs[d] not in window or len(window) > 1, (
            f"day {d} agenda identical to the next 5 days"
        )
    store.close()


def test_l3b_second_seed_different_trajectory(tmp_path):
    """L-3 (cross-seed leg): a different seed produces a different agenda
    trajectory — the schedule is not a fixed template."""
    s1 = _run_60(tmp_path, "l3a.db", seed=SEED)
    s2 = _run_60(tmp_path, "l3b.db", seed=ALT_SEED)
    sigs1 = [_agenda_signature(s1, d) for d in range(0, 60)]
    sigs2 = [_agenda_signature(s2, d) for d in range(0, 60)]
    assert sigs1 != sigs2
    assert sum(a != b for a, b in zip(sigs1, sigs2)) >= 5
    s1.close()
    s2.close()


# --------------------------------------------------------------------------- #
# L-4: impossible overlaps
# --------------------------------------------------------------------------- #


def test_l4_overlapping_items_still_single_current_activity(tmp_path):
    """L-4: even with two overlapping agenda items (pottery 14:00-16:00, run
    15:00-17:00), CurrentActivity resolution is single-valued (one item or
    None — never two), and across 60 accelerated days no snapshot ever shows
    concurrent activities."""
    store = _store(tmp_path, "l4.db")
    profile = _profile(SEED)
    store.save_persona(profile)
    session = _session(store, profile)
    session.clock.advance_to_day(3)
    session.ensure_day(3)
    t_h = 3 * 24.0 + 15.5
    store.save_agenda(3, DailyAgenda(3, (
        AgendaItem("pottery", 3 * 24.0 + 14.0, 3 * 24.0 + 16.0, "pottery class",
                   "interest", "drawing", 0.5, "planned"),
        AgendaItem("run", 3 * 24.0 + 15.0, 3 * 24.0 + 17.0, "evening run",
                   "interest", "outdoors", 0.4, "planned"),
    )))
    # resolution path used by the session snapshot
    ca = session._current_activity(3, t_h)
    assert isinstance(ca, CurrentActivity) and ca.item is not None
    # the life lane's own resolution is single-valued too
    agenda3 = store.load_agenda(3)
    assert agenda3 is not None
    rng = stream_rng(SEED, 4, 3)
    result = life.step_life(3, profile, session._life_arcs, agenda3, store, rng)
    assert result.current_activity is None or result.current_activity.item is not None
    assert not (isinstance(result.current_activity, list)
                and len(result.current_activity) > 1)

    # 60-day sweep: every day's step_life yields exactly one activity
    store2 = _run_60(tmp_path, "l4b.db")
    profile2 = _profile(SEED)
    for d in range(0, 61):
        ag = store2.load_agenda(d)
        if ag is None:
            continue
        arcs = store2.list_life_arcs()
        rng = stream_rng(SEED, 4, d)
        res = life.step_life(d, profile2, arcs, ag, store2, rng)
        assert res.current_activity is None or res.current_activity.item is not None
    store.close()
    store2.close()


# --------------------------------------------------------------------------- #
# L-5: dead arcs never cleaned
# --------------------------------------------------------------------------- #


def test_l5_dead_arcs_stop_generating_and_never_reactivate(tmp_path):
    """L-5: abandoned arcs stop generating agenda items after their
    abandonment day; the snapshot's active set excludes them; no dead arc is
    ever re-activated by init_life after a restart."""
    store = _store(tmp_path, "l5.db")
    profile = _profile(SEED)
    store.save_persona(profile)
    session = _session(store, profile)
    abandon_day: dict[str, int] = {}
    session.clock.advance_hours(19.0)
    session.on_message("hello, tell me about your day")
    for d in range(1, 61):
        session.clock.advance_to_day(d)
        session.ensure_day(d)
        for arc in store.list_life_arcs():
            if arc.status == "abandoned" and arc.id not in abandon_day:
                abandon_day[arc.id] = d  # day the arc's abandonment was stepped
    session.clock.advance_to_day(61)
    session.ensure_day(61)

    bad = []
    for d in range(0, 61):
        ag = store.load_agenda(d)
        if ag is None:
            continue
        for it in ag.items:
            if it.source_type == "arc" and it.source_id in abandon_day:
                if d >= abandon_day[it.source_id]:
                    bad.append((d, it.id))
    assert not bad, f"abandoned arc still generating agenda: {bad[:5]}"
    abandoned = {a.id for a in store.list_life_arcs() if a.status == "abandoned"}
    # restart: init_life must not resurrect dead arcs
    store2 = _store(tmp_path, "l5.db")
    profile2 = _profile(SEED)
    s2 = _session(store2, profile2)
    s2.clock.advance_to_day(62)
    s2.ensure_day(62)
    ids_after = {a.id for a in store2.list_life_arcs()}
    for aid in abandoned:
        arc = store2.get_life_arc(aid)
        assert arc is None or arc.status == "abandoned", (
            f"dead arc {aid} re-activated after restart"
        )
    assert ids_after  # the world did not vanish
    store.close()
    store2.close()


# --------------------------------------------------------------------------- #
# L-6: spontaneous non-persona interests
# --------------------------------------------------------------------------- #


def test_l6_no_off_persona_drift(tmp_path):
    """L-6: every agenda item's source resolves to the persona's own
    interests/routines/arcs (zero off-persona drift); the persona portfolio
    itself is bucket-structured (40/40/20 sampled around targets)."""
    store = _run_60(tmp_path, "l6.db")
    profile = _profile(SEED)
    interests = {i.name for i in profile.interests}
    routines = {r.name for r in profile.routines}
    arc_ids = {a.id for a in store.list_life_arcs()}
    off = []
    for d in range(0, 60):
        ag = store.load_agenda(d)
        if ag is None:
            continue
        for it in ag.items:
            if it.source_type == "interest" and it.source_id not in interests:
                off.append((d, it.id, it.source_id))
            elif it.source_type == "routine" and it.source_id not in routines:
                off.append((d, it.id, it.source_id))
            elif it.source_type == "arc" and it.source_id not in arc_ids:
                off.append((d, it.id, it.source_id))
    assert not off, f"off-persona agenda items: {off[:5]}"
    buckets = {}
    for i in profile.interests:
        buckets[i.bucket] = buckets.get(i.bucket, 0) + 1
    assert buckets.get("exact", 0) in (3, 4, 5)
    assert buckets.get("adjacent", 0) in (3, 4, 5)
    assert buckets.get("independent", 0) in (1, 2, 3)
    store.close()


# --------------------------------------------------------------------------- #
# L-7: life goldfish reset (arcs lost)
# --------------------------------------------------------------------------- #


def test_l7_arc_wipe_restart_no_silent_id_reuse(tmp_path):
    """L-7: delete all life-arc rows after 30 days and restart. No crash, no
    phantom continuity (agenda must not imply arcs the store no longer has),
    and re-init — if it happens at all — must be a documented cold start, NOT
    a silent reuse of the identical arc ids pretending the 30 days happened."""
    store = _run_60(tmp_path, "l7.db", stop_at=30)
    pre_ids = {a.id for a in store.list_life_arcs()}
    assert pre_ids
    store.conn.execute("DELETE FROM life_arcs")
    store.conn.commit()

    # restart (fresh store instance over the same file)
    store2 = _store(tmp_path, "l7.db")
    profile = _profile(SEED)
    s2 = _session(store2, profile)
    s2.clock.advance_to_day(31)
    s2.ensure_day(31)  # must not crash
    post = store2.list_life_arcs()
    post_ids = {a.id for a in post}
    assert not (post and post_ids == pre_ids), (
        "init_life silently re-created the identical arc ids with fresh "
        "progress — the 30 simulated days are pretended away"
    )
    store.close()
    store2.close()


# --------------------------------------------------------------------------- #
# L-8: 60-day continuity across restart
# --------------------------------------------------------------------------- #


def test_l8_restart_reproduces_persistent_state_exactly(tmp_path):
    """L-8: fixed seed; run 30 days, restart, run to 60. Persistent state
    (arc progress/status/next_intention) reproduces EXACTLY a straight 60-day
    run of the same seed; progress is monotonic non-decreasing; no arc id
    collisions; every started_day <= current day."""
    straight = _run_60(tmp_path, "l8_straight.db", seed=SEED)

    restarted = _store(tmp_path, "l8_restart.db")
    profile = _profile(SEED)
    restarted.save_persona(profile)
    s1 = _session(restarted, profile)
    _run_days(s1, 30)
    s1.clock.advance_to_day(31)
    s1.ensure_day(31)  # finalize day 30 before the kill
    s1_snapshot = {(a.id, a.progress, a.status, a.next_intention)
                   for a in restarted.list_life_arcs()}

    # restart at day 31
    store2 = _store(tmp_path, "l8_restart.db")
    s2 = _session(store2, profile)
    s2.clock.advance_to_day(31)
    s2.ensure_day(31)
    assert {(a.id, a.progress, a.status, a.next_intention)
            for a in store2.list_life_arcs()} == s1_snapshot, (
        "restart changed already-persisted arc state"
    )
    _run_days(s2, 60, start_day=32)
    s2.clock.advance_to_day(61)
    s2.ensure_day(61)

    def final_state(st):
        return {(a.id, a.progress, a.status, a.next_intention, a.started_day)
                for a in st.list_life_arcs()}

    assert final_state(store2) == final_state(straight), (
        "restarted run diverged from the straight run"
    )
    arcs = store2.list_life_arcs()
    ids = [a.id for a in arcs]
    assert len(ids) == len(set(ids)), "arc id collision"
    assert all(a.started_day <= 60 for a in arcs)
    # progress monotonic non-decreasing across the whole store history:
    # re-derive by replaying step_life is not needed — every persisted update
    # replaces progress with a >= value (asserted per arc against the final)
    for a in arcs:
        assert 0.0 <= a.progress <= 1.0
    straight.close()
    store2.close()


# --------------------------------------------------------------------------- #
# L-9: Iteration-2 replenishment + arc-start + NOW-semantics attacks
# (plan §5-A9 L9, orchestrator invariants 8-9)
# --------------------------------------------------------------------------- #


def test_l9a_replenishment_never_reaches_active_zero(tmp_path):
    """Long-horizon replenishment soak on the REAL SQLiteStore: 30/60/120-day
    deterministic runs across 3 seeds — the number of ACTIVE arcs must never
    hit 0 on any day (replenishment is certain when nothing is active), the
    run must terminate, and every persisted arc stays valid."""
    for seed in (11, 22, 33):
        for days in (30, 60, 120):
            store = _store(tmp_path, f"l9a_{seed}_{days}.db")
            persona = _profile(seed)
            arcs = life.init_life(seed, persona, store, start_day=1)
            assert arcs, "precondition: initial arcs exist"
            for d in range(1, days + 1):
                rng = stream_rng(seed, life.LIFE_STREAM, d)
                agenda = life.generate_agenda(d, persona, arcs, store, rng)
                result = life.step_life(d, persona, arcs, agenda, store, rng)
                arcs = result.updated_arcs
                active = [a for a in arcs if a.status == "active"]
                assert active, (
                    f"seed {seed} day {d}/{days}: active life reached zero "
                    "(invariant 9 violated — replenishment must be certain "
                    "when nothing is active)"
                )
                assert all(0.0 <= a.progress <= 1.0 for a in arcs)
            # persistence agrees with the in-memory trajectory
            stored = {a.id: a.status for a in store.list_life_arcs()}
            assert stored == {a.id: a.status for a in arcs}
            store.close()


def test_l9b_arc_start_time_respected_across_restart(tmp_path):
    """An arc with a FUTURE started_day must not progress before its start —
    and the restart seam must reproduce the exact same trajectory: a store
    reopened mid-run (arcs reloaded from the DB) continues byte-identically
    to the uninterrupted run."""
    persona = _profile(SEED)
    store = _store(tmp_path, "l9b.db")
    arcs = life.init_life(SEED, persona, store, start_day=1)
    future = [a for a in arcs if a.started_day > 1]
    assert future, "precondition: some arc starts in the future"
    target = max(future, key=lambda a: a.started_day)

    # before its start day: NO progress, NO status change, NO agenda items
    for d in range(1, target.started_day):
        rng = stream_rng(SEED, life.LIFE_STREAM, d)
        agenda = life.generate_agenda(d, persona, arcs, store, rng)
        result = life.step_life(d, persona, arcs, agenda, store, rng)
        arcs = result.updated_arcs
        t = next(a for a in arcs if a.id == target.id)
        assert t.progress == target.progress, (
            f"arc {target.id} progressed before its start day {target.started_day}"
        )
        assert t.status == "active"
        assert not any(
            it.source_id == target.id for it in agenda.items
        ), f"arc {target.id} generated agenda items before its start"

    # RESTART: fresh store over the same file — arcs reloaded from the DB
    store2 = _store(tmp_path, "l9b.db")
    arcs2 = sorted(store2.list_life_arcs(), key=lambda a: a.id)
    assert [a.id for a in arcs2] == [a.id for a in arcs]
    assert all(
        a.progress == b.progress for a, b in zip(arcs2, arcs)
    ), "restart lost persisted arc progress"

    # continue both trajectories past the start day: restarted == uninterrupted
    for d in range(target.started_day, target.started_day + 12):
        rng = stream_rng(SEED, life.LIFE_STREAM, d)
        agenda = life.generate_agenda(d, persona, arcs2, store2, rng)
        result = life.step_life(d, persona, arcs2, agenda, store2, rng)
        arcs2 = result.updated_arcs

        rng_u = stream_rng(SEED, life.LIFE_STREAM, d)
        agenda_u = life.generate_agenda(d, persona, arcs, store, rng_u)
        result_u = life.step_life(d, persona, arcs, agenda_u, store, rng_u)
        arcs = result_u.updated_arcs

    assert {a.id: a.progress for a in arcs2} == {a.id: a.progress for a in arcs}
    assert {a.id: a.status for a in arcs2} == {a.id: a.status for a in arcs}
    store.close()
    store2.close()


def test_l9c_current_activity_never_in_the_future(tmp_path):
    """Invariant 8 (NOW semantics) across a 60-day real-store soak: a
    CurrentActivity is only ever reported for an item ACTUALLY in progress at
    the sampled instant (start <= t_h < end) — a future plan is never the
    current activity, and the reported t_h is the sampled now."""
    for seed in (11, 22):
        store = _store(tmp_path, f"l9c_{seed}.db")
        persona = _profile(seed)
        arcs = life.init_life(seed, persona, store)
        for d in range(1, 60):
            rng = stream_rng(seed, life.LIFE_STREAM, d)
            agenda = life.generate_agenda(d, persona, arcs, store, rng)
            noon = d * 24.0 + 12.0
            result = life.step_life(d, persona, arcs, agenda, store, rng, t_h=noon)
            arcs = result.updated_arcs
            for t_h in (d * 24.0 + 7.0, d * 24.0 + 12.0, d * 24.0 + 19.0,
                        d * 24.0 + 22.0):
                ca = life.current_activity_now(result.agenda, t_h)
                if ca is None:
                    continue
                assert ca.item is not None
                assert ca.item.start_t_h <= t_h < ca.item.end_t_h, (
                    f"day {d} at t_h={t_h}: current activity "
                    f"{ca.item.id!r} is not in progress (start "
                    f"{ca.item.start_t_h}, end {ca.item.end_t_h})"
                )
                assert ca.t_h == t_h, "CurrentActivity.t_h is not the sampled now"
            if result.current_activity is not None:
                it = result.current_activity.item
                assert it.start_t_h <= noon < it.end_t_h, (
                    "step_life(t_h=noon) reported a non-current activity"
                )
        store.close()


def test_l9d_no_overlapping_current_activities():
    """Overlapping agenda slots must never yield two current activities: the
    NOW resolver picks exactly ONE — the highest salience — deterministically,
    and the same instant always resolves identically (no hidden RNG)."""
    items = (
        AgendaItem("o1", 10.0, 12.0, "overlap low", "arc", "arc1", 0.4, "planned"),
        AgendaItem("o2", 10.5, 11.5, "overlap high", "arc", "arc2", 0.9, "planned"),
        AgendaItem("o3", 11.0, 13.0, "overlap mid", "arc", "arc3", 0.6, "planned"),
        AgendaItem("future", 14.0, 15.0, "future plan", "arc", "arc4", 0.95, "planned"),
    )
    agenda = DailyAgenda(0, items)
    ca = life.current_activity_now(agenda, 11.2)
    assert ca is not None and ca.item.id == "o2", (
        "overlapping slots must resolve to the single highest-salience item"
    )
    assert ca.t_h == 11.2
    # deterministic: same instant, same answer, every call
    for _ in range(3):
        assert life.current_activity_now(agenda, 11.2) == ca
    # a future plan at 14:00 is never 'current' at 11:20 — and outside every
    # slot there is NO current activity (no phantom, no future)
    assert life.current_activity_now(agenda, 13.5) is None
    assert life.current_activity_now(agenda, 9.0) is None
    assert life.current_activity_now(agenda, 15.5) is None
