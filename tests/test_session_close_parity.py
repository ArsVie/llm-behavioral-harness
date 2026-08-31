"""W-close: FLAG-OFF BYTE-PARITY (merge blocker) + flag-on vestigial pin.

BYTE PARITY (merge blocker)
---------------------------
The closing draw is FEATURE-FLAGGED OFF (harness/tunables.py:
CLOSING_TENDENCY_ENABLED=False, MAX_TURNS=None, since commit 365ad33), and
the flag-off path itself changed shape with it: no per-turn taper draw
fires, no turn cap exists, and the ONLY closures are the boundary closes
(``quiet_hours`` at 23:00, ``user_left`` at ``USER_LEFT_THRESHOLD_H`` of
user silence). The pin below is a sha256 over the canonical persisted
trace (conversations + turns + messages + state_events) of a seeded,
bounded scripted feed with two_phase_close OFF. It was REGENERATED from
the current implementation after commit 365ad33 — an OWNER-APPROVED
_rebaseline to the draw-OFF reality (2026-08) — and is frozen HERE: any
change to the flag-off path (close timing, event strings, message flow,
RNG consumption) breaks it. Regenerating the pin stays an orchestrator
decision, never a silent test edit.

The test also recomputes the expected close pattern INDEPENDENTLY from the
boundary discipline (the first fed message whose local hour reaches the
quiet-hours start closes the open conversation with ``quiet_hours`` just
past the boundary) and asserts the recorded pattern matches, so a changed
close timing or reason fails even before the hash check.

FLAG-ON VESTIGIAL PIN (_rebaseline)
-----------------------------------
With the draw flagged OFF the wind-down machinery is UNREACHABLE: the
flag-on arm must be byte-identical to the flag-off arm, and NEITHER arm
may produce wind-down artifacts. Sentinel: any ``wind_down_started`` event
while the draw is flagged off means the gating regressed. When the draw is
re-enabled, re-base this test back to the B3-style contract (a fired draw
closes exactly TWO turns later under two-phase close; draws identical
across arms because the keys are unchanged).
"""

import hashlib

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import GenerationControls
from harness.judge import ScriptedJudge
from harness.session import Session
from harness.store import SQLiteStore
from harness.tunables import CLOSING_TENDENCY_ENABLED

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345

#: sha256 of the canonical trace of the seeded feed, two_phase_close OFF.
PARITY_PIN = "be27e97cc366ae6307e63f8b828bf775e905eb21014a6ddd92f7bb61ff87f78e"

#: Messages land at START_H + m*GAP_H, crossing one quiet-hours boundary.
START_H = 8.1667
GAP_H = 0.05
N_MESSAGES = 300
THRESHOLD = 0.5  # forced closing_tendency — inert while the draw is OFF


def _forced_controls(directive):
    return GenerationControls(
        max_tokens=300, response_delay_s=1.0,
        closing_tendency=THRESHOLD, initiative_factor=1.0,
    )


def _run_feed(tmp_path, *, two_phase: bool) -> tuple[SQLiteStore, list]:
    """Bounded scripted feed: exactly ``N_MESSAGES`` user messages every
    ``GAP_H`` virtual hours from ``START_H`` on day 0 (draw-OFF helper —
    closures are boundary-driven, so the feed is bounded by message count,
    not by waiting for N closes). Returns the store and the
    ``(id, close_reason, turn_count)`` pattern of conversations closed SO
    FAR (the successor conversation stays open)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock()
    client = FakeClient(responses=["ok!"])
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=client,
        clock=clock,
        judge=ScriptedJudge(score=0.5).judge_day,
        two_phase_close=two_phase,
    )
    clock.advance_to_day(0)
    clock.advance_hours(START_H)
    for i in range(N_MESSAGES):
        clock.advance_hours(GAP_H)
        session.on_message(f"scripted message {i}")
    closed = [c for c in store.list_conversations() if c.close_reason is not None]
    pattern = [(c.id, c.close_reason, len(c.turns)) for c in closed]
    return store, pattern


def _canonical_trace(store: SQLiteStore) -> str:
    """sha256 over the persisted artifacts in deterministic order."""
    convs = [
        (c.id, c.opened_by, c.close_reason,
         round(c.closed_t_h, 6) if c.closed_t_h is not None else None,
         tuple((t.speaker, t.text, round(t.t_h, 6), t.turn_index) for t in c.turns))
        for c in store.list_conversations()
    ]
    msgs = [
        (m["id"], m["role"], m["content"], round(m["t_h"], 6), m["day"], m["conversation_id"])
        for m in store.conn.execute(
            "SELECT id, role, content, t_h, day, conversation_id FROM messages ORDER BY id"
        )
    ]
    events = [
        (e["id"], e["day"], round(e["t_h"], 6), e["event"], e["detail"])
        for e in store.conn.execute(
            "SELECT id, day, t_h, event, detail FROM state_events ORDER BY id"
        )
    ]
    return hashlib.sha256(repr((convs, msgs, events)).encode()).hexdigest()


def _expected_boundary_close() -> tuple[int, float]:
    """Independently recompute the draw-OFF close discipline: the first fed
    message whose local hour reaches the quiet-hours start closes the open
    conversation with reason ``quiet_hours``, recorded AT that message's
    t_h (lazy close-before-turn ordering). Returns ``(message_index_1based,
    expected_closed_t_h)``."""
    quiet_start = TIMING.quiet_hours[0]
    m = next(
        m for m in range(1, N_MESSAGES + 1)
        if (START_H + m * GAP_H) % 24.0 >= quiet_start
    )
    return m, START_H + m * GAP_H


def test_flag_off_byte_parity_is_pinned(tmp_path, monkeypatch):
    """MERGE BLOCKER (_rebaseline to draw-OFF reality, owner-approved
    2026-08). Flag off: (a) two fresh runs are byte-identical, (b) the
    frozen pin matches, (c) the recorded close pattern matches the
    independently recomputed boundary discipline, (d) zero wind-down
    artifacts reach the store."""
    assert CLOSING_TENDENCY_ENABLED is False, (
        "this battery encodes the DRAW-OFF reality; regenerate PARITY_PIN "
        "and re-base the pins when the draw is re-enabled"
    )
    monkeypatch.setattr("harness.session.controls_from_directive", _forced_controls)

    store_a, pattern_a = _run_feed(tmp_path / "a", two_phase=False)
    trace_a = _canonical_trace(store_a)
    assert trace_a == PARITY_PIN, (
        "FLAG-OFF BYTE PARITY BROKEN: the replay pin changed. Any change to "
        "the flag-off closing path (draw keys, RNG consumption, close timing) "
        "is a merge blocker — regenerating the pin is an orchestrator decision."
    )

    store_b, pattern_b = _run_feed(tmp_path / "b", two_phase=False)
    assert _canonical_trace(store_b) == trace_a, "flag-off runs are non-deterministic"
    assert pattern_a == pattern_b

    # (c) One quiet_hours close at the first message past 23:00.
    exp_m, exp_t = _expected_boundary_close()
    assert len(pattern_a) == 1, f"exactly one boundary close expected: {pattern_a}"
    cid, reason, turns = pattern_a[0]
    assert (cid, reason) == ("conv-0", "quiet_hours")
    assert turns == 2 * (exp_m - 1), (
        f"conversation {cid}: closed at {turns} turns but the first message "
        f"past the quiet boundary is #{exp_m} ({2 * (exp_m - 1)} turns)"
    )
    conv0 = store_a.load_conversation("conv-0")
    assert conv0 is not None and conv0.closed_t_h is not None
    assert abs(conv0.closed_t_h - exp_t) < 1e-9
    successor = store_a.load_open_conversation()
    assert successor is not None and successor.id == "conv-1"
    assert successor.close_reason is None

    # (d) no wind-down artifacts anywhere
    kv_rows = store_a.conn.execute("SELECT COUNT(*) FROM kv_store").fetchone()[0]
    assert kv_rows == 0
    pend = store_a.conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE closing_pending_t_h IS NOT NULL"
    ).fetchone()[0]
    assert pend == 0
    events = [r["event"] for r in store_a.conn.execute("SELECT event FROM state_events")]
    assert "wind_down_started" not in events
    store_a.close()
    store_b.close()


def test_flag_on_turn_count_rebaseline(tmp_path, monkeypatch):
    """RE-BASELINE (draw-OFF reality, owner-approved 2026-08): with the
    closing draw flagged OFF the wind-down machinery is UNREACHABLE —
    two_phase_close is VESTIGIAL and the flag-on arm is byte-identical to
    the flag-off arm over the same feed. Sentinel: any wind-down artifact
    in either arm means the flag gate regressed. When the draw is
    re-enabled, re-base this test back to the B3-style contract (flag-on
    closes every drawn conversation exactly TWO turns later than flag-off;
    mean-turns >= 4 bound in both arms)."""
    monkeypatch.setattr("harness.session.controls_from_directive", _forced_controls)

    off_store, off_pattern = _run_feed(tmp_path / "off", two_phase=False)
    on_store, on_pattern = _run_feed(tmp_path / "on", two_phase=True)

    assert off_pattern == on_pattern, (
        f"two-phase flag changed the recorded closes while the draw is OFF: "
        f"{off_pattern} vs {on_pattern}"
    )
    assert _canonical_trace(off_store) == _canonical_trace(on_store), (
        "two_phase_close diverged from the flag-off path while the draw is "
        "flagged OFF (the wind-down branch must be unreachable)"
    )

    # Zero wind-down artifacts in either arm.
    for store in (off_store, on_store):
        events = [
            r["event"]
            for r in store.conn.execute("SELECT event FROM state_events")
        ]
        assert "wind_down_started" not in events
        pend = store.conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE closing_pending_t_h IS NOT NULL"
        ).fetchone()[0]
        assert pend == 0

    off_store.close()
    on_store.close()
