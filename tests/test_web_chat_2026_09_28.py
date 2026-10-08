"""The web chat (/chat) as a second channel (2026-09-28).

The owner asked for a chat page that shares the one conversation with
Telegram: same session, same history. Proactive messages stay on Telegram.

- ``MultiChannel`` sends a reply back to the channel the message came from,
  and a send with no origin (the life loop's reach-outs) to the primary.
- ``WebChannel`` turns a POST into an ``InboundMessage`` for the runtime,
  keeps harness text (command answers) as notes, and reports "typing" with
  the id her reply is held behind.
- Through a real ``AsyncRuntime``: a web message is answered on the web and
  saved in the same messages table; Telegram gets nothing.
- The observability server serves the three pages, reads the chat history
  (his and her lines plus the call marks: started / ended / missed) and
  forwards the writes.
"""

import asyncio
import base64
import io
import json
import threading
import urllib.error
import urllib.request

import pytest
from PIL import Image

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness import images
from harness.channels.base import FakeChannel, OutboundMessage
from harness.channels.multi import MultiChannel, origin
from harness.channels.web import WebChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.faces import with_call_face_rule
from harness.judge import ScriptedJudge
from harness.prompts import CALL_END_ROW, CALL_HUNG_UP_RINGING_ROW, CALL_START_ROW
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import ProactiveSchedule
from harness.session import Session
from harness.store import SQLiteStore

SEED = 12345


class Recording(WebChannel):
    """A web channel that also keeps every message it was sent."""

    def __init__(self, **kw):
        super().__init__(port=0, **kw)
        self.sent: list[OutboundMessage] = []

    async def send(self, message):
        self.sent.append(message)
        await super().send(message)


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


# ------------------------------------------------------------ MultiChannel

def test_replies_go_back_to_the_origin_and_reach_outs_to_the_primary():
    tg, web = FakeChannel(), FakeChannel()
    web.name = "web"
    multi = MultiChannel(tg, web)

    async def on_message(msg):
        assert origin() is web
        await multi.send(OutboundMessage(text="reply to " + msg.text, paced=True))

    async def scenario():
        await multi.start(on_message)
        await web.feed("hi")
        await multi.send(OutboundMessage(text="reach-out", proactive=True))

    asyncio.run(scenario())
    assert [m.text for m in web.sent] == ["reply to hi"]
    assert [m.text for m in tg.sent] == ["reach-out"]


def test_a_task_spawned_by_the_handler_keeps_the_origin():
    # The /call hook schedules the ring as its own task: a missed-call text
    # must still go back where the call was asked for.
    tg, web = FakeChannel(), FakeChannel()
    multi = MultiChannel(tg, web)
    tasks = []

    async def on_message(msg):
        tasks.append(asyncio.ensure_future(multi.send(OutboundMessage(text="later"))))

    async def scenario():
        await multi.start(on_message)
        await web.feed("call")
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert [m.text for m in web.sent] == ["later"] and tg.sent == []


# ------------------------------------------------------------- WebChannel

def test_web_channel_posts_become_inbound_messages_and_commands():
    got, cmds = [], []

    async def on_message(msg):
        got.append(msg)

    async def on_command(cmd):
        cmds.append((cmd.name, cmd.args))

    async def scenario():
        web = WebChannel(port=0)
        await web.start(on_message, on_command=on_command)
        base = f"http://127.0.0.1:{web.port}"
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, _post, base + "/web/send", {"text": " hey "})
        bad = await loop.run_in_executor(None, _post, base + "/web/send", {"text": "/call"})
        cmd = await loop.run_in_executor(None, _post, base + "/web/command",
                                         {"name": "/mute", "args": "4"})
        await asyncio.sleep(0.05)
        await web.stop()
        return ok, bad, cmd

    ok, bad, cmd = asyncio.run(scenario())
    assert ok[0] == 202 and cmd[0] == 202
    assert bad[0] == 400                     # commands go through /web/command
    assert [(m.text, m.sender_id) for m in got] == [("hey", "web")]
    assert cmds == [("mute", "4")]


def test_web_channel_keeps_harness_text_as_notes_and_holds_her_reply_while_typing():
    async def scenario():
        web = WebChannel(port=0, watermark=lambda: 41)
        await web.start(lambda m: None)
        states = []
        async with web.typing_context():
            states.append(web.state())
        states.append(web.state())
        await web.send(OutboundMessage(text="her words", paced=True))
        await web.send(OutboundMessage(text="Hung up."))
        states.append(web.state())
        await web.stop()
        return states

    during, after, noted = asyncio.run(scenario())
    assert during["typing"] is True and during["hold_after"] == 41
    assert after["typing"] is False and after["hold_after"] is None
    assert [n["text"] for n in noted["notes"]] == ["Hung up."]   # her words live in the DB


# ------------------------------------------------- through the runtime

def test_a_web_message_is_answered_on_the_web_in_the_shared_history(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    session = Session(store, persona=PersonaParams(), timing=TimingParams(),
                      variant=MoodVariant.DECOUPLED_OFFSETS, seed=SEED,
                      client=FakeClient(responses=["hi from her"]), clock=VirtualClock(0.0),
                      judge=ScriptedJudge(score=0.5).judge_day)
    tg, web = FakeChannel(), Recording(watermark=store.max_message_id)
    multi = MultiChannel(tg, web)

    async def no_sleep(_s):
        return None

    rt = AsyncRuntime(session, ProactiveSchedule.restore(SEED, store), multi,
                      store=store, timing=TimingParams(), seed=SEED,
                      time_scale=TimeScale(0.02), max_virtual_hours=24.0, sleeper=no_sleep)

    async def scenario():
        await multi.start(rt._on_inbound)
        loop = asyncio.get_running_loop()
        status, _ = await loop.run_in_executor(
            None, _post, f"http://127.0.0.1:{web.port}/web/send", {"text": "hello"})
        for _ in range(200):
            if web.sent:
                break
            await asyncio.sleep(0.02)
        await multi.stop()
        rt._executor.shutdown()
        return status

    assert asyncio.run(scenario()) == 202
    assert [m.text for m in web.sent] == ["hi from her"]
    assert tg.sent == []
    rows = [(m["role"], m["content"]) for m in store.recent_messages(20)
            if m["role"] in ("user", "assistant")]
    assert rows == [("user", "hello"), ("assistant", "hi from her")]


def test_a_photo_sent_from_the_page_reaches_her(tmp_path, monkeypatch):
    """End to end through the runtime: a photo posted on the page is stored
    (shrunk to a jpeg reference), stays in her stream, and she answers on the
    web."""
    monkeypatch.delenv("HARNESS_IMAGE_DIR", raising=False)
    images.configure(tmp_path / "images")
    buf = io.BytesIO()
    Image.new("RGBA", (64, 48), (200, 30, 30, 255)).save(buf, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    store = SQLiteStore(tmp_path / "s.db")
    session = Session(store, persona=PersonaParams(), timing=TimingParams(),
                      variant=MoodVariant.DECOUPLED_OFFSETS, seed=SEED,
                      client=FakeClient(responses=["cute"]), clock=VirtualClock(0.0),
                      judge=ScriptedJudge(score=0.5).judge_day)
    tg, web = FakeChannel(), Recording(watermark=store.max_message_id)
    multi = MultiChannel(tg, web)

    async def no_sleep(_s: float) -> None:
        return None

    rt = AsyncRuntime(session, ProactiveSchedule.restore(SEED, store), multi,
                      store=store, timing=TimingParams(), seed=SEED,
                      time_scale=TimeScale(0.02), max_virtual_hours=24.0, sleeper=no_sleep)

    async def scenario():
        await multi.start(rt._on_inbound)
        loop = asyncio.get_running_loop()
        status, _ = await loop.run_in_executor(
            None, _post, f"http://127.0.0.1:{web.port}/web/send",
            {"text": "my cat", "photos": [url]})
        for _ in range(200):
            if web.sent:
                break
            await asyncio.sleep(0.02)
        await multi.stop()
        rt._executor.shutdown()
        return status

    try:
        assert asyncio.run(scenario()) == 202
        assert [m.text for m in web.sent] == ["cute"]
        assert tg.sent == []
        [row] = [m for m in store.messages_since(0) if m["role"] == "user"]
        [name] = images.of(row)
        assert (tmp_path / "images" / name).is_file()
    finally:
        store.close()
        images.configure(None)


# ---------------------------------------------------- observability side

@pytest.fixture
def app(tmp_path, monkeypatch):
    from observability import server as server_mod

    db = tmp_path / "live.db"
    store = SQLiteStore(db)
    store.add_message("system", "STATE UPDATE: card", 1.0, 0)
    store.add_message("user", "hi", 1.1, 0)
    store.add_message("assistant", "*waves* hey", 1.2, 0)
    store.add_message("system",
                      with_call_face_rule(CALL_START_ROW.format(time="21:46")), 1.3, 0)
    store.add_message("user", "you there?", 1.4, 0)
    store.add_message("assistant", "mhm", 1.5, 0)
    store.add_message("system",
                      CALL_END_ROW.format(start="21:46", end="21:51", minutes=5), 1.6, 0)
    store.log_event(0, 1.7, "call_decided", "call=deadbeef answer=False")
    store.add_message("assistant", "can't talk now", 1.7, 0)
    store.add_message("system", CALL_HUNG_UP_RINGING_ROW.format(time="22:00"), 1.8, 0)
    store.close()
    monkeypatch.setenv("HARNESS_CHAT_DB", str(db))
    (tmp_path / "amadeus/face").mkdir(parents=True)
    (tmp_path / "amadeus/face/live-face.js").write_text("export {}")
    (tmp_path / "amadeus/secret.txt").write_text("no")
    monkeypatch.setattr(server_mod, "AMADEUS_DIR", tmp_path / "amadeus")
    srv = server_mod.serve(host="127.0.0.1", port=0, root=tmp_path, announce=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_the_three_pages_are_served(app):
    for path, marker in (("/chat", b"chat.js"), ("/setup", b"data-cmd"),
                         ("/debug", b"app.js"), ("/", b"app.js")):
        with urllib.request.urlopen(app + path, timeout=5) as r:
            assert marker in r.read(), path


def test_chat_history_marks_calls_and_keeps_other_system_rows_out(app):
    data = _get(app + "/api/chat/history")
    entries = data["messages"]
    # his and her lines, with the call marks in their place: a mark opens
    # before the lines it explains (the missed call before her text) and a
    # call closes after its turns; the state card never reaches the page.
    assert [(m.get("kind") or m["who"]) for m in entries] == [
        "him", "her", "call_started", "him", "her", "call_ended",
        "missed_call", "her", "missed_call"]
    started = next(m for m in entries if m.get("kind") == "call_started")
    assert started["src"] == "m" and started["sub"] is None
    ended = next(m for m in entries if m.get("kind") == "call_ended")
    assert (ended["start"], ended["end"], ended["minutes"]) == ("21:46", "21:51", 5)
    missed = [m for m in entries if m.get("kind") == "missed_call"]
    assert missed[0]["src"] == "e" and missed[0]["sub"] is None      # the ring-out
    assert missed[1]["sub"] == "hung up while ringing"               # his hang-up
    assert all("STATE UPDATE" not in (m.get("text") or "") for m in entries)


def test_chat_history_after_cursors_return_only_newer_rows(app):
    first = _get(app + "/api/chat/history")
    msg_id = max(m["id"] for m in first["messages"] if m["src"] == "m")
    ev_id = max(m["id"] for m in first["messages"] if m["src"] == "e")
    newer = _get(app + f"/api/chat/history?after={msg_id}&after_event={ev_id}")
    assert newer["messages"] == []
    # a message cursor alone still replays the marks it has not sent yet
    only_msg = _get(app + "/api/chat/history?after=3")
    assert [(m.get("kind") or m["who"]) for m in only_msg["messages"]] == [
        "call_started", "him", "her", "call_ended", "missed_call", "her",
        "missed_call"]


def test_only_face_and_sprites_are_served_from_amadeus(app):
    with urllib.request.urlopen(app + "/face/live-face.js", timeout=5) as r:
        assert r.read() == b"export {}"
    for bad in ("/face/../secret.txt", "/sprites/../secret.txt"):
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(app + bad, timeout=5)


def test_writes_are_forwarded_to_the_bot(app, monkeypatch):
    async def run():
        got = []

        async def on_message(msg):
            got.append(msg.text)

        web = WebChannel(port=0)
        await web.start(on_message)
        monkeypatch.setenv("HARNESS_WEB_PORT", str(web.port))
        loop = asyncio.get_running_loop()
        status, _ = await loop.run_in_executor(None, _post, app + "/api/chat/send", {"text": "yo"})
        state = await loop.run_in_executor(None, _get, app + "/api/chat/state")
        await asyncio.sleep(0.05)
        await web.stop()
        return status, state, got

    status, state, got = asyncio.run(run())
    assert status == 202 and got == ["yo"] and state["typing"] is False


def test_photos_sent_from_the_page_reach_the_bot(app, monkeypatch):
    """The page posts photos as data URLs, past the 64 KB a text send fits
    in; a photo alone is a message. Malformed or too many is a 400."""
    import base64

    photo = bytes(range(256)) * 400                    # ~100 KB, not inspected here
    url = "data:image/jpeg;base64," + base64.b64encode(photo).decode()

    async def run():
        got = []

        async def on_message(msg):
            got.append((msg.text, msg.photos))

        web = WebChannel(port=0)
        await web.start(on_message)
        monkeypatch.setenv("HARNESS_WEB_PORT", str(web.port))
        loop = asyncio.get_running_loop()
        send = app + "/api/chat/send"
        out = [
            await loop.run_in_executor(None, _post, send, {"text": "look", "photos": [url]}),
            await loop.run_in_executor(None, _post, send, {"text": "", "photos": [url]}),
            await loop.run_in_executor(None, _post, send, {"text": "x", "photos": ["%%%"]}),
            await loop.run_in_executor(None, _post, send, {"text": "x", "photos": [url] * 5}),
        ]
        await asyncio.sleep(0.05)
        await web.stop()
        return [status for status, _ in out], got

    statuses, got = asyncio.run(run())
    assert statuses == [202, 202, 400, 400]
    assert got == [("look", [photo]), ("", [photo])]


def test_writes_report_502_when_the_bot_is_down(app, monkeypatch):
    import socket
    with socket.socket() as sock:           # a port nothing listens on
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
    monkeypatch.setenv("HARNESS_WEB_PORT", str(free))
    status, body = _post(app + "/api/chat/send", {"text": "yo"})
    assert status == 502 and "not reachable" in body["error"]
