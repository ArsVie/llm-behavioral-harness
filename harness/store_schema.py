"""SQLite schema, DDL and the versioned migration chain (§4b of the
thermo-review follow-up).

Lifted VERBATIM out of ``harness.store``. Two properties make this a
contract rather than ordinary code, and both are why the strings below must
not be "tidied":

* The migration chain is ADDITIVE and ORDER-DEPENDENT. ``_migrate`` walks
  v1 -> v8 in sequence; each step assumes exactly what the previous one
  left. Reordering or merging steps changes what an existing database
  becomes.
* The DDL strings are byte-significant. A live database was created by THIS
  text; a reformatted CREATE TABLE can silently produce a different column
  order or affinity, which the migration tests
  (test_store_migrations*.py) exist to catch.

``SCHEMA_VERSION`` lives here and ``harness.store`` re-exports it, so
``harness.store.SCHEMA_VERSION`` keeps working.
"""

from __future__ import annotations

import sqlite3

from harness.domain import UserModelCategory

SCHEMA_VERSION = 8

# --- v1 base schema ---
_SCHEMA = """
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
    t_h      REAL    NOT NULL,        -- absolute virtual hour of the planned firing
    day      INTEGER NOT NULL,        -- int(t_h // 24)
    reason   TEXT    NOT NULL,        -- one of VALID_REASONS
    status   TEXT    NOT NULL DEFAULT 'pending',  -- 'pending' | 'fired' | 'expired'
    fired_t_h REAL,                   -- actual virtual hour it fired (may differ slightly)
    UNIQUE(seed, t_h)
);
CREATE INDEX IF NOT EXISTS idx_schedule_events_seed_status
    ON schedule_events(seed, status);
"""

# --- schema_meta bookkeeping ---
_SCHEMA_META = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def schema_meta(version: int) -> str:
    """Return the DDL for the ``schema_meta`` bookkeeping table.

    ``version`` is the schema version the store targets (``SCHEMA_VERSION``);
    the migration framework records it as a row in this table once the
    database has been brought up to it. A fresh database starts with the v1
    base tables only (effective version 1), so the version row is never
    written ahead of the migration.
    """
    if version < 1:
        raise ValueError(f"schema version must be >= 1, got {version}")
    return _SCHEMA_META


# --- Migration v1 -> v2 (additive only) ---
_V2_TABLES = """
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
"""

# memory_turns is the session-scoped read view over L1 turns in messages.
_V2_VIEWS = """
CREATE VIEW IF NOT EXISTS memory_turns AS
SELECT id, session_id, role, content, t_h, day, proactive, meta
FROM messages
WHERE session_id IS NOT NULL;
"""

def _current_version(conn: sqlite3.Connection) -> int:
    """Highest recorded schema version; 1 when the meta table is absent or
    empty (the legacy base schema)."""
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_meta").fetchone()
    except sqlite3.OperationalError:
        return 1
    v = row["v"] if row is not None else None
    return int(v) if v is not None else 1


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    """Add a column if missing (PRAGMA-guarded ALTER TABLE)."""
    cols = {
        r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v1 -> v2: new tables + additive messages.session_id + memory_turns view."""
    conn.executescript(_V2_TABLES)
    _ensure_column(conn, "messages", "session_id", "TEXT")
    conn.executescript(_V2_VIEWS)


# Migration v2 -> v3 (additive): intent_id, category, repro_json.
_V3_VIEWS = """
CREATE VIEW memory_turns AS
SELECT id, session_id, intent_id, role, content, t_h, day, proactive, meta
FROM messages
WHERE session_id IS NOT NULL;
"""

# Legacy assertion-key prefixes mapping to canonical categories for pre-v3
# rows and callers that do not pass the enum explicitly.
_LEGACY_PREFIX_CATEGORIES = (
    ("stable_preferences", UserModelCategory.STABLE_PREFERENCE),
    ("current_preferences", UserModelCategory.CURRENT_PREFERENCE),
    ("preference", UserModelCategory.CURRENT_PREFERENCE),
    ("boundaries", UserModelCategory.BOUNDARY),
    ("boundary", UserModelCategory.BOUNDARY),
    ("vulnerabilities", UserModelCategory.VULNERABILITY),
    ("vulnerability", UserModelCategory.VULNERABILITY),
    ("recurring_interests", UserModelCategory.RECURRING_INTEREST),
    ("interest", UserModelCategory.RECURRING_INTEREST),
    ("relationship_patterns", UserModelCategory.RELATIONSHIP_PATTERN),
    ("relationship", UserModelCategory.RELATIONSHIP_PATTERN),
    ("important_entities", UserModelCategory.IMPORTANT_ENTITY),
    ("entity", UserModelCategory.IMPORTANT_ENTITY),
)


def _category_from_key(key: str) -> UserModelCategory:
    """Canonical category for a legacy key (documented prefixes only).

    Compatibility derivation for rows written before v3 and for callers that
    do not pass ``category`` explicitly. Keys without a documented prefix
    surface under ``IMPORTANT_ENTITY`` (the legacy load default). This is a
    WRITE-time / migration-time mapping; the load path reads the stored
    ``category`` column and never parses keys.
    """
    head, _, _ = key.partition(":")
    if key == "identity" or head == "identity":
        return UserModelCategory.IDENTITY
    for prefix, cat in _LEGACY_PREFIX_CATEGORIES:
        if head == prefix:
            return cat
    return UserModelCategory.IMPORTANT_ENTITY


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v2 -> v3: additive A7 columns + canonical L4 backfill + view rebuild."""
    _ensure_column(conn, "messages", "intent_id", "TEXT")
    _ensure_column(conn, "user_model_assertions", "category", "TEXT")
    _ensure_column(conn, "llm_calls", "repro_json", "TEXT")
    # Backfill the canonical category for rows with NULL; only the new column
    # is written.
    rows = conn.execute(
        "SELECT seq, key FROM user_model_assertions WHERE category IS NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE user_model_assertions SET category = ? WHERE seq = ?",
            (_category_from_key(row["key"]).value, row["seq"]),
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_model_assertions_category "
        "ON user_model_assertions(category)"
    )
    conn.execute("DROP VIEW IF EXISTS memory_turns")
    conn.executescript(_V3_VIEWS)


# Migration v3 -> v4 (additive): conversation tables + conversation_id.
_V4_TABLES = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    opened_t_h REAL NOT NULL,
    closed_t_h REAL,
    opened_by TEXT NOT NULL,   -- 'user' | 'companion'
    close_reason TEXT          -- 'closing_tendency' | 'user_left'
                               -- | 'quiet_hours' | 'max_turns'
);
CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    speaker TEXT NOT NULL,                -- 'user' | 'companion'
    text TEXT NOT NULL,
    t_h REAL NOT NULL,
    turn_index INTEGER NOT NULL,          -- 0-based within the conversation
    message_id INTEGER,                   -- links to messages.id (provenance)
    UNIQUE (conversation_id, turn_index)
);
CREATE INDEX IF NOT EXISTS idx_conversation_turns_conversation
    ON conversation_turns(conversation_id);
"""


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v3 -> v4: additive conversation tables + messages.conversation_id."""
    conn.executescript(_V4_TABLES)
    _ensure_column(conn, "messages", "conversation_id", "TEXT")


# Migration v4 -> v5 (additive): decision_records + steering_queue.
_V5_TABLES = """
CREATE TABLE IF NOT EXISTS decision_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day INTEGER NOT NULL,
    t_h REAL NOT NULL,
    popup_kind TEXT NOT NULL,          -- 'tool_decide_event' | 'tool_decide_reply'
    event_id TEXT,
    event_label TEXT,
    state_label TEXT,
    time TEXT,
    inputs_json TEXT,                  -- the drawn pop-up inputs, verbatim
    raw_reply TEXT,                    -- the RAW model output (dual persistence)
    verdict_json TEXT,                 -- the parsed verdict (dual persistence)
    source TEXT NOT NULL,              -- 'model' | 'server_draw'
    transport TEXT NOT NULL,           -- 'native' | 'textual' | 'server_draw'
                                       -- | 'server_draw_fallback'
    delivered_t_h REAL,
    budget_consumed INTEGER NOT NULL DEFAULT 0,
    replay_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_records_day
    ON decision_records(day);
CREATE INDEX IF NOT EXISTS idx_decision_records_replay
    ON decision_records(replay_id);
CREATE TABLE IF NOT EXISTS steering_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day INTEGER NOT NULL,
    t_h REAL NOT NULL,                 -- enqueue time (virtual hour)
    kind TEXT NOT NULL,                -- e.g. 'popup' | 'user_message' | 'schedule'
    payload_json TEXT NOT NULL DEFAULT '{}',
    delivered_t_h REAL,                -- actual delivery time (summary #23)
    boundary TEXT,                     -- 'idle' | 'after_tool' | 'after_reply'
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'delivered'
    seen_turn_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_steering_queue_status
    ON steering_queue(status);
CREATE INDEX IF NOT EXISTS idx_steering_queue_day
    ON steering_queue(day);
"""


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """v4 -> v5: additive decision_records + steering_queue tables."""
    conn.executescript(_V5_TABLES)


# Migration v5 -> v6 (additive): kv_store + closing_pending_t_h.
_V6_TABLES = """
CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """v5 -> v6: additive kv_store table + conversations.closing_pending_t_h."""
    conn.executescript(_V6_TABLES)
    _ensure_column(conn, "conversations", "closing_pending_t_h", "REAL")


# Migration v6 -> v7 (additive): nullable REAL timestamp columns.
_V7_COLUMNS = (
    ("conversations", "opened_at", "REAL"),
    ("conversations", "closed_at", "REAL"),
    ("agenda_items", "start_at", "REAL"),
    ("agenda_items", "end_at", "REAL"),
    ("proactive_intents", "created_at", "REAL"),
    ("proactive_intents", "valid_until_at", "REAL"),
    ("messages", "sent_at", "REAL"),
)


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """v6 -> v7: additive nullable REAL timestamp columns (S1 real time)."""
    for table, column, decl in _V7_COLUMNS:
        _ensure_column(conn, table, column, decl)


# Migration v7 -> v8 (additive): usage, lane and raw_cost columns.
_V8_COLUMNS = (
    ("llm_calls", "prompt_tokens", "INTEGER"),
    ("llm_calls", "completion_tokens", "INTEGER"),
    ("llm_calls", "total_tokens", "INTEGER"),
    ("llm_calls", "cached_tokens", "INTEGER"),
    ("llm_calls", "cache_miss_tokens", "INTEGER"),
    ("llm_calls", "lane", "TEXT"),
    ("llm_calls", "raw_cost", "REAL"),
)


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """v7 -> v8: additive llm_calls usage/lane/raw_cost columns (WS-D)."""
    for table, column, decl in _V8_COLUMNS:
        _ensure_column(conn, table, column, decl)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring the schema up to SCHEMA_VERSION with additive migrations only.

    Version-gated: each migration runs at most once per database (the
    bookkeeping row is written only after the migration completes, so a crash
    mid-migration re-runs it safely — every step is idempotent). After the
    chain completes, bookkeeping collapses to a single row at the current
    version (the pre-slice invariant: exactly one version row).
    """
    version = _current_version(conn)
    if version < 2:
        _migrate_v2(conn)
    if version < 3:
        _migrate_v3(conn)
    if version < 4:
        _migrate_v4(conn)
    if version < 5:
        _migrate_v5(conn)
    if version < 6:
        _migrate_v6(conn)
    if version < 7:
        _migrate_v7(conn)
    if version < 8:
        _migrate_v8(conn)
    if version < SCHEMA_VERSION:
        conn.execute("DELETE FROM schema_meta")
        conn.execute(
            "INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,)
        )
    conn.commit()


def _usage_columns(usage) -> tuple:
    """Normalize a parsed usage object into the five llm_calls token columns.

    Accepts a ``harness.client.Usage`` (attribute access) or a plain dict
    with the same keys; anything else (or None) yields all-NULLs. Never
    raises on malformed shapes — usage capture is best-effort.
    """
    if usage is None:
        return (None, None, None, None, None)
    if isinstance(usage, dict):
        get = lambda k: usage.get(k)  # noqa: E731
    else:
        get = lambda k: getattr(usage, k, None)  # noqa: E731
    return (
        get("prompt_tokens"),
        get("completion_tokens"),
        get("total_tokens"),
        get("cached_tokens"),
        get("cache_miss_tokens"),
    )
