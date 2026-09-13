"""Wire dumps: the exact-body save path and the observability reader.

The promise under test: what lands in ``wire/`` is the payload handed to the
transport, the client dumps it before every send, and the app can read files
back without any name escaping the wire directory.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from harness import wire as harness_wire
from harness.store import SQLiteStore
from observability import server as server_mod
from observability import wire as obs_wire


@pytest.fixture(autouse=True)
def _clean_wire_env(monkeypatch):
    monkeypatch.delenv("HARNESS_WIRE_DIR", raising=False)
    harness_wire.configure(None)
    yield
    harness_wire.configure(None)


def _payload() -> dict:
    return {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "tool_decide_event"}}],
        "temperature": 0.8,
    }


# -- the save path -----------------------------------------------------


def test_save_writes_the_body_verbatim(tmp_path):
    harness_wire.configure(tmp_path)
    payload = _payload()
    harness_wire.save(payload)
    files = sorted(tmp_path.glob("[0-9]*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8")) == payload
    index = [json.loads(line)
             for line in (tmp_path / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert index[0]["file"] == files[0].name
    assert index[0]["tools"] == ["tool_decide_event"]
    assert index[0]["messages"] == 1


def test_env_dir_overrides_the_configured_dir(tmp_path, monkeypatch):
    harness_wire.configure(tmp_path / "configured")
    other = tmp_path / "other"
    monkeypatch.setenv("HARNESS_WIRE_DIR", str(other))
    harness_wire.save(_payload())
    assert not list((tmp_path / "configured").glob("[0-9]*.json"))
    assert len(list(other.glob("[0-9]*.json"))) == 1


def test_no_dump_without_a_dir(tmp_path):
    harness_wire.configure(None)
    harness_wire.save(_payload())
    assert list(tmp_path.glob("**/*.json")) == []


def test_save_never_raises(tmp_path):
    target = tmp_path / "occupied"
    target.write_text("not a directory", encoding="utf-8")
    harness_wire.configure(target)          # mkdir fails → dormant
    harness_wire.save(_payload())           # must not raise
    assert target.read_text(encoding="utf-8") == "not a directory"


def test_the_client_dumps_exactly_what_it_sends(tmp_path):
    """The dump equals the payload handed to the transport, field for field."""
    seen: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class _Transport:
        def post(self, url, *, headers=None, json=None):
            seen["payload"] = json
            return _Resp()

    from harness.client import OpenAICompatibleClient

    client = OpenAICompatibleClient(base_url="http://stub", api_key="k",
                                    model="m", max_retries=0)
    client._client = _Transport()
    harness_wire.configure(tmp_path)
    reply = client.chat([{"role": "user", "content": "hey"}], system="sys")
    assert reply == "ok"
    files = list(tmp_path.glob("[0-9]*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8")) == seen["payload"]


# -- the observability reader ------------------------------------------


def _run_with_wire(tmp_path):
    run = tmp_path / "runs" / "companion.db"
    run.parent.mkdir(parents=True, exist_ok=True)
    return run


def test_listing_and_read(tmp_path):
    run = _run_with_wire(tmp_path)
    wire_dir = run.parent / "wire"
    wire_dir.mkdir()
    (wire_dir / "00001_20260913-131931.json").write_text('{"a": 1}', encoding="utf-8")
    (wire_dir / "00002_20260913-132045.json").write_text('{"a": 2}', encoding="utf-8")
    (wire_dir / "index.jsonl").write_text("{}\n", encoding="utf-8")

    listing = obs_wire.wire_listing(run)
    assert [f["name"] for f in listing["files"]] == [
        "00001_20260913-131931.json", "00002_20260913-132045.json",
    ]
    assert obs_wire.wire_read(run, "00001_20260913-131931.json") == '{"a": 1}'
    # the index is not a dump; nor is anything that could leave the directory
    assert obs_wire.wire_read(run, "index.jsonl") is None
    assert obs_wire.wire_read(run, "../companion.db") is None
    assert obs_wire.wire_read(run, "/etc/passwd") is None
    assert obs_wire.wire_read(run, "00003_20260913-132100.json") is None


def test_missing_wire_dir_is_empty_not_an_error(tmp_path):
    run = _run_with_wire(tmp_path)
    assert obs_wire.wire_listing(run)["files"] == []
    assert obs_wire.wire_read(run, "00001_20260913-131931.json") is None


# -- the HTTP routes ---------------------------------------------------


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_server_wire_routes(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    store = SQLiteStore(runs / "companion.db")
    store.close()
    wire_dir = runs / "wire"
    wire_dir.mkdir()
    (wire_dir / "00001_20260913-131931.json").write_text("body-1", encoding="utf-8")

    app = server_mod.serve(host="127.0.0.1", port=0, root=tmp_path, announce=False)
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{app.server_address[1]}"
    try:
        runs = json.loads(_get(f"{base}/api/runs?all=1")[1])
        run_id = urllib.parse.quote(runs["runs"][0]["path"])
        status, body = _get(f"{base}/api/run/wire?id={run_id}")
        assert status == 200
        listing = json.loads(body)
        assert [f["name"] for f in listing["files"]] == ["00001_20260913-131931.json"]

        status, body = _get(
            f"{base}/api/run/wire/file?id={run_id}&name=00001_20260913-131931.json")
        assert status == 200 and body == b"body-1"

        status, _ = _get(f"{base}/api/run/wire/file?id={run_id}&name=../companion.db")
        assert status == 404
        status, _ = _get(f"{base}/api/run/wire/file?id={run_id}&name=index.jsonl")
        assert status == 404
    finally:
        app.shutdown()
        app.server_close()
        thread.join(timeout=5)
