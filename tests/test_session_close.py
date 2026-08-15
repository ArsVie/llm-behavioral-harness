"""W-close: two-phase close behavior (seam S1, flag ``two_phase_close``).

Covers the two-phase lifecycle end to end, all with the flag ON (or env-
driven), against the v6 store:

* seeded 3-turn goodbye path — the closing draw (stream 6, keys
  ``(conv_seq, turn_index)`` UNCHANGED) persists ``closing_pending_t_h``
  instead of closing; the next companion turn's state card renders the
  wind-down guidance through the assembler's existing ``closing_guidance``
  channel; the user's reply triggers the deterministic goodbye close with
  reason ``closing_tendency`` (taxonomy unchanged);
* user-silent expiry — the grace candidate
  ``closing_pending_t_h + WIND_DOWN_GRACE_H`` (1.0 vh) surfaces from
  ``next_conversation_close_t_h`` and the ``check_conversation_lifecycle``
  branch closes with reason ``closing_tendency`` (NOT ``user_left``, which
  stays the 12 h outer backstop);
* resume — the persisted wind-down marker rides along a session restart;
* flag plumbing — OFF by default, ON via the constructor kwarg or the
  ``HARNESS_TWO_PHASE_CLOSE`` env var.

Draw discipline under test: with ``closing_tendency = 1.0`` forced via
``harness.session.controls_from_directive`` (the same injection the it3 B2
suite uses), the draw at the FIRST eligible companion turn (turn index 3 —
the first companion turn is exempt by design) always fires.
"""

import pytest

from engine.rng import stream_rng
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import GenerationControls
from harness.judge import ScriptedJudge
from harness.session import (
    CONVERSATION_STREAM,
    WIND_DOWN_GUIDANCE,
    WIND_DOWN_GRACE_H,
    Session,
)
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345

#: With the threshold at 1.0 the draw at the first eligible companion turn
#: always fires (first companion turn = turn index 1, exempt; first
#: eligible = turn index 3).
FIRST_ELIGIBLE_TURN = 3


def _forced_controls(closing_tendency: float):
    def forced(directive):
        return GenerationControls(
            max_tokens=300, response_delay_s=1.0,
            closing_tendency=closing_tendency, initiative_factor=1.0,
        )

    return forced


def _session(tmp_path, *, two_phase: bool, closing_tendency: float = 1.0):
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
    return store, clock, client, session


@pytest.fixture
def forced_close(monkeypatch):
    """Force a fixed closing_tendency (1.0) for every directive."""

    def _apply(closing_tendency: float = 1.0):
        monkeypatch.setattr(
            "harness.session.controls_from_directive",
            _forced_controls(closing_tendency),
        )

    return _apply


def _drive_exchange(session, clock, text: str, *, gap_h: float = 0.05):
    """One user message followed by the companion reply (one exchange)."""
    clock.advance_hours(gap_h)
    return session.on_message(text)


def test_seeded_three_turn_goodbye_path(tmp_path, forced_close):
    """Draw -> pending; guidance visible in the NEXT companion turn's
    prompt; the user's reply closes the conversation deterministically
    with reason ``closing_tendency`` at the goodbye turn."""
    forced_close(1.0)
    store, clock, client, session = _session(tmp_path, two_phase=True)
    clock.advance_hours(8.5)
    # exchange 1: u0 -> c1 (first companion turn, no-taper floor)
    _drive_exchange(session, clock, "hi there")
    # exchange 2: u2 -> c3 -> the closing draw FIRES and starts the wind-down
    _drive_exchange(session, clock, "so, about today...")
    convs = store.list_conversations()
    assert len(convs) == 1
    conv = convs[0]
    assert conv.close_reason is None, "wind-down must NOT close the conversation"
    assert len(conv.turns) == 4
    # draw fired at (conv_seq=0, turn_index=3): stream-6 keys unchanged
    draw = stream_rng(SEED, CONVERSATION_STREAM, 0, FIRST_ELIGIBLE_TURN).uniform()
    assert draw < 1.0
    pending = store.conversation_closing_pending(conv.id)
    assert pending is not None and pending > 8.5, pending
    events = [r["event"] for r in store.conn.execute("SELECT event FROM state_events")]
    assert "wind_down_started" in events
    # exchange 3: the wind-down guidance is rendered into the state card of
    # the next companion turn via the existing closing_guidance channel
    _drive_exchange(session, clock, "ok, bye!")
    last_system = client.calls[-1]["system"]
    assert WIND_DOWN_GUIDANCE in last_system
    convs = store.list_conversations()
    assert len(convs) == 1
    assert convs[0].close_reason == "closing_tendency"
    assert len(convs[0].turns) == 6, "goodbye turn closes the conversation"
    assert convs[0].closed_t_h is not None
    # the persisted wind-down marker is cleared on close
    assert store.conversation_closing_pending(convs[0].id) is None
    store.close()


def test_guidance_only_while_wind_down_pending(tmp_path, forced_close):
    """The wind-down guidance replaces the band-derived closing guidance
    ONLY while a wind-down is pending (and only with the flag on)."""
    forced_close(1.0)
    store, clock, client, session = _session(tmp_path, two_phase=True)
    clock.advance_hours(8.5)
    _drive_exchange(session, clock, "hi there")
    assert WIND_DOWN_GUIDANCE not in client.calls[-1]["system"], (
        "no wind-down guidance before the draw fires"
    )
    _drive_exchange(session, clock, "so, about today...")
    _drive_exchange(session, clock, "ok, bye!")
    # the goodbye turn carried the wind-down guidance
    assert WIND_DOWN_GUIDANCE in client.calls[-1]["system"]
    # a NEW conversation (after the close) has no wind-down guidance
    _drive_exchange(session, clock, "another day, another chat")
    assert WIND_DOWN_GUIDANCE not in client.calls[-1]["system"], (
        "wind-down guidance must not leak into the next conversation"
    )
    store.close()


def test_user_silent_expiry_via_virtual_clock(tmp_path, forced_close):
    """A user who never replies: the conversation stays open through the
    grace window, the grace deadline is the next close instant, and the
    wind-down EXPIRES with reason ``closing_tendency`` (not ``user_left``,
    the 12 h outer backstop)."""
    forced_close(1.0)
    store, clock, client, session = _session(tmp_path, two_phase=True)
    clock.advance_hours(8.5)
    _drive_exchange(session, clock, "hi there")
    _drive_exchange(session, clock, "so, about today...")
    pending = store.conversation_closing_pending("conv-0")
    assert pending is not None
    # inside the grace window: still open, close NOT due
    assert session.check_conversation_lifecycle(pending + 0.5) is None
    # the runtime parks at the grace deadline (next_conversation_close_t_h)
    nxt = session.next_conversation_close_t_h(pending + 0.5)
    assert nxt is not None
    assert abs(nxt - (pending + WIND_DOWN_GRACE_H)) < 1e-9, nxt
    assert nxt < pending + 12.0, "grace must precede the user_left deadline"
    # past the grace deadline: expired wind-down closes with the SAME
    # taxonomy reason the draw would have used
    reason = session.check_conversation_lifecycle(pending + WIND_DOWN_GRACE_H + 0.01)
    assert reason == "closing_tendency"
    convs = store.list_conversations()
    assert len(convs) == 1
    assert convs[0].close_reason == "closing_tendency"
    assert store.conversation_closing_pending("conv-0") is None
    # user_left remains the outer backstop: the expiry recorded the
    # wind-down close, not user_left
    assert abs(convs[0].closed_t_h - (pending + WIND_DOWN_GRACE_H)) < 0.02
    store.close()


def test_wind_down_pending_survives_restart(tmp_path, forced_close):
    """Resume: a restarted session restores the persisted wind-down marker
    and still closes deterministically on the next reply."""
    forced_close(1.0)
    store, clock, client, session = _session(tmp_path, two_phase=True)
    clock.advance_hours(8.5)
    _drive_exchange(session, clock, "hi there")
    _drive_exchange(session, clock, "so, about today...")
    pending = store.conversation_closing_pending("conv-0")
    assert pending is not None
    store.close()

    # restart against the same store: open conversation + marker restored
    store2 = SQLiteStore(tmp_path / "s.db")
    clock2 = VirtualClock()
    clock2.advance_to_day(0)
    clock2.advance_hours(8.7)
    client2 = FakeClient(responses=["ok!"])
    session2 = Session(
        store2,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=client2,
        clock=clock2,
        judge=ScriptedJudge(score=0.5).judge_day,
        two_phase_close=True,
    )
    assert session2._closing_pending_t_h == pytest.approx(pending)
    # the reopened conversation continues (no rewind) and the next user
    # reply lands in the SAME conversation -> goodbye closes it
    session2.on_message("back again")
    convs = store2.list_conversations()
    assert len(convs) == 1
    assert convs[0].close_reason == "closing_tendency"
    assert len(convs[0].turns) == 6
    assert WIND_DOWN_GUIDANCE in client2.calls[-1]["system"]
    store2.close()


def test_flag_defaults_off_and_env_enables(tmp_path, monkeypatch):
    """``two_phase_close`` defaults to False; the HARNESS_TWO_PHASE_CLOSE
    env var (any non-empty value) turns it on; flag-off runs never touch
    the wind-down machinery."""
    store, clock, client, session = _session(tmp_path, two_phase=False)
    assert session.two_phase_close is False
    assert session._closing_pending_t_h is None
    store.close()

    monkeypatch.setenv("HARNESS_TWO_PHASE_CLOSE", "1")
    store2 = SQLiteStore(tmp_path / "s2.db")
    clock2 = VirtualClock()
    session2 = Session(
        store2,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=FakeClient(responses=["ok!"]),
        clock=clock2,
        judge=ScriptedJudge(score=0.5).judge_day,
    )
    assert session2.two_phase_close is True
    store2.close()


def test_flag_off_closes_at_the_draw_turn(tmp_path, forced_close):
    """Flag off = today's behavior: the draw closes the conversation at
    the drawn turn; no wind-down marker, no wind-down event."""
    forced_close(1.0)
    store, clock, client, session = _session(tmp_path, two_phase=False)
    clock.advance_hours(8.5)
    _drive_exchange(session, clock, "hi there")
    _drive_exchange(session, clock, "so, about today...")
    convs = store.list_conversations()
    assert len(convs) == 1
    assert convs[0].close_reason == "closing_tendency"
    assert len(convs[0].turns) == 4
    assert store.conversation_closing_pending("conv-0") is None
    events = [r["event"] for r in store.conn.execute("SELECT event FROM state_events")]
    assert "wind_down_started" not in events
    assert WIND_DOWN_GUIDANCE not in client.calls[-1]["system"]
    store.close()
