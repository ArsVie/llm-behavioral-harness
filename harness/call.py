"""Voice call: one call's state and the local HTTP server live-talk talks to.

What it does: live-talk (on Windows) reaches the harness over
``http://127.0.0.1:<port>/call/<id>`` (WSL2 forwards localhost). Each thing he
says is POSTed as a turn and her reply streams back sentence by sentence as
Server-Sent Events; live-talk then reports what was actually heard. The
contract is the one fixed for this build: ``/turn``, ``/heard``, ``GET``
status, ``/end``, ``/heartbeat``.

Interface:
    ActiveCall(call_id)                  one call's bookkeeping (thread-safe)
        .begin_turn() / .finish_heard()  the /turn -> /heard alternation
        .touch() / .idle_s()             heartbeat bookkeeping
        .status() -> dict                what GET {base} answers
    CallServer(host, port=8790)          .start() / .stop() / .base_url(call_id)
    host (the runtime) implements, all called on server threads:
        host.call_lookup(call_id) -> ActiveCall | None
        host.call_turn(call, text, cancel) -> Iterator[str]   her sentences
                                          (a FaceEvent first when her reply
                                          opens with a face tag: {"face": name})
        host.call_heard(call, text, cut) -> None
        host.call_end(call, reason) -> None                    idempotent
    CallConflict                         raised for the 409 cases
    split_sentences(buf) -> (sentences, rest)   copied from live-talk llm.py
    launch_command(url) -> list[str]     the Windows command that opens live-talk

Dependencies: standard library only. Nothing here touches the session or the
store; the host does, under the runtime lock.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: Default port; ``HARNESS_CALL_PORT`` overrides it.
DEFAULT_PORT = 8790

#: No heartbeat and no request for this long ends the call (``rig_lost``).
HEARTBEAT_TIMEOUT_S = 30.0

#: The call lane's reasoning setting (``HARNESS_CALL_THINKING``): "disabled"
#: (the default) sends ``thinking: {type: disabled}`` for a fast first
#: sentence, the way live-talk does against the same provider; anything else
#: keeps the text lane's reasoning setting.
THINKING_ENV = "HARNESS_CALL_THINKING"

#: The launch command (``HARNESS_CALL_LAUNCH`` overrides it, ``{url}``
#: substituted). ``start`` opens a visible console window on the desktop.
_CMD_EXE = "/mnt/c/Windows/System32/cmd.exe"
_LIVE_TALK_DIR = r"C:\dev\live-talk"
_LIVE_TALK_PYTHON = r"C:\dev\live-talk\.venv\Scripts\python.exe"


class CallConflict(RuntimeError):
    """A request the call's state does not allow right now (HTTP 409)."""


def call_port() -> int:
    raw = os.environ.get("HARNESS_CALL_PORT", "").strip()
    return int(raw) if raw else DEFAULT_PORT


def thinking_extra() -> dict | None:
    """The extra request fields a call turn sends, or None."""
    value = os.environ.get(THINKING_ENV, "disabled").strip().lower()
    if value in ("", "disabled", "off", "0", "no"):
        return {"thinking": {"type": "disabled"}}
    return None


def launch_command(url: str) -> list[str]:
    """argv that opens live-talk on the Windows desktop, pointed at ``url``.

    Runs through WSL interop (``cmd.exe`` by absolute path: a system unit's
    PATH does not carry the Windows directories). ``HARNESS_CALL_LAUNCH``
    replaces the whole command: a shell line run by ``/bin/sh -c``, with
    ``{url}`` substituted (quoted).
    """
    override = os.environ.get("HARNESS_CALL_LAUNCH", "").strip()
    if override:
        return ["/bin/sh", "-c", override.replace("{url}", shlex.quote(url))]
    return [
        _CMD_EXE, "/c", "start", "Call with Lily", "/D", _LIVE_TALK_DIR,
        _LIVE_TALK_PYTHON, "talk.py", "--harness", url,
    ]


# Sentence splitting for TTS: a copy of live-talk llm.py's split_sentences
# (copied, not imported: live-talk is a separate program on Windows).

SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")
FORCE_SPLIT = 160


def split_sentences(buf: str):
    """Pull complete sentences out of a growing buffer. Returns (sentences, rest)."""
    out = []
    while True:
        m = SENT_SPLIT.search(buf)
        if not m:
            break
        sent = buf[: m.start()].strip()
        buf = buf[m.end():]
        if sent:
            out.append(sent)
    if len(buf) > FORCE_SPLIT:
        cut = buf.rfind(" ", 0, FORCE_SPLIT)
        if cut > 0:
            out.append(buf[:cut].strip())
            buf = buf[cut:].lstrip()
    return out, buf


class ActiveCall:
    """One call's bookkeeping, shared by the server threads and the runtime.

    ``awaiting_heard`` is the /turn -> /heard alternation: a turn is accepted
    only once the previous reply has been reported. ``ending`` is set as soon
    as an end is requested (status reads "ended" from then on), ``ended`` once
    the end row is written.
    """

    def __init__(self, call_id: str, *, clock=time.monotonic):
        self.call_id = call_id
        self._clock = clock
        self._lock = threading.Lock()
        self.awaiting_heard = False
        self.ending = False
        self.ended = False
        self.reason: str | None = None
        self.turns = 0
        #: Cancel handle of the reply being generated, if any.
        self.cancel = None
        #: Set once the reply of the current turn has been reported (or the
        #: call ended): the end path waits on it briefly.
        self.heard_done = threading.Event()
        self.heard_done.set()
        self._last_seen = clock()

    def touch(self) -> None:
        with self._lock:
            self._last_seen = self._clock()

    def idle_s(self) -> float:
        with self._lock:
            return self._clock() - self._last_seen

    def begin_turn(self, cancel) -> int:
        """Accept a /turn or raise :class:`CallConflict`; returns its index."""
        with self._lock:
            self._last_seen = self._clock()
            if self.ending or self.ended:
                raise CallConflict("the call is not active")
            if self.awaiting_heard:
                raise CallConflict("the previous turn has not been reported with /heard")
            self.awaiting_heard = True
            self.heard_done.clear()
            self.cancel = cancel
            self.turns += 1
            return self.turns

    def check_heard(self) -> None:
        """Raise :class:`CallConflict` when there is no reply to report."""
        with self._lock:
            self._last_seen = self._clock()
            if self.ended:
                raise CallConflict("the call has ended")
            if not self.awaiting_heard:
                raise CallConflict("no turn is waiting for /heard")

    def finish_heard(self) -> None:
        with self._lock:
            self.awaiting_heard = False
            self.cancel = None
        self.heard_done.set()

    def stop_reply(self) -> None:
        """Cancel the reply being generated (a /heard or an end came in)."""
        with self._lock:
            cancel = self.cancel
        if cancel is not None:
            cancel.set()

    def request_end(self, reason: str) -> bool:
        """Mark the call as ending; False when it was already ending."""
        with self._lock:
            if self.ending or self.ended:
                return False
            self.ending = True
            self.reason = reason
        return True

    def status(self) -> dict:
        with self._lock:
            state = "ended" if (self.ending or self.ended) else "active"
            return {"state": state, "reason": self.reason}


class CallServer:
    """The call API over a stdlib ``ThreadingHTTPServer`` on 127.0.0.1.

    One thread per request, so a streaming /turn never blocks /heard, /end or
    the heartbeat. The server never holds the runtime lock itself; the host
    takes it per operation.
    """

    def __init__(self, host, port: int | None = None, bind: str = "127.0.0.1"):
        self.host = host
        self.port = call_port() if port is None else port
        self.bind = bind
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "CallServer":
        if self._server is not None:
            return self
        handler = _make_handler(self.host)
        server = ThreadingHTTPServer((self.bind, self.port), handler)
        server.daemon_threads = True
        self.port = server.server_address[1]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="llh-call-server", daemon=True
        )
        self._thread.start()
        return self

    def base_url(self, call_id: str) -> str:
        return f"http://127.0.0.1:{self.port}/call/{call_id}"

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def _make_handler(host):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "harness-call/1"

        def log_message(self, fmt, *args):  # noqa: D401 - quiet by design
            return

        # -- plumbing --------------------------------------------------- #

        def _json(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except ValueError:
                return {}
            return value if isinstance(value, dict) else {}

        def _route(self):
            """(call, action) for /call/<id>[/<action>], or None (404 sent)."""
            parts = [p for p in self.path.split("?", 1)[0].split("/") if p]
            if len(parts) not in (2, 3) or parts[0] != "call":
                self._json(404, {"error": "not found"})
                return None
            call = host.call_lookup(parts[1])
            if call is None:
                self._json(404, {"error": "unknown call"})
                return None
            return call, (parts[2] if len(parts) == 3 else "")

        # -- verbs ------------------------------------------------------ #

        def do_GET(self):
            routed = self._route()
            if routed is None:
                return
            call, action = routed
            if action:
                self._json(404, {"error": "not found"})
                return
            call.touch()
            self._json(200, call.status())

        def do_POST(self):
            routed = self._route()
            if routed is None:
                return
            call, action = routed
            body = self._body()
            try:
                if action == "turn":
                    self._turn(call, body)
                elif action == "heard":
                    text = body.get("text")
                    host.call_heard(call, text if isinstance(text, str) else "",
                                    bool(body.get("cut")))
                    self._json(200, {"ok": True})
                elif action == "end":
                    reason = body.get("reason")
                    host.call_end(call, str(reason) if reason else "hung_up")
                    self._json(200, {"ok": True})
                elif action == "heartbeat":
                    call.touch()
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"error": "not found"})
            except CallConflict as exc:
                self._json(409, {"error": str(exc)})

        def _turn(self, call: ActiveCall, body: dict) -> None:
            text = body.get("text")
            text = text if isinstance(text, str) else None
            from harness.client import StreamCancel

            cancel = StreamCancel()
            call.begin_turn(cancel)      # raises CallConflict -> 409
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            # No Content-Length: the stream ends when the connection closes.
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def send(event: dict) -> bool:
                try:
                    line = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                    return True
                except OSError:
                    return False

            try:
                for sentence in host.call_turn(call, text, cancel):
                    call.touch()
                    face = getattr(sentence, "face", None)    # harness.faces.FaceEvent
                    blush = getattr(sentence, "blush", None)
                    scene = getattr(sentence, "scene", None)
                    event = {"face": face} if face is not None else {"sentence": sentence}
                    if face is not None and blush:
                        event["blush"] = blush
                    if face is not None and scene:
                        event["scene"] = scene
                    if not send(event):
                        cancel.set()     # he closed the stream: stop generating
                if not cancel.is_set():
                    send({"done": True})
            except CallConflict as exc:
                send({"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - reported on the stream
                send({"error": f"{type(exc).__name__}: {exc}"})

    return Handler
