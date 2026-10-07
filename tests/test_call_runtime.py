"""Voice call in the runtime: /call and /hangup, the HTTP API live-talk uses,
the pauses while a call is on, and his held messages replayed after it.

No model and no live-talk: a fake client answers, the launcher is replaced by
a recorder, and the test itself plays live-talk over real HTTP on an
ephemeral port.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import pytest

import harness.runtime as runtime_mod
from harness.call import ActiveCall, CallConflict, launch_command, split_sentences
from harness.channels.base import FakeChannel, InboundMessage
from harness.clock import VirtualClock
from harness.commands import CommandContext, handle_command
from harness.channels.telegram import USER_COMMANDS, ControlCommand
from harness.prompts import CALL_END_OPENER, CALL_START_OPENER
from harness.runtime import AsyncRuntime
from harness.scheduler import ProactiveSchedule
from tests.helpers.runtime import TIMING
from tests.helpers.store import make_session, make_store
from tests.test_cache_prefix_gate import _persona
from tests.test_call_decision import CallAwareClient

SEED = 4242


@pytest.fixture()
def call_env(monkeypatch):
    monkeypatch.setenv("HARNESS_DECISIONS", "1")
    monkeypatch.setenv("HARNESS_DECISION_SOURCE", "model")
    monkeypatch.setenv("HARNESS_TOOL_MODE", "textual")
    monkeypatch.setenv("HARNESS_CALL_PORT", "0")      # ephemeral port
    monkeypatch.delenv("HARNESS_CALL_THINKING", raising=False)


def _runtime(tmp_path, *, answer=True, replies=None, name="rt.db"):
    store = make_store(tmp_path, name)
    profile = _persona()
    store.save_persona(profile)
    verdict = "true" if answer else "false"
    client = CallAwareClient(
        list(replies or [f"Line {i}. Then {i}." for i in range(30)]),
        f'tool_decide_call: {{"answer": {verdict}, "reason": "mine"}}',
    )
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    channel = FakeChannel()
    async def no_wait(seconds: float) -> None:
        return None

    rt = AsyncRuntime(session, ProactiveSchedule.restore(SEED, store), channel,
                      store=store, timing=TIMING, seed=SEED, sleeper=no_wait)
    launched: list[str] = []
    rt._launch_live_talk = launched.append
    return rt, store, session, channel, client, launched


async def _until(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.01)


# -- live-talk's side of the contract, over real HTTP ----------------------- #


def _post(url, body, *, sse=False):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            if sse:
                return r.status, [json.loads(line[6:]) for line in raw.splitlines()
                                  if line.startswith("data: ")]
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, None


async def _ring(rt):
    rt._loop = asyncio.get_running_loop()
    return rt._request_call()


def test_the_contract_end_to_end(tmp_path, call_env):
    rt, store, session, channel, client, launched = _runtime(tmp_path)

    async def scenario():
        await rt._session_call(session.on_message, "hey")
        assert await _ring(rt) == ""
        await _until(lambda: launched)
        base = launched[0]
        assert base.startswith("http://127.0.0.1:") and "/call/" in base
        assert channel.sent == []           # picked up: nothing on Telegram

        th = asyncio.to_thread
        assert await th(_get, base) == (200, {"state": "active", "reason": None})
        assert (await th(_get, base.rsplit("/", 1)[0] + "/nope"))[0] == 404

        # Her opening line.
        status, events = await th(_post, base + "/turn", {"text": None}, sse=True)
        assert status == 200
        assert [e.get("sentence") for e in events[:-1]] == ["Line 1.", "Then 1."]
        assert events[-1] == {"done": True}
        # No second turn before /heard.
        status, _ = await th(_post, base + "/turn", {"text": "hi"})
        assert status == 409
        assert await th(_post, base + "/heard", {"text": "Line 1.", "cut": True}) == (
            200, {"ok": True})
        # /heard with no turn waiting.
        assert (await th(_post, base + "/heard", {"text": "x", "cut": False}))[0] == 409

        # He speaks; his row lands at once.
        status, events = await th(_post, base + "/turn", {"text": "how are you"},
                                  sse=True)
        assert events[-1] == {"done": True}
        await th(_post, base + "/heard", {"text": "", "cut": True})
        assert (await th(_post, base + "/heartbeat", {}))[0] == 200

        # A Telegram message during the call is held, not answered.
        sent_before = len(channel.sent)
        await rt._on_inbound(InboundMessage(text="are you on the phone", t_h=None))
        assert len(channel.sent) == sent_before

        # live-talk closes: /end, twice (idempotent).
        assert await th(_post, base + "/end", {"reason": "window_closed"}) == (
            200, {"ok": True})
        assert (await th(_post, base + "/end", {"reason": "ctrl_c"}))[0] == 200
        assert await th(_get, base) == (200, {"state": "ended", "reason": "window_closed"})
        assert (await th(_post, base + "/turn", {"text": "hello?"}))[0] == 409
        await _until(lambda: not rt._call_mode)
        rt._stop_call_server()
        return sent_before

    sent_before = asyncio.run(scenario())
    rows = store.recent_messages(limit=100)
    contents = [(m["role"], m["content"]) for m in rows]
    start = next(i for i, (r, c) in enumerate(contents) if c.startswith(CALL_START_OPENER))
    end = next(i for i, (r, c) in enumerate(contents) if c.startswith(CALL_END_OPENER))
    in_call = [rc for rc in contents[start + 1:end] if rc[0] != "system"]
    assert in_call == [("assistant", "Line 1."), ("user", "how are you")]
    # The held message replays after the end row, and is answered.
    after = [rc for rc in contents[end + 1:] if rc[0] != "system"]
    assert after[0] == ("user", "are you on the phone")
    assert after[1][0] == "assistant"
    assert len(channel.sent) == sent_before + 1
    voice = [json.loads(r["meta"]) for r in store.conn.execute(
        "SELECT meta FROM llm_calls WHERE role = 'chat'") if r["meta"]
        and json.loads(r["meta"]).get("voice")]
    assert len(voice) == 2
    store.close()


def test_a_declined_call_texts_him(tmp_path, call_env):
    rt, store, session, channel, client, launched = _runtime(
        tmp_path, answer=False, replies=["hi.", "can't talk, at the store."])

    async def scenario():
        await rt._session_call(session.on_message, "hey")
        await _ring(rt)
        await _until(lambda: len(channel.sent) >= 1)

    asyncio.run(scenario())
    assert launched == []
    assert channel.sent[-1].text == "can't talk, at the store."
    assert channel.sent[-1].proactive is True
    assert rt._call_mode is False
    store.close()


def test_hangup_while_it_rings_means_the_call_never_opens(tmp_path, call_env):
    """He hangs up before her pick-up decision is in: she answers, but the
    call does not open, nothing of hers goes to Telegram, and the stream
    says he hung up first (a system row)."""
    rt, store, session, channel, client, launched = _runtime(tmp_path)

    async def scenario():
        await rt._session_call(session.on_message, "hey")
        sent_before = len(channel.sent)
        async with rt._lock:                 # the ring waits for the lock
            await _ring(rt)
            assert rt._ringing
            await rt._on_command(ControlCommand(name="hangup", args="", sender_id=1))
        await _until(lambda: not rt._ringing)
        return channel.sent[sent_before:]

    sent = asyncio.run(scenario())
    assert [m.text for m in sent] == ["Hung up."]
    assert launched == [] and rt._call is None and not rt._call_mode
    assert session.call_active() is None
    assert [d["popup_kind"] for d in store.decisions_for_day(0)] == ["tool_decide_call"]
    rows = store.recent_messages(limit=50)
    assert rows[-1]["role"] == "system"
    assert rows[-1]["content"].startswith("He hung up at")
    assert not any(m["content"].startswith(CALL_START_OPENER) for m in rows)
    store.close()


def test_a_second_call_gets_a_plain_notice(tmp_path, call_env):
    rt, store, session, channel, client, launched = _runtime(tmp_path)

    async def scenario():
        rt._loop = asyncio.get_running_loop()
        async with rt._lock:
            assert rt._request_call() == ""
            ringing = rt._request_call()
        await _until(lambda: launched)
        on_call = rt._request_call()
        # The call ended; his held messages are still being answered.
        call = rt._call
        rt._call = None
        replaying = rt._request_call()
        rt._call = call
        rt.call_end(call, "ctrl_c")
        await _until(lambda: not rt._call_mode)
        rt._stop_call_server()
        return ringing, on_call, replaying

    ringing, on_call, replaying = asyncio.run(scenario())
    assert ringing == "Already calling her…"
    assert on_call.startswith("You're already on a call")
    assert replaying == "Call in progress — try again in a moment."
    assert len(launched) == 1
    store.close()


def test_hangup_on_telegram_ends_the_call(tmp_path, call_env):
    rt, store, session, channel, client, launched = _runtime(tmp_path)

    async def scenario():
        await rt._session_call(session.on_message, "hey")
        await _ring(rt)
        await _until(lambda: launched)
        base = launched[0]
        await asyncio.to_thread(_post, base + "/turn", {"text": None}, sse=True)
        await asyncio.to_thread(_post, base + "/heard", {"text": "Hi.", "cut": False})
        await rt._on_command(ControlCommand(name="hangup", args="", sender_id=1))
        assert channel.sent[-1].text == "Call ended."
        await _until(lambda: not rt._call_mode)
        state = await asyncio.to_thread(_get, base)
        await rt._on_command(ControlCommand(name="hangup", args="", sender_id=1))
        rt._stop_call_server()
        return state

    state = asyncio.run(scenario())
    assert state == (200, {"state": "ended", "reason": "hangup"})
    assert channel.sent[-1].text == "No call is on."
    assert store.recent_messages(limit=1)[0]["content"].startswith(CALL_END_OPENER)
    store.close()


def test_a_silent_rig_ends_the_call(tmp_path, call_env, monkeypatch):
    monkeypatch.setattr(runtime_mod, "HEARTBEAT_TIMEOUT_S", 0.2)
    rt, store, session, channel, client, launched = _runtime(tmp_path)
    rt.call_watch_interval_s = 0.05

    async def scenario():
        await rt._session_call(session.on_message, "hey")
        await _ring(rt)
        await _until(lambda: launched)
        await _until(lambda: not rt._call_mode)
        rt._stop_call_server()

    asyncio.run(scenario())
    assert rt._last_call.status() == {"state": "ended", "reason": "rig_lost"}
    assert store.recent_messages(limit=1)[0]["content"].startswith(CALL_END_OPENER)
    store.close()


def test_a_failed_launch_ends_the_call(tmp_path, call_env):
    rt, store, session, channel, client, launched = _runtime(tmp_path)

    def boom(url):
        raise FileNotFoundError("cmd.exe")

    rt._launch_live_talk = boom

    async def scenario():
        await rt._session_call(session.on_message, "hey")
        await _ring(rt)
        await _until(lambda: rt._last_call is not None and not rt._call_mode)
        rt._stop_call_server()

    asyncio.run(scenario())
    assert "Couldn't open the call window" in channel.sent[-1].text
    assert store.recent_messages(limit=1)[0]["content"].startswith(CALL_END_OPENER)
    store.close()


def test_a_failed_end_row_does_not_leave_the_runtime_paused(tmp_path, call_env,
                                                             monkeypatch):
    """The end row failing must not keep proactives, settling and the
    rollover waiting forever, nor swallow the messages held meanwhile."""
    rt, store, session, channel, client, launched = _runtime(tmp_path)

    def broken_end(reason, **kwargs):
        raise RuntimeError("disk full")

    async def scenario():
        await rt._session_call(session.on_message, "hey")
        await _ring(rt)
        await _until(lambda: launched)
        monkeypatch.setattr(session, "end_call", broken_end)
        await rt._on_inbound(InboundMessage(text="still there?",
                                            t_h=session.clock.now_h()))
        await rt._on_command(ControlCommand(name="hangup", args="", sender_id=1))
        await _until(lambda: not rt._call_mode)
        rt._stop_call_server()

    asyncio.run(scenario())
    assert rt._call_idle.is_set() and not rt._call_held
    assert "call_end_failed" in [e["event"] for e in store.events_since(0)]
    users = [m for m in store.recent_messages(limit=50) if m["role"] == "user"]
    assert users[-1]["content"] == "still there?"
    store.close()


def test_boundaries_crossed_during_a_call_settle_when_it_ends(tmp_path, call_env):
    """The life loop only wakes for instants ahead of it: a start that passed
    while they talked is decided when the call ends, not at the next wake."""
    from tests.test_cache_prefix_gate import _agenda_item

    rt, store, session, channel, client, launched = _runtime(tmp_path)
    # The event pop-up is answered with a verdict, not prose: prose is a
    # parse failure on every phase (inform included).
    ask = client.chat_with_meta

    def answer_events(messages, **kwargs):
        last = (messages[-1].get("content") or "") if messages else ""
        if "{Event:" in last:
            client.responses.appendleft(
                'tool_decide_event: {"message": "class soon", '
                '"initiate": "yes", "reason": "mine"}'
            )
        return ask(messages, **kwargs)

    client.chat_with_meta = answer_events

    async def scenario():
        await rt._session_call(session.on_message, "hey")
        await _ring(rt)
        await _until(lambda: launched)
        now = session.clock.now_h()
        _agenda_item(store, start=now + 0.05, end=now + 3.0)
        session.clock.advance_hours(0.1)
        await asyncio.to_thread(_post, launched[0] + "/end", {"reason": "ctrl_c"})
        await _until(lambda: not rt._call_mode)
        rt._stop_call_server()

    asyncio.run(scenario())
    kinds = [d["popup_kind"] for d in store.decisions_for_day(0)]
    assert "tool_decide_event" in kinds
    store.close()


# -- pauses while the call is on ---------------------------------------------- #


def test_nothing_fires_settles_or_rolls_over_during_a_call(tmp_path, call_env):
    rt, store, session, channel, client, launched = _runtime(tmp_path)

    async def scenario():
        rt._call_mode = True
        rt._call_idle.clear()
        now = session.clock.now_h()
        # The firing loop's event service leaves the row and the clock alone.
        assert await rt._service_event(now + 1.0) == (None, now)
        assert session.clock.now_h() == now
        # Closes and negotiation legs are held.
        hooks: list[float] = []
        session.check_conversation_lifecycle = hooks.append
        session.check_negotiation = hooks.append
        await rt._run_due_hooks(now + 30.0)
        assert hooks == []
        # The rollover does not land.
        await rt._land_on_target(now + 20.0, next_midnight=24.0)
        assert session.clock.now_h() == now
        # The loops wait on the call.
        assert not rt._call_idle.is_set()

    asyncio.run(scenario())
    store.close()


def test_messages_held_during_a_call_replay_in_order(tmp_path, call_env):
    rt, store, session, channel, client, launched = _runtime(tmp_path)

    async def scenario():
        await rt._session_call(session.on_message, "hey")
        await _ring(rt)
        await _until(lambda: launched)
        base = launched[0]
        t0 = session.clock.now_h()
        await rt._on_inbound(InboundMessage(text="first", t_h=t0 + 0.01))
        await rt._on_inbound(InboundMessage(text="second", t_h=t0 + 0.02))
        assert [m.text for m, _ in rt._call_held] == ["first", "second"]
        await asyncio.to_thread(_post, base + "/end", {"reason": "window_closed"})
        await _until(lambda: not rt._call_mode)
        rt._stop_call_server()
        return t0

    t0 = asyncio.run(scenario())
    users = [m for m in store.recent_messages(limit=100) if m["role"] == "user"]
    assert [m["content"] for m in users[-2:]] == ["first", "second"]
    stamps = [json.loads(m["meta"])["stamp_t_h"] for m in users[-2:]]
    assert stamps == pytest.approx([t0 + 0.01, t0 + 0.02])
    assert rt._call_idle.is_set()
    store.close()


# -- commands and the menu ------------------------------------------------------ #


def test_call_and_hangup_commands():
    calls = []
    ctx = CommandContext(store=None, clock=None,
                         request_call=lambda: calls.append("call") or "",
                         request_hangup=lambda: "Call ended.")
    assert handle_command(ControlCommand(name="call", args="", sender_id=1), ctx) == ""
    assert calls == ["call"]
    assert handle_command(ControlCommand(name="hangup", args="", sender_id=1), ctx) == "Call ended."
    bare = CommandContext(store=None, clock=None)
    assert "not available" in handle_command(ControlCommand(name="call", args="", sender_id=1), bare)
    assert "not available" in handle_command(ControlCommand(name="hangup", args="", sender_id=1), bare)
    menu = dict(USER_COMMANDS)
    assert menu["call"] and menu["hangup"]
    help_text = handle_command(ControlCommand(name="help", args="", sender_id=1), bare)
    assert "/call" in help_text and "/hangup" in help_text


def test_call_needs_the_decision_layer(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_DECISIONS", raising=False)
    rt, store, session, channel, client, launched = _runtime(tmp_path)

    async def scenario():
        return await _ring(rt)

    assert "decision layer" in asyncio.run(scenario())
    store.close()


# -- pieces of harness/call.py ---------------------------------------------------- #


def test_split_sentences_matches_live_talk():
    assert split_sentences("Hi there. How are") == (["Hi there."], "How are")
    long = "word " * 50
    out, rest = split_sentences(long)
    assert out and len(out[0]) <= 160


def test_launch_command(monkeypatch):
    monkeypatch.delenv("HARNESS_CALL_LAUNCH", raising=False)
    argv = launch_command("http://127.0.0.1:8790/call/abc")
    assert argv[-1] == "http://127.0.0.1:8790/call/abc"
    monkeypatch.setenv("HARNESS_CALL_LAUNCH", "echo {url}")
    assert launch_command("http://x/call/1") == ["/bin/sh", "-c", "echo http://x/call/1"]


def test_active_call_alternation():
    call = ActiveCall("id")
    call.begin_turn(None)
    with pytest.raises(CallConflict):
        call.begin_turn(None)
    call.check_heard()
    call.finish_heard()
    with pytest.raises(CallConflict):
        call.check_heard()
    assert call.request_end("hangup") is True
    assert call.request_end("again") is False
    assert call.status() == {"state": "ended", "reason": "hangup"}
    with pytest.raises(CallConflict):
        call.begin_turn(None)
