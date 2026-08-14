"""Audit export tests (WS1): per-call context reconstruction from the store
with typed headers, honest errors on hash-only rows (invariant 19), and the
conversation-level markdown export. Prompt persistence is VERIFIED here, not
rebuilt: the round-trip itself is covered by tests/test_store_it3.py
(repro_json alone reconstructs the call byte-exact); these tests render it.
"""

import pytest

from harness.audit import (
    main,
    render_call_extract,
    render_conversation_export,
    render_conversations_export,
)
from harness.prompts import (
    HEADER_REPLY,
    HEADER_SYSTEM,
    HEADER_THINKING,
    HEADER_TOOL,
    HEADER_TOOL_CALL,
    HEADER_USER,
)
from harness.store import SQLiteStore

SYSTEM = "STABLE CORE\n\nCORE TEXT.\n\nCurrent activity: practice pottery"
MESSAGES = [
    {"role": "user", "content": "hello there"},
    {"role": "tool", "name": "tool_decide_event", "content": '{"initiate": true}'},
]
RESPONSE = "hi! i am glazing the bowl."


def _repro(**overrides) -> dict:
    return {
        "model": "fake-model",
        "system": SYSTEM,
        "messages": MESSAGES,
        "max_tokens": 600,
        "temperature": 0.8,
        "json_mode": False,
        **overrides,
    }


def _log_call(store: SQLiteStore, *, repro: dict | None, reasoning: str | None = None,
              t_h: float = 12.5) -> int:
    meta = {"reasoning": reasoning} if reasoning else None
    return store.log_llm_call(0, t_h, "chat", SYSTEM + "\n" + repr(MESSAGES),
                              RESPONSE, "fake-model", meta=meta, repro=repro)


def test_call_extract_round_trip_with_typed_headers(tmp_path):
    store = SQLiteStore(tmp_path / "audit.db", audit_mode=True)
    call_id = _log_call(store, repro=_repro())
    text = render_call_extract(store, call_id)
    # the exact payload the model saw is rendered back
    assert HEADER_SYSTEM in text
    assert SYSTEM in text
    assert HEADER_USER in text
    assert "hello there" in text
    assert HEADER_TOOL_CALL.format(tool="tool_decide_event") in text
    assert HEADER_TOOL in text
    assert HEADER_REPLY in text
    assert RESPONSE in text
    # typed headers appear in reading order
    assert text.index(HEADER_SYSTEM) < text.index(HEADER_USER)
    assert text.index(HEADER_USER) < text.index(HEADER_TOOL_CALL.format(tool="tool_decide_event"))
    assert text.index(HEADER_TOOL_CALL.format(tool="tool_decide_event")) < text.index(HEADER_REPLY)
    store.close()


def test_call_extract_renders_thinking_when_present(tmp_path):
    store = SQLiteStore(tmp_path / "audit.db", audit_mode=True)
    call_id = _log_call(store, repro=_repro(), reasoning="she seems tired today")
    text = render_call_extract(store, call_id)
    assert HEADER_THINKING in text
    assert "she seems tired today" in text
    assert text.index(HEADER_THINKING) < text.index(HEADER_REPLY)
    store.close()


def test_call_extract_no_thinking_without_reasoning(tmp_path):
    store = SQLiteStore(tmp_path / "audit.db", audit_mode=True)
    call_id = _log_call(store, repro=_repro())
    assert HEADER_THINKING not in render_call_extract(store, call_id)
    store.close()


def test_call_extract_hash_only_row_raises_clear_error(tmp_path):
    """Non-eval rows persist hash only: reconstructing must raise a clear
    error, never fake coverage (invariant 19)."""
    store = SQLiteStore(tmp_path / "audit.db")  # audit_mode=False
    call_id = _log_call(store, repro=None)
    with pytest.raises(ValueError, match="hash-only"):
        render_call_extract(store, call_id)
    store.close()


def test_call_extract_unknown_call_raises(tmp_path):
    store = SQLiteStore(tmp_path / "audit.db", audit_mode=True)
    with pytest.raises(KeyError):
        render_call_extract(store, 999)
    store.close()


def _seed_conversation(store: SQLiteStore, conv_id: str = "conv-0",
                       *, audit: bool = True) -> None:
    """A user turn + a companion turn with a matching llm_call row."""
    store.open_conversation(conv_id, 10.0, "user")
    mid = store.add_message("user", "hello there", 10.0, 0,
                            conversation_id=conv_id)
    store.add_conversation_turn(conv_id, "user", "hello there", 10.0, 0,
                                message_id=mid)
    mid2 = store.add_message("assistant", RESPONSE, 10.1, 0,
                             conversation_id=conv_id)
    store.add_conversation_turn(conv_id, "companion", RESPONSE, 10.1, 1,
                                message_id=mid2)
    store.log_llm_call(0, 10.1, "chat", SYSTEM + "\n" + repr(MESSAGES),
                       RESPONSE, "fake-model",
                       repro=_repro() if audit else None,
                       meta={"reasoning": "glazing takes patience"} if audit else None)


def test_conversation_export_full_transcript_with_payloads(tmp_path):
    store = SQLiteStore(tmp_path / "audit.db", audit_mode=True)
    _seed_conversation(store)
    text = render_conversation_export(store, "conv-0")
    assert "# Conversation conv-0" in text
    # user turn
    assert HEADER_USER in text
    assert "hello there" in text
    # the companion turn carries the system prompt + thinking + reply
    assert HEADER_SYSTEM in text
    assert SYSTEM in text
    assert HEADER_THINKING in text
    assert "glazing takes patience" in text
    assert HEADER_REPLY in text
    assert RESPONSE in text
    # reading order: system before thinking before reply
    assert text.index(HEADER_SYSTEM) < text.index(HEADER_THINKING)
    assert text.index(HEADER_THINKING) < text.index(HEADER_REPLY)
    store.close()


def test_conversation_export_hash_only_turn_reported_honestly(tmp_path):
    store = SQLiteStore(tmp_path / "audit.db", audit_mode=False)
    _seed_conversation(store, audit=False)
    text = render_conversation_export(store, "conv-0")
    assert RESPONSE in text
    assert "hash-only" in text
    assert "no persisted payload" in text
    # no faked system prompt
    assert SYSTEM not in text
    store.close()


def test_conversations_export_multiple_in_one_document(tmp_path):
    store = SQLiteStore(tmp_path / "audit.db", audit_mode=True)
    _seed_conversation(store, "conv-0")
    _seed_conversation(store, "conv-1")
    text = render_conversations_export(store, ["conv-0", "conv-1"])
    assert text.count("# Conversation") == 2
    assert "# Conversation conv-0" in text
    assert "# Conversation conv-1" in text
    store.close()


def test_conversation_export_unknown_id_raises(tmp_path):
    store = SQLiteStore(tmp_path / "audit.db", audit_mode=True)
    with pytest.raises(KeyError):
        render_conversation_export(store, "nope")
    store.close()


def test_cli_call_and_conversation(tmp_path, capsys):
    store = SQLiteStore(tmp_path / "audit.db", audit_mode=True)
    call_id = _log_call(store, repro=_repro())
    store.close()

    assert main(["--store", str(tmp_path / "audit.db"), "--call", str(call_id)]) == 0
    out = capsys.readouterr().out
    assert HEADER_SYSTEM in out and RESPONSE in out

    out_file = tmp_path / "conv.md"
    assert main(["--store", str(tmp_path / "audit.db"),
                 "--conversation", "missing-1", "--out", str(out_file)]) != 0
    assert main(["--store", str(tmp_path / "audit.db"),
                 "--call", str(call_id), "--out", str(out_file)]) == 0
    assert HEADER_SYSTEM in out_file.read_text(encoding="utf-8")
