"""A real Session on a SQLite store with a Lily persona (moved from
test_prompt_content_2026_09_07, which test_turn_style also drives)."""

from __future__ import annotations

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import Interest, PersonaProfile
from harness.session import Session
from harness.store import SQLiteStore

#: An absolute virtual hour that is unmistakably not a wall clock: t_h 80.98
#: is day 3, 08:59 local -- the value the live run put in front of the model.
LIVE_T_H = 80.98


def lily_persona() -> PersonaProfile:
    return PersonaProfile(
        name="Lily",
        core="You are Lily, sharp and unimpressed until proven otherwise.",
        interests=(Interest("alternative music", "music", 0.8),),
        routines=(),
    )


def lily_session(tmp_path, *, t_h: float = LIVE_T_H, responses=None):
    """(session, client, store): a Session at ``t_h`` whose FakeClient
    replays ``responses``."""
    store = SQLiteStore(tmp_path / "s.db")
    profile = lily_persona()
    store.save_persona(profile)
    client = FakeClient(responses=responses or ["fine, what broke."])
    session = Session(
        store=store,
        persona=PersonaParams(),
        timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=3,
        client=client,
        clock=VirtualClock(t_h=t_h),
    )
    session._profile = profile
    return session, client, store
