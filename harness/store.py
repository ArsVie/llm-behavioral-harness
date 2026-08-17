"""SQLite persistence for the harness (W-E1) — schema-versioned (vertical slice A2).

Pattern follows Hermes session storage: single SQLite file, WAL mode, append-
only trace tables alongside canonical state tables. Canonical tables hold the
current truth (daily_state, messages, judgements); `state_events` and
`llm_calls` are the audit/replay log (model, prompt hash, seed, clock time,
state version recorded per call).

Schema versioning (A2 + A7)
---------------------------
The pre-slice schema is version 1 and is frozen verbatim in ``_SCHEMA``; it is
executed with ``CREATE TABLE IF NOT EXISTS`` on every open (legacy behavior,
idempotent). ``schema_meta(version)`` creates the ``schema_meta`` bookkeeping
table; the migration framework (``_migrate``) reads the recorded version and
applies **additive** migrations to reach ``SCHEMA_VERSION`` (currently 8).
Migrations never drop or alter existing columns; all ``ALTER TABLE`` steps are
guarded by ``PRAGMA table_info``. On a fresh database the effective version is
1 (only the v1 base tables exist), so the migration chain runs and the
bookkeeping collapses to a single row at the current version. Reopening a
migrated database sees the current version and skips the migration
(idempotent; safe to run twice).

Migration v1 -> v2 adds: ``session_id`` on ``messages`` (A5 needs L1 session
scoping) and the vertical-slice tables below.

Migration v2 -> v3 (A7) adds:
  - ``messages.intent_id`` (NULLABLE TEXT) — proactive provenance: the exact
    validated intent id that produced an outgoing message (invariant 6);
    reactive messages keep it NULL.
  - ``user_model_assertions.category`` — the canonical L4 taxonomy
    (``UserModelCategory`` value) stored DIRECTLY on every assertion row.
    Legacy rows are backfilled once from the documented key-prefix
    conventions; loads never re-infer categories from keys.
  - ``llm_calls.repro_json`` — call-reproducibility audit payload (JSON) for
    eval mode: exact request/response fields needed to reproduce a call.
    Production privacy default: not logged (configurable via ``audit_mode``).

Migration v4 -> v5 (runtime redesign WS2) adds:
  - ``decision_records`` — one row per pop-up decision (tool_decide_event /
    tool_decide_reply): the drawn pop-up inputs (JSON), the RAW model reply,
    the parsed verdict (JSON), source (model|server_draw), transport
    (native|textual|server_draw|server_draw_fallback), budget consumption
    and the ``replay_id`` natural key. Replay reads the recorded verdict —
    it never re-rolls.
  - ``steering_queue`` — pending arriving events (pop-ups due, user messages
    mid-turn, schedule fires) awaiting delivery at the next safe boundary;
    ``status`` is 'pending' | 'delivered', delivery records the actual
    ``delivered_t_h``/``boundary``/``seen_turn_id`` (summary #23). The
    enqueue/pending/mark/requeue methods are the WS3 steering backend
    contract.

Migration v6 -> v7 (S1 real time, additive only) adds nullable REAL
timestamp columns holding the UTC epoch instant resolved via the
RealTimeAnchor at row-creation time:
  - ``conversations.opened_at`` / ``conversations.closed_at``
  - ``agenda_items.start_at`` / ``agenda_items.end_at``
  - ``proactive_intents.created_at`` / ``proactive_intents.valid_until_at``
  - ``messages.sent_at``
NULL = no anchor present (pre-anchor / replay rows) — replay parity is
preserved. No backfill, no NOT NULL, no defaults, no new indexes: purely
additive, safe on the populated live database. The tz name already lives
in the anchor (kv_store ``anchor.tz``), so the columns store the instant
only.

Tables (slice scope of the plan's data model):
  - daily_state(day PK, M, m_level, g, p, arg, mu, eta, cycle_day, phase_label,
    seed, score)                       -- FROZEN legacy table, untouched
  - messages(id PK, role, content, t_h, day, proactive, meta, session_id)
  - judgements(day PK, score, justification, model, shadow)
  - state_events(id PK, day, t_h, event, detail)
  - llm_calls(id PK, day, t_h, role, model, prompt_hash, response, meta) —
    v8 (WS-D) adds the spend ledger: prompt_tokens, completion_tokens,
    total_tokens, cached_tokens, cache_miss_tokens, lane, raw_cost (all
    nullable; legacy rows NULL)
  - schedule_events(id PK, seed, t_h, day, reason, status, fired_t_h)
  - persona(id=1 singleton, name, core, routines_json)   -- routines as JSON
  - interests(name PK, bucket, salience)                 -- portfolio bucket
  - life_arcs(id PK, name, interest, started_day, progress, status,
    next_intention)
  - agenda_items(id PK, day, start_t_h, end_t_h, activity, source_type,
    source_id, salience, status)
  - proactive_intents(id PK, reason, source_type, source_id, hook,
    created_t_h, valid_until_t_h, salience, evidence, status)
  - memory_sessions(session_id PK, started_at_t_h, ended_at_t_h)
  - memory_turns: **VIEW over messages** (rows with a session_id) — L1 turns
    are stored in the existing `messages` table (reused deliberately: one
    append-only conversation log, no dual-write); the view gives the
    session-scoped read shape without duplicating rows.
  - memory_session_summaries(session_id PK, summary, <tuples as JSON>,
    emotional_peak, importance, source_turn_ids_json)
  - memory_episodes(id PK, summary, category, occurred_at_t_h, created_at_t_h,
    importance, access_count, last_accessed_t_h, affect_json,
    source_session_id, source_turn_ids_json, verbatim_anchors_json, tags_json)
  - memory_episode_sources(episode_id, turn_id, PK(episode_id, turn_id))
    -- normalized provenance links episode -> exact turn ids
  - user_model_assertions(seq PK, key, value, confidence, updated_at_t_h,
    source_memory_ids_json, status)
  - memory_embeddings(episode_id PK, vector BLOB, dim)   -- local BLOB
    embeddings, brute-force cosine at retrieval (no vector DB)
  - decision_records(id PK, day, t_h, popup_kind, event_id, event_label,
    state_label, time, inputs_json, raw_reply, verdict_json, source,
    transport, delivered_t_h, budget_consumed, replay_id)  -- v5: one row
    per pop-up decision; raw reply AND parsed verdict (dual persistence)
  - steering_queue(id PK, day, t_h, kind, payload_json, delivered_t_h,
    boundary, status, seen_turn_id)   -- v5: pending arriving events (WS3
    backend contract)
  - kv_store(key PK, value)   -- v6: generic key/value table (seam S1);
    Wave-2 real-time anchor persists under keys ``anchor.epoch0_s`` /
    ``anchor.t_h0`` / ``anchor.tz``; ``conversations`` gains the nullable
    ``closing_pending_t_h`` column (v6: NULL = no wind-down pending).

Conventions (no business logic lives here — pure persistence + simple queries)
-------------------------------------------------------------------------------
* Tuple fields are stored as JSON arrays in ``*_json`` columns; bools as 0/1
  ints; ``AffectMetadata`` as a JSON dict. Reconstruction is exact.
* ``resolve_intent_source`` maps ``ProactiveIntent.source_type``:
  ``"agenda_item"`` -> AgendaItem, ``"life_arc"`` -> LifeArc,
  ``"episodic_memory"`` -> EpisodicMemory, anything else -> None.
* ``upsert_assertion`` implements the L4 rule "new evidence updates the
  model": inserting a ``current`` assertion flips the previous ``current``
  row of the same key to ``superseded`` (provenance kept, no deletion).
* ``load_user_model`` groups current assertions by their key prefix
  ``"<group>:<name>"`` where ``<group>`` is one of the seven UserModel group
  names (or exactly ``"identity"``); ungrouped keys surface under
  ``important_entities``.

All writes go through `conn` transactions; reads are plain SELECTs. No
secrets are stored (credentials stay in the environment).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from array import array
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from harness.domain import (
    AffectMetadata,
    AgendaItem,
    Conversation,
    ConversationTurn,
    DailyAgenda,
    EpisodicMemory,
    Interest,
    LifeArc,
    MemoryKind,
    PersonaProfile,
    ProactiveIntent,
    Routine,
    SessionSummary,
    UserModel,
    UserModelAssertion,
    UserModelCategory,
)

SCHEMA_VERSION = 8

# --------------------------------------------------------------------------- #
# v1 base schema — FROZEN verbatim from the pre-slice store (do not edit).
# --------------------------------------------------------------------------- #
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

# --------------------------------------------------------------------------- #
# schema_meta bookkeeping
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Migration v1 -> v2 (additive only)
# --------------------------------------------------------------------------- #
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

# L1 turns live in `messages` (see module docstring); memory_turns is the
# session-scoped read shape over them.
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


# --------------------------------------------------------------------------- #
# Migration v2 -> v3 (A7, additive only)
# --------------------------------------------------------------------------- #
# v3 adds the A7 persistence columns: messages.intent_id (proactive
# provenance), user_model_assertions.category (canonical L4 storage), and
# llm_calls.repro_json (call-reproducibility audit). The memory_turns view is
# recreated to expose intent_id (SQLite views are not updated by ALTER TABLE).
_V3_VIEWS = """
CREATE VIEW memory_turns AS
SELECT id, session_id, intent_id, role, content, t_h, day, proactive, meta
FROM messages
WHERE session_id IS NOT NULL;
"""

# Documented legacy assertion-key prefixes (the store's group-name convention
# AND harness.memory's subject convention), used ONLY to derive the canonical
# category for rows written before v3 and for callers that do not pass the
# enum explicitly. The stored column is always the canonical value.
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
    # Backfill the canonical category for every existing assertion row
    # (legacy rows have NULL; new rows always carry a value). Non-destructive:
    # only the new column is written.
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


# --------------------------------------------------------------------------- #
# Migration v3 -> v4 (it3 B2, additive only): conversation persistence
# --------------------------------------------------------------------------- #
# v4 adds the conversation seam (module invariant 8): ``conversations`` +
# ``conversation_turns`` are the dialogue unit that memory sessions, judge
# sampling and relational metrics key off, and ``messages`` gains the
# additive ``conversation_id`` linkage column (NULL for pre-v4 rows).
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


# --------------------------------------------------------------------------- #
# Migration v4 -> v5 (runtime redesign WS2, additive only): decisions +
# steering queue
# --------------------------------------------------------------------------- #
# v5 adds the decision layer persistence: ``decision_records`` (one row per
# pop-up decision — drawn inputs, raw reply, parsed verdict, source,
# transport, budget, replay natural key) and ``steering_queue`` (pending
# arriving events awaiting delivery at the next safe boundary; the WS3
# steering backend contract). Both are pure additive tables; no existing
# table or column is touched.
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


# --------------------------------------------------------------------------- #
# Migration v5 -> v6 (W-close, additive only): kv_store + wind-down column
# --------------------------------------------------------------------------- #
# v6 adds the two-phase-close persistence (seam S1): ``kv_store`` — a generic
# key/value table (INSERT OR REPLACE semantics; the Wave-2 real-time anchor
# persists under keys ``anchor.epoch0_s``/``anchor.t_h0``/``anchor.tz``) —
# and the nullable ``conversations.closing_pending_t_h`` column (NULL = no
# wind-down pending; a real value = the closing draw fired and the
# conversation is in its wind-down grace window). Purely additive: a new
# table plus one guarded nullable column; no existing table or column is
# touched, so the migration is safe on a populated live database.
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


# --------------------------------------------------------------------------- #
# Migration v6 -> v7 (S1 real time, additive only): nullable real timestamps
# --------------------------------------------------------------------------- #
# v7 adds nullable REAL columns holding the UTC epoch instant resolved via
# the RealTimeAnchor at row-creation time, alongside each virtual-hour
# column: conversations.opened_at/closed_at, agenda_items.start_at/end_at,
# proactive_intents.created_at/valid_until_at, messages.sent_at. NULL = no
# anchor present (pre-anchor / replay rows) — replay parity is preserved.
# No backfill, no NOT NULL, no defaults, no new indexes: purely additive,
# safe on the populated live database. The tz name already lives in the
# anchor (kv_store ``anchor.tz``), so the columns store the instant only.
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


# --------------------------------------------------------------------------- #
# Migration v7 -> v8 (WS-D spend accounting, additive only): usage on llm_calls
# --------------------------------------------------------------------------- #
# v8 adds the token-usage ledger columns to ``llm_calls`` so every logged
# generation call carries its spend: prompt/completion/total tokens, the
# cache split (cached vs miss — whichever cache-field variant the gateway
# returned, or the all-miss default when no split surfaced), the calling
# lane ("product" | "research", WS-C attribution) and the gateway-reported
# cost in USD (raw_cost, discovery 2026-08-16 — cross-check for G-cost).
# ``model`` already exists on llm_calls since v1. All columns nullable with
# no defaults and no backfill: legacy rows stay NULL and the replay path is
# byte-identical (usage capture is an ADDITION, not a change to prompt/
# reply handling).
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


def _hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def _json(value) -> str | None:
    """Encode a tuple/list/dataclass structure as a JSON string."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    return json.dumps(value)


def _unjson(raw: str | None, default):
    if not raw:
        return default
    return json.loads(raw)


# UserModel group names (assertion key prefix convention "<group>:<name>").
_USER_MODEL_GROUPS = (
    "stable_preferences",
    "current_preferences",
    "boundaries",
    "vulnerabilities",
    "recurring_interests",
    "relationship_patterns",
    "important_entities",
)

# Canonical UserModelCategory -> UserModel group field (invariant 10: the
# taxonomy is defined once in domain.py; the store only projects it).
_CATEGORY_TO_GROUP = {
    UserModelCategory.STABLE_PREFERENCE: "stable_preferences",
    UserModelCategory.CURRENT_PREFERENCE: "current_preferences",
    UserModelCategory.BOUNDARY: "boundaries",
    UserModelCategory.VULNERABILITY: "vulnerabilities",
    UserModelCategory.RECURRING_INTEREST: "recurring_interests",
    UserModelCategory.RELATIONSHIP_PATTERN: "relationship_patterns",
    UserModelCategory.IMPORTANT_ENTITY: "important_entities",
}


class SQLiteStore:
    """Thin wrapper over sqlite3 with the versioned harness schema."""

    def __init__(self, path: str | Path, *, audit_mode: bool = False):
        """Open (creating if needed) the harness database.

        ``audit_mode`` enables eval-mode call reproducibility: when True,
        ``log_llm_call(..., repro=...)`` persists the exact request/response
        payload (model, temperature, max_tokens, seed, system context, message
        payload, generation controls, memory policy, intent id, snapshot refs,
        timestamp, response) as JSON. Default False = production privacy:
        only the prompt hash is logged (privacy-configurable, plan §5-A7 M3).
        """
        self.path = str(path)
        self.audit_mode = bool(audit_mode)
        self._anchor = None
        self.conn = sqlite3.connect(self.path, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(_SCHEMA)  # v1 base: idempotent on every open
        self.conn.executescript(schema_meta(SCHEMA_VERSION))  # bookkeeping
        _migrate(self.conn)  # version-gated, additive, completes in __init__
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- real-time anchor (S1) ----------------------------------------------

    def attach_anchor(self, anchor) -> None:
        """Attach the RealTimeAnchor used to resolve real timestamps (S1).

        Optional and additive: with no anchor attached (the default) every
        new ``*_at`` column stays NULL — byte-identical to the pre-v7 write
        path (replay parity). ``anchor`` must expose
        ``real_at(t_h) -> aware datetime`` (the ``RealTimeAnchor``
        contract); its ``timestamp()`` is stored as UTC epoch seconds.
        """
        self._anchor = anchor

    def _real_at(self, t_h: float) -> float | None:
        """UTC epoch seconds of virtual hour ``t_h`` per the attached anchor.

        None when no anchor is attached — all ``*_at`` columns stay NULL
        (pre-anchor / replay rows).
        """
        if self._anchor is None:
            return None
        return self._anchor.real_at(t_h).timestamp()

    # -- canonical state ----------------------------------------------------

    def save_daily_state(self, day: int, record: dict) -> None:
        # SQLite column names are case-insensitive: "m" collides with "M",
        # so the offset column is stored as m_level and mapped at this boundary.
        required = {"day", "M", "m", "g", "p", "arg", "mu", "eta", "cycle_day",
                    "phase_label", "seed"}
        missing = required - set(record)
        if missing:
            raise ValueError(f"daily_state record missing keys: {sorted(missing)}")
        row = dict(record)
        row["m_level"] = row.pop("m")
        self.conn.execute(
            """
            INSERT INTO daily_state (day, M, m_level, g, p, arg, mu, eta,
                                     cycle_day, phase_label, seed, score)
            VALUES (:day, :M, :m_level, :g, :p, :arg, :mu, :eta, :cycle_day,
                    :phase_label, :seed, :score)
            ON CONFLICT(day) DO UPDATE SET
                M=excluded.M, m_level=excluded.m_level, g=excluded.g,
                p=excluded.p, arg=excluded.arg, mu=excluded.mu,
                eta=excluded.eta, cycle_day=excluded.cycle_day,
                phase_label=excluded.phase_label, seed=excluded.seed,
                score=excluded.score
            """,
            row,
        )
        self.conn.commit()

    def _row_to_record(self, row: dict) -> dict:
        record = dict(row)
        record["m"] = record.pop("m_level")
        return record

    def load_daily_state(self, day: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM daily_state WHERE day = ?", (day,)
        ).fetchone()
        return self._row_to_record(dict(row)) if row else None

    def latest_daily_state(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM daily_state ORDER BY day DESC LIMIT 1"
        ).fetchone()
        return self._row_to_record(dict(row)) if row else None

    def update_daily_score(self, day: int, score: float) -> None:
        """Set the score column of one day without a read-modify-write."""
        self.conn.execute(
            "UPDATE daily_state SET score = ? WHERE day = ?", (score, day)
        )
        self.conn.commit()

    # -- messages -----------------------------------------------------------

    def add_message(
        self,
        role: str,
        content: str,
        t_h: float,
        day: int,
        proactive: bool = False,
        session_id: str | None = None,
        intent_id: str | None = None,
        conversation_id: str | None = None,
    ) -> int:
        """Append a message; ``session_id`` (A5 L1 session scoping),
        ``intent_id`` (A7 proactive provenance — the exact validated intent
        id that produced an outgoing message, invariant 6; reactive messages
        keep it None) and ``conversation_id`` (it3 B2 conversation linkage,
        module invariant 8) are optional and backward compatible with all
        pre-slice callers."""
        cur = self.conn.execute(
            "INSERT INTO messages (role, content, t_h, day, proactive, "
            "session_id, intent_id, conversation_id, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (role, content, t_h, day, int(proactive), session_id, intent_id,
             conversation_id, self._real_at(t_h)),
        )
        self.conn.commit()
        last_id = cur.lastrowid
        return int(last_id) if last_id is not None else -1

    def recent_messages(self, limit: int = 12) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def messages_for_day(self, day: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE day = ? ORDER BY id", (day,)
        ).fetchall()
        return [dict(r) for r in rows]

    def proactive_count(self, day: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE day = ? AND proactive = 1",
            (day,),
        ).fetchone()
        return int(row["n"])

    def messages_for_session(self, session_id: str) -> list[dict]:
        """L1 turns of one memory session (the documented store seam read).

        Since it3 B2 the session id is the conversation id (one memory
        session per conversation, module invariant 8); legacy day-scoped
        ids keep working because ``messages.session_id`` is unchanged.
        """
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- conversations (it3 B2, module invariant 8) -------------------------

    def open_conversation(
        self, conversation_id: str, opened_t_h: float, opened_by: str
    ) -> None:
        """Register a conversation as open (no-op if the id already exists)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO conversations (id, opened_t_h, opened_by, "
            "opened_at) VALUES (?, ?, ?, ?)",
            (conversation_id, opened_t_h, opened_by, self._real_at(opened_t_h)),
        )
        self.conn.commit()

    def close_conversation(
        self, conversation_id: str, closed_t_h: float, close_reason: str
    ) -> None:
        """Record the close of a conversation (idempotent re-close)."""
        self.conn.execute(
            "UPDATE conversations SET closed_t_h = ?, close_reason = ?, "
            "closed_at = ? WHERE id = ?",
            (closed_t_h, close_reason, self._real_at(closed_t_h),
             conversation_id),
        )
        self.conn.commit()

    def add_conversation_turn(
        self,
        conversation_id: str,
        speaker: str,
        text: str,
        t_h: float,
        turn_index: int,
        *,
        message_id: int | None = None,
    ) -> int:
        """Persist one turn of a conversation; returns the turn row id."""
        cur = self.conn.execute(
            "INSERT INTO conversation_turns "
            "(conversation_id, speaker, text, t_h, turn_index, message_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, speaker, text, t_h, turn_index, message_id),
        )
        self.conn.commit()
        last_id = cur.lastrowid
        return int(last_id) if last_id is not None else -1

    @staticmethod
    def _row_to_conversation_turn(row: dict) -> ConversationTurn:
        return ConversationTurn(
            speaker=row["speaker"],
            text=row["text"],
            t_h=float(row["t_h"]),
            turn_index=int(row["turn_index"]),
            conversation_id=row["conversation_id"],
        )

    @staticmethod
    def _row_to_conversation(row: dict, turns: tuple[ConversationTurn, ...]) -> Conversation:
        return Conversation(
            id=row["id"],
            opened_t_h=float(row["opened_t_h"]),
            closed_t_h=float(row["closed_t_h"]) if row["closed_t_h"] is not None else None,
            opened_by=row["opened_by"],
            close_reason=row["close_reason"],
            turns=turns,
        )

    def _turns_for_conversation(self, conversation_id: str) -> tuple[ConversationTurn, ...]:
        rows = self.conn.execute(
            "SELECT * FROM conversation_turns WHERE conversation_id = ? "
            "ORDER BY turn_index",
            (conversation_id,),
        ).fetchall()
        return tuple(self._row_to_conversation_turn(dict(r)) for r in rows)

    def load_conversation(self, conversation_id: str) -> Conversation | None:
        """Full ``Conversation`` (turns included) by id, or None."""
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_conversation(
            dict(row), self._turns_for_conversation(conversation_id)
        )

    def load_open_conversation(self) -> Conversation | None:
        """The currently open conversation (``closed_t_h IS NULL``), or None.

        At most one conversation is open at a time by construction (the
        session opens the next only after closing the previous); the most
        recently opened row wins defensively.
        """
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE closed_t_h IS NULL "
            "ORDER BY opened_t_h DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._row_to_conversation(
            dict(row), self._turns_for_conversation(row["id"])
        )

    def list_conversations(self) -> list[Conversation]:
        """All conversations, oldest first (stable order for id derivation)."""
        rows = self.conn.execute(
            "SELECT * FROM conversations ORDER BY opened_t_h, rowid"
        ).fetchall()
        return [
            self._row_to_conversation(dict(r), self._turns_for_conversation(r["id"]))
            for r in rows
        ]

    # -- kv_store (seam S1, v6) -------------------------------------------

    def get_kv(self, key: str) -> str | None:
        """Value stored under ``key`` (seam S1), or None when absent."""
        row = self.conn.execute(
            "SELECT value FROM kv_store WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else None

    def set_kv(self, key: str, value: str) -> None:
        """Store ``value`` under ``key`` (``INSERT OR REPLACE`` semantics)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def set_conversation_closing_pending(
        self, conversation_id: str, t_h: float | None
    ) -> None:
        """Persist (or clear, with None) the conversation's wind-down marker.

        ``closing_pending_t_h`` is the virtual hour at which the closing
        draw fired (two-phase close, seam S1); NULL means no wind-down is
        pending. Additive v6 column; idempotent.
        """
        self.conn.execute(
            "UPDATE conversations SET closing_pending_t_h = ? WHERE id = ?",
            (t_h, conversation_id),
        )
        self.conn.commit()

    def conversation_closing_pending(self, conversation_id: str) -> float | None:
        """The conversation's persisted ``closing_pending_t_h``, or None."""
        row = self.conn.execute(
            "SELECT closing_pending_t_h FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        value = row["closing_pending_t_h"]
        return float(value) if value is not None else None

    # -- judgements ----------------------------------------------------------

    def save_judgement(
        self,
        day: int,
        score: float,
        justification: str | None,
        model: str | None,
        shadow: bool,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO judgements (day, score, justification, model, shadow)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                score=excluded.score, justification=excluded.justification,
                model=excluded.model, shadow=excluded.shadow
            """,
            (day, score, justification, model, int(shadow)),
        )
        self.conn.commit()

    def load_judgement(self, day: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM judgements WHERE day = ?", (day,)
        ).fetchone()
        return dict(row) if row else None

    def load_previous_judgement(self, day: int) -> float | None:
        """Score of the most recent judgement recorded before ``day``."""
        row = self.conn.execute(
            "SELECT score FROM judgements WHERE day < ? ORDER BY day DESC LIMIT 1",
            (day,),
        ).fetchone()
        if row is None or row["score"] is None:
            return None
        return float(row["score"])

    def latest_interaction_t_h(self) -> float | None:
        """Virtual hour of the user's most recent message (None if never)."""
        row = self.conn.execute(
            "SELECT MAX(t_h) AS m FROM messages WHERE role = 'user'"
        ).fetchone()
        if row is None or row["m"] is None:
            return None
        return float(row["m"])

    # -- audit log -----------------------------------------------------------

    def log_event(self, day: int, t_h: float, event: str, detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO state_events (day, t_h, event, detail) VALUES (?, ?, ?, ?)",
            (day, t_h, event, detail),
        )
        self.conn.commit()

    def log_llm_call(
        self,
        day: int,
        t_h: float,
        role: str,
        prompt: str,
        response: str,
        model: str | None,
        meta: dict | None = None,
        *,
        repro: dict | None = None,
        usage: object | None = None,
        lane: str | None = None,
        raw_cost: float | None = None,
    ) -> int:
        """Record one generation call; returns the call id.

        The prompt hash is ALWAYS kept. When ``audit_mode`` is enabled and a
        ``repro`` payload is supplied (eval-mode call reproducibility, plan
        §5-A7 M3: model, temperature, max_tokens, seed, system context, message
        payload, generation controls, memory policy, intent id, snapshot/
        reference ids, timestamp, response), the exact payload is persisted as
        JSON so the call can be reproduced from the run manifest (invariant
        19). In production privacy mode the payload is dropped.

        WS-D spend accounting (additive): ``usage`` (a ``harness.client.Usage``
        or plain dict) persists the token ledger columns (prompt/completion/
        total/cached/cache-miss); ``lane`` carries the WS-C attribution
        ("product" | "research"); ``raw_cost`` persists the gateway-reported
        cost (G-cost cross-check). All optional — callers that predate usage
        capture are unchanged and the new columns stay NULL (replay parity).
        """
        repro_json = None
        if self.audit_mode and repro is not None:
            repro_json = json.dumps(repro, sort_keys=True)
        (pt, ct, tt, cached, miss) = _usage_columns(usage)
        cur = self.conn.execute(
            "INSERT INTO llm_calls (day, t_h, role, model, prompt_hash, "
            "response, meta, repro_json, prompt_tokens, completion_tokens, "
            "total_tokens, cached_tokens, cache_miss_tokens, lane, raw_cost) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                day,
                t_h,
                role,
                model,
                _hash(prompt),
                response,
                json.dumps(meta) if meta else None,
                repro_json,
                pt, ct, tt, cached, miss,
                lane,
                raw_cost,
            ),
        )
        self.conn.commit()
        last_id = cur.lastrowid
        return int(last_id) if last_id is not None else -1

    def get_llm_call(self, call_id: int) -> dict | None:
        """One audit row by id, with ``repro`` and ``meta`` parsed back to
        dicts (None when absent). Used by the eval harness to reconstruct the
        exact inputs of a call (invariant 19)."""
        row = self.conn.execute(
            "SELECT * FROM llm_calls WHERE id = ?", (call_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["repro"] = json.loads(out["repro_json"]) if out.get("repro_json") else None
        out.pop("repro_json", None)
        out["meta"] = json.loads(out["meta"]) if out.get("meta") else None
        return out

    def rebuild_call(self, call_id: int) -> dict:
        """Reconstruct the exact request envelope of one logged call from its
        ``repro_json`` payload ALONE (it3 B7, invariant 19).

        Returns ``{"model", "system", "messages", "temperature",
        "max_tokens", "json_mode"}`` — the exact inputs the client received,
        so the call can be replayed byte-for-byte from the row. Raises
        ``ValueError`` for hash-only rows: non-eval runs persist no payload
        by design, and the leak audit reports those rows honestly instead of
        faking coverage.
        """
        row = self.get_llm_call(call_id)
        if row is None:
            raise KeyError(f"llm_call {call_id} not found")
        repro = row.get("repro") or {}
        missing = [
            k for k in ("model", "system", "messages", "max_tokens",
                        "temperature", "json_mode")
            if k not in repro
        ]
        if missing:
            raise ValueError(
                f"llm_call {call_id} is not reconstructable: repro payload "
                f"missing {missing} (hash-only row from a non-eval run)"
            )
        return {k: repro[k] for k in (
            "model", "system", "messages", "temperature", "max_tokens",
            "json_mode")}

    def events_since(self, day: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM state_events WHERE day >= ? ORDER BY id", (day,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- schedule_events (persisted proactive schedule) ----------------------

    def save_schedule_events(self, seed: int, events: list[dict]) -> None:
        """Upsert planned events. Each dict: {"t_h": float, "day": int,
        "reason": str}. INSERT OR IGNORE on (seed, t_h) so re-planning the same
        horizon is idempotent and never resurrects a fired/expired row."""
        self.conn.executemany(
            "INSERT OR IGNORE INTO schedule_events (seed, t_h, day, reason) "
            "VALUES (?, ?, ?, ?)",
            [(seed, e["t_h"], e["day"], e["reason"]) for e in events],
        )
        self.conn.commit()

    def pending_schedule_events(self, seed: int) -> list[dict]:
        """Rows with status='pending' for seed, ascending by t_h. Each dict has
        keys: id, seed, t_h, day, reason, status, fired_t_h."""
        rows = self.conn.execute(
            "SELECT * FROM schedule_events WHERE seed = ? AND status = 'pending' "
            "ORDER BY t_h",
            (seed,),
        ).fetchall()
        return [dict(r) for r in rows]

    def schedule_events_for_seed(self, seed: int) -> list[dict]:
        """All rows for seed, ascending by t_h (any status). Used by
        ProactiveSchedule.restore to rebuild event_hours without re-planning."""
        rows = self.conn.execute(
            "SELECT * FROM schedule_events WHERE seed = ? ORDER BY t_h", (seed,)
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_schedule_fired(self, seed: int, t_h: float, fired_t_h: float) -> None:
        """Set status='fired', fired_t_h=<arg> for the row (seed, t_h)."""
        self.conn.execute(
            "UPDATE schedule_events SET status = 'fired', fired_t_h = ? "
            "WHERE seed = ? AND t_h = ?",
            (fired_t_h, seed, t_h),
        )
        self.conn.commit()

    def mark_schedule_expired(self, seed: int, t_h: float) -> None:
        """Set status='expired' for the row (seed, t_h)."""
        self.conn.execute(
            "UPDATE schedule_events SET status = 'expired' "
            "WHERE seed = ? AND t_h = ?",
            (seed, t_h),
        )
        self.conn.commit()

    def last_proactive_t_h(self, seed: int) -> float | None:
        """Max fired_t_h over status='fired' rows for seed, else None. Used by
        the context gate for cooldown across restarts."""
        row = self.conn.execute(
            "SELECT MAX(fired_t_h) AS m FROM schedule_events "
            "WHERE seed = ? AND status = 'fired'",
            (seed,),
        ).fetchone()
        return float(row["m"]) if row["m"] is not None else None

    # -- persona + interests (A6) -------------------------------------------

    def save_persona(self, profile: PersonaProfile) -> None:
        """Upsert the singleton persona row (routines as JSON) and replace the
        interest portfolio with ``profile.interests``."""
        self.conn.execute(
            "INSERT INTO persona (id, name, core, routines_json) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name, core=excluded.core, "
            "routines_json=excluded.routines_json",
            (
                profile.name,
                profile.core,
                json.dumps([asdict(r) for r in profile.routines]),
            ),
        )
        self.conn.execute("DELETE FROM interests")
        self.conn.executemany(
            "INSERT INTO interests (name, bucket, salience) VALUES (?, ?, ?)",
            [(i.name, i.bucket, i.salience) for i in profile.interests],
        )
        self.conn.commit()

    def load_persona(self) -> PersonaProfile | None:
        row = self.conn.execute("SELECT * FROM persona WHERE id = 1").fetchone()
        if row is None:
            return None
        routines = tuple(
            Routine(**d) for d in _unjson(row["routines_json"], [])
        )
        interests = tuple(self.list_interests())
        return PersonaProfile(
            name=row["name"], core=row["core"],
            interests=interests, routines=routines,
        )

    def save_interests(self, interests: list[Interest]) -> None:
        """Replace the stored interest portfolio."""
        self.conn.execute("DELETE FROM interests")
        self.conn.executemany(
            "INSERT INTO interests (name, bucket, salience) VALUES (?, ?, ?)",
            [(i.name, i.bucket, i.salience) for i in interests],
        )
        self.conn.commit()

    def list_interests(self) -> list[Interest]:
        rows = self.conn.execute(
            "SELECT * FROM interests ORDER BY name"
        ).fetchall()
        return [
            Interest(name=r["name"], bucket=r["bucket"], salience=float(r["salience"]))
            for r in rows
        ]

    # -- life arcs (A4) ------------------------------------------------------

    def upsert_life_arc(self, arc: LifeArc) -> None:
        self.conn.execute(
            """
            INSERT INTO life_arcs (id, name, interest, started_day, progress,
                                   status, next_intention)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, interest=excluded.interest,
                started_day=excluded.started_day, progress=excluded.progress,
                status=excluded.status, next_intention=excluded.next_intention
            """,
            (arc.id, arc.name, arc.interest, arc.started_day, arc.progress,
             arc.status, arc.next_intention),
        )
        self.conn.commit()

    def get_life_arc(self, arc_id: str) -> LifeArc | None:
        row = self.conn.execute(
            "SELECT * FROM life_arcs WHERE id = ?", (arc_id,)
        ).fetchone()
        if row is None:
            return None
        return LifeArc(
            id=row["id"], name=row["name"], interest=row["interest"],
            started_day=int(row["started_day"]), progress=float(row["progress"]),
            status=row["status"], next_intention=row["next_intention"],
        )

    def list_life_arcs(self, status: str | None = None) -> list[LifeArc]:
        if status is None:
            rows = self.conn.execute(
                "SELECT * FROM life_arcs ORDER BY started_day, id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM life_arcs WHERE status = ? ORDER BY started_day, id",
                (status,),
            ).fetchall()
        return [
            LifeArc(
                id=r["id"], name=r["name"], interest=r["interest"],
                started_day=int(r["started_day"]), progress=float(r["progress"]),
                status=r["status"], next_intention=r["next_intention"],
            )
            for r in rows
        ]

    def update_life_arc_status(self, arc_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE life_arcs SET status = ? WHERE id = ?", (status, arc_id)
        )
        self.conn.commit()

    def wipe_life_arcs(self) -> None:
        """Delete every life-arc row (NO_LIFE goldfish day-boundary wipe).

        The life layer re-seeds on the next ``Session._ensure_life`` under a
        fresh epoch (``_life_epoch`` counts ``life_wipe`` as a generation
        boundary), so a wiped generation's arc ids are never reused. Agenda
        items are NOT touched: past days' items keep their (now-historical)
        arc references — the content gate resolves missing arcs to None.
        """
        self.conn.execute("DELETE FROM life_arcs")
        self.conn.commit()

    # -- agenda (A4) ---------------------------------------------------------

    def _row_to_agenda_item(self, row: dict) -> AgendaItem:
        return AgendaItem(
            id=row["id"],
            start_t_h=float(row["start_t_h"]),
            end_t_h=float(row["end_t_h"]),
            activity=row["activity"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            salience=float(row["salience"]),
            status=row["status"],
        )

    def save_agenda(self, day: int, agenda: DailyAgenda) -> None:
        """Replace the agenda of ``day`` with ``agenda.items`` (a day's agenda
        is regenerated, never merged)."""
        self.conn.execute("DELETE FROM agenda_items WHERE day = ?", (day,))
        self.conn.executemany(
            "INSERT INTO agenda_items (id, day, start_t_h, end_t_h, activity, "
            "source_type, source_id, salience, status, start_at, end_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (item.id, day, item.start_t_h, item.end_t_h, item.activity,
                 item.source_type, item.source_id, item.salience, item.status,
                 self._real_at(item.start_t_h), self._real_at(item.end_t_h))
                for item in agenda.items
            ],
        )
        self.conn.commit()

    def load_agenda(self, day: int) -> DailyAgenda | None:
        rows = self.conn.execute(
            "SELECT * FROM agenda_items WHERE day = ? ORDER BY start_t_h", (day,)
        ).fetchall()
        if not rows:
            return None
        return DailyAgenda(day=day, items=tuple(self._row_to_agenda_item(r) for r in rows))

    def update_agenda_item_status(self, item_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE agenda_items SET status = ? WHERE id = ?", (status, item_id)
        )
        self.conn.commit()

    def list_agenda_items(
        self, day: int | None = None, status: str | None = None
    ) -> list[AgendaItem]:
        clauses, params = [], []
        if day is not None:
            clauses.append("day = ?")
            params.append(day)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM agenda_items {where} ORDER BY start_t_h", params
        ).fetchall()
        return [self._row_to_agenda_item(r) for r in rows]

    # -- memory tiers: L1 sessions + turns (A5) ------------------------------

    def open_session(self, session_id: str, started_at_t_h: float) -> None:
        """Register a session (no-op if already known)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO memory_sessions (session_id, started_at_t_h) "
            "VALUES (?, ?)",
            (session_id, started_at_t_h),
        )
        self.conn.commit()

    def close_session(self, session_id: str, ended_at_t_h: float) -> None:
        self.conn.execute(
            "UPDATE memory_sessions SET ended_at_t_h = ? WHERE session_id = ?",
            (ended_at_t_h, session_id),
        )
        self.conn.commit()

    def turns_for_session(self, session_id: str) -> list[dict]:
        """L1 turns of one session (via the memory_turns view over messages)."""
        rows = self.conn.execute(
            "SELECT * FROM memory_turns WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def session_exists(self, session_id: str) -> bool:
        """True when the L1 session row is registered (open_session was
        called and the row was not deleted). The content gate's broken-
        provenance check (A9 G-8b): a memory whose source session is gone
        has no record of what it claims."""
        row = self.conn.execute(
            "SELECT 1 FROM memory_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row is not None

    # -- memory tiers: L2 session summaries (A5) -----------------------------

    def save_session_summary(self, summary: SessionSummary) -> None:
        self.conn.execute(
            """
            INSERT INTO memory_session_summaries (
                session_id, started_at_t_h, ended_at_t_h, summary,
                topics_json, user_facts_json,
                preference_updates_json, companion_events_json,
                relationship_events_json, callbacks_json,
                affect_observations_json, emotional_peak, importance,
                source_turn_ids_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                started_at_t_h=excluded.started_at_t_h,
                ended_at_t_h=excluded.ended_at_t_h,
                summary=excluded.summary, topics_json=excluded.topics_json,
                user_facts_json=excluded.user_facts_json,
                preference_updates_json=excluded.preference_updates_json,
                companion_events_json=excluded.companion_events_json,
                relationship_events_json=excluded.relationship_events_json,
                callbacks_json=excluded.callbacks_json,
                affect_observations_json=excluded.affect_observations_json,
                emotional_peak=excluded.emotional_peak,
                importance=excluded.importance,
                source_turn_ids_json=excluded.source_turn_ids_json
            """,
            (
                summary.session_id,
                summary.started_at_t_h,
                summary.ended_at_t_h,
                summary.summary,
                _json(summary.topics),
                _json(summary.user_facts),
                _json(summary.preference_updates),
                _json(summary.companion_events),
                _json(summary.relationship_events),
                _json(summary.callbacks),
                json.dumps([asdict(a) for a in summary.affect_observations]),
                int(summary.emotional_peak),
                summary.importance,
                _json(summary.source_turn_ids),
            ),
        )
        self.conn.commit()

    def load_session_summary(self, session_id: str) -> SessionSummary | None:
        row = self.conn.execute(
            "SELECT * FROM memory_session_summaries WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return SessionSummary(
            session_id=row["session_id"],
            started_at_t_h=float(row["started_at_t_h"]),
            ended_at_t_h=float(row["ended_at_t_h"]),
            summary=row["summary"],
            topics=tuple(_unjson(row["topics_json"], [])),
            user_facts=tuple(_unjson(row["user_facts_json"], [])),
            preference_updates=tuple(_unjson(row["preference_updates_json"], [])),
            companion_events=tuple(_unjson(row["companion_events_json"], [])),
            relationship_events=tuple(_unjson(row["relationship_events_json"], [])),
            callbacks=tuple(_unjson(row["callbacks_json"], [])),
            affect_observations=tuple(
                AffectMetadata(**d)
                for d in _unjson(row["affect_observations_json"], [])
            ),
            emotional_peak=bool(row["emotional_peak"]),
            importance=float(row["importance"]),
            source_turn_ids=tuple(int(x) for x in _unjson(row["source_turn_ids_json"], [])),
        )

    # -- memory tiers: L3 episodes (A5) --------------------------------------

    def _row_to_episode(self, row: dict) -> EpisodicMemory:
        return EpisodicMemory(
            id=row["id"],
            summary=row["summary"],
            category=MemoryKind(row["category"]),
            occurred_at_t_h=float(row["occurred_at_t_h"]),
            created_at_t_h=float(row["created_at_t_h"]),
            importance=float(row["importance"]),
            access_count=int(row["access_count"]),
            last_accessed_t_h=(
                float(row["last_accessed_t_h"])
                if row["last_accessed_t_h"] is not None else None
            ),
            affect=(
                AffectMetadata(**json.loads(row["affect_json"]))
                if row["affect_json"] else None
            ),
            source_session_id=row["source_session_id"],
            source_turn_ids=tuple(int(x) for x in _unjson(row["source_turn_ids_json"], [])),
            verbatim_anchors=tuple(_unjson(row["verbatim_anchors_json"], [])),
            tags=tuple(_unjson(row["tags_json"], [])),
        )

    def insert_episode(self, episode: EpisodicMemory) -> str:
        """Persist an episode plus its normalized source-turn links; returns
        the episode id."""
        self.conn.execute(
            """
            INSERT INTO memory_episodes (
                id, summary, category, occurred_at_t_h, created_at_t_h,
                importance, access_count, last_accessed_t_h, affect_json,
                source_session_id, source_turn_ids_json, verbatim_anchors_json,
                tags_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                summary=excluded.summary, category=excluded.category,
                occurred_at_t_h=excluded.occurred_at_t_h,
                created_at_t_h=excluded.created_at_t_h,
                importance=excluded.importance,
                access_count=excluded.access_count,
                last_accessed_t_h=excluded.last_accessed_t_h,
                affect_json=excluded.affect_json,
                source_session_id=excluded.source_session_id,
                source_turn_ids_json=excluded.source_turn_ids_json,
                verbatim_anchors_json=excluded.verbatim_anchors_json,
                tags_json=excluded.tags_json
            """,
            (
                episode.id,
                episode.summary,
                episode.category.value,
                episode.occurred_at_t_h,
                episode.created_at_t_h,
                episode.importance,
                episode.access_count,
                episode.last_accessed_t_h,
                json.dumps(asdict(episode.affect)) if episode.affect else None,
                episode.source_session_id,
                _json(episode.source_turn_ids),
                _json(episode.verbatim_anchors),
                _json(episode.tags),
            ),
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO memory_episode_sources (episode_id, turn_id) "
            "VALUES (?, ?)",
            [(episode.id, t) for t in episode.source_turn_ids],
        )
        self.conn.commit()
        return episode.id

    def get_episode(self, episode_id: str) -> EpisodicMemory | None:
        row = self.conn.execute(
            "SELECT * FROM memory_episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        return self._row_to_episode(row) if row else None

    def list_episodes(
        self, limit: int = 500, category: str | MemoryKind | None = None
    ) -> list[EpisodicMemory]:
        """Most recent episodes first; optionally filtered by category."""
        if category is None:
            rows = self.conn.execute(
                "SELECT * FROM memory_episodes ORDER BY created_at_t_h DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            cat = category.value if isinstance(category, MemoryKind) else category
            rows = self.conn.execute(
                "SELECT * FROM memory_episodes WHERE category = ? "
                "ORDER BY created_at_t_h DESC LIMIT ?",
                (cat, limit),
            ).fetchall()
        return [self._row_to_episode(r) for r in rows]

    def touch_episode(self, episode_id: str, t_h: float) -> int:
        """Bump access_count and set last_accessed_t_h; returns the new count."""
        self.conn.execute(
            "UPDATE memory_episodes SET access_count = access_count + 1, "
            "last_accessed_t_h = ? WHERE id = ?",
            (t_h, episode_id),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT access_count FROM memory_episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        return int(row["access_count"]) if row else 0

    def list_episode_sources(self, episode_id: str) -> list[int]:
        """Exact turn ids an episode points at (provenance, normalized)."""
        rows = self.conn.execute(
            "SELECT turn_id FROM memory_episode_sources WHERE episode_id = ? "
            "ORDER BY turn_id",
            (episode_id,),
        ).fetchall()
        return [int(r["turn_id"]) for r in rows]

    def episodes_for_turn(self, turn_id: int) -> list[str]:
        """Episode ids anchored to a given turn id."""
        rows = self.conn.execute(
            "SELECT episode_id FROM memory_episode_sources WHERE turn_id = ? "
            "ORDER BY episode_id",
            (turn_id,),
        ).fetchall()
        return [r["episode_id"] for r in rows]

    # -- memory tiers: local BLOB embeddings (A5) ----------------------------

    def save_embedding(self, episode_id: str, vector: Sequence[float]) -> None:
        """Store/overwrite the float vector of an episode as a BLOB."""
        vec = list(vector)
        blob = array("f", vec).tobytes()
        self.conn.execute(
            "INSERT INTO memory_embeddings (episode_id, vector, dim) VALUES (?, ?, ?) "
            "ON CONFLICT(episode_id) DO UPDATE SET "
            "vector=excluded.vector, dim=excluded.dim",
            (episode_id, blob, len(vec)),
        )
        self.conn.commit()

    def load_embeddings(self) -> list[tuple[str, list[float]]]:
        rows = self.conn.execute(
            "SELECT episode_id, vector, dim FROM memory_embeddings"
        ).fetchall()
        out = []
        for r in rows:
            vec = array("f")
            vec.frombytes(bytes(r["vector"]))
            out.append((r["episode_id"], list(vec)))
        return out

    # -- memory tiers: L4 user model assertions (A5) -------------------------

    def _row_to_assertion(self, row: dict) -> UserModelAssertion:
        return UserModelAssertion(
            key=row["key"],
            value=row["value"],
            confidence=float(row["confidence"]),
            updated_at_t_h=float(row["updated_at_t_h"]),
            source_memory_ids=tuple(_unjson(row["source_memory_ids_json"], [])),
            status=row["status"],
        )

    def upsert_assertion(
        self,
        assertion: UserModelAssertion,
        *,
        category: UserModelCategory | str | None = None,
    ) -> None:
        """Insert an assertion; a new ``current`` one supersedes (status flip,
        provenance kept) the previous ``current`` row of the same key.

        The canonical L4 category (plan §5-A7 M2, invariant 10) is stored
        DIRECTLY on the row: pass the ``UserModelCategory`` explicitly, or omit
        it for legacy-compatible derivation from the documented key prefixes
        (``_category_from_key``). The load path reads this column only — keys
        are never parsed to infer categories.
        """
        if category is None:
            cat = _category_from_key(assertion.key)
        elif isinstance(category, UserModelCategory):
            cat = category
        else:
            cat = UserModelCategory(str(category))  # canonical only; raises
        if assertion.status == "current":
            self.conn.execute(
                "UPDATE user_model_assertions SET status = 'superseded' "
                "WHERE key = ? AND status = 'current'",
                (assertion.key,),
            )
        self.conn.execute(
            "INSERT INTO user_model_assertions (key, value, confidence, "
            "updated_at_t_h, source_memory_ids_json, status, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                assertion.key,
                assertion.value,
                assertion.confidence,
                assertion.updated_at_t_h,
                _json(assertion.source_memory_ids),
                assertion.status,
                cat.value,
            ),
        )
        self.conn.commit()

    def list_assertions(
        self,
        status: str = "current",
        category: UserModelCategory | str | None = None,
    ) -> list[UserModelAssertion]:
        clauses, params = ["status = ?"], [status]
        if category is not None:
            cat = (
                category.value
                if isinstance(category, UserModelCategory)
                else UserModelCategory(str(category)).value
            )
            clauses.append("category = ?")
            params.append(cat)
        rows = self.conn.execute(
            "SELECT * FROM user_model_assertions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY seq DESC",
            params,
        ).fetchall()
        return [self._row_to_assertion(r) for r in rows]

    def get_assertion_category(self, key: str) -> UserModelCategory | None:
        """Canonical category of the most recent assertion row for ``key``
        (None when the key has no rows). The stored value is always a
        canonical ``UserModelCategory``."""
        row = self.conn.execute(
            "SELECT category FROM user_model_assertions WHERE key = ? "
            "ORDER BY seq DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is None or row["category"] is None:
            return None
        return UserModelCategory(row["category"])

    def supersede_assertion(
        self,
        key: str,
        *,
        source_memory_ids: Sequence[str] | None = None,
        updated_at_t_h: float | None = None,
    ) -> None:
        """Flip every current assertion of ``key`` to superseded (provenance
        kept). Cross-key negation support: a "user no longer has X" fact
        supersedes unrelated keys whose values mention X (memory.py M-1b).

        Optional ``source_memory_ids`` / ``updated_at_t_h`` rewrite the
        superseded row's provenance and timestamp so the negation evidence
        is PERSISTED, not only merged in the caller's return value (A9
        M-1b provenance leg: no provenance -> no truth).
        """
        sets = ["status = 'superseded'"]
        params: list = []
        if source_memory_ids is not None:
            sets.append("source_memory_ids_json = ?")
            params.append(_json(source_memory_ids))
        if updated_at_t_h is not None:
            sets.append("updated_at_t_h = ?")
            params.append(float(updated_at_t_h))
        params.append(key)
        self.conn.execute(
            "UPDATE user_model_assertions SET " + ", ".join(sets)
            + " WHERE key = ? AND status = 'current'",
            params,
        )
        self.conn.commit()

    def get_assertion(self, key: str) -> UserModelAssertion | None:
        """Most recent assertion row for ``key`` (any status — full history)."""
        row = self.conn.execute(
            "SELECT * FROM user_model_assertions WHERE key = ? ORDER BY seq DESC "
            "LIMIT 1",
            (key,),
        ).fetchone()
        return self._row_to_assertion(row) if row else None

    def _stored_category(self, key: str) -> UserModelCategory:
        """Canonical category of the most recent row of ``key``.

        Primary path: the stored ``category`` column (canonical values only).
        Defensive fallback for rows with a NULL category (only possible in
        hand-edited databases — the v3 migration backfills every row): the
        documented legacy key-prefix derivation. Loads never parse arbitrary
        keys.
        """
        row = self.conn.execute(
            "SELECT category FROM user_model_assertions WHERE key = ? "
            "ORDER BY seq DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is not None and row["category"] is not None:
            return UserModelCategory(row["category"])
        return _category_from_key(key)

    def load_user_model(self) -> UserModel:
        """Project current assertions into the L4 UserModel.

        Bucketing uses the CANONICAL ``category`` column only (plan §5-A7 M2,
        invariant 10): each assertion carries its ``UserModelCategory`` value
        directly, so no semantic category is ever inferred from string
        prefixes or free-form keys at load time. Keys remain visible for
        provenance but are not parsed for grouping.
        """
        current = self.list_assertions(status="current")
        identity = ""
        groups: dict[str, list[UserModelAssertion]] = {g: [] for g in _USER_MODEL_GROUPS}
        for a in current:
            cat = self._stored_category(a.key)
            if cat is UserModelCategory.IDENTITY:
                identity = a.value
            else:
                groups[_CATEGORY_TO_GROUP[cat]].append(a)
        return UserModel(
            identity=identity,
            stable_preferences=tuple(groups["stable_preferences"]),
            current_preferences=tuple(groups["current_preferences"]),
            boundaries=tuple(groups["boundaries"]),
            vulnerabilities=tuple(groups["vulnerabilities"]),
            recurring_interests=tuple(groups["recurring_interests"]),
            relationship_patterns=tuple(groups["relationship_patterns"]),
            important_entities=tuple(groups["important_entities"]),
        )

    # -- proactive intents (A7) ----------------------------------------------

    def save_proactive_intent(self, intent: ProactiveIntent) -> None:
        """Upsert an intent; an existing row keeps its lifecycle status."""
        self.conn.execute(
            """
            INSERT INTO proactive_intents (
                id, reason, source_type, source_id, hook, created_t_h,
                valid_until_t_h, salience, evidence, created_at, valid_until_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                reason=excluded.reason, source_type=excluded.source_type,
                source_id=excluded.source_id, hook=excluded.hook,
                created_t_h=excluded.created_t_h,
                valid_until_t_h=excluded.valid_until_t_h,
                salience=excluded.salience, evidence=excluded.evidence,
                created_at=excluded.created_at,
                valid_until_at=excluded.valid_until_at
            """,
            (
                intent.id, intent.reason, intent.source_type, intent.source_id,
                intent.hook, intent.created_t_h, intent.valid_until_t_h,
                intent.salience, intent.evidence,
                self._real_at(intent.created_t_h),
                self._real_at(intent.valid_until_t_h),
            ),
        )
        self.conn.commit()

    def load_proactive_intent(self, intent_id: str) -> ProactiveIntent | None:
        row = self.conn.execute(
            "SELECT * FROM proactive_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        if row is None:
            return None
        return ProactiveIntent(
            id=row["id"], reason=row["reason"], source_type=row["source_type"],
            source_id=row["source_id"], hook=row["hook"],
            created_t_h=float(row["created_t_h"]),
            valid_until_t_h=float(row["valid_until_t_h"]),
            salience=float(row["salience"]), evidence=row["evidence"],
        )

    def list_proactive_intents(
        self, status: str | None = None
    ) -> list[ProactiveIntent]:
        if status is None:
            rows = self.conn.execute(
                "SELECT * FROM proactive_intents ORDER BY created_t_h DESC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM proactive_intents WHERE status = ? "
                "ORDER BY created_t_h DESC",
                (status,),
            ).fetchall()
        return [
            ProactiveIntent(
                id=r["id"], reason=r["reason"], source_type=r["source_type"],
                source_id=r["source_id"], hook=r["hook"],
                created_t_h=float(r["created_t_h"]),
                valid_until_t_h=float(r["valid_until_t_h"]),
                salience=float(r["salience"]), evidence=r["evidence"],
            )
            for r in rows
        ]

    def update_proactive_intent_status(self, intent_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE proactive_intents SET status = ? WHERE id = ?",
            (status, intent_id),
        )
        self.conn.commit()

    def resolve_intent_source(
        self, intent: ProactiveIntent
    ) -> AgendaItem | LifeArc | EpisodicMemory | None:
        """The persisted source an intent points at, or None when the source
        no longer exists (the content gate's existence check).

        source_type vocabulary (aligned with harness.proactive):
        - agenda_item / life_event -> agenda_items (life events ARE completed
          agenda items)
        - life_arc -> life_arcs
        - episodic_memory / callback / shared_interest / check_in -> episodes
          (callbacks, shared-interest anchors and check-in anchors are all
          episodic memories)
        """
        st = intent.source_type
        if st in ("agenda_item", "life_event"):
            row = self.conn.execute(
                "SELECT * FROM agenda_items WHERE id = ?", (intent.source_id,)
            ).fetchone()
            return self._row_to_agenda_item(row) if row else None
        if st == "life_arc":
            return self.get_life_arc(intent.source_id)
        if st in ("episodic_memory", "callback", "shared_interest", "check_in"):
            return self.get_episode(intent.source_id)
        return None

    # -- steering_queue (WS3 backend contract) ------------------------------

    def enqueue_steer(
        self, day: int, t_h: float, kind: str, payload: dict | list | str
    ) -> int:
        """Queue one arriving event for delivery at the next safe boundary;
        returns the steer id. ``payload`` is persisted as JSON (dict/list)
        or verbatim (str). Status starts as ``'pending'``."""
        payload_json = (
            payload if isinstance(payload, str) else _json(payload)
        )
        cur = self.conn.execute(
            "INSERT INTO steering_queue (day, t_h, kind, payload_json, "
            "status) VALUES (?, ?, ?, ?, 'pending')",
            (day, t_h, kind, payload_json),
        )
        self.conn.commit()
        last_id = cur.lastrowid
        return int(last_id) if last_id is not None else -1

    def pending_steers(
        self, day: int | None = None, limit: int = 50
    ) -> list[dict]:
        """Undelivered steers, oldest first; ``payload`` parsed back to a
        dict when it is JSON. ``day`` filters the queue to one day (None =
        all days)."""
        if day is None:
            rows = self.conn.execute(
                "SELECT * FROM steering_queue WHERE status = 'pending' "
                "ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM steering_queue WHERE status = 'pending' "
                "AND day = ? ORDER BY id LIMIT ?",
                (day, limit),
            ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            try:
                row["payload"] = _unjson(row["payload_json"], {})
            except ValueError:
                # enqueue_steer also accepts verbatim string payloads
                row["payload"] = row["payload_json"]
            out.append(row)
        return out

    def mark_steer_delivered(
        self,
        steer_id: int,
        delivered_t_h: float,
        boundary: str,
        seen_turn_id: str | None,
    ) -> None:
        """Record the delivery of one steer: status -> 'delivered' with the
        actual delivery time, boundary ('idle'|'after_tool'|'after_reply')
        and the turn id that saw it (summary #23)."""
        self.conn.execute(
            "UPDATE steering_queue SET status = 'delivered', "
            "delivered_t_h = ?, boundary = ?, seen_turn_id = ? WHERE id = ?",
            (delivered_t_h, boundary, seen_turn_id, steer_id),
        )
        self.conn.commit()

    def requeue_steer(self, steer_id: int) -> None:
        """Return a steer to 'pending' (interrupted delivery: the turn it
        was appended to did not complete). Delivery fields are cleared so a
        pending row always means undelivered."""
        self.conn.execute(
            "UPDATE steering_queue SET status = 'pending', delivered_t_h = "
            "NULL, boundary = NULL, seen_turn_id = NULL WHERE id = ?",
            (steer_id,),
        )
        self.conn.commit()

    # -- decision_records (pop-up decision layer, WS2) ----------------------

    def record_decision(
        self,
        day: int,
        t_h: float,
        popup_kind: str,
        event_id: str | None,
        event_label: str | None,
        state_label: str | None,
        time: str | None,
        inputs_json: str | None,
        raw_reply: str | None,
        verdict_json: str | None,
        source: str,
        transport: str,
        delivered_t_h: float | None,
        budget_consumed: int,
        *,
        replay_id: str | None = None,
    ) -> int:
        """Persist one pop-up decision (raw reply AND parsed verdict — dual
        persistence); returns the record id. ``replay_id`` is the natural
        key used by :meth:`decision_for_replay` (deterministic replay)."""
        cur = self.conn.execute(
            "INSERT INTO decision_records (day, t_h, popup_kind, event_id, "
            "event_label, state_label, time, inputs_json, raw_reply, "
            "verdict_json, source, transport, delivered_t_h, "
            "budget_consumed, replay_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)",
            (
                day, t_h, popup_kind, event_id, event_label, state_label,
                time, inputs_json, raw_reply, verdict_json, source, transport,
                delivered_t_h, budget_consumed, replay_id,
            ),
        )
        self.conn.commit()
        last_id = cur.lastrowid
        return int(last_id) if last_id is not None else -1

    def decision_for_replay(self, decision_id: str) -> dict | None:
        """The latest decision recorded for a natural key (``replay_id``),
        with ``inputs`` and ``verdict`` parsed back to dicts. Replay reads
        this — it never re-rolls. None when the key is unknown."""
        row = self.conn.execute(
            "SELECT * FROM decision_records WHERE replay_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["inputs"] = _unjson(out["inputs_json"], {})
        out["verdict"] = _unjson(out["verdict_json"], {})
        return out

    def decisions_for_day(self, day: int) -> list[dict]:
        """All decision records for one day, oldest first, with ``inputs``
        and ``verdict`` parsed back to dicts (the budget window key)."""
        rows = self.conn.execute(
            "SELECT * FROM decision_records WHERE day = ? ORDER BY id",
            (day,),
        ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            row["inputs"] = _unjson(row["inputs_json"], {})
            row["verdict"] = _unjson(row["verdict_json"], {})
            out.append(row)
        return out
