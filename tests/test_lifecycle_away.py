"""WS-A: away-as-presence, close-as-checkpoint.

15 min of silence marks the user "away" (presence signal) - conversation
stays OPEN. Only the 6 h backstop or quiet-hours/day boundary actually
closes it. Return within the backstop continues the SAME conversation
with full raw continuity (no rebuild from summary). Replay parity holds
(no new RNG consumed for the derived away signal).
"""

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.judge import ScriptedJudge
from harness.session import Session
from harness.store import SQLiteStore
from harness.tunables import USER_AWAY_THRESHOLD_H, USER_LEFT_THRESHOLD_H, WIND_DOWN_GRACE_H

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 4242


def _session(store, clock, *, replies=None):
    return Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=FakeClient(responses=replies or ["ok!"]),
        clock=clock,
        judge=ScriptedJudge(score=0.5).judge_day,
    )


def test_away_at_15min_stays_open(tmp_path):
    """Silence past USER_AWAY_THRESHOLD_H (15 min) marks away but does NOT close."""
    store = SQLiteStore(tmp_path / "a.db")
    clock = VirtualClock(t_h=10.0)
    session = _session(store, clock)
    session.on_message("hello")  # conv-0 opens at 10:00
    assert session.open_conversation_id() == "conv-0"
    assert not session.is_user_away(clock.now_h())
    clock.advance_hours(0.35)  # 10:21
    assert session.is_user_away(clock.now_h()) is True
    assert session.check_conversation_lifecycle(clock.now_h()) is None
    assert session.open_conversation_id() == "conv-0"
    store.close()


def test_away_then_resume_same_conversation(tmp_path):
    """away -> dormant -> return within backstop continues SAME conversation."""
    store = SQLiteStore(tmp_path / "b.db")
    clock = VirtualClock(t_h=10.0)
    session = _session(store, clock)
    session.on_message("hello")  # conv-0 @ 10:00
    old_id = session.open_conversation_id()
    assert old_id == "conv-0"
    clock.advance_hours(0.5)  # 10:30, well past away
    assert session.is_user_away(clock.now_h())
    assert session.check_conversation_lifecycle(clock.now_h()) is None
    session.on_message("back")
    assert session.open_conversation_id() == old_id
    assert not session.is_user_away(clock.now_h())
    conv = store.load_conversation(old_id)
    assert conv is not None
    assert len(conv.turns) == 4  # user, assistant, user, assistant
    store.close()


def test_backstop_close_at_6h(tmp_path):
    """Silence past USER_LEFT_THRESHOLD_H (6 h) actually closes (user_left)."""
    store = SQLiteStore(tmp_path / "c.db")
    clock = VirtualClock(t_h=10.0)
    session = _session(store, clock)
    session.on_message("hello")  # conv-0 @ 10:00
    clock.advance_hours(6.1)  # 16:06
    assert session.is_user_away(clock.now_h()) is False  # past backstop, not away
    reason = session.check_conversation_lifecycle(clock.now_h())
    assert reason == "user_left"
    assert session.open_conversation_id() is None
    conv = store.load_conversation("conv-0")
    assert conv is not None and conv.close_reason == "user_left"
    session.on_message("after backstop")
    assert session.open_conversation_id() == "conv-1"
    store.close()


def test_backstop_not_before_6h(tmp_path):
    """Just before 6 h the conversation is away but still open."""
    store = SQLiteStore(tmp_path / "d.db")
    clock = VirtualClock(t_h=10.0)
    session = _session(store, clock)
    session.on_message("hello")
    clock.advance_hours(5.9)  # 15:54, almost backstop
    assert session.is_user_away(clock.now_h()) is True
    assert session.check_conversation_lifecycle(clock.now_h()) is None
    assert session.open_conversation_id() == "conv-0"
    store.close()


def test_day_boundary_still_closes(tmp_path):
    """Quiet-hours / day boundary checkpoint still closes deterministically."""
    store = SQLiteStore(tmp_path / "e.db")
    clock = VirtualClock(t_h=22.833)  # 22:50
    session = _session(store, clock, replies=["night reply"])
    session.on_message("good evening")  # conv-0 @ 22:50
    assert session.open_conversation_id() == "conv-0"
    clock.advance_hours(0.5)  # 23:20
    reason = session.check_conversation_lifecycle(clock.now_h())
    assert reason == "quiet_hours"
    assert session.open_conversation_id() is None
    store.close()


def test_no_rebuild_on_return_raw_continuity(tmp_path):
    """After a checkpoint close, the next conversation carries raw prior turns."""
    store = SQLiteStore(tmp_path / "f.db")
    clock = VirtualClock(t_h=10.0)
    session = _session(store, clock)
    session.on_message("hello from conv0")
    conv0 = store.load_conversation("conv-0")
    assert conv0 is not None and len(conv0.turns) == 2
    clock.advance_hours(6.1)
    session.check_conversation_lifecycle(clock.now_h())
    assert session.open_conversation_id() is None
    recent_before = store.recent_messages()
    assert any("hello from conv0" in r["content"] for r in recent_before)
    clock.advance_hours(0.1)  # 16:12
    session.on_message("hello conv1")
    recent_after = store.recent_messages()
    assert any("hello from conv0" in r["content"] for r in recent_after)
    assert any("hello conv1" in r["content"] for r in recent_after)
    conv1 = store.load_conversation("conv-1")
    assert conv1 is not None
    assert conv1.turns[0].text == "hello conv1"
    assert conv1.turns[0].turn_index == 0
    store.close()


def test_intermittent_texting_stays_one_conversation(tmp_path):
    """20-40 min gaps stay ONE conversation (the WS-A done-when)."""
    store = SQLiteStore(tmp_path / "g.db")
    clock = VirtualClock(t_h=10.0)
    session = _session(store, clock)
    session.on_message("msg1")  # 10:00
    clock.advance_hours(0.4)   # +24 min, past away but before backstop
    assert session.is_user_away(clock.now_h())
    session.on_message("msg2")  # should stay conv-0
    assert session.open_conversation_id() == "conv-0"
    clock.advance_hours(0.6)   # +36 min
    assert session.is_user_away(clock.now_h())
    session.on_message("msg3")
    assert session.open_conversation_id() == "conv-0"
    conv = store.load_conversation("conv-0")
    assert conv is not None and len(conv.turns) == 6
    store.close()


def test_replay_parity_no_new_rng(tmp_path):
    """Calling is_user_away does not consume RNG - two identical runs match."""
    def run_one(with_checks: bool):
        store = SQLiteStore(tmp_path / f"parity_{with_checks}.db")
        clock = VirtualClock(t_h=10.0)
        session = _session(store, clock)
        session.on_message("hello")
        if with_checks:
            for _ in range(10):
                clock.advance_hours(0.05)
                _ = session.is_user_away(clock.now_h())
        else:
            clock.advance_hours(0.5)
        session.on_message("again")
        convs = store.list_conversations()
        history = [(c.id, c.close_reason, tuple(t.text for t in c.turns)) for c in convs]
        store.close()
        return history

    assert run_one(False) == run_one(True)


def test_tunables_values():
    """Pin the tuned values so drift is caught immediately."""
    assert USER_AWAY_THRESHOLD_H == 0.25
    assert USER_LEFT_THRESHOLD_H == 6.0
    assert WIND_DOWN_GRACE_H < USER_LEFT_THRESHOLD_H
    assert WIND_DOWN_GRACE_H == 0.0833
