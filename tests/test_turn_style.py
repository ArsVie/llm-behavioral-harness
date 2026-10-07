"""The writing block (TURN_STYLE).

Stored once, as a system row at the start of each day's stream right after
the day's plan, not attached to every message (owner, 2026-09-28).
"""

from __future__ import annotations

import pytest

from harness.assembler import harness_text_in_user_roles
from harness.prompts import TURN_STYLE
from tests.helpers.prompt_session import lily_session


def _contents(messages):
    return [m["content"] for m in messages]


def test_the_writing_block_is_stored_once_and_sent_once(tmp_path):
    session, client, store = lily_session(tmp_path, responses=["one.", "two.", "three."])
    session.on_message("hey")
    first = client.calls[-1]["messages"]
    for text in ("again", "and again"):
        session.clock.advance_hours(0.05)
        session.on_message(text)
    last = client.calls[-1]["messages"]
    stored = [row[0] for row in store.conn.execute("SELECT content FROM messages")]
    store.close()
    assert stored.count(TURN_STYLE) == 1
    assert _contents(first).count(TURN_STYLE) == 1
    assert _contents(last).count(TURN_STYLE) == 1
    # It sits before his first message, not before his newest one.
    first_user = min(i for i, m in enumerate(last) if m["role"] == "user")
    assert _contents(last).index(TURN_STYLE) < first_user
    # Append-only: the first request opens the last one.
    assert _contents(last)[: len(first)] == _contents(first)
    assert harness_text_in_user_roles(last) == []


def test_a_restart_the_same_day_does_not_repeat_it(tmp_path):
    session, _client, store = lily_session(tmp_path)
    session.on_message("hey")
    session._emit_turn_style_row(session.clock.day(), session.clock.now_h())
    stored = [row[0] for row in store.conn.execute("SELECT content FROM messages")]
    store.close()
    assert stored.count(TURN_STYLE) == 1


@pytest.mark.parametrize("off", ["0", "false", "off"])
def test_the_writing_block_can_be_switched_off(tmp_path, monkeypatch, off):
    monkeypatch.setenv("HARNESS_TURN_STYLE", off)
    session, client, store = lily_session(tmp_path)
    session.on_message("hey")
    messages = client.calls[-1]["messages"]
    store.close()
    assert TURN_STYLE not in _contents(messages)


@pytest.mark.parametrize("count", [0, 57])
def test_the_provider_reasoning_count_is_stored(tmp_path, count):
    import json
    session, _client, store = lily_session(tmp_path, responses=[{
        "content": "fine.",
        "usage": {"prompt_tokens": 100, "completion_tokens": 80,
                  "completion_tokens_details": {"reasoning_tokens": count}},
    }])
    session.on_message("hey")
    meta = store.conn.execute(
        "SELECT meta FROM llm_calls WHERE role='chat'").fetchone()[0]
    store.close()
    assert json.loads(meta)["reasoning_tokens"] == count
