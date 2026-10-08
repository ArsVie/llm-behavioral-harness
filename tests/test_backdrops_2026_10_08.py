"""The /chat page backdrops: /api/backdrops lists the images in the folder."""

import json
import threading
import urllib.request

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    from observability import server as server_mod

    monkeypatch.setattr(server_mod, "BACKDROPS_DIR", tmp_path / "backdrops")
    srv = server_mod.serve(host="127.0.0.1", port=0, root=tmp_path, announce=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "backdrops"
    srv.shutdown()
    srv.server_close()


def _list(url):
    with urllib.request.urlopen(url + "/api/backdrops", timeout=5) as r:
        return json.loads(r.read())["backdrops"]


def test_no_backdrops_folder_lists_nothing(app):
    url, _ = app
    assert _list(url) == []


def test_only_images_are_listed_by_name(app):
    url, folder = app
    folder.mkdir()
    for name in ("street.webp", "Room.JPG", "notes.txt"):
        (folder / name).write_bytes(b"x")
    (folder / "sub").mkdir()
    assert _list(url) == [{"name": "Room", "url": "/static/backdrops/Room.JPG"},
                          {"name": "street", "url": "/static/backdrops/street.webp"}]
