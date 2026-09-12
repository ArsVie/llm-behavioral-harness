"""Observability read model, event fold and HTTP surface.

Every test drives the real modules against a real SQLite store written by the
repo's own ``SQLiteStore``, so the read model is pinned to the schema the bot
actually writes (not to a hand-built fixture that can drift).
"""

from __future__ import annotations

import json
from pathlib import Path
import http.client
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from harness.client import FakeClient, Usage
from harness.store import SQLiteStore
from harness.trace import CACHE_FLOOR
from observability import calls
from observability import context as context_mod
from observability import db
from observability import events as events_mod
from observability import reader
from observability import server as server_mod
from observability import tokens
from tests.helpers.store import make_store

_MESSAGES = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello there"},
    {"role": "user", "content": "did you read what I sent you earlier?"},
    {"role": "assistant", "content": "yes, twice over, and I made a note"},
    {"role": "tool", "content": "recorded at 17:42: replied."},
    {"role": "user", "content": "good, then keep going the way you were"},
    {"role": "assistant", "content": "already am, no need to hover"},
    {"role": "system", "content": "today's plan: read a little"},
]

ENVELOPE_A = {
    "model": "deepseek/deepseek-v4-flash",
    "system": "You are Lily.\n\nA state card rides at the end of this conversation.",
    "messages": _MESSAGES,
    "temperature": 0.8,
}


def _store_with_history(tmp_path, *, audit: bool = True):
    """A store holding two calls, messages, a steer, a decision and a check-worthy row."""
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(tmp_path / "runs" / "companion.db", audit_mode=audit)
    store.conn.execute("insert into kv_store (key, value) values (?, ?)",
                       ("anchor.epoch0_s", "1788910828.9"))
    store.conn.execute("insert into kv_store (key, value) values (?, ?)",
                       ("anchor.t_h0", "17.67"))
    store.conn.execute("insert into kv_store (key, value) values (?, ?)",
                       ("anchor.tz", "America/Chihuahua"))
    store.conn.commit()

    first = dict(ENVELOPE_A)
    second = json.loads(json.dumps(ENVELOPE_A))
    second["messages"] = first["messages"][:-1] + [{"role": "user", "content": "and now?"}]

    store.log_llm_call(
        1, 24.5, "chat", "prompt-one", "hi back", "deepseek/deepseek-v4-flash",
        {"reasoning": "thinking", "tool_calls": ["record_reply"]},
        repro=first if audit else None, lane="product",
        usage=Usage(prompt_tokens=1000, completion_tokens=40, total_tokens=1040,
                    cached_tokens=800, cache_miss_tokens=200),
    )
    store.log_llm_call(
        1, 24.6, "chat", "prompt-two", "yes", "deepseek/deepseek-v4-flash",
        None, repro=second if audit else None, lane="product",
        usage={"prompt_tokens": 1200, "completion_tokens": 10, "total_tokens": 1210,
               "cached_tokens": 10, "cache_miss_tokens": 1190},
    )
    store.add_message("user", "hi", 24.5, 1)
    store.add_message("assistant", "hi back", 24.5, 1)
    store.conn.execute("insert into state_events (day, t_h, event, detail) values (?,?,?,?)",
                       (1, 24.5, "decision_parse_failed", '{"decision_id": "x"}'))
    store.conn.execute("insert into state_events (day, t_h, event, detail) values (?,?,?,?)",
                       (1, 24.6, "assistant_reply", "len=7"))
    store.conn.execute(
        "insert into decision_records (day, t_h, popup_kind, inputs_json, raw_reply, "
        "verdict_json, source, transport, budget_consumed) values (?,?,?,?,?,?,?,?,?)",
        (1, 24.5, "tool_decide_event", '{"hook": "h"}', '{"action": "skip"}',
         '{"action": "skip"}', "model", "native", 0),
    )
    store.enqueue_steer(1, 24.6, "popup", {"kind": "popup"})
    store.conn.execute(
        "insert into proactive_intents (id, reason, source_type, source_id, hook, "
        "created_t_h, valid_until_t_h, salience, evidence, status) "
        "values (?,?,?,?,?,?,?,?,?,?)",
        ("pi_1", "agenda", "agenda_item", "ag_1", "the villanelle is due", 30.0, 40.0, 0.7,
         "{}", "active"),
    )
    store.conn.execute(
        "insert into judgements (day, score, justification, model, shadow) values (?,?,?,?,?)",
        (1, 0.42, "fine", "judge", 1),
    )
    store.conn.commit()
    return store


# --------------------------------------------------------------------- tokens
def test_estimate_text_and_message_prices_scale_with_content():
    assert tokens.estimate_text("") == 0
    assert tokens.estimate_text(None) == 0
    assert tokens.estimate_text("abcd") == 2
    plain = tokens.estimate_message({"role": "user", "content": "abcd"})
    assert plain == tokens.estimate_text("abcd") + tokens.MESSAGE_OVERHEAD_TOKENS
    with_calls = tokens.estimate_message(
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]})
    assert with_calls > tokens.MESSAGE_OVERHEAD_TOKENS
    blocks = tokens.estimate_message({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    assert blocks >= tokens.MESSAGE_OVERHEAD_TOKENS


def test_estimate_tools_names_each_schema_and_prices_it():
    priced = tokens.estimate_tools([{"function": {"name": "record_reply"}}, {"name": "bare"}])
    assert [entry["name"] for entry in priced] == ["record_reply", "bare"]
    assert all(entry["tokens"] > tokens.TOOL_OVERHEAD_TOKENS for entry in priced)
    assert tokens.estimate_tools(None) == []


def test_reconcile_scales_to_the_provider_total_both_ways():
    anchored = tokens.reconcile(1000, 1250)
    assert (anchored.kind, anchored.scale) == ("usage", 1.25)
    assert tokens.price([400, 600], anchored) == [500, 750]
    shrunk = tokens.reconcile(1000, 900)
    assert (shrunk.kind, shrunk.scale) == ("estimated", 0.9)
    assert tokens.price([400, 600], shrunk) == [360, 540]
    assert tokens.reconcile(1000, None).kind == "none"
    assert tokens.reconcile(1000, None).scale == 1.0
    assert tokens.reconcile(0, 900).kind == "none"


def test_anchor_json_rounds_the_scale():
    payload = tokens.reconcile(3, 4).to_json()
    assert payload == {"kind": "usage", "provider_tokens": 4, "heuristic_tokens": 3,
                       "scale": 1.3333}


# --------------------------------------------------------------------- events
def test_seq_of_is_monotonic_in_time_and_stable_for_one_row():
    first = events_mod.seq_of(10.0, "message", 1)
    assert first < events_mod.seq_of(10.0, "call", 1)
    assert events_mod.seq_of(10.0, "call", 1) < events_mod.seq_of(10.000001, "message", 1)
    assert events_mod.seq_of(0.0, "unknown-lane", 1) > events_mod.seq_of(0.0, "message", 1)


def test_severity_reads_the_row_wording():
    assert events_mod._severity("decision_parse_failed", "") == "error"
    assert events_mod._severity("decision_catchup_omit", "") == "warn"
    assert events_mod._severity("assistant_reply", "len=7") == "info"


def test_loads_is_tolerant_and_clip_shortens():
    assert events_mod._loads(None) is None
    assert events_mod._loads("") is None
    assert events_mod._loads("not json") == "not json"
    assert events_mod._loads('{"a": 1}') == {"a": 1}
    assert events_mod._clip("x" * 30, 10).endswith("…")
    assert events_mod._clip("line\nbreak") == "line ⏎ break"


def test_event_builders_cover_every_table(tmp_path):
    store = _store_with_history(tmp_path)
    ref = reader.find_runs(tmp_path)[0]
    with reader.open_run(ref.path) as conn:
        anchor = reader.load_anchor(conn)
        collected = events_mod.collect_events(conn, anchor)
    kinds = {event.kind for event in collected}
    assert {"message", "state", "decision", "steer", "proactive", "call", "judgement"} <= kinds
    assert [event.seq for event in collected] == sorted(event.seq for event in collected)
    assert all(event.real is not None for event in collected)
    assert any(event.severity == "error" for event in collected)


def test_proactive_and_conversation_events_without_anchor_have_no_real_clock(tmp_path):
    store = _store_with_history(tmp_path)
    ref = reader.find_runs(tmp_path)[0]
    with reader.open_run(ref.path) as conn:
        rows = db.rows(conn, "select * from proactive_intents")
    event = events_mod.from_proactive(None, rows)[0]
    assert event.real is None
    day_rows = events_mod.from_judgements(None, [{"day": 3, "score": 0.1, "shadow": 0}])
    assert day_rows[0].day == 3
    assert "live" in day_rows[0].label


def test_conversation_events_render_open_and_close(tmp_path):
    store = _store_with_history(tmp_path)
    rows = [{"id": "c1", "opened_t_h": 10.0, "closed_t_h": 12.0, "opened_by": "user",
             "close_reason": "user_left"}]
    event = events_mod.from_conversations(None, rows)[0]
    assert "opened by user" in event.detail and "user_left" in event.detail


def test_state_events_render_their_detail_as_raw():
    rows = [{"id": 1, "day": 1, "t_h": 2.0, "event": "assistant_reply", "detail": '{"len": 3}'}]
    event = events_mod.from_state_events(None, rows)[0]
    assert event.raw == {"raw": {"len": 3}}


def test_a_broken_anchor_yields_no_local_clock():
    class Broken:
        def real(self, t_h):
            raise ValueError("boom")

    rows = [{"id": 1, "day": 1, "t_h": 2.0, "role": "user", "content": "hi"}]
    assert events_mod.from_messages(Broken(), rows)[0].real is None


def test_schedule_events_show_the_fire_time_and_the_cause():
    rows = [{"id": 9, "seed": 8001, "t_h": 30.0, "day": 1, "reason": "agenda",
             "status": "fired", "fired_t_h": 30.25, "caused_by": "agenda_item"}]
    event = events_mod.from_schedule(None, rows)[0]
    assert "fired" in event.detail and "30.25" in event.detail
    assert "agenda_item" in event.detail
    assert event.raw["caused_by"] == "agenda_item"


def test_schedule_events_without_a_fire_time_stay_plain():
    rows = [{"id": 10, "seed": 8001, "t_h": 31.0, "day": 1, "reason": "callback",
             "status": "expired", "fired_t_h": None}]
    event = events_mod.from_schedule(None, rows)[0]
    assert "at t_h" not in event.detail
    assert event.severity == "warn"


def test_steering_rows_show_status_and_payload():
    rows = [{"id": 3, "day": 1, "t_h": 5.0, "kind": "popup", "status": "delivered",
             "payload_json": '{"kind": "popup"}'}]
    event = events_mod.from_steering(None, rows)[0]
    assert "delivered" in event.label
    assert event.raw["payload"] == {"kind": "popup"}


# -------------------------------------------------------------------- context
def test_system_parts_split_blocks_and_classify_them():
    parts = context_mod.system_parts(
        "You are Lily.\n\nA state card rides at the end.\n\nSomething else entirely.")
    assert [part["kind"] for part in parts] == ["persona & voice", "state card", "prompt"]
    assert all(part["tokens"] > 0 for part in parts)
    assert context_mod.system_parts("") == []


def test_system_parts_classify_every_marker():
    parts = context_mod.system_parts(
        "You are Lily.\n\nWhen a decision tool is attached, fill in what it asks."
        "\n\nTuesday's plan: read a little")
    assert [part["kind"] for part in parts] == [
        "persona & voice", "decision tools", "internal card"]


def test_seq_of_keeps_siblings_distinct_and_time_ordered():
    """Two rows at the same instant (chat leg + decide legs) must not share a cursor."""
    first = events_mod.seq_of(109.576, "call", 21)
    second = events_mod.seq_of(109.576, "call", 22)
    later = events_mod.seq_of(109.577, "message", 1)
    assert first != second
    assert second < later
    assert events_mod.seq_of(109.576, "call", 21) == first  # stable across polls


def test_shared_message_count_ignores_the_previous_trailing_card():
    previous = {"system": "s", "messages": [{"role": "user", "content": "a"},
                                           {"role": "system", "content": "card"}]}
    current = {"system": "s", "messages": [{"role": "user", "content": "a"},
                                           {"role": "user", "content": "b"}]}
    shared, chars, stable = context_mod.shared_message_count(previous, current)
    assert (shared, stable) == (1, True)
    assert chars > 0
    drifted = dict(current, system="t")
    assert context_mod.shared_message_count(previous, drifted)[2] is False
    assert context_mod.shared_message_count(None, current) == (0, 0, False)


def test_shared_message_count_stops_when_the_current_request_is_shorter():
    previous = {"system": "s", "messages": [{"role": "user", "content": "a"},
                                           {"role": "assistant", "content": "b"},
                                           {"role": "system", "content": "card"}]}
    current = {"system": "s", "messages": [{"role": "tool", "content": "x"}]}
    assert context_mod.shared_message_count(previous, current) == (0, 0, True)
    assert context_mod.shared_message_count({"system": "s"}, {"system": "s"}) == (0, 0, True)


def test_message_nodes_mark_the_shared_prefix_and_tool_calls():
    envelope = {"messages": [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "x"}}]},
        {"role": "system", "content": "card"},
        {"role": "tool", "content": "result"},
    ]}
    nodes = context_mod.message_nodes(envelope, 1)
    assert [node["cached"] for node in nodes] == [True, False, False, False]
    assert nodes[1]["label"] == "tool call" and nodes[1]["tool_calls"] == ["x"]
    assert nodes[2]["label"] == "internal card" and nodes[3]["label"] == "tool result"


def test_build_context_without_envelope_reports_unavailable():
    payload = context_mod.build_context(envelope=None, tools=None, tools_source="none",
                                        previous=None, context_window=1000, usage={})
    assert payload["available"] is False
    assert "invariant 19" in payload["reason"]


def test_build_context_anchors_on_the_provider_and_prices_every_bucket():
    payload = context_mod.build_context(
        envelope=ENVELOPE_A,
        tools=[{"function": {"name": "record_reply"}}],
        tools_source="code",
        previous=None,
        context_window=1000,
        usage={"prompt_tokens": 900, "cached_tokens": 600, "cache_miss_tokens": 300,
               "completion_tokens": 30},
    )
    assert payload["available"] is True
    assert payload["anchor"]["kind"] == "usage"
    keys = [bucket["key"] for bucket in payload["buckets"]]
    assert keys == ["system", "tools", "messages"]
    messages = payload["buckets"][2]
    # The provider anchor covers system + messages; the tool schema stays out of it.
    assert messages["priced_tokens"] > messages["tokens"]
    assert payload["buckets"][1]["priced_tokens"] == payload["buckets"][1]["tokens"]
    assert payload["pressure"]["cache_hit_pct"] == 66.7
    assert payload["pressure"]["saturation_pct"] == 90.0
    assert payload["pressure"]["ledger_ok"] is True
    assert payload["pressure"]["fresh_tokens"] == 300
    assert sum(node["priced_tokens"] for node in payload["surface"]) == messages["priced_tokens"]
    assert all("priced_tokens" in part for part in payload["system_parts"])


def test_build_context_derives_the_fresh_share_when_the_ledger_does_not_sum():
    payload = context_mod.build_context(
        envelope=ENVELOPE_A, tools=[], tools_source="none", previous=None,
        context_window=1000,
        usage={"prompt_tokens": 900, "cached_tokens": 600, "cache_miss_tokens": 0,
               "completion_tokens": 10},
    )
    assert payload["pressure"]["ledger_ok"] is False
    assert payload["pressure"]["fresh_tokens"] == 300
    assert payload["pressure"]["cache_miss_tokens"] == 0


def test_build_context_marks_fresh_prices_without_a_provider_anchor():
    payload = context_mod.build_context(
        envelope=ENVELOPE_A, tools=[], tools_source="none", previous=None,
        context_window=0, usage={})
    assert payload["anchor"]["kind"] == "none"
    assert payload["buckets"][0]["share_pct"] is None
    assert payload["pressure"]["cache_hit_pct"] is None
    assert payload["pressure"]["saturation_pct"] is None


# --------------------------------------------------------------------- reader
def test_find_runs_skips_dependency_dirs_and_orders_by_mtime(tmp_path):
    store = _store_with_history(tmp_path)
    hidden = tmp_path / ".venv" / "x.db"
    hidden.parent.mkdir(parents=True)
    hidden.write_bytes(b"")
    runs = reader.find_runs(tmp_path)
    assert [ref.label for ref in runs] == ["runs/companion"]
    assert str(runs[0].path) == store.path


def test_run_ref_liveness_from_db_and_wal_mtimes(tmp_path):
    ref = reader.RunRef(path=tmp_path / "a.db", mtime=100.0, wal_mtime=110.0)
    assert ref.idle_seconds(115.0) == 5.0
    assert ref.is_active(115.0) is True
    assert ref.is_active(10_000.0) is False
    plain = reader.RunRef(path=tmp_path / "b.db", mtime=200.0, wal_mtime=None)
    assert plain.idle_seconds(190.0) == 0.0


def test_open_run_is_read_only(tmp_path):
    store = _store_with_history(tmp_path)
    with reader.open_run(store.path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("delete from messages")


def test_rows_and_counts_tolerate_a_missing_table(tmp_path):
    store = _store_with_history(tmp_path)
    with reader.open_run(store.path) as conn:
        assert db.rows(conn, "select * from no_such_table") == []
        assert db.count(conn, "no_such_table") == 0
        assert reader.counters(conn)["messages"] == 2


def test_usage_payload_matches_the_spend_formula(tmp_path):
    store = _store_with_history(tmp_path)
    with reader.open_run(store.path) as conn:
        payload = reader.usage_payload(reader.call_rows(conn))
    totals = payload["totals"]
    assert totals["calls"] == 2
    assert totals["prompt_tokens"] == 2200
    assert totals["cached_tokens"] == 810
    assert totals["cache_hit_pct"] == pytest.approx(810 / 2200 * 100, abs=0.1)
    assert totals["prompt_hit_pct"] == pytest.approx(810 / 2200 * 100, abs=0.1)
    assert totals["ledger_mismatch_calls"] == 0
    assert totals["cost_usd"] > 0
    assert [lane["key"] for lane in payload["by_lane"]] == ["product"]
    assert {role["key"] for role in payload["by_role"]} == {"chat"}
    assert totals["savings_usd"] > 0
    assert reader.usage_payload([])["totals"]["calls"] == 0
    assert reader.usage_payload([])["by_lane"] == []


def test_usage_payload_flags_legacy_rows_whose_split_does_not_sum(tmp_path):
    store = _store_with_history(tmp_path)
    conn = _writable(store.path)
    try:
        conn.execute("update llm_calls set cache_miss_tokens = 0 where id = 2")
        conn.commit()
    finally:
        conn.close()
    with reader.open_run(store.path) as conn:
        totals = reader.usage_payload(reader.call_rows(conn))["totals"]
    assert totals["ledger_mismatch_calls"] == 1
    # cached / (cached + miss) overstates the hit share on that row; cached /
    # prompt does not — and only the flagged row changes.
    assert totals["cache_hit_pct"] == pytest.approx(810 / 1010 * 100, abs=0.1)
    assert totals["prompt_hit_pct"] == pytest.approx(810 / 2200 * 100, abs=0.1)
    assert totals["cache_hit_pct"] > totals["prompt_hit_pct"]


def test_usage_payload_ignores_rows_without_a_prompt_total(tmp_path):
    store = _store_with_history(tmp_path)
    conn = _writable(store.path)
    try:
        conn.execute("update llm_calls set prompt_tokens = NULL where id = 2")
        conn.commit()
    finally:
        conn.close()
    with reader.open_run(store.path) as conn:
        totals = reader.usage_payload(reader.call_rows(conn))["totals"]
    assert totals["ledger_mismatch_calls"] == 0


def test_checks_payload_reports_a_crashing_check_instead_of_raising(tmp_path, monkeypatch):
    store = _store_with_history(tmp_path)
    with reader.open_run(store.path) as conn:
        def boom(_ctx):
            raise IndexError("No item with that key")

        monkeypatch.setattr(reader, "collect_findings", boom)
        payload = reader.checks_payload(conn, reader.load_anchor(conn))
    assert payload["counts"]["error"] == 1
    assert payload["findings"][0]["code"] == "checks-crashed"
    assert "IndexError" in payload["findings"][0]["message"]


def test_clock_payload_reads_the_anchor_and_local_now(tmp_path):
    store = _store_with_history(tmp_path)
    with reader.open_run(store.path) as conn:
        payload = reader.clock_payload(conn, reader.load_anchor(conn))
        assert payload["anchor"]["tz"] == "America/Chihuahua"
        assert payload["real_now"].startswith("2026-")
        unanchored = reader.clock_payload(conn, None)
    assert unanchored["anchor"] is None
    assert unanchored["real_now"] is None
    assert unanchored["tz"] is None
    assert unanchored["local_now"]


def test_call_rows_measure_the_proven_prefix_and_flag_a_warm_miss(tmp_path):
    store = _store_with_history(tmp_path)
    with reader.open_run(store.path) as conn:
        rows = reader.call_rows(conn)
    first, second = rows
    assert first["prefix_share_pct"] is None
    assert second["prefix_share_pct"] >= CACHE_FLOOR * 100
    assert second["system_stable"] is True
    assert second["verdict"] == "prefix shared but not cached"
    payload = reader.call_payload(second, None)
    assert payload["cache_hit_pct"] == 0.8
    assert payload["tool_calls"] == []
    assert payload["has_envelope"] is True
    assert payload["reasoning_chars"] == 0
    assert payload["real"] is None


def _writable(path: str) -> sqlite3.Connection:
    """A write connection for test setup (the read model itself stays ro)."""
    return sqlite3.connect(path)


def test_call_rows_flag_a_changed_system_prefix(tmp_path):
    store = _store_with_history(tmp_path)
    drifted = json.loads(json.dumps(ENVELOPE_A))
    drifted["system"] = "different"
    drifted["messages"] = drifted["messages"][:-1] + [{"role": "user", "content": "z"}]
    conn = _writable(store.path)
    try:
        conn.execute("update llm_calls set repro_json = ? where id = 2",
                     (json.dumps(drifted),))
        conn.commit()
    finally:
        conn.close()
    with reader.open_run(store.path) as conn:
        rows = reader.call_rows(conn)
    assert rows[1]["system_stable"] is False
    assert rows[1]["verdict"] == "stable prefix CHANGED"


def test_call_rows_without_an_envelope_say_so(tmp_path):
    store = _store_with_history(tmp_path, audit=False)
    with reader.open_run(store.path) as conn:
        rows = reader.call_rows(conn)
    assert rows[0]["verdict"] == "no persisted envelope"
    assert reader.call_payload(rows[0], None)["has_envelope"] is False


def test_call_rows_limit_keeps_the_newest_but_the_order_oldest_first(tmp_path):
    store = _store_with_history(tmp_path)
    with reader.open_run(store.path) as conn:
        rows = reader.call_rows(conn, limit=1)
    assert [row["id"] for row in rows] == [2]
    assert rows[0]["prefix_share_pct"] is None


def test_call_payload_reads_a_json_meta_blob(tmp_path):
    store = _store_with_history(tmp_path)
    conn = _writable(store.path)
    try:
        conn.execute("update llm_calls set meta = ? where id = 1",
                     ('{"reasoning": "abc", "tool_calls": ["record_reply"]}',))
        conn.commit()
    finally:
        conn.close()
    with reader.open_run(store.path) as conn:
        rows = db.rows(conn, "select * from llm_calls order by id")
    assert reader.call_payload(rows[0], None)["reasoning_chars"] == 3
    assert reader.call_payload(rows[0], None)["tool_calls"] == ["record_reply"]
    assert reader.call_payload(dict(rows[0], meta="{not json"), None)["reasoning_chars"] == 0


def test_tool_schemas_for_every_role(tmp_path):
    tools, source = reader.tool_schemas_for("chat")
    # The chat leg sends none AND says where schemas do go: an empty panel on
    # a chat call must not read as "this harness never sends schemas".
    assert tools == []
    assert "none on this call" in source and "prose by design" in source
    assert "decide legs carry the schema" in source
    tools, source = reader.tool_schemas_for("tool_decide_event")
    assert tools and "harness.tools" in source
    assert "OpenAI shape" in source and "tool_choice=auto" in source
    assert all(tool["name"] == "tool_decide_event" for tool in tools)
    tools, _ = reader.tool_schemas_for("tool_decide_unknown")
    assert len(tools) > 1
    tools, source = reader.tool_schemas_for("day_plan")
    assert tools == [] and "unknown role" in source


def test_find_runs_ignores_a_dangling_symlink(tmp_path):
    (tmp_path / "ok").mkdir()
    good = tmp_path / "ok" / "real.db"
    good.write_bytes(b"")
    (tmp_path / "gone.db").symlink_to(tmp_path / "missing-target")
    runs = reader.find_runs(tmp_path)
    assert [ref.label for ref in runs] == ["ok/real"]
    assert runs[0].wal_mtime is None


def test_stamp_survives_a_broken_anchor():
    class Broken:
        def real(self, t_h):
            raise OverflowError("no")

    assert calls.stamp(Broken(), 1.0) is None
    assert calls.stamp(None, 1.0) is None
    assert calls.stamp(Broken(), None) is None


def test_envelope_rejects_json_that_is_not_an_object():
    assert calls.envelope_of({"repro_json": None}) is None
    assert calls.envelope_of({"repro_json": "[1, 2]"}) is None
    assert calls.envelope_of({"repro_json": "{not json"}) is None
    assert calls.envelope_of({"repro_json": '{"system": "s"}'}) == {"system": "s"}


def test_call_detail_tolerates_unparseable_meta(tmp_path):
    store = _store_with_history(tmp_path)
    conn = _writable(store.path)
    try:
        conn.execute("update llm_calls set meta = ? where id = 1", ("{broken",))
        conn.commit()
    finally:
        conn.close()
    with reader.open_run(store.path) as conn:
        detail = reader.call_detail(conn, None, 1)
    assert detail is not None
    assert detail["meta"] == {}
    assert detail["reasoning"] == ""


def test_stats_json_omits_the_hit_rate_without_input_tokens():
    from harness.spend import GroupStats

    assert reader.stats_json(GroupStats())["cache_hit_pct"] is None


def test_context_payload_reports_the_latest_call_and_its_provenance(tmp_path):
    store = _store_with_history(tmp_path)
    with reader.open_run(store.path) as conn:
        payload = reader.context_payload(conn, reader.load_anchor(conn), call_id=None,
                                        context_window=1_000_000)
    assert payload["available"] is True
    assert payload["call"]["id"] == 2
    assert payload["prefix"]["shared_messages"] == len(_MESSAGES) - 1
    assert payload["prefix"]["system_stable"] is True
    assert payload["tools_source"].startswith("none on this call")
    assert "prose by design" in payload["tools_source"]


def test_context_payload_without_any_call_is_explicit(tmp_path):
    (tmp_path / "empty").mkdir(parents=True, exist_ok=True)
    store = make_store(tmp_path, "empty/companion.db")
    with reader.open_run(store.path) as conn:
        payload = reader.context_payload(conn, None, call_id=5, context_window=10)
    assert payload == {"available": False, "reason": "no model call recorded yet",
                       "context_window": 10}


def test_call_detail_renders_the_envelope_and_reports_hash_only_rows(tmp_path):
    store = _store_with_history(tmp_path)
    with reader.open_run(store.path) as conn:
        detail = reader.call_detail(conn, reader.load_anchor(conn), 1)
        assert detail is not None
        assert detail["envelope"]["system"].startswith("You are Lily")
        assert detail["reasoning"] == "thinking"
        assert detail["response"] == "hi back"
        assert detail["meta"]["tool_calls"] == ["record_reply"]
        assert reader.call_detail(conn, None, 999) is None
    hash_only = _store_with_history(tmp_path / "plain", audit=False)
    with reader.open_run(hash_only.path) as conn:
        detail = reader.call_detail(conn, None, 1)
    assert detail is not None and detail["envelope_available"] is False
    assert detail["tools"] == []


def test_events_payload_filters_by_cursor_and_reports_the_newest(tmp_path):
    store = _store_with_history(tmp_path)
    with reader.open_run(store.path) as conn:
        anchor = reader.load_anchor(conn)
        everything = reader.events_payload(conn, anchor)
        assert everything["returned"] == len(everything["events"])
        newest = everything["latest_seq"]
        page = reader.events_payload(conn, anchor, after=everything["events"][0]["seq"])
        assert page["events"][-1]["seq"] == newest
        assert len(page["events"]) < len(everything["events"])
        empty = reader.events_payload(conn, anchor, after=newest)
        assert empty["events"] == [] and empty["latest_seq"] == newest
        truncated = reader.events_payload(conn, anchor, limit=2)
        assert truncated["truncated"] is True and len(truncated["events"]) == 2


def test_checks_payload_reports_the_inspector_verdicts(tmp_path):
    store = _store_with_history(tmp_path)
    # A model id the rate table does not know is a WARN (spend would be
    # derived from the fallback tier), so the warn bucket has a real row to
    # count now that a missing raw_cost alone is no longer a warning.
    store.conn.execute("update llm_calls set model = 'unknown-model' where id = 2")
    store.conn.commit()
    with reader.open_run(store.path) as conn:
        payload = reader.checks_payload(conn, reader.load_anchor(conn))
    assert payload["counts"]["warn"] >= 1
    assert any(finding["code"] == "cost-rates-unknown"
               for finding in payload["findings"])
    assert all(finding["severity"] in {"error", "warn", "info"} for finding in payload["findings"])


def test_latest_ids_and_run_summary(tmp_path):
    store = _store_with_history(tmp_path)
    ref = reader.find_runs(tmp_path)[0]
    with reader.open_run(store.path) as conn:
        latest = reader.latest_ids(conn)
    assert latest["call_id"] == 2 and latest["message_id"] == 2
    summary = reader.run_summary(ref, now=ref.mtime, context_window=555)
    assert summary["calls"] == 2
    assert summary["model"] == "deepseek/deepseek-v4-flash"
    assert summary["lane"] == "product"
    assert summary["context_window"] == 555
    assert summary["active"] is True
    assert summary["idle_s"] == 0.0
    assert summary["usage"]["calls"] == 2



def test_deployments_parse_launchers_and_match_only_their_databases(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    root = tmp_path / "repo"
    (root / "results" / "live-companion").mkdir(parents=True)
    (root / "results" / "profiles" / "smoke").mkdir(parents=True)
    (root / "results" / "decision-probe").mkdir(parents=True)
    telegram_db = root / "results" / "live-companion" / "companion.db"
    cli_db = root / "results" / "profiles" / "smoke" / "companion.db"
    probe_db = root / "results" / "decision-probe" / "decision_probe.db"
    for path in (telegram_db, cli_db, probe_db):
        path.write_bytes(b"")

    (scripts / "live_telegram.sh").write_text(
        "exec .venv/bin/python -m experiments.live_companion --channel telegram "
        "--db results/live-companion/companion.db --condition FULL\n")
    (scripts / "live_cli.sh").write_text(
        'DB="$REPO/results/profiles/$PROFILE/companion.db"\n'
        "exec .venv/bin/python -m experiments.live_companion --channel cli "
        '--db "$DB" --enable-commands\n')
    (scripts / "live_old.sh.bak").write_text("--db results/retired/companion.db\n")

    found = db.deployments(scripts_dir=scripts, root=root)
    assert sorted(item.channel for item in found) == ["cli", "telegram"]
    assert all(item.pattern.startswith(str(root)) for item in found)
    assert db.deployments(scripts_dir=tmp_path / "missing", root=root) == []
    (scripts / "live_nodb.sh").write_text("echo nothing to deploy\n")
    assert sorted(item.channel for item in db.deployments(scripts_dir=scripts, root=root)) \
        == ["cli", "telegram"]  # a launcher without --db is skipped
    blocked = scripts / "live_blocked.sh"
    blocked.write_text('--db "results/x/companion.db"\n')
    blocked.chmod(0o000)
    assert db.deployments(scripts_dir=scripts, root=root) is not None  # unreadable: skipped
    blocked.chmod(0o644)
    not_a_dir = tmp_path / "plain-file"
    not_a_dir.write_text("x")
    assert db.deployments(scripts_dir=not_a_dir, root=root) == []
    closed = tmp_path / "closed-dir"
    closed.mkdir()
    closed.chmod(0o000)
    assert db.deployments(scripts_dir=closed, root=root) == []  # unreadable dir
    closed.chmod(0o755)

    table = db.deployed_runs(scripts_dir=scripts, root=root)
    assert table[str(telegram_db.resolve())].channel == "telegram"
    assert table[str(cli_db.resolve())].channel == "cli"
    assert str(probe_db.resolve()) not in table
    assert db.deployment_for(probe_db, table) is None
    assert db.deployment_for(telegram_db, table) is not None


def test_runs_payload_hides_probe_runs_unless_asked(tmp_path, monkeypatch):
    _store_with_history(tmp_path)
    monkeypatch.setattr(reader, "deployed_runs", lambda root=None: {})
    monkeypatch.setattr(reader, "live_processes", lambda pattern="live_companion": {})
    hidden = reader.runs_payload(tmp_path, context_window=100)
    assert hidden["runs"] == []
    assert (hidden["deployed"], hidden["total"]) == (0, 1)
    full = reader.runs_payload(tmp_path, context_window=100, include_all=True)
    assert len(full["runs"]) == 1
    assert full["runs"][0]["deployed"] is None


def test_runs_payload_marks_a_deployed_run(tmp_path, monkeypatch):
    store = _store_with_history(tmp_path)
    deployment = db.Deployment(channel="telegram", script="live_telegram.sh", pattern="*")
    monkeypatch.setattr(
        reader, "deployed_runs",
        lambda root=None: {str(Path(store.path).resolve()): deployment})
    monkeypatch.setattr(reader, "live_processes", lambda pattern="live_companion": {})
    payload = reader.runs_payload(tmp_path, context_window=100)
    assert [run["deployed"] for run in payload["runs"]] == ["telegram"]
    assert payload["runs"][0]["deployed_by"] == "live_telegram.sh"

def test_live_processes_map_databases_to_pids(monkeypatch, tmp_path):
    class Completed:
        returncode = 0
        stdout = (
            "  PID ARGS\n"
            "  877 .venv/bin/python -m experiments.live_companion --channel telegram "
            "--db results/live-companion/companion.db --condition FULL\n"
            " 1537 /app/.venv/bin/python -m src.deriver\n"
            " 900 python -m experiments.live_companion --db /abs/other.db --seed 1\n"
            " 901 python -m experiments.live_companion --db\n"
        )

    monkeypatch.setattr(db.subprocess, "run", lambda *a, **k: Completed())
    found = reader.live_processes()
    relative = str((reader.repo_root() / "results/live-companion/companion.db").resolve())
    assert found[relative] == 877
    assert found["/abs/other.db"] == 900
    assert len(found) == 2


def test_live_processes_without_ps_is_empty(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no ps here")

    monkeypatch.setattr(db.subprocess, "run", boom)
    assert reader.live_processes() == {}


def test_run_summary_reports_process_liveness(tmp_path, monkeypatch):
    store = _store_with_history(tmp_path)
    ref = reader.find_runs(tmp_path)[0]
    monkeypatch.setattr(reader, "live_processes", lambda pattern="live_companion": {})
    summary = reader.run_summary(ref, now=ref.mtime, context_window=10)
    assert summary["process_up"] is False
    assert summary["pid"] is None
    monkeypatch.setattr(reader, "live_processes",
                        lambda pattern="live_companion": {str(ref.path.resolve()): 4242})
    assert reader.run_summary(ref, now=ref.mtime, context_window=10)["pid"] == 4242


def test_run_detail_assembles_every_panel(tmp_path):
    store = _store_with_history(tmp_path)
    ref = reader.find_runs(tmp_path)[0]
    detail = reader.run_detail(ref, context_window=1000)
    assert set(detail) == {"summary", "clock", "usage", "counters", "checks", "latest",
                           "calls", "events", "context"}
    assert detail["counters"]["messages"] == 2
    assert detail["context"]["call"]["id"] == 2
    trimmed = reader.run_detail(ref, context_window=1000, call_id=1, limit=1)
    assert trimmed["context"]["call"]["id"] == 1
    assert len(trimmed["calls"]) == 1


def test_run_detail_on_a_store_with_no_calls(tmp_path):
    (tmp_path / "fresh").mkdir(parents=True, exist_ok=True)
    store = make_store(tmp_path, "fresh/companion.db")
    ref = reader.find_runs(tmp_path)[0]
    detail = reader.run_detail(ref, context_window=1000)
    assert detail["summary"]["calls"] == 0
    assert detail["context"]["available"] is False
    assert detail["calls"] == []


def test_repo_root_points_at_the_checkout():
    assert (reader.repo_root() / "harness" / "store.py").is_file()


# --------------------------------------------------------------------- server
@pytest.fixture()
def live_server(tmp_path):
    _store_with_history(tmp_path)
    app = server_mod.serve(host="127.0.0.1", port=0, root=tmp_path, context_window=1234,
                           announce=False)
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{app.server_address[1]}"
    try:
        yield app, base, tmp_path
    finally:
        app.shutdown()
        app.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _json(url: str) -> dict:
    status, body = _get(url)
    assert status == 200, body
    return json.loads(body)


def test_server_serves_the_page_and_the_run_list(live_server):
    app, base, tmp_path = live_server
    status, body = _get(f"{base}/")
    assert status == 200 and b"Harness observability" in body
    status, css = _get(f"{base}/static/app.css")
    assert status == 200 and b"--lav" in css
    runs = _json(f"{base}/api/runs")
    assert runs["root"] == str(tmp_path)
    # A fixture database is nobody's deployment, so the default list hides it.
    assert runs["runs"] == [] and runs["total"] == 1 and runs["deployed"] == 0
    runs = _json(f"{base}/api/runs?all=1")
    assert runs["root"] == str(tmp_path)
    assert len(runs["runs"]) == 1
    assert runs["runs"][0]["label"] == "runs/companion"
    assert runs["runs"][0]["context_window"] == 1234
    assert runs["runs"][0]["active"] is True
    newest = app.state.resolve_run(None)
    assert newest is not None and str(newest.path) == runs["runs"][0]["path"]


def test_server_run_endpoints(live_server):
    app, base, tmp_path = live_server
    run_id = urllib.parse.quote(_json(f"{base}/api/runs?all=1")["runs"][0]["path"])
    quoted = urllib.parse.quote(run_id)
    detail = _json(f"{base}/api/run?id={quoted}")
    assert detail["summary"]["calls"] == 2
    events = _json(f"{base}/api/run/events?id={quoted}&limit=3")
    assert events["latest_seq"] > 0
    context = _json(f"{base}/api/run/context?id={quoted}&call=1")
    assert context["call"]["id"] == 1
    call = _json(f"{base}/api/run/call?id={quoted}&call=2")
    assert call["envelope"]["messages"][0]["content"] == "hi"
    assert _json(f"{base}/api/run?id={quoted}&limit=1")["summary"]["calls"] == 2


def test_server_rejects_unknown_endpoints_runs_and_paths(live_server):
    app, base, tmp_path = live_server
    status, _ = _get(f"{base}/api/nope")
    assert status == 404
    status, _ = _get(f"{base}/api/run?id={tmp_path}/runs/missing.db")
    assert status == 404
    status, _ = _get(f"{base}/api/run/call?id={urllib.parse.quote(str(tmp_path / 'runs' / 'companion.db'))}&call=99")
    assert status == 404
    status, _ = _get(f"{base}/static/../reader.py")
    assert status == 404
    status, _ = _get(f"{base}/nope")
    assert status == 404
    assert app.state.resolve_run(str(tmp_path / "nope.db")) is None


def test_server_params_survive_garbage(live_server):
    app, base, tmp_path = live_server
    runs = _json(f"{base}/api/runs")
    assert runs["runs"] == [] and runs["deployed"] == 0  # not deployed
    runs = _json(f"{base}/api/runs?all=1")
    run_id = urllib.parse.quote(runs["runs"][0]["path"])
    payload = _json(f"{base}/api/run/events?id={run_id}&after=abc&limit=xyz")
    assert payload["returned"] > 0
    handler = server_mod.Handler
    assert handler._int_param(handler, {"n": "3"}, "n", 0) == 3
    assert handler._int_param(handler, {}, "n", 7) == 7


def test_stream_emits_a_probe_frame(live_server):
    app, base, tmp_path = live_server
    runs = _json(f"{base}/api/runs")
    assert runs["runs"] == [] and runs["deployed"] == 0  # not deployed
    runs = _json(f"{base}/api/runs?all=1")
    run_id = urllib.parse.quote(runs["runs"][0]["path"])
    with urllib.request.urlopen(f"{base}/api/run/stream?id={run_id}", timeout=15) as stream:
        chunk = stream.readline() + stream.readline()
    assert b"data:" in chunk
    payload = json.loads(chunk.split(b"data: ", 1)[1].strip())
    assert payload["call_id"] == 2
    assert payload["size"] > 0
    assert "mtime" in payload and "call_id" in payload
    assert "now" not in payload  # the page ticks its own clock


def test_stream_rejects_an_unknown_run(live_server):
    app, base, tmp_path = live_server
    status, _ = _get(f"{base}/api/run/stream?id={tmp_path}/nope.db")
    assert status == 404


def test_main_runs_the_cli_and_parses_flags(monkeypatch, tmp_path):
    captured = {}

    class FakeServer:
        server_address = ("127.0.0.1", 9999)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            captured["closed"] = True

    def fake_serve(**kwargs):
        captured.update(kwargs)
        return FakeServer()

    monkeypatch.setattr("observability.__main__.serve", fake_serve)
    from observability.__main__ import main
    assert main(["--port", "9999", "--root", str(tmp_path), "--context-window", "77"]) == 0
    assert captured["port"] == 9999
    assert captured["context_window"] == 77
    assert captured["root"] == tmp_path
    assert captured["closed"] is True


def test_server_rejects_unknown_ids_on_every_run_endpoint(live_server):
    app, base, tmp_path = live_server
    missing = urllib.parse.quote(str(tmp_path / "nope.db"))
    for path in ("/api/run/events", "/api/run/context", "/api/run/call"):
        status, body = _get(f"{base}{path}?id={missing}")
        assert status == 404, (path, body)
    status, _ = _get(f"{base}/api/run/events")
    assert status == 200  # no id at all means "the newest run"


def test_server_closes_the_connection_when_the_client_vanishes(live_server, monkeypatch):
    app, base, tmp_path = live_server

    def boom(self, params):
        raise BrokenPipeError()

    monkeypatch.setattr(server_mod.Handler, "_runs", boom)
    with pytest.raises((urllib.error.URLError, ConnectionError, http.client.HTTPException)):
        _get(f"{base}/api/runs")


def test_serve_announces_the_url(tmp_path, capsys):
    store = _store_with_history(tmp_path)
    server = server_mod.serve(host="127.0.0.1", port=0, root=tmp_path, announce=True)
    try:
        assert "harness observability" in capsys.readouterr().out
    finally:
        server.server_close()


def test_resolve_run_accepts_only_discovered_paths(tmp_path):
    store = _store_with_history(tmp_path)
    state = server_mod.AppState(root=tmp_path, context_window=10)
    resolved = state.resolve_run(str(store.path))
    assert resolved is not None and str(resolved.path) == store.path
    # An empty id means "the newest run", never a filesystem path.
    newest = state.resolve_run("")
    assert newest is not None and str(newest.path) == store.path
    assert state.resolve_run(str(tmp_path / "elsewhere.db")) is None



