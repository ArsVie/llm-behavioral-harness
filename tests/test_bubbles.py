import os
import asyncio
import pytest
from harness.bubbles import BUBBLE_INSTRUCTION, bubbles_enabled, parse_bubbles
from harness.session import Session
from harness.store import SQLiteStore
from harness.client import FakeClient
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.clock import VirtualClock
from harness.domain import Interest, PersonaProfile

PERSONA = PersonaParams()
TIMING = TimingParams()
PROF = PersonaProfile(
    name="Nova",
    core="warm",
    interests=(Interest("chat", "exact", 0.8),),
    routines=(),
)


def test_parse_both_newline_styles():
    assert parse_bubbles("a\nb") == ["a", "b"]
    assert parse_bubbles("a\n\nb") == ["a", "b"]
    assert parse_bubbles("a\n \n\nb") == ["a", "b"]
    assert parse_bubbles("a\n\n\nb") == ["a", "b"]
    assert parse_bubbles("hello") == ["hello"]
    assert parse_bubbles("") == []
    assert parse_bubbles("  hello  \n\n  world  ") == ["hello", "world"]
    assert parse_bubbles("one\ntwo\nthree") == ["one", "two", "three"]


def test_flag_off_is_parity(tmp_path):
    os.environ.pop("HARNESS_BUBBLES", None)
    cli = FakeClient(responses=["Hello there.\n\nHow are you?"])
    sess = Session(
        store=SQLiteStore(":memory:"),
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=1,
        client=cli,
        clock=VirtualClock(),
    )
    sess._profile = PROF
    r = sess.on_message("hi")
    assert r.bubbles is None
    assert r.reply == "Hello there.\n\nHow are you?"
    assert "bubbles" not in cli.calls[0]["system"].lower()


def test_flag_on_splits_and_injects_prompt(tmp_path):
    os.environ["HARNESS_BUBBLES"] = "1"
    try:
        cli = FakeClient(responses=["Hello there.\n\nHow are you?"])
        sess = Session(
            store=SQLiteStore(":memory:"),
            persona=PERSONA,
            timing=TIMING,
            variant=MoodVariant.DECOUPLED_OFFSETS,
            seed=2,
            client=cli,
            clock=VirtualClock(),
        )
        sess._profile = PROF
        r = sess.on_message("hi")
        assert r.bubbles == ("Hello there.", "How are you?")
        assert "bubbles" in cli.calls[0]["system"].lower()
        # single newline also counts
        cli2 = FakeClient(responses=["a\nb"])
        sess2 = Session(
            store=SQLiteStore(":memory:"),
            persona=PERSONA,
            timing=TIMING,
            variant=MoodVariant.DECOUPLED_OFFSETS,
            seed=3,
            client=cli2,
            clock=VirtualClock(),
        )
        sess2._profile = PROF
        r2 = sess2.on_message("hi")
        assert r2.bubbles == ("a", "b")
        # no newline -> no bubbles even when on
        cli3 = FakeClient(responses=["one message"])
        sess3 = Session(
            store=SQLiteStore(":memory:"),
            persona=PERSONA,
            timing=TIMING,
            variant=MoodVariant.DECOUPLED_OFFSETS,
            seed=4,
            client=cli3,
            clock=VirtualClock(),
        )
        sess3._profile = PROF
        r3 = sess3.on_message("hi")
        assert r3.bubbles is None
    finally:
        os.environ.pop("HARNESS_BUBBLES", None)


def test_runtime_paced_multi_send():
    os.environ["HARNESS_BUBBLES"] = "1"
    try:
        import asyncio
        from harness.channels.base import FakeChannel
        from harness.runtime import AsyncRuntime
        from harness.scheduler import ProactiveSchedule
        from harness.proactive import IntentResolver

        cli = FakeClient(responses=["Hello there.\n\nHow are you?"])
        sess = Session(
            store=SQLiteStore(":memory:"),
            persona=PERSONA,
            timing=TIMING,
            variant=MoodVariant.DECOUPLED_OFFSETS,
            seed=10,
            client=cli,
            clock=VirtualClock(),
        )
        sess._profile = PROF
        r = sess.on_message("hi")
        assert r.bubbles is not None
        ch = FakeChannel()
        rt = AsyncRuntime(
            session=sess,
            channel=ch,
            store=sess.store,
            timing=TIMING,
            seed=10,
            schedule=ProactiveSchedule.restore(10, sess.store),
            resolver=IntentResolver(store=sess.store),
            sleeper=lambda _: asyncio.sleep(0),
        )
        asyncio.run(rt._send_turn_outputs(r, proactive=False))
        assert len(ch.sent) == 2
        assert [m.text for m in ch.sent] == list(r.bubbles)
        # non-bubbled -> single send (parity)
        os.environ.pop("HARNESS_BUBBLES", None)
        cli2 = FakeClient(responses=["Hello there.\n\nHow are you?"])
        sess2 = Session(
            store=SQLiteStore(":memory:"),
            persona=PERSONA,
            timing=TIMING,
            variant=MoodVariant.DECOUPLED_OFFSETS,
            seed=11,
            client=cli2,
            clock=VirtualClock(),
        )
        sess2._profile = PROF
        r2 = sess2.on_message("hi")
        assert r2.bubbles is None
        ch2 = FakeChannel()
        rt2 = AsyncRuntime(
            session=sess2,
            channel=ch2,
            store=sess2.store,
            timing=TIMING,
            seed=11,
            schedule=ProactiveSchedule.restore(11, sess2.store),
            resolver=IntentResolver(store=sess2.store),
            sleeper=lambda _: asyncio.sleep(0),
        )
        asyncio.run(rt2._send_turn_outputs(r2, proactive=False))
        assert len(ch2.sent) == 1
        assert ch2.sent[0].text == r2.reply
    finally:
        os.environ.pop("HARNESS_BUBBLES", None)
