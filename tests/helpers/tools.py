"""Tools-layer test helpers (consolidated from test_tools + test_negotiation_schema)."""

from __future__ import annotations

import json

from harness.store import SQLiteStore
from harness.tools import RawReply


def store(tmp_path):
    return SQLiteStore(tmp_path / "decisions.db", audit_mode=True)


def native_call(name, args: dict):
    return RawReply(
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]
    )
