"""Length-based pauses between the messages of one reply.

The first message goes out as before; each following one waits
``typing_pause_s(its text)`` with the Telegram "typing" indicator shown.
Covers the pause formula, the channel's paragraph split (bubbles off), the
runtime's bubble fan-out (streamed and post-hoc), and proactive sends. All
waits are recorded by an injected sleeper; nothing really sleeps.
"""

import asyncio
import re
from types import SimpleNamespace

import pytest

from harness.bubbles import (
    PAUSE_MAX_S,
    PAUSE_MIN_S,
    typing_pause_s,
)
from harness.channels.base import OutboundMessage
from harness.channels.telegram import TelegramChannel
from harness.client import FakeClient
from harness.runtime import AsyncRuntime
from harness.scheduler import ProactiveSchedule
from tests.helpers.channel_fakes import FakeApplication
from tests.test_bubble_backend_stream import (
    ALL_OFF,
    BUBBLES_ONLY,
    STREAM_ON,
    TIMING,
    _env,
    _session,
)

_PAUSE_ENV = (
    "HARNESS_BUBBLE_PACING",
    "HARNESS_BUBBLE_PAUSE_BASE_S",
    "HARNESS_BUBBLE_PAUSE_PER_CHAR_S",
    "HARNESS_BUBBLE_PAUSE_MIN_S",
    "HARNESS_BUBBLE_PAUSE_MAX_S",
)


@pytest.fixture(autouse=True)
def _default_pacing(monkeypatch):
    for name in _PAUSE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HARNESS_TYPING", "1")


def _wired(log: list):
    """A TelegramChannel over a fake bot, with sends, typing actions and
    sleeps all appended to one ordered ``log``."""
    app = FakeApplication()
    bot = app.bot

    async def send_message(chat_id, text, **kwargs):
        # Log the plain text: undo the MarkdownV2 escaping.
        log.append(("send", re.sub(r"\\(.)", r"\1", text)))

    async def send_chat_action(chat_id, action, **kwargs):
        log.append(("typing",))

    bot.send_message = send_message
    bot.send_chat_action = send_chat_action

    async def sleeper(seconds: float) -> None:
        log.append(("sleep", seconds))

    channel = TelegramChannel(application=app, owner_chat_id="42", sleeper=sleeper)
    return channel, sleeper


def _sleeps(log):
    return [e[1] for e in log if e[0] == "sleep"]


def _sends(log):
    return [e[1] for e in log if e[0] == "send"]


# --- the formula ------------------------------------------------------------ #


def test_pause_grows_with_length_and_is_clamped():
    short, mid, long_ = "ok.", "x" * 40, "x" * 90
    assert typing_pause_s(short) == PAUSE_MIN_S
    assert PAUSE_MIN_S < typing_pause_s(mid) < typing_pause_s(long_) < PAUSE_MAX_S
    assert typing_pause_s("x" * 2000) == PAUSE_MAX_S


def test_pause_knobs_are_env_overridable(monkeypatch):
    monkeypatch.setenv("HARNESS_BUBBLE_PAUSE_BASE_S", "1")
    monkeypatch.setenv("HARNESS_BUBBLE_PAUSE_PER_CHAR_S", "0.1")
    monkeypatch.setenv("HARNESS_BUBBLE_PAUSE_MIN_S", "2")
    monkeypatch.setenv("HARNESS_BUBBLE_PAUSE_MAX_S", "3")
    assert typing_pause_s("x") == 2.0
    assert typing_pause_s("x" * 15) == pytest.approx(2.5)
    assert typing_pause_s("x" * 100) == 3.0
    monkeypatch.setenv("HARNESS_BUBBLE_PAUSE_MAX_S", "not-a-number")
    assert typing_pause_s("x" * 1000) == PAUSE_MAX_S
    monkeypatch.setenv("HARNESS_BUBBLE_PACING", "0")
    assert typing_pause_s("x" * 100) == 0.0


# --- the channel: one reply, several paragraphs (bubbles off) ---------------- #


def test_paced_paragraphs_wait_by_their_own_length_with_typing():
    """First paragraph immediately; each later one after typing + a pause
    sized by ITS text."""
    log: list = []
    channel, _ = _wired(log)
    p1, p2, p3 = "Hi.", "x" * 30, "y" * 80
    asyncio.run(channel.send(
        OutboundMessage(text=f"{p1}\n\n{p2}\n\n{p3}", paced=True)))
    assert log == [
        ("send", p1),
        ("typing",), ("sleep", typing_pause_s(p2)),
        ("send", p2),
        ("typing",), ("sleep", typing_pause_s(p3)),
        ("send", p3),
    ]
    assert _sleeps(log)[0] < _sleeps(log)[1]


def test_pause_longer_than_the_indicator_refreshes_typing():
    """The indicator expires after ~5 s, so a 7 s pause is two waits with a
    typing action before each."""
    log: list = []
    channel, _ = _wired(log)
    asyncio.run(channel.send(
        OutboundMessage(text="a.\n\n" + "z" * 500, paced=True)))
    assert log[1:5] == [
        ("typing",), ("sleep", 4.5),
        ("typing",), ("sleep", pytest.approx(PAUSE_MAX_S - 4.5)),
    ]
    assert sum(_sleeps(log)) == pytest.approx(PAUSE_MAX_S)


def test_unpaced_text_goes_out_at_once():
    """Harness text (command replies, notices) is not her typing."""
    log: list = []
    channel, _ = _wired(log)
    asyncio.run(channel.send(OutboundMessage(text="one\n\ntwo\n\nthree")))
    assert log == [("send", "one"), ("send", "two"), ("send", "three")]


def test_typing_off_still_pauses_without_the_indicator(monkeypatch):
    monkeypatch.setenv("HARNESS_TYPING", "0")
    log: list = []
    channel, _ = _wired(log)
    asyncio.run(channel.send(OutboundMessage(text="a.\n\nbb.", paced=True)))
    assert log == [("send", "a."), ("sleep", PAUSE_MIN_S), ("send", "bb.")]


def test_pacing_off_sends_back_to_back(monkeypatch):
    monkeypatch.setenv("HARNESS_BUBBLE_PACING", "0")
    log: list = []
    channel, _ = _wired(log)
    asyncio.run(channel.send(OutboundMessage(text="a.\n\nbb.", paced=True)))
    assert log == [("send", "a."), ("send", "bb.")]


def test_a_failed_typing_action_does_not_skip_the_pause():
    log: list = []
    channel, _ = _wired(log)

    async def boom(chat_id, action, **kwargs):
        raise RuntimeError("network")

    channel.application.bot.send_chat_action = boom
    asyncio.run(channel.send(OutboundMessage(text="a.\n\nbb.", paced=True)))
    assert log == [("send", "a."), ("sleep", PAUSE_MIN_S), ("send", "bb.")]


# --- the runtime: bubbles (streamed / post-hoc), plain replies, proactive --- #


def _runtime(sess, channel, sleeper, seed):
    return AsyncRuntime(
        session=sess, channel=channel, store=sess.store, timing=TIMING,
        seed=seed, schedule=ProactiveSchedule.restore(seed, sess.store),
        resolver=None, sleeper=sleeper,
    )


REPLY = "Hello there.\n\nHow are you today, love?\n\n" + "w" * 120 + "."


@pytest.mark.parametrize("flags", [STREAM_ON, BUBBLES_ONLY, ALL_OFF],
                         ids=["streamed", "posthoc", "bubbles-off"])
def test_runtime_reply_is_paced_the_same_on_every_path(monkeypatch, flags):
    """Streamed bubbles, post-hoc bubbles and an unbubbled multi-paragraph
    reply all reach Telegram as the same paced sequence."""
    _env(monkeypatch, flags)
    sess = _session(seed=21, client=FakeClient(responses=[REPLY]))
    result = sess.on_message("hi")
    assert result.streamed is (flags is STREAM_ON)
    log: list = []
    channel, sleeper = _wired(log)
    asyncio.run(_runtime(sess, channel, sleeper, 21)._send_turn_outputs(
        result, proactive=False))
    parts = [p for p in REPLY.split("\n\n")]
    assert _sends(log) == parts
    assert log[0] == ("send", parts[0])  # first message: no extra wait
    expected = [typing_pause_s(parts[1]), 4.5,
                pytest.approx(typing_pause_s(parts[2]) - 4.5)]
    assert _sleeps(log) == expected
    assert log.count(("typing",)) == 3


def test_proactive_turn_and_its_popups_are_paced(monkeypatch):
    """A proactive reply paces like a reactive one; a proactive_out
    message split into paragraphs is paced too; a notice is not."""
    _env(monkeypatch, ALL_OFF)
    sess = _session(seed=22, client=FakeClient(responses=["unused"]))
    result = SimpleNamespace(
        proactive_out=(("event_popup", "Heads up.\n\nGym in ten."),),
        notices=("(seen)\n\n(no reply)",),
        bubbles=("Morning.", "Did you sleep at all?"),
        reply="Morning.\n\nDid you sleep at all?",
    )
    log: list = []
    channel, sleeper = _wired(log)
    asyncio.run(_runtime(sess, channel, sleeper, 22)._send_turn_outputs(
        result, proactive=True, reason="checkin"))
    assert log == [
        ("send", "Heads up."),
        ("typing",), ("sleep", typing_pause_s("Gym in ten.")),
        ("send", "Gym in ten."),
        ("send", "(seen)"), ("send", "(no reply)"),
        ("send", "Morning."),
        ("typing",), ("sleep", typing_pause_s("Did you sleep at all?")),
        ("send", "Did you sleep at all?"),
    ]


def test_runtime_without_channel_pause_uses_its_sleeper(monkeypatch):
    """A channel without typing_pause (Fake/CLI): the runtime sleeper
    carries the same length-based gap."""
    from harness.channels.base import FakeChannel

    _env(monkeypatch, BUBBLES_ONLY)
    sess = _session(seed=23, client=FakeClient(responses=[REPLY]))
    result = sess.on_message("hi")
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    ch = FakeChannel()
    asyncio.run(_runtime(sess, ch, sleeper, 23)._send_turn_outputs(
        result, proactive=False))
    assert sleeps == [typing_pause_s(b) for b in result.bubbles[1:]]
    assert all(m.paced for m in ch.sent)
