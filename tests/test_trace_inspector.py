"""The run inspector: every check fires on the shape it names, and stays
quiet on a clean run.

The fixtures build the tables with raw SQL rather than through
``SQLiteStore`` on purpose — the inspector's whole value is reading runs
whose schema does not match the current code, so its tests must not depend
on the current schema either.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from harness import trace


def _db(tmp_path, name="run.db"):
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table kv_store (key text primary key, value text);
        create table messages (
            id integer primary key, role text, content text, t_h real,
            day integer, proactive integer default 0, meta text,
            conversation_id text);
        create table conversations (
            id text primary key, opened_t_h real, closed_t_h real,
            opened_by text, close_reason text);
        create table agenda_items (
            id text primary key, day integer, start_t_h real, end_t_h real,
            activity text, source_type text, source_id text, salience real,
            status text);
        create table life_arcs (
            id text primary key, name text, interest text, progress real,
            status text, next_intention text);
        create table steering_queue (
            id integer primary key, day integer, t_h real, kind text,
            payload_json text, delivered_t_h real, boundary text,
            status text, seen_turn_id text, attempts integer default 0);
        create table decision_records (
            id integer primary key, day integer, t_h real, popup_kind text,
            event_id text, event_label text, state_label text, time text,
            inputs_json text, raw_reply text, verdict_json text,
            source text, transport text, delivered_t_h real,
            budget_consumed integer, replay_id text);
        create table state_events (
            id integer primary key, day integer, t_h real, event text,
            detail text);
        create table llm_calls (
            id integer primary key, day integer, t_h real, role text,
            model text, prompt_hash text, response text, meta text,
            repro_json text, prompt_tokens integer, completion_tokens integer,
            total_tokens integer, cached_tokens integer,
            cache_miss_tokens integer, lane text, raw_cost real);
        create table memory_episodes (
            id text primary key, summary text, category text,
            occurred_at_t_h real, importance real, affect_json text,
            verbatim_anchors_json text, tags_json text,
            source_turn_ids_json text);
        create table user_model_assertions (
            seq integer primary key, key text, value text, confidence real,
            updated_at_t_h real, status text, category text);
        """
    )
    return conn, path


def _ctx(path):
    conn = trace.open_db(path)
    return trace.Ctx(conn=conn, anchor=trace.load_anchor(conn))


def _codes(path):
    ctx = _ctx(path)
    try:
        found = [f for check in trace.CHECKS for f in check(ctx)]
    finally:
        ctx.conn.close()
    return {f.code: f for f in found}


def _call(env: dict, **kw) -> tuple:
    base = {"prompt": 1000, "cached": 900, "lane": "product", "cost": 0.01}
    base.update(kw)
    return (
        0, 1.0, "chat", "m", "h", "r", None, json.dumps(env),
        base["prompt"], 10, base["prompt"] + 10, base["cached"], 0,
        base["lane"], base["cost"],
    )


# a clean run is quiet


def test_a_clean_run_reports_nothing(tmp_path):
    conn, path = _db(tmp_path)
    conn.executemany(
        "insert into messages (id,role,content,t_h,day) values (?,?,?,?,?)",
        [(1, "user", "hi", 1.0, 0), (2, "assistant", "hey", 1.0, 0),
         (3, "user", "more", 2.0, 0), (4, "assistant", "sure", 2.0, 0)],
    )
    env1 = {"system": "PERSONA", "messages": [
        {"role": "user", "content": "hi"}, {"role": "system", "content": "c"}]}
    env2 = {"system": "PERSONA", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hey"},
        {"role": "system", "content": "c2"}]}
    conn.executemany(
        "insert into llm_calls (day,t_h,role,model,prompt_hash,response,meta,"
        "repro_json,prompt_tokens,completion_tokens,total_tokens,"
        "cached_tokens,cache_miss_tokens,lane,raw_cost) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_call(env1), _call(env2)],
    )
    conn.commit()
    conn.close()
    assert _codes(path) == {}


def test_checks_exit_code_is_zero_on_a_clean_run(tmp_path):
    conn, path = _db(tmp_path)
    conn.commit()
    conn.close()
    assert trace.main(["checks", "--db", str(path)]) == 0


# the negotiation that never got its decision


def _informed_then_forced(conn, *, afk=21.88, resolved=23.0):
    snaps = [
        {"item_id": "ag1", "activity": "read history", "phase": "inform",
         "informed": False, "start_t_h": 21.84, "end_t_h": 22.59,
         "delay_count": 0, "afk_deadline_t_h": None,
         "resolved_action": None, "resolved_t_h": None},
        {"item_id": "ag1", "activity": "read history", "phase": "decide",
         "informed": True, "start_t_h": 21.84, "end_t_h": 22.59,
         "delay_count": 0, "afk_deadline_t_h": afk,
         "resolved_action": None, "resolved_t_h": None},
        {"item_id": "ag1", "activity": "read history",
         "phase": "resolved_forced", "informed": True, "start_t_h": 21.84,
         "end_t_h": 22.59, "delay_count": 0, "afk_deadline_t_h": afk,
         "resolved_action": "forced", "resolved_t_h": resolved},
    ]
    conn.executemany(
        "insert into state_events (day,t_h,event,detail) values (?,?,?,?)",
        [(0, 21.7 + i * 0.1, "negotiation_state", json.dumps(s))
         for i, s in enumerate(snaps)],
    )


def test_a_due_afk_leg_that_never_ran_is_an_error(tmp_path):
    conn, path = _db(tmp_path)
    _informed_then_forced(conn)
    conn.commit()
    conn.close()
    found = _codes(path)
    assert "afk-decide-never-ran" in found
    assert found["afk-decide-never-ran"].severity == trace.ERROR
    assert "informed-never-decided" in found


def test_an_afk_deadline_past_the_window_is_not_a_missed_leg(tmp_path):
    """A deadline outside the window is the backstop's business, not a
    missed decide leg — the check must not cry wolf on it."""
    conn, path = _db(tmp_path)
    _informed_then_forced(conn, afk=23.5, resolved=23.6)
    conn.commit()
    conn.close()
    assert "afk-decide-never-ran" not in _codes(path)


def test_a_model_decide_leg_clears_informed_never_decided(tmp_path):
    conn, path = _db(tmp_path)
    _informed_then_forced(conn)
    conn.execute(
        "insert into decision_records (day,t_h,popup_kind,event_id,"
        "state_label,source,verdict_json) values (?,?,?,?,?,?,?)",
        (0, 22.0, "tool_decide_event", "ag1", "decide", "model",
         json.dumps({"initiate": True})),
    )
    conn.commit()
    conn.close()
    assert "informed-never-decided" not in _codes(path)


# the decision lane


def test_prose_replies_are_counted_against_the_verdicts(tmp_path):
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into decision_records (day,t_h,popup_kind,event_id,"
        "state_label,source,verdict_json) values (?,?,?,?,?,?,?)",
        (0, 1.0, "tool_decide_event", "ag1", "start", "model", "{}"),
    )
    conn.executemany(
        "insert into state_events (day,t_h,event,detail) values (?,?,?,?)",
        [(0, 1.0, "decision_parse_failed", json.dumps(
            {"decision_id": "steer-1", "popup_kind": "tool_decide_event",
             "parse_failure_mode": "requeue", "raw_excerpt": "*giggles*"}))
         for _ in range(3)],
    )
    conn.commit()
    conn.close()
    found = _codes(path)
    assert found["decision-prose-reply"].severity == trace.ERROR
    assert "3 of 4" in found["decision-prose-reply"].message


def test_an_abandoned_boundary_steer_is_an_error(tmp_path):
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into steering_queue (id,day,t_h,kind,payload_json,status,"
        "attempts) values (?,?,?,?,?,?,?)",
        (1, 0, 7.92, "event_popup",
         json.dumps({"event": "math practice", "state": "start"}),
         "abandoned", 3),
    )
    conn.commit()
    conn.close()
    assert _codes(path)["steer-abandoned"].severity == trace.ERROR


def test_a_placeholder_event_name_is_flagged(tmp_path):
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into steering_queue (id,day,t_h,kind,payload_json,status) "
        "values (?,?,?,?,?,?)",
        (1, 0, 20.5, "user_message_mid_turn",
         json.dumps({"event": "?", "state": "in_progress"}), "delivered"),
    )
    conn.commit()
    conn.close()
    assert _codes(path)["steer-placeholder-event"].severity == trace.WARN


# the persisted stream


def test_two_system_messages_in_a_row_are_an_error(tmp_path):
    conn, path = _db(tmp_path)
    conn.executemany(
        "insert into messages (id,role,content,t_h,day) values (?,?,?,?,?)",
        [(1, "system", "card", 1.0, 0), (2, "system", "popup", 1.0, 0)],
    )
    conn.commit()
    conn.close()
    assert _codes(path)["adjacent-system-messages"].severity == trace.ERROR


def test_a_system_message_between_turns_is_not_adjacency(tmp_path):
    conn, path = _db(tmp_path)
    conn.executemany(
        "insert into messages (id,role,content,t_h,day) values (?,?,?,?,?)",
        [(1, "user", "hi", 1.0, 0), (2, "assistant", "hey", 1.0, 0),
         (3, "system", "day block", 2.0, 0), (4, "user", "more", 2.0, 0),
         (5, "assistant", "sure", 2.0, 0)],
    )
    conn.commit()
    conn.close()
    found = _codes(path)
    assert "adjacent-system-messages" not in found
    assert "unanswered-user-turn" not in found


def test_an_unanswered_user_turn_is_found_across_a_system_row(tmp_path):
    """The leak cleanup left a user turn with no reply and a system row
    between it and the resend; neither may hide it."""
    conn, path = _db(tmp_path)
    conn.executemany(
        "insert into messages (id,role,content,t_h,day) values (?,?,?,?,?)",
        [(1, "user", "same text", 1.0, 0), (2, "system", "day", 2.0, 0),
         (3, "user", "same text", 2.0, 0), (4, "assistant", "hey", 2.0, 0)],
    )
    conn.commit()
    conn.close()
    found = _codes(path)
    assert found["unanswered-user-turn"].severity == trace.ERROR
    assert found["duplicate-user-turn"].severity == trace.WARN


# the card, metering and the cache


def test_a_skipped_item_listed_as_done_is_an_error(tmp_path):
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into agenda_items (id,day,start_t_h,end_t_h,activity,"
        "source_type,source_id,salience,status) values (?,?,?,?,?,?,?,?,?)",
        ("ag1", 0, 21.8, 22.6, "read history", "routine", "read history",
         0.8, "skipped"),
    )
    env = {"system": "P", "messages": [{"role": "system", "content":
           "TEMPORAL FRAME:\nDone earlier:\n- read history (21:50-22:35)\n"}]}
    conn.execute(
        "insert into llm_calls (day,t_h,role,model,prompt_hash,response,meta,"
        "repro_json,prompt_tokens,completion_tokens,total_tokens,"
        "cached_tokens,cache_miss_tokens,lane,raw_cost) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _call(env),
    )
    conn.commit()
    conn.close()
    found = _codes(path)
    assert found["card-reports-skipped-as-done"].severity == trace.ERROR


def test_a_skipped_item_under_its_own_heading_is_not_reported(tmp_path):
    """The fix: the card now says "Did not happen". The check must read the
    heading, not just look for the activity anywhere in the card."""
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into agenda_items (id,day,start_t_h,end_t_h,activity,"
        "source_type,source_id,salience,status) values (?,?,?,?,?,?,?,?,?)",
        ("ag1", 0, 21.8, 22.6, "read history", "routine", "read history",
         0.8, "skipped"),
    )
    env = {"system": "P", "messages": [{"role": "system", "content":
           "TEMPORAL FRAME:\nDone earlier:\n- math practice (07:55-08:40)\n"
           "Did not happen:\n- read history (21:50-22:35)\n"}]}
    conn.execute(
        "insert into llm_calls (day,t_h,role,model,prompt_hash,response,meta,"
        "repro_json,prompt_tokens,completion_tokens,total_tokens,"
        "cached_tokens,cache_miss_tokens,lane,raw_cost) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _call(env),
    )
    conn.commit()
    conn.close()
    assert "card-reports-skipped-as-done" not in _codes(path)


def test_unmetered_decision_calls_are_an_error(tmp_path):
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into decision_records (day,t_h,popup_kind,event_id,"
        "state_label,source,verdict_json) values (?,?,?,?,?,?,?)",
        (0, 1.0, "tool_decide_event", "ag1", "start", "model", "{}"),
    )
    conn.execute(
        "insert into llm_calls (day,t_h,role,model,prompt_hash,response,meta,"
        "repro_json,prompt_tokens,completion_tokens,total_tokens,"
        "cached_tokens,cache_miss_tokens,lane,raw_cost) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _call({}),
    )
    conn.commit()
    conn.close()
    found = _codes(path)["decision-calls-unmetered"]
    assert found.severity == trace.ERROR
    # It is the ROLE that tells the lanes apart: the decision calls are the
    # same model on the same lane as the mainline.
    assert "tool_decide_event" in found.message


def test_a_shared_prefix_the_provider_did_not_cache_is_an_error(tmp_path):
    conn, path = _db(tmp_path)
    body = [{"role": "user", "content": "x" * 4000}]
    env1 = {"system": "P", "messages": body + [
        {"role": "system", "content": "card"}]}
    env2 = {"system": "P", "messages": body + [
        {"role": "assistant", "content": "y"},
        {"role": "system", "content": "card2"}]}
    conn.executemany(
        "insert into llm_calls (day,t_h,role,model,prompt_hash,response,meta,"
        "repro_json,prompt_tokens,completion_tokens,total_tokens,"
        "cached_tokens,cache_miss_tokens,lane,raw_cost) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_call(env1), _call(env2, prompt=1000, cached=100)],
    )
    conn.commit()
    conn.close()
    assert _codes(path)["prefix-shared-but-not-cached"].severity == trace.ERROR


def test_a_changed_system_prefix_is_an_error(tmp_path):
    conn, path = _db(tmp_path)
    conn.executemany(
        "insert into llm_calls (day,t_h,role,model,prompt_hash,response,meta,"
        "repro_json,prompt_tokens,completion_tokens,total_tokens,"
        "cached_tokens,cache_miss_tokens,lane,raw_cost) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [_call({"system": "PERSONA A", "messages": []}),
         _call({"system": "PERSONA B", "messages": []})],
    )
    conn.commit()
    conn.close()
    assert _codes(path)["stable-prefix-changed"].severity == trace.ERROR


def test_missing_cost_is_only_a_warning(tmp_path):
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into llm_calls (day,t_h,role,model,prompt_hash,response,meta,"
        "repro_json,prompt_tokens,completion_tokens,total_tokens,"
        "cached_tokens,cache_miss_tokens,lane,raw_cost) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _call({}, cost=None),
    )
    conn.commit()
    conn.close()
    assert _codes(path)["no-cost-recorded"].severity == trace.WARN


# memory


def test_a_forced_verdict_reason_stored_as_memory_is_flagged(tmp_path):
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into memory_episodes (id,summary,category,occurred_at_t_h,"
        "importance,verbatim_anchors_json,tags_json) values (?,?,?,?,?,?,?)",
        ("neg-ag1-FORCED-23.0", "missed it entirely — window closed",
         "companion_episode", 23.0, 0.8, "[]",
         json.dumps(["negotiation_forced"])),
    )
    conn.commit()
    conn.close()
    assert _codes(path)["machine-reason-stored-as-memory"].severity == trace.WARN


def test_a_keyword_shaped_user_fact_is_flagged(tmp_path):
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into memory_episodes (id,summary,category,occurred_at_t_h,"
        "importance,verbatim_anchors_json,tags_json) values (?,?,?,?,?,?,?)",
        ("ep-0", "user no longer has bloat", "user_fact", 18.2, 0.6,
         "[]", "[]"),
    )
    conn.commit()
    conn.close()
    assert _codes(path)["thin-user-fact"].severity == trace.WARN


# plumbing


def test_missing_tables_do_not_crash_any_view(tmp_path):
    """Old and half-written runs are exactly what this tool is for."""
    path = tmp_path / "bare.db"
    sqlite3.connect(path).close()
    for view in trace.VIEWS:
        assert trace.main([view, "--db", str(path)]) in (0, 1)


def test_a_missing_database_is_reported_not_created(tmp_path):
    missing = tmp_path / "nope.db"
    assert trace.main(["checks", "--db", str(missing)]) == 2
    assert not missing.exists()


def test_the_database_is_opened_read_only(tmp_path):
    conn, path = _db(tmp_path)
    conn.commit()
    conn.close()
    ro = trace.open_db(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("insert into kv_store (key,value) values ('a','b')")
    finally:
        ro.close()


def test_virtual_hours_render_against_the_run_anchor(tmp_path):
    conn, path = _db(tmp_path)
    conn.executemany(
        "insert into kv_store (key,value) values (?,?)",
        [("anchor.epoch0_s", "1788910828.9251575"),
         ("anchor.t_h0", "17.674701432651943"),
         ("anchor.tz", "America/Chihuahua")],
    )
    conn.commit()
    conn.close()
    ctx = _ctx(path)
    try:
        assert ctx.anchor is not None
        # One virtual hour on is one real hour on.
        first = ctx.anchor.real(17.674701432651943)
        later = ctx.anchor.real(18.674701432651943)
        assert (later - first).total_seconds() == pytest.approx(3600.0)
        assert "t=  20.583" in ctx.when(20.583)
    finally:
        ctx.conn.close()


def test_an_unanchored_run_prints_the_virtual_coordinate_alone(tmp_path):
    conn, path = _db(tmp_path)
    conn.commit()
    conn.close()
    ctx = _ctx(path)
    try:
        assert ctx.anchor is None
        assert ctx.when(20.583).strip() == "t=  20.583"
    finally:
        ctx.conn.close()


def test_the_timeline_orders_a_replayed_decision_at_its_own_boundary(tmp_path):
    """A decision executed later is stamped at the ORIGINAL instant; the
    timeline must sort it there and name the instant it actually ran."""
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into decision_records (day,t_h,popup_kind,event_id,"
        "state_label,source,verdict_json,delivered_t_h) "
        "values (?,?,?,?,?,?,?,?)",
        (0, 17.0, "tool_decide_event", "ag1", "start", "model",
         json.dumps({"initiate": True}), 17.7),
    )
    conn.execute(
        "insert into messages (id,role,content,t_h,day) values (?,?,?,?,?)",
        (1, "user", "hi", 17.7, 0),
    )
    conn.commit()
    conn.close()
    ctx = _ctx(path)
    try:
        lines = trace.timeline(ctx)
    finally:
        ctx.conn.close()
    body = [ln for ln in lines if "DECIDE" in ln or "USER" in ln]
    assert "DECIDE" in body[0] and "ran at t=17.700" in body[0]
    assert "USER" in body[1]


def test_a_native_popup_metered_by_its_mainline_call_is_not_reported(tmp_path):
    """A native pop-up is answered INSIDE the mainline call the model was
    already making, so its tokens are that call's tokens. A ledger row at the
    same instant is the metering; the check must not call it missing."""
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into decision_records (day,t_h,popup_kind,event_id,"
        "state_label,source,transport,verdict_json) values (?,?,?,?,?,?,?,?)",
        (0, 1.0, "tool_decide_reply", "ag1", "decide", "model", "native", "{}"),
    )
    conn.execute(
        "insert into llm_calls (day,t_h,role,model,prompt_hash,response,meta,"
        "repro_json,prompt_tokens,completion_tokens,total_tokens,"
        "cached_tokens,cache_miss_tokens,lane,raw_cost) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _call({}),
    )
    conn.commit()
    conn.close()
    assert "decision-calls-unmetered" not in _codes(path)


def test_a_native_popup_with_no_call_at_its_instant_is_still_reported(tmp_path):
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into decision_records (day,t_h,popup_kind,event_id,"
        "state_label,source,transport,verdict_json) values (?,?,?,?,?,?,?,?)",
        (0, 9.5, "tool_decide_reply", "ag1", "decide", "model", "native", "{}"),
    )
    conn.execute(
        "insert into llm_calls (day,t_h,role,model,prompt_hash,response,meta,"
        "repro_json,prompt_tokens,completion_tokens,total_tokens,"
        "cached_tokens,cache_miss_tokens,lane,raw_cost) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _call({}),
    )
    conn.commit()
    conn.close()
    found = _codes(path)["decision-calls-unmetered"]
    assert found.severity == trace.ERROR
    assert "1 of 1 model decision call" in found.message
    assert "1" in found.detail[1] or "ids: [1]" in found.detail[1]


def test_a_textual_popup_needs_its_own_ledger_row(tmp_path):
    """A marker-transport verdict is a model call of its own, so it must be
    its own ledger row even when a mainline call shares its instant."""
    conn, path = _db(tmp_path)
    conn.execute(
        "insert into decision_records (day,t_h,popup_kind,event_id,"
        "state_label,source,transport,verdict_json) values (?,?,?,?,?,?,?,?)",
        (0, 1.0, "tool_decide_reply", "ag1", "decide", "model", "text", "{}"),
    )
    conn.execute(
        "insert into llm_calls (day,t_h,role,model,prompt_hash,response,meta,"
        "repro_json,prompt_tokens,completion_tokens,total_tokens,"
        "cached_tokens,cache_miss_tokens,lane,raw_cost) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _call({}),
    )
    conn.commit()
    conn.close()
    found = _codes(path)["decision-calls-unmetered"]
    assert found.severity == trace.ERROR and "tool_decide_reply" in found.message
