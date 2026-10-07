"""Decision/tools-layer test helpers (consolidated from test_tools,
test_negotiation_schema and test_proactive_decision, which carried
byte-identical copies)."""

from __future__ import annotations

import json

from harness.store import SQLiteStore
from harness.tools import RawReply


def decision_store(tmp_path):
    """An audit-mode SQLiteStore for decision persistence tests."""
    return SQLiteStore(tmp_path / "decisions.db", audit_mode=True)


def call_from(responses):
    """Injected callable factory: returns queued RawReply objects and records
    every request on ``.calls``."""
    queue = list(responses)
    calls = []

    def _call(request):
        calls.append(request)
        return queue.pop(0)

    _call.calls = calls  # type: ignore[attr-defined]
    return _call


def native_call(name, args: dict):
    """A RawReply carrying one native tool call ``name(args)``."""
    return RawReply(
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]
    )
