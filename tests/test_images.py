"""Photos the owner sends: storage, the request shape, Telegram intake."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from harness import images
from harness.assembler import build_messages, stamped_stream
from harness.client import FakeClient, OpenAICompatibleClient
from tests.helpers.store import make_session, make_store


def _png(size=(40, 30), color=(200, 30, 30, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, color).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def image_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_IMAGE_DIR", raising=False)
    images.configure(tmp_path / "images")
    yield tmp_path / "images"
    images.configure(None)


# storage

def test_a_photo_is_stored_once_as_an_upright_bounded_jpeg(image_dir):
    name = images.save(_png((4000, 1000)))
    assert name.endswith(".jpg") and len(name) == 64 + 4
    with Image.open(image_dir / name) as img:
        assert img.format == "JPEG"
        assert max(img.size) == images.MAX_SIDE
    assert images.save(_png((4000, 1000))) == name
    assert len(list(image_dir.iterdir())) == 1


def test_an_unreadable_file_is_refused(image_dir):
    with pytest.raises(ValueError):
        images.save(b"not an image")


# the request

def test_a_user_turn_with_a_photo_is_sent_as_parts_with_the_reference():
    [msg] = build_messages([], "look", photos=["abc.jpg"])
    assert msg["content"] == [
        {"type": "image_url", "image_url": {"url": "harness-image:abc.jpg"}},
        {"type": "text", "text": "look"},
    ]


def test_a_photo_turn_replays_from_its_row_byte_for_byte():
    """Append-only: request N+1 starts with request N's user turn."""
    anchor = SimpleNamespace(real_at=lambda t: SimpleNamespace(hour=21, minute=42))
    [sent] = build_messages([], "", anchor=anchor, t_h=5.0, photos=["abc.jpg"])
    row = {"role": "user", "content": "", "t_h": 5.0,
           "meta": json.dumps({"images": ["abc.jpg"]})}
    [replayed] = stamped_stream([row], anchor)
    assert replayed == sent
    assert sent["content"][-1]["text"].startswith("Time: 21:42")


def test_inline_swaps_the_reference_for_the_bytes_and_leaves_the_caller_alone(image_dir):
    name = images.save(_png())
    messages = build_messages([], "hi", photos=[name, "missing.jpg"])
    out = images.inline(messages)
    url = out[0]["content"][0]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == (image_dir / name).read_bytes()
    assert len(out[0]["content"]) == 2  # the missing photo dropped out
    assert messages[0]["content"][0]["image_url"]["url"] == images.SCHEME + name


def test_the_client_posts_the_bytes_and_dumps_the_reference(image_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_WIRE_DIR", str(tmp_path / "wire"))
    name = images.save(_png())
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatibleClient(base_url="https://example.test/v1",
                                    api_key="k", model="m")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    client.chat(build_messages([], "hi", photos=[name]), system="S")
    posted = seen["body"]["messages"][1]["content"][0]["image_url"]["url"]
    assert posted.startswith("data:image/jpeg;base64,")
    [dump] = sorted((tmp_path / "wire").glob("0*.json"))
    assert images.SCHEME + name in dump.read_text()
    assert "base64" not in dump.read_text()


# the session

def test_a_photo_message_is_stored_with_its_reference_and_stays_in_the_stream(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_IMAGE_DIR", raising=False)
    store = make_store(tmp_path)
    client = FakeClient(responses=["[smile] cute", "[smile] yes"])
    session = make_session(store, client=client)
    try:
        session.on_message("my cat", photos=[_png()])
        session.on_message("right?")
        [row] = [m for m in store.messages_since(0) if m["content"] == "my cat"]
        [name] = images.of(row)
        assert (tmp_path / "images" / name).is_file()
        first, second = client.calls[0]["messages"], client.calls[-1]["messages"]
        sent = [m for m in first if isinstance(m["content"], list)]
        assert sent and sent[-1] in second
        assert sent[-1]["content"][0]["image_url"]["url"] == images.SCHEME + name
        assert any(e["event"] == "photo_received"
                   for e in store.events_since(0))
    finally:
        session.store.close()
        images.configure(None)


def test_an_unreadable_photo_with_a_caption_still_sends_the_caption(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_IMAGE_DIR", raising=False)
    store = make_store(tmp_path)
    client = FakeClient(responses=["[smile] hm?"])
    session = make_session(store, client=client)
    try:
        session.on_message("look", photos=[b"garbage"])
        [row] = [m for m in store.messages_since(0) if m["role"] == "user"]
        assert images.of(row) == []
        assert any(e["event"] == "photo_unreadable"
                   for e in store.events_since(0))
        with pytest.raises(ValueError):
            session.on_message("", photos=[b"garbage"])
    finally:
        session.store.close()
        images.configure(None)


# Telegram

class _PhotoBot:
    def __init__(self, files):
        self.files = files

    async def get_file(self, file_id):
        data = self.files[file_id]

        async def download_as_bytearray():
            return bytearray(data)
        return SimpleNamespace(download_as_bytearray=download_as_bytearray)


def _photo_update(chat_id, caption=None, file_id="big"):
    sizes = [SimpleNamespace(file_id="small", width=90, height=60),
             SimpleNamespace(file_id=file_id, width=1280, height=853)]
    return SimpleNamespace(message=SimpleNamespace(
        text=None, caption=caption, photo=sizes, chat=SimpleNamespace(id=chat_id)))


def _channel(files):
    pytest.importorskip("telegram")
    from harness.channels.telegram import TelegramChannel
    from tests.helpers import FakeApplication

    app = FakeApplication()
    app.bot = _PhotoBot(files)
    return TelegramChannel(application=app, owner_chat_id="42"), app


def test_telegram_forwards_the_largest_photo_with_its_caption():
    channel, app = _channel({"big": b"BIG", "small": b"small"})
    got = []

    async def handler(msg):
        got.append(msg)

    async def scenario():
        await channel.start(handler)
        await app.handlers[0](_photo_update(42, caption="my cat"))
        await app.handlers[0](_photo_update(42))
        await app.handlers[0](_photo_update(7, caption="stranger"))

    asyncio.run(scenario())
    assert [(m.text, m.photos) for m in got] == [("my cat", [b"BIG"]), ("", [b"BIG"])]


def test_telegram_drops_a_photo_it_cannot_fetch_unless_it_has_a_caption():
    channel, app = _channel({})
    got = []

    async def handler(msg):
        got.append(msg)

    async def scenario():
        await channel.start(handler)
        await app.handlers[0](_photo_update(42))
        await app.handlers[0](_photo_update(42, caption="see?"))

    asyncio.run(scenario())
    assert [(m.text, m.photos) for m in got] == [("see?", [])]


def test_telegram_debounce_sends_an_album_as_one_message():
    channel, app = _channel({"a": b"A", "b": b"B"})
    channel.debounce_enabled = True
    got = []

    async def handler(msg):
        got.append(msg)

    async def scenario():
        await channel.start(handler)
        await app.handlers[0](_photo_update(42, caption="trip", file_id="a"))
        await app.handlers[0](_photo_update(42, file_id="b"))
        await channel._flush_debounce()

    asyncio.run(scenario())
    assert [(m.text, m.photos) for m in got] == [("trip", [b"A", b"B"])]


# the chat page

def test_the_chat_page_lists_and_serves_the_photo(tmp_path, monkeypatch):
    import sqlite3

    from observability import chat

    monkeypatch.delenv("HARNESS_IMAGE_DIR", raising=False)
    store = make_store(tmp_path)
    session = make_session(store, client=FakeClient(responses=["[smile] cute"]))
    try:
        session.on_message("my cat", photos=[_png()])
    finally:
        store.close()
        images.configure(None)
    monkeypatch.setenv("HARNESS_CHAT_DB", str(tmp_path / "s.db"))
    conn = sqlite3.connect(tmp_path / "s.db")
    try:
        rows = chat.history(conn, None)["messages"]
    finally:
        conn.close()
    [him] = [r for r in rows if r["who"] == "him"]
    [name] = him["photos"]
    assert chat.photo(name) == tmp_path / "images" / name
    assert chat.photo("../s.db") is None
    assert all(r["photos"] == [] for r in rows if r["who"] == "her")
