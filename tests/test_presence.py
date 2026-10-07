"""Presence mode: apart (default) and together, and the register it swaps.

/here and /leave flip it; a flip writes a mode row into the stream (the
voice-call rows' pattern), so the writing register changes from the next
message on. The fiction frame rides the stable system in both modes.
"""

from __future__ import annotations

import re

from harness.assembler import stable_system
from harness.channels.telegram import ControlCommand
from harness.commands import CommandContext, handle_command
from harness.prompts import (
    FICTION_FRAME,
    PRESENCE_END_OPENER,
    PRESENCE_START_OPENER,
    SYSTEM_CORE_WITH_TOOLS,
    TURN_STYLE,
    TURN_STYLE_TOGETHER,
)
from observability.context import system_row_label
from tests.helpers.prompt_session import lily_session


def _stored(store):
    return [row[0] for row in store.conn.execute("SELECT content FROM messages")]


def test_the_default_is_apart_and_the_texting_register(tmp_path):
    session, client, store = lily_session(tmp_path)
    assert session.presence_mode() == "apart"
    session.on_message("hey")
    contents = [m["content"] for m in client.calls[-1]["messages"]]
    assert TURN_STYLE in contents
    assert TURN_STYLE_TOGETHER not in contents
    store.close()


def test_here_swaps_the_register_from_the_next_message(tmp_path):
    session, client, store = lily_session(tmp_path, responses=["one.", "two."])
    session.on_message("hey")
    reply = session.set_presence("together")
    assert "here" in reply
    assert session.presence_mode() == "together"
    stored = _stored(store)
    assert any(c.startswith(PRESENCE_START_OPENER) for c in stored)
    session.clock.advance_hours(0.05)
    session.on_message("I'm back")
    contents = [m["content"] for m in client.calls[-1]["messages"]]
    # The together row sits after the day's texting row: newest register wins.
    apart_at = contents.index(TURN_STYLE)
    together_at = next(
        i for i, c in enumerate(contents) if c.startswith(PRESENCE_START_OPENER)
    )
    assert apart_at < together_at
    store.close()


def test_leave_returns_to_texting(tmp_path):
    session, client, store = lily_session(tmp_path, responses=["one.", "two."])
    session.on_message("hey")
    session.set_presence("together")
    session.clock.advance_hours(0.05)
    reply = session.set_presence("apart")
    assert "texting" in reply
    assert session.presence_mode() == "apart"
    stored = _stored(store)
    assert any(c.startswith(PRESENCE_END_OPENER) for c in stored)
    session.clock.advance_hours(0.05)
    session.on_message("you around?")
    contents = [m["content"] for m in client.calls[-1]["messages"]]
    end_at = next(
        i for i, c in enumerate(contents) if c.startswith(PRESENCE_END_OPENER)
    )
    assert end_at > 0
    store.close()


def test_a_flip_to_the_same_mode_changes_nothing(tmp_path):
    session, _client, store = lily_session(tmp_path)
    session.on_message("hey")
    session.set_presence("together")
    before = len(_stored(store))
    reply = session.set_presence("together")
    assert "already" in reply
    assert len(_stored(store)) == before
    store.close()


def test_a_day_started_together_opens_with_the_together_register(tmp_path):
    session, _client, store = lily_session(tmp_path)
    session.set_presence("together")
    session._emit_turn_style_row(session.clock.day(), session.clock.now_h())
    stored = _stored(store)
    assert TURN_STYLE_TOGETHER in stored
    assert TURN_STYLE not in stored
    store.close()


def test_the_here_and_leave_commands_flow_through_the_hook(tmp_path):
    session, _client, store = lily_session(tmp_path)
    ctx = CommandContext(store=store, clock=session.clock,
                         request_presence=session.set_presence)
    reply = handle_command(ControlCommand(name="here", args="", sender_id=1), ctx)
    assert session.presence_mode() == "together"
    assert reply
    reply = handle_command(ControlCommand(name="leave", args="", sender_id=1), ctx)
    assert session.presence_mode() == "apart"
    assert reply
    store.close()


def test_the_commands_without_a_hook_say_so(tmp_path):
    session, _client, store = lily_session(tmp_path)
    ctx = CommandContext(store=store, clock=session.clock)
    reply = handle_command(ControlCommand(name="here", args="", sender_id=1), ctx)
    assert "not available" in reply
    assert session.presence_mode() == "apart"
    store.close()


def test_the_flip_rows_are_labelled_presence_on_the_page():
    start = PRESENCE_START_OPENER + " at 20:31: he came over."
    end = PRESENCE_END_OPENER + " at 21:02: he left."
    assert system_row_label(start) == "presence"
    assert system_row_label(end) == "presence"


def test_the_stable_system_carries_the_fiction_frame():
    system = stable_system("CORE TEXT.")
    assert FICTION_FRAME in system
    assert (system.index("CORE TEXT.") < system.index(FICTION_FRAME)
            < system.index(SYSTEM_CORE_WITH_TOOLS))


def test_the_fiction_frame_and_registers_never_name_the_machinery():
    """Owner, 2026-10-03: an "never mention being an AI" line just plants the
    idea. The frame and both registers stay scene-native: no AI words at all.
    """
    for text in (FICTION_FRAME, TURN_STYLE_TOGETHER, TURN_STYLE):
        assert not re.search(
            r"\b(ai|assistant|model|chatbot|program|llm)\b", text.lower()
        ), text


def test_every_setup_button_maps_to_a_real_command():
    """The Setup page is the owner's command surface: each button's data-cmd
    must be a real command, and the presence pair must be offered there."""
    from pathlib import Path

    from harness.commands import _COMMANDS
    page = (Path(__file__).resolve().parents[1] / "observability" / "static"
            / "setup.html").read_text(encoding="utf-8")
    names = set(re.findall(r'data-cmd="([a-z_]+)"', page))
    assert {"here", "leave"} <= names
    assert names <= set(_COMMANDS)
