"""W-close: FLAG-OFF BYTE-PARITY (merge blocker) + flag-on turn-count
re-baseline (B3-style).

BYTE PARITY (merge blocker)
---------------------------
Two-phase close must be STRICTLY OFF by default: the closing draw (stream
6, keys ``(conv_seq, turn_index)``) and every artifact it produces are
byte-identical to the pre-W-close behavior. The pin below is a sha256 over
the canonical persisted trace (conversations + turns + messages +
state_events) of a seeded scripted feed with the flag off. It was generated
from the CURRENT implementation at commit 79c9b30 + W-close (2026-08-15)
and is frozen HERE — any change to the flag-off path (draw keying, RNG
consumption order, close timing, event strings, message flow) breaks it.
Regenerating the pin is an orchestrator decision, never a silent test edit.

The test also recomputes the expected close pattern INDEPENDENTLY from the
draw discipline (``stream_rng(seed, CONVERSATION_STREAM, seq, idx)`` at
each eligible companion turn) and asserts the recorded pattern matches, so
a changed draw key or consumption order fails even before the hash check.

RE-BASELINE (flag on, B3-style)
-------------------------------
With the flag ON the same seed/feed re-baselines: a fired draw now starts
the wind-down instead of closing, so a ``closing_tendency`` conversation
closes exactly TWO turns later (the user's reply + the companion's goodbye
turn — the draw keys are unchanged, so the draws themselves are identical
across arms). B3's mean-turns >= 4 bound holds in BOTH arms (the shift is
strictly upward; ``max_turns`` conversations are capped at 12 in both).
"""

import hashlib

from engine.rng import stream_rng
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import GenerationControls
from harness.judge import ScriptedJudge
from harness.session import CONVERSATION_STREAM, MAX_TURNS, Session
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345

#: Frozen byte-parity pin: sha256 of the canonical trace of the seeded
#: feed below with two_phase_close OFF. Generated 2026-08-15 from commit
#: 79c9b30 + W-close; see module docstring (MERGE BLOCKER).
PARITY_PIN = "c6903a927d039bab3a769fa11806945a0e2d8c1f24aefc8a2f619b998525d947"

#: Feed parameters (identical across arms).
START_H = 8.1667
GAP_H = 0.05
N_CLOSED = 8
THRESHOLD = 0.5


def _forced_controls(directive):
    return GenerationControls(
        max_tokens=300, response_delay_s=1.0,
        closing_tendency=THRESHOLD, initiative_factor=1.0,
    )


def _run_feed(tmp_path, *, two_phase: bool) -> tuple[SQLiteStore, list]:
    """Scripted feed: a user message every ``GAP_H`` virtual hours until
    ``N_CLOSED`` conversations have closed. Returns the store and the
    ``(id, close_reason, turn_count)`` close pattern."""
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
    guard = 0
    while len([c for c in store.list_conversations() if c.close_reason is not None]) < N_CLOSED and guard < 4000:
        guard += 1
        clock.advance_hours(GAP_H)
        session.on_message(f"scripted message {guard}")
    closed = [c for c in store.list_conversations() if c.close_reason is not None]
    assert clock.local_hour() < 23.0, "feed crossed into quiet hours"
    pattern = [(c.id, c.close_reason, len(c.turns)) for c in closed[:N_CLOSED]]
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


def _expected_close_turn(conv_seq: int, turn_count: int) -> int | None:
    """Independently recompute the draw discipline: walk companion turns
    from index 3 (first eligible) and return the turn INDEX at which the
    draw fires (None = no draw fired before MAX_TURNS)."""
    for idx in range(3, MAX_TURNS, 2):
        if stream_rng(SEED, CONVERSATION_STREAM, conv_seq, idx).uniform() < THRESHOLD:
            return idx
    return None


def test_flag_off_byte_parity_is_pinned(tmp_path, monkeypatch):
    """MERGE BLOCKER. Flag off: (a) two fresh runs are byte-identical,
    (b) the frozen pin matches, (c) the recorded close pattern matches the
    independently recomputed draw discipline, (d) zero wind-down artifacts
    reach the store or the prompts."""
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

    # (c) draw discipline recomputed independently: same close turns
    for (cid, reason, turns), conv_seq in zip(pattern_a, range(len(pattern_a))):
        assert reason == "closing_tendency"
        expected_idx = _expected_close_turn(conv_seq, turns)
        assert expected_idx is not None
        assert turns == expected_idx + 1, (
            f"conversation {cid}: closed at {turns} turns but the draw at "
            f"(seq={conv_seq}, idx={expected_idx}) is the first firing turn"
        )

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
    """RE-BASELINE (B3-style, explicitly documented): same seed + feed with
    the flag ON closes every ``closing_tendency`` conversation exactly TWO
    turns later (user reply + goodbye turn) than the flag-off arm — the
    draws are identical (keys unchanged); ``max_turns`` stays capped at 12.
    B3's mean-turns >= 4 bound holds in both arms."""
    monkeypatch.setattr("harness.session.controls_from_directive", _forced_controls)

    off_store, off_pattern = _run_feed(tmp_path / "off", two_phase=False)
    on_store, on_pattern = _run_feed(tmp_path / "on", two_phase=True)

    assert len(off_pattern) == len(on_pattern) == N_CLOSED
    off_turns = [turns for _, _, turns in off_pattern]
    on_turns = [turns for _, _, turns in on_pattern]

    for (cid_off, reason_off, turns_off), (cid_on, reason_on, turns_on) in zip(
        off_pattern, on_pattern
    ):
        assert cid_off == cid_on, "conversation ids must line up (same feed)"
        assert reason_on in ("closing_tendency", "max_turns"), (
            "flag-on close reasons stay inside the frozen taxonomy"
        )
        if reason_off == "closing_tendency":
            # the wind-down adds exactly one full exchange (user reply +
            # companion goodbye) before the deterministic close
            assert reason_on == "closing_tendency"
            assert turns_on == turns_off + 2, (
                f"{cid_off}: flag-on closed at {turns_on} turns, expected "
                f"{turns_off} + 2 (the flag-off draw turn {turns_off - 1} "
                f"starts the wind-down instead)"
            )
        else:
            assert turns_on == turns_off == MAX_TURNS

    # B3 bound holds in BOTH arms (flag-on shifts strictly upward)
    off_mean = sum(off_turns) / len(off_turns)
    on_mean = sum(on_turns) / len(on_turns)
    assert off_mean >= 4.0, f"flag-off mean turns {off_mean} < 4 (B3 violated)"
    assert on_mean >= 4.0, f"flag-on mean turns {on_mean} < 4 (B3 violated)"
    assert on_mean >= off_mean, "two-phase close must not shorten conversations"

    # the flag-on arm used the wind-down machinery (sanity)
    events = [r["event"] for r in on_store.conn.execute("SELECT event FROM state_events")]
    assert "wind_down_started" in events
    off_store.close()
    on_store.close()
