"""v2 -> v3 migration tests (iteration-2 A7).

Builds a database with the PRE-A7 schema (the CURRENT repo schema at the A7
base commit — v2 DDL embedded VERBATIM below, deliberately NOT imported from
harness.store), seeds it with messages/judgements/schedule/memory/persona,
then instantiates the new store: the v2 -> v3 migration must succeed, every
piece of legacy data must remain present and interpretable, the canonical L4
categories must be backfilled, and re-opening the same database (migration
runs twice) must be a no-op. No destructive migration.
"""

import sqlite3
from array import array

from harness.domain import UserModelAssertion, UserModelCategory
from harness.store import SCHEMA_VERSION, SQLiteStore

# Current (v2) repo schema, VERBATIM from harness/store.py at d71df14
# (v1 frozen base + v2 tables + the memory_turns view).
_V2_SCHEMA = """
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
    meta TEXT,
    session_id TEXT
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
    t_h      REAL    NOT NULL,        -- absolute virtual hour of the planned firing
    day      INTEGER NOT NULL,        -- int(t_h // 24)
    reason   TEXT    NOT NULL,        -- one of VALID_REASONS
    status   TEXT    NOT NULL DEFAULT 'pending',  -- 'pending' | 'fired' | 'expired'
    fired_t_h REAL,                   -- actual virtual hour it fired (may differ slightly)
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
"""


def _build_v2_db(path) -> None:
    """Create a database exactly as the current repo would after v2, and seed
    it across every table family (messages, judgements, schedule, memory
    tiers L1-L4, persona, arcs, agenda, intents, audit)."""
    conn = sqlite3.connect(path)
    conn.executescript(_V2_SCHEMA)
    conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_meta (version) VALUES (2)")
    # daily state
    conn.execute(
        "INSERT INTO daily_state (day, M, m_level, g, p, arg, mu, eta, "
        "cycle_day, phase_label, seed, score) "
        "VALUES (0, 7, 0.1, 1.0, 0.7, 0.8, 0.2, 0.1, 3.0, 'follicular', 42, 0.5)"
    )
    # messages (incl. session-scoped turn; NO intent_id column yet)
    conn.executemany(
        "INSERT INTO messages (role, content, t_h, day, proactive, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("user", "legacy hello", 0.5, 0, 0, None),
            ("assistant", "legacy reply", 0.7, 0, 0, None),
            ("assistant", "legacy proactive", 10.0, 0, 1, None),
            ("assistant", "session turn", 11.0, 0, 0, "s1"),
        ],
    )
    # judgements
    conn.execute(
        "INSERT INTO judgements (day, score, justification, model, shadow) "
        "VALUES (0, 0.8, 'legacy judgement', 'fake', 1)"
    )
    # state events + llm calls
    conn.execute(
        "INSERT INTO state_events (day, t_h, event, detail) "
        "VALUES (0, 0.5, 'day_rollover', 'M=7')"
    )
    conn.execute(
        "INSERT INTO llm_calls (day, t_h, role, model, prompt_hash, response, "
        "meta) VALUES (0, 1.0, 'chat', 'fake', 'abc123', 'legacy response', "
        "'{\"state_version\": 2}')"
    )
    # schedule (pending + fired)
    conn.executemany(
        "INSERT INTO schedule_events (seed, t_h, day, reason, status, fired_t_h) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (4242, 10.0, 0, "schedule", "pending", None),
            (4242, 26.5, 1, "schedule", "fired", 26.9),
        ],
    )
    # persona + interests
    conn.execute(
        "INSERT INTO persona (id, name, core, routines_json) VALUES (1, "
        "'Lily', 'Curious and warm.', '[{\"name\": \"morning walk\", "
        "\"start_frac\": 0.25, \"duration_h\": 1.0, \"cadence\": 0.9, "
        "\"salience\": 0.5}]')"
    )
    conn.executemany(
        "INSERT INTO interests (name, bucket, salience) VALUES (?, ?, ?)",
        [("photography", "exact", 0.9), ("jazz", "independent", 0.4)],
    )
    # life arcs + agenda + intents
    conn.execute(
        "INSERT INTO life_arcs (id, name, interest, started_day, progress, "
        "status, next_intention) VALUES ('a1', 'photography', 'photography', "
        "4, 0.37, 'active', 'practice portraits')"
    )
    conn.execute(
        "INSERT INTO agenda_items (id, day, start_t_h, end_t_h, activity, "
        "source_type, source_id, salience, status) VALUES ('i1', 4, 9.0, "
        "10.0, 'portraits', 'arc', 'a1', 0.8, 'planned')"
    )
    conn.execute(
        "INSERT INTO proactive_intents (id, reason, source_type, source_id, "
        "hook, created_t_h, valid_until_t_h, salience, evidence, status) "
        "VALUES ('p1', 'arc progress', 'life_arc', 'a1', 'progress', 100.0, "
        "104.0, 0.7, 'arc a1 at progress 0.37', 'active')"
    )
    # memory L1: session + turn
    conn.execute(
        "INSERT INTO memory_sessions (session_id, started_at_t_h, ended_at_t_h) "
        "VALUES ('s1', 0.0, 2.0)"
    )
    # memory L2: session summary
    conn.execute(
        "INSERT INTO memory_session_summaries (session_id, started_at_t_h, "
        "ended_at_t_h, summary, topics_json, user_facts_json, "
        "preference_updates_json, companion_events_json, "
        "relationship_events_json, callbacks_json, affect_observations_json, "
        "emotional_peak, importance, source_turn_ids_json) VALUES ('s1', 0.0, "
        "2.0, 'User introduced Bruno.', '[\"dogs\"]', '[\"dog named Bruno\"]', "
        "'[]', '[]', '[]', '[]', '[]', 1, 0.7, '[1, 2]')"
    )
    # memory L3: episode + normalized sources + embedding blob
    conn.execute(
        "INSERT INTO memory_episodes (id, summary, category, occurred_at_t_h, "
        "created_at_t_h, importance, access_count, last_accessed_t_h, "
        "affect_json, source_session_id, source_turn_ids_json, "
        "verbatim_anchors_json, tags_json) VALUES ('ep1', 'User''s dog is "
        "Bruno.', 'user_fact', 1.0, 2.0, 0.8, 0, NULL, NULL, 's1', '[1, 2]', "
        "'[\"my dog is Bruno\"]', '[\"dog\"]')"
    )
    conn.executemany(
        "INSERT INTO memory_episode_sources (episode_id, turn_id) VALUES (?, ?)",
        [("ep1", 1), ("ep1", 2)],
    )
    blob = array("f", [0.1, 0.2, 0.3]).tobytes()
    conn.execute(
        "INSERT INTO memory_embeddings (episode_id, vector, dim) VALUES (?, ?, 3)",
        ("ep1", blob),
    )
    # memory L4: assertions under BOTH documented key conventions, with a
    # superseded row (espresso -> flat white) exactly as upserts would leave it
    conn.executemany(
        "INSERT INTO user_model_assertions (key, value, confidence, "
        "updated_at_t_h, source_memory_ids_json, status) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("current_preferences:coffee", "espresso", 0.8, 1.0, '["e1"]',
             "superseded"),
            ("current_preferences:coffee", "flat white", 0.85, 5.0, '["e4"]',
             "current"),
            ("identity", "Bruno's human", 0.9, 2.0, '["e2"]', "current"),
            ("boundaries:late_night_chat", "avoid after 23h", 0.6, 3.0,
             '["e3"]', "current"),
            ("user:dog:name", "Bruno", 0.7, 4.0, '["ep1"]', "current"),
            ("relationship:mentor", "Ana", 0.7, 6.0, '["e5"]', "current"),
        ],
    )
    conn.commit()
    conn.close()


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "daily_state", "messages", "judgements", "state_events", "llm_calls",
        "schedule_events", "persona", "interests", "life_arcs",
        "agenda_items", "proactive_intents", "memory_sessions",
        "memory_session_summaries", "memory_episodes",
        "memory_episode_sources", "memory_embeddings",
        "user_model_assertions",
    ]
    return {
        t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()[0]
        for t in tables
    }


def test_v2_db_migrates_and_all_data_survives(tmp_path):
    db = tmp_path / "v2.db"
    _build_v2_db(db)
    before = _table_counts(sqlite3.connect(db))

    store = SQLiteStore(db)  # v2 -> v4 migration runs inside __init__

    # version bookkeeping: exactly one row, at the current version
    rows = store.conn.execute("SELECT * FROM schema_meta").fetchall()
    assert len(rows) == 1
    assert rows[0]["version"] == SCHEMA_VERSION == 4

    # M1: messages gained intent_id; legacy rows are NULL
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(messages)")}
    assert "intent_id" in cols
    assert cols >= {"id", "role", "content", "t_h", "day", "proactive", "meta",
                    "session_id"}

    # M2: assertions gained the canonical category column, all backfilled
    cols = {r["name"]
            for r in store.conn.execute("PRAGMA table_info(user_model_assertions)")}
    assert "category" in cols
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM user_model_assertions "
        "WHERE category IS NULL"
    ).fetchone()["n"] == 0

    # M3: llm_calls gained repro_json (NULL for legacy rows)
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(llm_calls)")}
    assert "repro_json" in cols

    # the memory_turns view exposes intent_id after the rebuild
    view_cols = [d[0] for d in store.conn.execute(
        "SELECT * FROM memory_turns LIMIT 0"
    ).description]
    assert "intent_id" in view_cols

    # --- verify ALL data -----------------------------------------------------
    msgs = store.recent_messages()
    assert [m["content"] for m in msgs] == [
        "legacy hello", "legacy reply", "legacy proactive", "session turn",
    ]
    assert all(m["intent_id"] is None for m in msgs)
    assert msgs[3]["session_id"] == "s1"
    assert store.proactive_count(0) == 1

    j = store.load_judgement(0)
    assert j is not None and j["score"] == 0.8 and j["shadow"] == 1
    assert store.load_previous_judgement(1) == 0.8

    state0 = store.load_daily_state(0)
    assert state0 is not None and state0["M"] == 7
    assert store.events_since(0)[0]["event"] == "day_rollover"

    calls = store.conn.execute("SELECT * FROM llm_calls").fetchall()
    assert len(calls) == 1
    assert calls[0]["prompt_hash"] == "abc123"
    assert calls[0]["response"] == "legacy response"
    assert calls[0]["repro_json"] is None

    sched = store.schedule_events_for_seed(4242)
    assert [(s["t_h"], s["status"]) for s in sched] == [
        (10.0, "pending"), (26.5, "fired"),
    ]
    assert store.last_proactive_t_h(4242) == 26.9

    persona = store.load_persona()
    assert persona is not None and persona.name == "Lily"
    assert [i.name for i in persona.interests] == ["jazz", "photography"]
    assert persona.routines[0].name == "morning walk"
    arc = store.get_life_arc("a1")
    assert arc is not None and arc.progress == 0.37
    agenda = store.load_agenda(4)
    assert agenda is not None and agenda.items[0].activity == "portraits"
    intent = store.load_proactive_intent("p1")
    assert intent is not None and intent.hook == "progress"
    assert store.resolve_intent_source(intent) is not None

    summary = store.load_session_summary("s1")
    assert summary is not None and summary.summary == "User introduced Bruno."
    assert summary.source_turn_ids == (1, 2)
    episode = store.get_episode("ep1")
    assert episode is not None and episode.summary == "User's dog is Bruno."
    assert store.list_episode_sources("ep1") == [1, 2]
    vec = store.load_embeddings()[0][1]
    assert len(vec) == 3
    assert all(abs(v - want) < 1e-6 for v, want in zip(vec, [0.1, 0.2, 0.3]))

    # canonical L4 categories backfilled from BOTH documented key conventions
    assert store.get_assertion_category("current_preferences:coffee") is \
        UserModelCategory.CURRENT_PREFERENCE
    assert store.get_assertion_category("identity") is \
        UserModelCategory.IDENTITY
    assert store.get_assertion_category("boundaries:late_night_chat") is \
        UserModelCategory.BOUNDARY
    # memory.py convention keys: documented semantic mapping, not data loss
    assert store.get_assertion_category("user:dog:name") is \
        UserModelCategory.IMPORTANT_ENTITY
    assert store.get_assertion_category("relationship:mentor") is \
        UserModelCategory.RELATIONSHIP_PATTERN

    # load_user_model buckets canonically; superseded provenance intact
    model = store.load_user_model()
    assert model.identity == "Bruno's human"
    assert [a.value for a in model.current_preferences] == ["flat white"]
    assert [a.key for a in model.boundaries] == ["boundaries:late_night_chat"]
    assert [a.key for a in model.important_entities] == ["user:dog:name"]
    assert [a.key for a in model.relationship_patterns] == ["relationship:mentor"]
    superseded = store.list_assertions(status="superseded")
    assert [a.key for a in superseded] == ["current_preferences:coffee"]
    assert superseded[0].value == "espresso"  # provenance kept

    # no destructive transformations: identical row counts on every table
    after = _table_counts(store.conn)
    assert after == before

    store.close()


def test_migration_runs_twice_without_error(tmp_path):
    db = tmp_path / "v2b.db"
    _build_v2_db(db)

    store1 = SQLiteStore(db, audit_mode=True)
    # write through the v3 schema after migration
    mid = store1.add_message("assistant", "post-migration", t_h=12.0, day=0,
                             proactive=True, intent_id="intent-87")
    store1.upsert_assertion(
        UserModelAssertion("vuln:x", "y", 0.5, 7.0, (), "current"),
        category=UserModelCategory.VULNERABILITY,
    )
    call_id = store1.log_llm_call(0, 12.0, "chat", "p", "r", "fake",
                                  repro={"model": "fake", "temperature": 0.7})
    rows_after_write = store1.conn.execute(
        "SELECT COUNT(*) AS n FROM messages"
    ).fetchone()["n"]
    store1.close()

    # re-open: migration must be a no-op, not an error, no duplicate rows
    store2 = SQLiteStore(db, audit_mode=True)
    rows = store2.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION
    assert store2.conn.execute(
        "SELECT COUNT(*) AS n FROM messages"
    ).fetchone()["n"] == rows_after_write

    # both legacy and post-migration data are intact
    msgs = store2.recent_messages()
    assert [m["content"] for m in msgs] == [
        "legacy hello", "legacy reply", "legacy proactive", "session turn",
        "post-migration",
    ]
    assert msgs[-1]["intent_id"] == "intent-87"
    assert msgs[-1]["id"] == mid
    assert store2.turns_for_session("s1")[0]["content"] == "session turn"
    assert store2.get_assertion_category("vuln:x") is \
        UserModelCategory.VULNERABILITY
    repro_call = store2.get_llm_call(call_id)
    assert repro_call is not None and repro_call["repro"] == {
        "model": "fake", "temperature": 0.7,
    }
    state0 = store2.load_daily_state(0)
    assert state0 is not None and state0["score"] == 0.5
    j0 = store2.load_judgement(0)
    assert j0 is not None and j0["score"] == 0.8
    assert len(store2.schedule_events_for_seed(4242)) == 2
    # canonical categories still backfilled, nothing re-run or duplicated
    assert store2.get_assertion_category("identity") is \
        UserModelCategory.IDENTITY
    assert len(store2.list_assertions(status="current")) == 6
    store2.close()


def test_fresh_db_reaches_v3_with_columns_and_new_apis(tmp_path):
    store = SQLiteStore(tmp_path / "fresh.db", audit_mode=True)
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION == 4

    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(messages)")}
    assert {"id", "role", "content", "t_h", "day", "proactive", "meta",
            "session_id", "intent_id"} <= cols
    cols = {r["name"]
            for r in store.conn.execute("PRAGMA table_info(user_model_assertions)")}
    assert "category" in cols
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(llm_calls)")}
    assert "repro_json" in cols

    # the new surface works end to end on a fresh database
    store.add_message("assistant", "spontaneous", t_h=1.0, day=0,
                      proactive=True, intent_id="i1")
    assert store.recent_messages()[0]["intent_id"] == "i1"
    store.upsert_assertion(
        UserModelAssertion("x:y", "z", 0.6, 1.0, (), "current"),
        category=UserModelCategory.RECURRING_INTEREST,
    )
    assert store.get_assertion_category("x:y") is \
        UserModelCategory.RECURRING_INTEREST
    cid = store.log_llm_call(0, 1.0, "chat", "p", "r", "m",
                             repro={"seed": 1})
    repro_call = store.get_llm_call(cid)
    assert repro_call is not None and repro_call["repro"] == {"seed": 1}
    store.close()
