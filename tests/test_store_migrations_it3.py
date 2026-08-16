"""v4 -> v5 migration tests (runtime redesign WS2: decisions + steering).

Builds a database with the PRE-v5 schema (the CURRENT repo schema at the WS2
base commit 3af0a5a — v4 DDL embedded VERBATIM below, deliberately NOT
imported from harness.store), seeds it across the table families, then
instantiates the new store: the v4 -> v5 migration must add the
``decision_records`` and ``steering_queue`` tables, every piece of legacy
data must remain present and interpretable, re-opening the same database
(migration runs twice) must be a no-op, and the new steer/decision APIs must
work on a fresh database. No destructive migration.
"""

import json
import sqlite3

from harness.store import SCHEMA_VERSION, SQLiteStore

# Current (v4) repo schema, VERBATIM from harness/store.py at 3af0a5a
# (v1 frozen base + v2 tables + memory_turns views + v3 columns + v4
# conversation tables; the v3/v4 additive columns applied as guarded ALTERs
# exactly like _migrate_v3/_migrate_v4 do).
_V4_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_state (
    day INTEGER PRIMARY KEY,
    M INTEGER NOT NULL,
    m_level REAL NOT NULL,
    g REAL NOT NULL,
    p REAL NOT NULL,
    arg REAL NOT NULL,
    mu REAL NOT NULL,
    eta REAL NOT NULL,
    cycle_day REAL NOT NULL,
    phase_label TEXT NOT NULL,
    seed INTEGER NOT NULL,
    score REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    t_h REAL NOT NULL,
    day INTEGER NOT NULL,
    proactive INTEGER NOT NULL DEFAULT 0,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_day ON messages(day);
CREATE TABLE IF NOT EXISTS judgements (
    day INTEGER PRIMARY KEY,
    score REAL NOT NULL,
    justification TEXT,
    model TEXT,
    shadow INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day INTEGER NOT NULL,
    t_h REAL NOT NULL,
    event TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_events_day ON state_events(day);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day INTEGER NOT NULL,
    t_h REAL NOT NULL,
    role TEXT NOT NULL,
    model TEXT,
    prompt_hash TEXT,
    response TEXT,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS schedule_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    seed     INTEGER NOT NULL,
    t_h      REAL    NOT NULL,
    day      INTEGER NOT NULL,
    reason   TEXT    NOT NULL,
    status   TEXT    NOT NULL DEFAULT 'pending',
    fired_t_h REAL,
    UNIQUE(seed, t_h)
);
CREATE INDEX IF NOT EXISTS idx_schedule_events_seed_status
    ON schedule_events(seed, status);
CREATE TABLE IF NOT EXISTS persona (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    core TEXT NOT NULL,
    routines_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS interests (
    name TEXT PRIMARY KEY,
    bucket TEXT NOT NULL,
    salience REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS life_arcs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    interest TEXT NOT NULL,
    started_day INTEGER NOT NULL,
    progress REAL NOT NULL,
    status TEXT NOT NULL,
    next_intention TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_life_arcs_status ON life_arcs(status);
CREATE TABLE IF NOT EXISTS agenda_items (
    id TEXT PRIMARY KEY,
    day INTEGER NOT NULL,
    start_t_h REAL NOT NULL,
    end_t_h REAL NOT NULL,
    activity TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    salience REAL NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agenda_items_day ON agenda_items(day);
CREATE INDEX IF NOT EXISTS idx_agenda_items_status ON agenda_items(status);
CREATE TABLE IF NOT EXISTS proactive_intents (
    id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    hook TEXT NOT NULL,
    created_t_h REAL NOT NULL,
    valid_until_t_h REAL NOT NULL,
    salience REAL NOT NULL,
    evidence TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_proactive_intents_status
    ON proactive_intents(status);
CREATE TABLE IF NOT EXISTS memory_sessions (
    session_id TEXT PRIMARY KEY,
    started_at_t_h REAL NOT NULL,
    ended_at_t_h REAL
);
CREATE TABLE IF NOT EXISTS memory_session_summaries (
    session_id TEXT PRIMARY KEY,
    started_at_t_h REAL NOT NULL DEFAULT 0.0,
    ended_at_t_h REAL NOT NULL DEFAULT 0.0,
    summary TEXT NOT NULL,
    topics_json TEXT NOT NULL DEFAULT '[]',
    user_facts_json TEXT NOT NULL DEFAULT '[]',
    preference_updates_json TEXT NOT NULL DEFAULT '[]',
    companion_events_json TEXT NOT NULL DEFAULT '[]',
    relationship_events_json TEXT NOT NULL DEFAULT '[]',
    callbacks_json TEXT NOT NULL DEFAULT '[]',
    affect_observations_json TEXT NOT NULL DEFAULT '[]',
    emotional_peak INTEGER NOT NULL DEFAULT 0,
    importance REAL NOT NULL DEFAULT 0.0,
    source_turn_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS memory_episodes (
    id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    category TEXT NOT NULL,
    occurred_at_t_h REAL NOT NULL,
    created_at_t_h REAL NOT NULL,
    importance REAL NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_t_h REAL,
    affect_json TEXT,
    source_session_id TEXT NOT NULL,
    source_turn_ids_json TEXT NOT NULL DEFAULT '[]',
    verbatim_anchors_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_memory_episodes_category
    ON memory_episodes(category);
CREATE TABLE IF NOT EXISTS memory_episode_sources (
    episode_id TEXT NOT NULL,
    turn_id INTEGER NOT NULL,
    PRIMARY KEY (episode_id, turn_id)
);
CREATE TABLE IF NOT EXISTS user_model_assertions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL,
    updated_at_t_h REAL NOT NULL,
    source_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'current'
);
CREATE INDEX IF NOT EXISTS idx_user_model_assertions_key_status
    ON user_model_assertions(key, status);
CREATE TABLE IF NOT EXISTS memory_embeddings (
    episode_id TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    dim INTEGER NOT NULL
);
CREATE VIEW IF NOT EXISTS memory_turns AS
SELECT id, session_id, role, content, t_h, day, proactive, meta
FROM messages
WHERE session_id IS NOT NULL;
DROP VIEW IF EXISTS memory_turns;
CREATE VIEW memory_turns AS
SELECT id, session_id, intent_id, role, content, t_h, day, proactive, meta
FROM messages
WHERE session_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    opened_t_h REAL NOT NULL,
    closed_t_h REAL,
    opened_by TEXT NOT NULL,
    close_reason TEXT
);
CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    t_h REAL NOT NULL,
    turn_index INTEGER NOT NULL,
    message_id INTEGER,
    UNIQUE (conversation_id, turn_index)
);
CREATE INDEX IF NOT EXISTS idx_conversation_turns_conversation
    ON conversation_turns(conversation_id);
"""

_V4_ALTERS = """
ALTER TABLE messages ADD COLUMN session_id TEXT;
ALTER TABLE messages ADD COLUMN intent_id TEXT;
ALTER TABLE user_model_assertions ADD COLUMN category TEXT;
ALTER TABLE llm_calls ADD COLUMN repro_json TEXT;
ALTER TABLE messages ADD COLUMN conversation_id TEXT;
"""

#: All tables the pre-v5 schema owns (row-count comparison after migration).
_LEGACY_TABLES = [
    "daily_state", "messages", "judgements", "state_events", "llm_calls",
    "schedule_events", "persona", "interests", "life_arcs", "agenda_items",
    "proactive_intents", "memory_sessions", "memory_session_summaries",
    "memory_episodes", "memory_episode_sources", "user_model_assertions",
    "memory_embeddings", "conversations", "conversation_turns",
]


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    conn.row_factory = sqlite3.Row
    return {
        t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        for t in _LEGACY_TABLES
    }


def _build_v4_db(path) -> None:
    """Create a database exactly as the current repo would after v4, and seed
    it across every table family (messages, judgements, schedule, memory,
    persona, arcs, agenda, intents, conversations, audit)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_V4_SCHEMA)
    conn.executescript(_V4_ALTERS)
    # bookkeeping: a real v4 database has exactly one row at version 4
    conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_meta (version) VALUES (4)")
    conn.executemany(
        "INSERT INTO messages (id, role, content, t_h, day, proactive, meta, "
        "session_id, intent_id, conversation_id) VALUES (?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?)",
        [
            (1, "user", "legacy hello", 10.0, 0, 0, None, None, None, None),
            (2, "assistant", "legacy reply", 10.1, 0, 0, None, None, None,
             None),
            (3, "assistant", "legacy proactive", 11.0, 0, 1, None, None,
             "intent-1", None),
            (4, "user", "session turn", 12.0, 0, 0, None, "s1", None, "c1"),
        ],
    )
    conn.execute(
        "INSERT INTO judgements (day, score, justification, model, shadow) "
        "VALUES (0, 0.8, 'legacy', 'fake', 1)"
    )
    conn.execute(
        "INSERT INTO daily_state (day, M, m_level, g, p, arg, mu, eta, "
        "cycle_day, phase_label, seed, score) VALUES "
        "(0, 7, 0.5, 1.2, 0.3, 0.1, 0.9, 0.2, 5.0, 'luteal', 4242, 0.5)"
    )
    conn.execute(
        "INSERT INTO state_events (day, t_h, event, detail) VALUES "
        "(0, 9.0, 'day_rollover', NULL)"
    )
    conn.execute(
        "INSERT INTO llm_calls (day, t_h, role, model, prompt_hash, "
        "response, meta, repro_json) VALUES "
        "(0, 10.0, 'chat', 'fake', 'abc123', 'legacy response', NULL, NULL)"
    )
    conn.execute(
        "INSERT INTO schedule_events (seed, t_h, day, reason, status, "
        "fired_t_h) VALUES (4242, 10.0, 0, 'agenda', 'pending', NULL)"
    )
    conn.execute(
        "INSERT INTO persona (id, name, core, routines_json) VALUES "
        "(1, 'Lily', 'core', '[]')"
    )
    conn.execute(
        "INSERT INTO interests (name, bucket, salience) VALUES "
        "('jazz', 'music', 0.7)"
    )
    conn.execute(
        "INSERT INTO life_arcs (id, name, interest, started_day, progress, "
        "status, next_intention) VALUES "
        "('a1', 'practice lifting', 'fitness', 0, 0.37, 'active', 'gym')"
    )
    conn.execute(
        "INSERT INTO agenda_items (id, day, start_t_h, end_t_h, activity, "
        "source_type, source_id, salience, status) VALUES "
        "('ag1', 0, 19.0, 20.5, 'practice lifting', 'life_arc', 'a1', 0.8, "
        "'planned')"
    )
    conn.execute(
        "INSERT INTO proactive_intents (id, reason, source_type, source_id, "
        "hook, created_t_h, valid_until_t_h, salience, evidence, status) "
        "VALUES ('p1', 'progress', 'life_arc', 'a1', 'progress', 9.0, 24.0, "
        "0.5, '{}', 'active')"
    )
    conn.execute(
        "INSERT INTO memory_sessions (session_id, started_at_t_h, "
        "ended_at_t_h) VALUES ('s1', 12.0, 13.0)"
    )
    conn.execute(
        "INSERT INTO memory_session_summaries (session_id, summary, "
        "source_turn_ids_json) VALUES ('s1', 'User introduced Bruno.', "
        "'[1, 2]')"
    )
    conn.execute(
        "INSERT INTO memory_episodes (id, summary, category, "
        "occurred_at_t_h, created_at_t_h, importance, source_session_id, "
        "source_turn_ids_json) VALUES ('ep1', 'User dog is Bruno.', "
        "'shared_episode', 10.0, 10.1, 0.9, 's1', '[1, 2]')"
    )
    conn.execute(
        "INSERT INTO memory_episode_sources (episode_id, turn_id) "
        "VALUES ('ep1', 1), ('ep1', 2)"
    )
    conn.execute(
        "INSERT INTO user_model_assertions (seq, key, value, confidence, "
        "updated_at_t_h, source_memory_ids_json, status, category) VALUES "
        "(1, 'identity', 'Bruno human', 0.9, 10.0, '[]', 'current', "
        "'identity'), "
        "(2, 'current_preferences:coffee', 'flat white', 0.8, 10.0, '[]', "
        "'current', 'current_preference')"
    )
    conn.execute(
        "INSERT INTO memory_embeddings (episode_id, vector, dim) VALUES "
        "('ep1', x'010203', 3)"
    )
    conn.execute(
        "INSERT INTO conversations (id, opened_t_h, closed_t_h, opened_by, "
        "close_reason) VALUES ('c1', 12.0, 13.0, 'user', 'user_left')"
    )
    conn.execute(
        "INSERT INTO conversation_turns (conversation_id, speaker, text, "
        "t_h, turn_index, message_id) VALUES "
        "('c1', 'user', 'session turn', 12.0, 0, 4)"
    )
    conn.commit()
    conn.close()


def test_v4_to_v5_migration_preserves_everything(tmp_path):
    db = tmp_path / "v4.db"
    _build_v4_db(db)
    before = _table_counts(sqlite3.connect(db))

    store = SQLiteStore(db)  # v4 -> v5 migration runs inside __init__

    # version bookkeeping: exactly one row, at the current version
    rows = store.conn.execute("SELECT * FROM schema_meta").fetchall()
    assert len(rows) == 1
    assert rows[0]["version"] == SCHEMA_VERSION == 7

    # M1: decision_records exists with the exact WS2 column set
    dr_cols = {
        r["name"] for r in store.conn.execute(
            "PRAGMA table_info(decision_records)"
        )
    }
    assert dr_cols == {
        "id", "day", "t_h", "popup_kind", "event_id", "event_label",
        "state_label", "time", "inputs_json", "raw_reply", "verdict_json",
        "source", "transport", "delivered_t_h", "budget_consumed",
        "replay_id",
    }

    # M2: steering_queue exists with the exact WS3 contract columns
    sq_cols = {
        r["name"] for r in store.conn.execute(
            "PRAGMA table_info(steering_queue)"
        )
    }
    assert sq_cols == {
        "id", "day", "t_h", "kind", "payload_json", "delivered_t_h",
        "boundary", "status", "seen_turn_id",
    }

    # --- legacy data fully intact -----------------------------------------
    msgs = store.recent_messages()
    assert [m["content"] for m in msgs] == [
        "legacy hello", "legacy reply", "legacy proactive", "session turn",
    ]
    assert msgs[0]["session_id"] is None and msgs[3]["session_id"] == "s1"
    assert msgs[2]["intent_id"] == "intent-1"
    assert msgs[3]["conversation_id"] == "c1"
    assert store.proactive_count(0) == 1

    j = store.load_judgement(0)
    assert j is not None and j["score"] == 0.8
    state0 = store.load_daily_state(0)
    assert state0 is not None and state0["M"] == 7
    assert store.events_since(0)[0]["event"] == "day_rollover"

    calls = store.conn.execute("SELECT * FROM llm_calls").fetchall()
    assert len(calls) == 1 and calls[0]["response"] == "legacy response"
    assert calls[0]["repro_json"] is None

    assert len(store.schedule_events_for_seed(4242)) == 1
    assert store.load_persona().name == "Lily"
    assert store.get_life_arc("a1").progress == 0.37
    assert store.load_agenda(0).items[0].activity == "practice lifting"
    assert store.load_proactive_intent("p1").hook == "progress"
    assert store.load_session_summary("s1").summary == "User introduced Bruno."
    assert store.get_episode("ep1").summary == "User dog is Bruno."
    assert store.list_episode_sources("ep1") == [1, 2]
    assert store.get_assertion_category("identity") is not None
    conv = store.conn.execute(
        "SELECT * FROM conversations WHERE id = 'c1'"
    ).fetchone()
    assert conv["close_reason"] == "user_left"
    turns = store.conn.execute(
        "SELECT * FROM conversation_turns WHERE conversation_id = 'c1'"
    ).fetchall()
    assert len(turns) == 1 and turns[0]["message_id"] == 4

    # no destructive transformations: identical row counts on every table
    after = _table_counts(store.conn)
    assert after == before

    # the new tables are empty until used
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM decision_records"
    ).fetchone()["n"] == 0
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM steering_queue"
    ).fetchone()["n"] == 0

    store.close()


def test_v5_migration_runs_twice_without_error(tmp_path):
    db = tmp_path / "v4b.db"
    _build_v4_db(db)

    store1 = SQLiteStore(db, audit_mode=True)
    # write through the v5 schema after migration
    steer_id = store1.enqueue_steer(0, 19.0, "popup",
                                    {"kind": "tool_decide_event"})
    dec_id = store1.record_decision(
        0, 19.0, "tool_decide_event", "evt-1", "gym", "start", "19.0",
        '{"event": "gym"}', 'tool_decide_event: {yes, "go"}',
        '{"initiate": true, "reason": "go", "action": null}',
        "model", "textual", None, 0, replay_id="dec-1",
    )
    store1.close()

    # re-open: migration must be a no-op, not an error, no duplicate rows
    store2 = SQLiteStore(db, audit_mode=True)
    rows = store2.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION == 7
    assert store2.pending_steers()[0]["id"] == steer_id
    replay = store2.decision_for_replay("dec-1")
    assert replay is not None and replay["id"] == dec_id
    assert replay["verdict"] == {"initiate": True, "reason": "go",
                                 "action": None}
    assert replay["raw_reply"] == 'tool_decide_event: {yes, "go"}'
    # legacy data still intact after the second open
    assert [m["content"] for m in store2.recent_messages()] == [
        "legacy hello", "legacy reply", "legacy proactive", "session turn",
    ]
    store2.close()


def test_fresh_db_reaches_v5_with_tables_and_new_apis(tmp_path):
    store = SQLiteStore(tmp_path / "fresh.db", audit_mode=True)
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION == 7

    # --- steering_queue contract (WS3) ------------------------------------
    s1 = store.enqueue_steer(0, 19.0, "popup",
                             {"kind": "tool_decide_event", "event": "gym"})
    s2 = store.enqueue_steer(0, 19.5, "user_message",
                             {"text": "are you coming?"})
    s3 = store.enqueue_steer(1, 25.0, "schedule", {"reason": "fire"})
    assert store.enqueue_steer(0, 20.0, "popup", "raw-string-payload") > 0

    pending = store.pending_steers()
    assert [p["id"] for p in pending] == [s1, s2, s3, s1 + 3]
    assert pending[0]["kind"] == "popup"
    assert pending[0]["payload"] == {
        "kind": "tool_decide_event", "event": "gym",
    }
    assert pending[3]["payload"] == "raw-string-payload"
    assert pending[0]["status"] == "pending"

    # day filter
    assert [p["id"] for p in store.pending_steers(day=1)] == [s3]
    # limit
    assert len(store.pending_steers(limit=2)) == 2

    # delivery records actual time/boundary/seen turn (summary #23)
    store.mark_steer_delivered(s1, 19.05, "after_tool", "turn-7")
    row = store.conn.execute(
        "SELECT * FROM steering_queue WHERE id = ?", (s1,)
    ).fetchone()
    assert row["status"] == "delivered"
    assert row["delivered_t_h"] == 19.05
    assert row["boundary"] == "after_tool"
    assert row["seen_turn_id"] == "turn-7"
    assert s1 not in [p["id"] for p in store.pending_steers()]

    # requeue after an interrupted delivery: back to pending, fields cleared
    store.requeue_steer(s1)
    row = store.conn.execute(
        "SELECT * FROM steering_queue WHERE id = ?", (s1,)
    ).fetchone()
    assert row["status"] == "pending"
    assert row["delivered_t_h"] is None
    assert row["boundary"] is None
    assert row["seen_turn_id"] is None

    # --- decision_records API (WS2) ---------------------------------------
    rid = store.record_decision(
        0, 19.0, "tool_decide_event", "evt-1", "gym", "start", "19.0",
        '{"event_id": "evt-1"}', 'tool_decide_event: {yes, "go"}',
        '{"initiate": true, "reason": "go", "action": null}',
        "model", "textual", 19.01, 0, replay_id="dec-1",
    )
    assert rid > 0

    replay = store.decision_for_replay("dec-1")
    assert replay is not None
    assert replay["id"] == rid
    assert replay["popup_kind"] == "tool_decide_event"
    assert replay["inputs"] == {"event_id": "evt-1"}
    assert replay["verdict"] == {"initiate": True, "reason": "go",
                                 "action": None}
    assert replay["transport"] == "textual"
    assert replay["budget_consumed"] == 0
    assert store.decision_for_replay("unknown") is None

    # a newer record for the same replay_id wins (replay reads the latest)
    rid2 = store.record_decision(
        0, 19.1, "tool_decide_event", "evt-1", "gym", "start", "19.0",
        '{"event_id": "evt-1"}', 'tool_decide_event: {no, "skip"}',
        '{"initiate": false, "reason": "skip", "action": null}',
        "model", "textual", 19.11, 0, replay_id="dec-1",
    )
    assert store.decision_for_replay("dec-1")["id"] == rid2

    # decisions_for_day returns all rows for the day, oldest first
    day_rows = store.decisions_for_day(0)
    assert [r["id"] for r in day_rows] == [rid, rid2]
    assert day_rows[1]["verdict"]["initiate"] is False
    assert store.decisions_for_day(3) == []

    store.close()
