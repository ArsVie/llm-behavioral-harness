"""The companion's web app: /chat, /debug and /setup.

- ``/debug`` (also ``/``): the observability dashboard, read-only JSON +
  Server-Sent Events.
- ``/chat``: the live conversation with her sprite and a call button.
- ``/setup``: the owner commands (/status, /tz, /mute, ...) as a page.

Standard library only. Every read goes through ``observability.reader`` or
``observability.chat``, which open the database read-only; the run id in a
request is matched against the discovered run list, so no path from the URL
reaches the filesystem. The only writes are the chat's and the setup page's,
and the server does not make them: it forwards them to the bot's web channel
(``harness.channels.web``), which hands them to the runtime like any message.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from harness.channels import web
from observability import chat, reader, study, wire
from observability.calls import envelope_of
from observability.db import rows as db_rows

#: Seconds between SSE probes.
STREAM_INTERVAL_S: float = 1.5

#: One SSE response lives at most this long, then the page reconnects.
STREAM_MAX_S: float = 1800.0

#: Static assets served from the package directory.
STATIC_DIR: Path = Path(__file__).resolve().parent / "static"
_CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                  ".css": "text/css; charset=utf-8",
                  ".js": "text/javascript; charset=utf-8",
                  ".svg": "image/svg+xml",
                  ".png": "image/png",
                  ".jpg": "image/jpeg",
                  ".jpeg": "image/jpeg",
                  ".ico": "image/x-icon",
                  ".json": "application/json; charset=utf-8",
                  ".webp": "image/webp"}

#: Page backdrops for /chat, picked on /setup: whatever images sit in this
#: folder. It is gitignored, so pictures he drops in stay off the repo.
BACKDROPS_DIR: Path = STATIC_DIR / "backdrops"
_BACKDROP_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

#: Her sprites and the face player live in the amadeus tree; only these two
#: directories under it are served.
AMADEUS_DIR: Path = Path.home() / "amadeus"
_AMADEUS_SERVED = ("face", "sprites")

#: The pages, by path.
_PAGES = {"/": "index.html", "/debug": "index.html",
          "/chat": "chat.html", "/setup": "setup.html"}

#: POST routes forwarded to the bot's web channel.
_CHAT_WRITES = {"/api/chat/send": "/web/send", "/api/chat/command": "/web/command"}


class AppState:
    """Configuration shared by every request handler."""

    def __init__(self, root: Path, context_window: int) -> None:
        self.root = root
        self.context_window = context_window

    def resolve_run(self, raw: str | None) -> reader.RunRef | None:
        """Map a request's ``id`` onto a discovered run, or None.

        The URL never names a file directly: only paths that a fresh scan
        returns are accepted, so no request can read outside the scan root.
        """
        if not raw:
            runs = reader.find_runs(self.root)
            return runs[0] if runs else None
        wanted = Path(raw).resolve()
        for ref in reader.find_runs(self.root):
            if ref.path.resolve() == wanted:
                return ref
        return None


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    """JSON/SSE/static endpoints; POST only for the forwarded chat writes."""

    protocol_version = "HTTP/1.1"
    server_version = "harness-observability/1.0"

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        """Quiet: the harness' own log is the record of interest.

        ``format``/``args`` are unused, required by the base class contract.
        """
        _ = (args, format)

    # -- plumbing ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _params(self) -> dict[str, str]:
        query = urllib.parse.urlparse(self.path).query
        return {key: values[0] for key, values in urllib.parse.parse_qs(query).items()}

    def _int_param(self, params: dict[str, str], name: str, default: int) -> int:
        try:
            return int(params.get(name, default))
        except (TypeError, ValueError):
            return default

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in _PAGES:
            self._send_static(_PAGES[path])
            return
        if path.startswith("/static/"):
            self._send_static(path[len("/static/"):])
            return
        if path.split("/")[1] in _AMADEUS_SERVED:
            self._send_amadeus(path.lstrip("/"))
            return
        if path == "/api/chat/history":
            self._chat_history(self._params())
            return
        if path.startswith("/api/chat/photo/"):
            target = chat.photo(path[len("/api/chat/photo/"):])
            if target is None:
                self._send_json({"error": "no such photo"}, status=404)
            else:
                self._send(200, target.read_bytes(), "image/jpeg")
            return
        if path == "/api/chat/state":
            self._send(*self._forward("GET", "/web/state"))
            return
        if path.startswith("/api/"):
            routes: dict[str, Callable[[dict[str, str]], None]] = {
                "/api/runs": self._runs,
                "/api/run": self._run,
                "/api/run/events": self._events,
                "/api/run/context": self._context,
                "/api/run/call": self._call,
                "/api/run/wire": self._wire_list,
                "/api/run/wire/file": self._wire_file,
                "/api/run/stream": self._stream,
                "/api/study/day": self._study_day,
                "/api/backdrops": self._backdrops,
            }
            handler = routes.get(path)
            if handler is None:
                self._send_json({"error": f"unknown endpoint {path}"}, status=404)
                return
            try:
                handler(self._params())
            except BrokenPipeError:
                self.close_connection = True
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        target = _CHAT_WRITES.get(path)
        if target is None:
            self._send_json({"error": "not found"}, status=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > (web.SEND_BODY_MAX if target == "/web/send" else web.BODY_MAX):
            self._send_json({"error": "too large"}, status=413)
            return
        body = self.rfile.read(length) if length > 0 else b"{}"
        self._send(*self._forward("POST", target, body))

    def _forward(self, method: str, target: str,
                 body: bytes | None = None) -> tuple[int, bytes, str]:
        status, data = chat.proxy(method, target, body, timeout=30.0)
        return status, data, "application/json; charset=utf-8"

    def _chat_history(self, params: dict[str, str]) -> None:
        path = chat.chat_db()
        if not path.is_file():
            self._send_json({"error": f"no chat database at {path}"}, status=404)
            return
        with reader.open_run(path) as conn:
            payload = chat.history(conn, reader.load_anchor(conn),
                                   after=self._int_param(params, "after", 0),
                                   after_event=self._int_param(params, "after_event", 0))
        self._send_json(payload)

    def _send_file(self, target: Path, roots: tuple[Path, ...]) -> None:
        """A file under one of ``roots``; anything resolving outside is a 404."""
        target = target.resolve()
        if not any(target.is_relative_to(root) for root in roots) or not target.is_file():
            self._send_json({"error": "no such asset"}, status=404)
            return
        self._send(200, target.read_bytes(),
                   _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

    def _send_amadeus(self, rel: str) -> None:
        root = AMADEUS_DIR.resolve()
        self._send_file(root / rel, tuple(root / d for d in _AMADEUS_SERVED))

    def _send_static(self, name: str) -> None:
        self._send_file(STATIC_DIR / name, (STATIC_DIR,))

    def _run_or_404(self, params: dict[str, str]) -> reader.RunRef | None:
        ref = self.state.resolve_run(params.get("id"))
        if ref is None:
            self._send_json({"error": "no run matched that id"}, status=404)
        return ref

    def _backdrops(self, params: dict[str, str]) -> None:
        """The images in the backdrops folder, by name, with their urls."""
        found = sorted(p for p in BACKDROPS_DIR.glob("*")
                       if p.is_file() and p.suffix.lower() in _BACKDROP_SUFFIXES) \
            if BACKDROPS_DIR.is_dir() else []
        self._send_json({"backdrops": [{"name": p.stem, "url": f"/static/backdrops/{p.name}"}
                                       for p in found]})

    def _runs(self, params: dict[str, str]) -> None:
        self._send_json(reader.runs_payload(
            self.state.root,
            context_window=self.state.context_window,
            include_all=params.get("all") == "1",
        ))

    def _run(self, params: dict[str, str]) -> None:
        ref = self._run_or_404(params)
        if ref is None:
            return
        call_id = params.get("call")
        self._send_json(reader.run_detail(
            ref,
            context_window=self.state.context_window,
            call_id=int(call_id) if call_id else None,
            limit=self._int_param(params, "limit", reader.DEFAULT_LIMIT),
        ))

    def _events(self, params: dict[str, str]) -> None:
        ref = self._run_or_404(params)
        if ref is None:
            return
        with reader.open_run(ref.path) as conn:
            payload = reader.events_payload(
                conn,
                reader.load_anchor(conn),
                after=self._int_param(params, "after", 0),
                limit=self._int_param(params, "limit", reader.DEFAULT_LIMIT),
            )
        self._send_json(payload)

    def _context(self, params: dict[str, str]) -> None:
        ref = self._run_or_404(params)
        if ref is None:
            return
        call_id = params.get("call")
        with reader.open_run(ref.path) as conn:
            payload = reader.context_payload(
                conn, reader.load_anchor(conn),
                call_id=int(call_id) if call_id else None,
                context_window=self.state.context_window,
                run_path=ref.path,
            )
        self._send_json(payload)

    def _call(self, params: dict[str, str]) -> None:
        ref = self._run_or_404(params)
        if ref is None:
            return
        with reader.open_run(ref.path) as conn:
            payload = reader.call_detail(
                conn, reader.load_anchor(conn), self._int_param(params, "call", 0),
                run_path=ref.path)
        if payload is None:
            self._send_json({"error": "no such call"}, status=404)
            return
        self._send_json(payload)

    def _study_day(self, params: dict[str, str]) -> None:
        """One day, as the calendar's Day view draws it."""
        ref = self._run_or_404(params)
        if ref is None:
            return
        with reader.open_run(ref.path) as conn:
            day = self._int_param(params, "day", study.latest_day(conn))
            payload = study.day_view(conn, day=day)
        self._send_json(payload)

    def _wire_list(self, params: dict[str, str]) -> None:
        ref = self._run_or_404(params)
        if ref is None:
            return
        payload = wire.wire_listing(ref.path)
        # Stamp each dump with the call it is the bytes of, so the verbatim
        # view is the same call the ledger and the composition are showing.
        with reader.open_run(ref.path) as conn:
            envelopes = [
                (int(row["id"]), envelope)
                for row in db_rows(conn, "select id, repro_json from llm_calls "
                                         "order by id")
                for envelope in [envelope_of(row)] if envelope
            ]
        wire.correlate(ref.path, payload["files"], envelopes)
        self._send_json(payload)

    def _wire_file(self, params: dict[str, str]) -> None:
        ref = self._run_or_404(params)
        if ref is None:
            return
        text = wire.wire_read(ref.path, params.get("name", ""))
        if text is None:
            self._send_json({"error": "no such wire dump"}, status=404)
            return
        self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _stream(self, params: dict[str, str]) -> None:
        """Server-Sent Events: a cheap id/mtime probe every interval."""
        ref = self._run_or_404(params)
        if ref is None:
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        deadline = time.monotonic() + STREAM_MAX_S
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            probe = self._probe(ref)
            if probe != last:
                self.wfile.write(f"data: {json.dumps(probe)}\n\n".encode())
                self.wfile.flush()
                last = probe
            time.sleep(STREAM_INTERVAL_S)

    def _probe(self, ref: reader.RunRef) -> dict[str, Any]:
        """What the page needs to know that something moved."""
        with reader.open_run(ref.path) as conn:
            payload = reader.latest_ids(conn)
        stat = ref.path.stat()
        # No clock here: the page ticks its own, so the probe only reports
        # what moved (emitting on change keeps an idle run nearly free).
        payload.update({"mtime": stat.st_mtime, "size": stat.st_size})
        return payload


class ObservabilityServer(ThreadingHTTPServer):
    """A threaded server carrying the app state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: AppState) -> None:
        super().__init__(address, Handler)
        self.state = state


def serve(host: str = "127.0.0.1", port: int = 8788, *,
          root: Path | None = None, context_window: int = reader.DEFAULT_CONTEXT_WINDOW,
          announce: bool = True) -> ObservabilityServer:
    """Start the server (already bound); the caller decides when to block."""
    state = AppState(root=root or reader.repo_root(), context_window=context_window)
    server = ObservabilityServer((host, port), state)
    if announce:
        print(f"harness observability → http://{host}:{server.server_address[1]}", flush=True)
    return server
