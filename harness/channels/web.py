"""The web chat as a channel: a small local HTTP API the /chat page talks to.

What it does: the page POSTs what he types and it reaches the runtime as an
``InboundMessage``, exactly like a Telegram message: same session, same
conversation, same database. Her reply is saved by the session as usual; the
page reads the history from the database (through the observability server),
so this channel keeps no copy of it. What the channel does keep is what the
database does not have:

- ``typing``: she is generating or waiting out her reply delay for a web
  message. While it is on, ``hold_after`` is the newest message id at the
  moment it started: the page hides her rows past it, so her reply shows
  when it is sent, not when it is saved (the same moment Telegram gets it).
- ``notes``: harness text sent here (command answers, errors), never saved
  to the database. The last ``NOTES_MAX`` are kept.

API (127.0.0.1 only, JSON):
    POST /web/send     {"text": "...", "photos": ["<base64>", ...]} -> 202 {"ok": true}
                       (photos optional, up to ``PHOTOS_MAX``, data URLs accepted;
                       a message needs text or a photo)
    POST /web/command  {"name": "call", "args": ""} -> 202 {"ok": true}
    GET  /web/state                               -> {"typing", "hold_after", "seq", "notes", "presence"}

Interface:
    WebChannel(port=None, bind="127.0.0.1", watermark=None, presence=None)
        watermark() -> int   newest message id (``store.max_message_id``)
        name = "web"; start(on_message, on_command=None); send; typing_context;
        typing_pause; stop
    web_port() -> int        HARNESS_WEB_PORT, default 8791

Dependencies: standard library and ``harness.channels.base``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness.channels.base import InboundMessage, OutboundMessage

_logger = logging.getLogger(__name__)

DEFAULT_PORT = 8791
NOTES_MAX = 50
#: Longest message accepted from the page (characters).
TEXT_MAX = 4000
#: Most photos one message carries (the session stores and shrinks each one).
PHOTOS_MAX = 4
#: Largest request body: a send with photos, base64 (phone photos are a few MB).
SEND_BODY_MAX = 40_000_000
#: Largest body for every other request.
BODY_MAX = 64_000


def web_port() -> int:
    raw = os.environ.get("HARNESS_WEB_PORT", "").strip()
    return int(raw) if raw else DEFAULT_PORT


class WebChannel:
    name = "web"

    def __init__(self, port: int | None = None, bind: str = "127.0.0.1",
                 watermark=None, presence=None):
        self.port = web_port() if port is None else port
        self.bind = bind
        self.watermark = watermark
        self.presence = presence
        self._on_message = None
        self._on_command = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._typing = 0
        self._hold_after: int | None = None
        self._seq = 0
        self._notes: list[dict] = []

    # -- channel ----------------------------------------------------------

    async def start(self, on_message, on_command=None) -> None:
        self._on_message = on_message
        self._on_command = on_command
        self._loop = asyncio.get_running_loop()
        server = ThreadingHTTPServer((self.bind, self.port), _make_handler(self))
        server.daemon_threads = True
        self.port = server.server_address[1]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="llh-web-channel", daemon=True)
        self._thread.start()

    async def send(self, message: OutboundMessage) -> None:
        with self._lock:
            self._seq += 1
            if not message.paced:
                # Harness text: not in the database, so the page needs it here.
                self._notes.append({"seq": self._seq, "text": message.text,
                                    "at": time.time()})
                del self._notes[:-NOTES_MAX]

    @contextlib.asynccontextmanager
    async def typing_context(self):
        mark = self.watermark() if self.watermark is not None else None
        with self._lock:
            if self._typing == 0:
                self._hold_after = mark
            self._typing += 1
            self._seq += 1
        try:
            yield
        finally:
            with self._lock:
                self._typing -= 1
                if self._typing == 0:
                    self._hold_after = None
                self._seq += 1

    async def typing_pause(self, seconds: float) -> None:
        async with self.typing_context():
            await asyncio.sleep(seconds)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # -- called on server threads ----------------------------------------

    def state(self) -> dict:
        with self._lock:
            return {"typing": self._typing > 0, "hold_after": self._hold_after,
                    "seq": self._seq, "notes": list(self._notes),
                    "presence": self.presence() if self.presence is not None else None}

    def _submit(self, coro) -> None:
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def report(f):
            exc = f.exception()
            if exc is not None:
                _logger.error("web channel handler failed", exc_info=exc)
        future.add_done_callback(report)

    def submit_text(self, text: str, photos: list[bytes] | None = None) -> None:
        if self._on_message is None:
            raise RuntimeError("the web channel is not started")
        self._submit(self._on_message(InboundMessage(
            text=text, sender_id="web", received_at=time.time(),
            photos=list(photos or []))))

    def submit_command(self, name: str, args: str) -> None:
        if self._on_command is None:
            raise RuntimeError("commands are not enabled")
        from harness.channels.telegram import ControlCommand
        self._submit(self._on_command(ControlCommand(name=name, args=args, sender_id=0)))


def _photos(raw) -> list[bytes] | None:
    """The photos of a send, decoded; None when they are malformed. Whether
    each is an image is the session's call (``images.save``)."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > PHOTOS_MAX:
        return None
    out: list[bytes] = []
    for item in raw:
        if not isinstance(item, str):
            return None
        _, _, data = item.rpartition(",")       # a data URL's "data:...;base64,"
        try:
            out.append(base64.b64decode(data, validate=True))
        except (binascii.Error, ValueError):
            return None
    return out


def _make_handler(channel: WebChannel):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "harness-web/1"

        def log_message(self, fmt, *args):
            return

        def _json(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self, limit: int = BODY_MAX) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > limit:
                return {}
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return {}
            return value if isinstance(value, dict) else {}

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/web/state":
                self._json(200, channel.state())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            body = self._body(SEND_BODY_MAX if path == "/web/send" else BODY_MAX)
            try:
                if path == "/web/send":
                    text = body.get("text")
                    text = text.strip() if isinstance(text, str) else ""
                    photos = _photos(body.get("photos"))
                    if photos is None:
                        self._json(400, {"error": f"photos: at most {PHOTOS_MAX}, as base64"})
                        return
                    if ((not text and not photos) or len(text) > TEXT_MAX
                            or text.startswith("/")):
                        self._json(400, {"error": "empty, too long, or a command"})
                        return
                    channel.submit_text(text, photos)
                elif path == "/web/command":
                    name = str(body.get("name") or "").strip().lstrip("/")
                    if not name.isidentifier():
                        self._json(400, {"error": "bad command name"})
                        return
                    channel.submit_command(name, str(body.get("args") or "").strip())
                else:
                    self._json(404, {"error": "not found"})
                    return
            except RuntimeError as exc:
                self._json(503, {"error": str(exc)})
                return
            self._json(202, {"ok": True})

    return Handler
