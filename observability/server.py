"""The observability web app: read-only JSON + Server-Sent Events + one page.

Standard library only. The server never mutates a run: every handler goes
through ``observability.reader``, which opens the database read-only. Two
concurrency guards keep a live bot's writer happy: the run id in a request is
matched against the discovered run list (no path from the URL reaches the
filesystem), and the SSE loop only polls cheap id/mtime probes.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from observability import reader

#: Seconds between SSE probes.
STREAM_INTERVAL_S: float = 1.5

#: One SSE response lives at most this long, then the page reconnects.
STREAM_MAX_S: float = 1800.0

#: Static assets served from the package directory.
STATIC_DIR: Path = Path(__file__).resolve().parent / "static"
_CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                  ".css": "text/css; charset=utf-8",
                  ".js": "text/javascript; charset=utf-8",
                  ".svg": "image/svg+xml"}


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
    """GET-only JSON/SSE/static endpoints."""

    protocol_version = "HTTP/1.1"
    server_version = "harness-observability/1.0"

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Quiet: the harness' own log is the record of interest.

        ``format``/``args`` are part of the ``BaseHTTPRequestHandler``
        contract and intentionally unused.
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
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._send_static("index.html")
            return
        if path.startswith("/static/"):
            self._send_static(path[len("/static/"):])
            return
        if path.startswith("/api/"):
            routes: dict[str, Callable[[dict[str, str]], None]] = {
                "/api/runs": self._runs,
                "/api/run": self._run,
                "/api/run/events": self._events,
                "/api/run/context": self._context,
                "/api/run/call": self._call,
                "/api/run/stream": self._stream,
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

    def _send_static(self, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        if not str(target).startswith(str(STATIC_DIR)) or not target.is_file():
            self._send_json({"error": "no such asset"}, status=404)
            return
        self._send(200, target.read_bytes(),
                   _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

    def _run_or_404(self, params: dict[str, str]) -> reader.RunRef | None:
        ref = self.state.resolve_run(params.get("id"))
        if ref is None:
            self._send_json({"error": "no run matched that id"}, status=404)
        return ref

    def _runs(self, params: dict[str, str]) -> None:
        now = time.time()
        runs = [reader.run_summary(ref, now=now, context_window=self.state.context_window)
                for ref in reader.find_runs(self.state.root)]
        self._send_json({"runs": runs, "root": str(self.state.root),
                         "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})

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
            )
        self._send_json(payload)

    def _call(self, params: dict[str, str]) -> None:
        ref = self._run_or_404(params)
        if ref is None:
            return
        with reader.open_run(ref.path) as conn:
            payload = reader.call_detail(
                conn, reader.load_anchor(conn), self._int_param(params, "call", 0))
        if payload is None:
            self._send_json({"error": "no such call"}, status=404)
            return
        self._send_json(payload)

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
                self.wfile.write(f"data: {json.dumps(probe)}\n\n".encode("utf-8"))
                self.wfile.flush()
                last = probe
            time.sleep(STREAM_INTERVAL_S)

    def _probe(self, ref: reader.RunRef) -> dict[str, Any]:
        """What the page needs to know that something moved."""
        with reader.open_run(ref.path) as conn:
            payload = reader.latest_ids(conn)
        stat = ref.path.stat()
        payload.update({"mtime": stat.st_mtime, "size": stat.st_size,
                        "now": time.strftime("%H:%M:%S")})
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
