"""Legacy (v1) -> v2 migration tests (vertical slice A2).

Builds a database with the PRE-migration schema (the legacy CREATE TABLE
statements embedded VERBATIM below — deliberately NOT imported from
harness.store), seeds it with existing state/messages/judgements/schedules,
then instantiates the new store: the migration must succeed, every piece of
legacy data must remain present and interpretable, and re-opening the same
database (migration runs twice) must be a no-op. No destructive migration.
"""

import sqlite3

from harness.domain import (
    AgendaItem,
    DailyAgenda,
    Interest,
    LifeArc,
    PersonaProfile,
    ProactiveIntent,
    Routine,
)
from harness.store import SCHEMA_VERSION, SQLiteStore

# Legacy v1 schema, VERBATIM from the pre-slice harness/store.py.
_LEGACY_SCHEMA = """
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


def _build_legacy_db(path) -> None:
    """Create a v1 database and seed it like the pre-slice system would."""
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO daily_state (day, M, m_level, g, p, arg, mu, eta, "
        "cycle_day, phase_label, seed, score) "
        "VALUES (0, 7, 0.1, 1.0, 0.7, 0.8, 0.2, 0.1, 3.0, 'follicular', 42, 0.5)"
    )
    conn.executemany(
        "INSERT INTO messages (role, content, t_h, day, proactive) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("user", "legacy hello", 0.5, 0, 0),
            ("assistant", "legacy reply", 0.7, 0, 0),
            ("assistant", "legacy proactive", 10.0, 0, 1),
        ],
    )
    conn.execute(
        "INSERT INTO judgements (day, score, justification, model, shadow) "
        "VALUES (0, 0.8, 'legacy judgement', 'fake', 1)"
    )
    conn.execute(
        "INSERT INTO state_events (day, t_h, event, detail) "
        "VALUES (0, 0.5, 'day_rollover', 'M=7')"
    )
    conn.execute(
        "INSERT INTO llm_calls (day, t_h, role, model, prompt_hash, response) "
        "VALUES (0, 1.0, 'chat', 'fake', 'abc123', 'legacy response')"
    )
    conn.executemany(
        "INSERT INTO schedule_events (seed, t_h, day, reason, status, fired_t_h) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (4242, 10.0, 0, "schedule", "pending", None),
            (4242, 26.5, 1, "schedule", "fired", 26.9),
        ],
    )
    conn.commit()
    conn.close()


def test_legacy_db_migrates_and_data_survives(tmp_path):
    db = tmp_path / "legacy.db"
    _build_legacy_db(db)

    store = SQLiteStore(db)  # migration runs inside __init__

    # version bookkeeping: exactly one row, at the current version
    rows = store.conn.execute("SELECT * FROM schema_meta").fetchall()
    assert len(rows) == 1
    assert rows[0]["version"] == SCHEMA_VERSION

    # all v2 tables + the memory_turns view exist
    names = {
        r["name"]
        for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }
    expected = {
        "persona", "interests", "life_arcs", "agenda_items",
        "proactive_intents", "memory_sessions", "memory_turns",
        "memory_session_summaries", "memory_episodes",
        "memory_episode_sources", "user_model_assertions",
        "memory_embeddings",
    }
    assert expected <= names

    # messages gained the additive session_id column; legacy rows are NULL
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(messages)")}
    assert "session_id" in cols
    assert cols >= {"id", "role", "content", "t_h", "day", "proactive", "meta"}

    # legacy daily state remains interpretable (m_level <-> m boundary intact)
    state = store.load_daily_state(0)
    assert state is not None
    assert state["M"] == 7 and state["m"] == 0.1 and state["score"] == 0.5

    # legacy messages remain present, ordered, with session_id NULL
    msgs = store.recent_messages()
    assert [m["content"] for m in msgs] == [
        "legacy hello", "legacy reply", "legacy proactive",
    ]
    assert all(m["session_id"] is None for m in msgs)
    assert store.proactive_count(0) == 1

    # legacy judgement survives
    j = store.load_judgement(0)
    assert j is not None and j["score"] == 0.8 and j["shadow"] == 1
    assert store.load_previous_judgement(1) == 0.8

    # legacy audit log survives
    assert store.events_since(0)[0]["event"] == "day_rollover"
    calls = store.conn.execute("SELECT * FROM llm_calls").fetchall()
    assert len(calls) == 1 and calls[0]["response"] == "legacy response"

    # legacy schedule rows survive with their statuses (never resurrected)
    sched = store.schedule_events_for_seed(4242)
    assert [(s["t_h"], s["status"]) for s in sched] == [
        (10.0, "pending"), (26.5, "fired"),
    ]
    assert store.last_proactive_t_h(4242) == 26.9

    store.close()


def test_migration_runs_twice_without_error(tmp_path):
    db = tmp_path / "legacy2.db"
    _build_legacy_db(db)

    store1 = SQLiteStore(db)
    # write through the new schema after migration
    store1.add_message("user", "post-migration", t_h=2.0, day=0, session_id="s1")
    store1.close()

    # re-open: migration must be a no-op, not an error, no duplicate rows
    store2 = SQLiteStore(db)
    rows = store2.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION

    # both legacy and post-migration data are intact
    msgs = store2.recent_messages()
    assert [m["content"] for m in msgs] == [
        "legacy hello", "legacy reply", "legacy proactive", "post-migration",
    ]
    assert store2.turns_for_session("s1")[0]["content"] == "post-migration"
    assert store2.load_daily_state(0)["score"] == 0.5
    assert store2.load_judgement(0)["score"] == 0.8
    assert len(store2.schedule_events_for_seed(4242)) == 2
    store2.close()


def test_fresh_db_reaches_v2_and_new_tables_are_usable(tmp_path):
    store = SQLiteStore(tmp_path / "fresh.db")
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION

    # vertical-slice tables work end to end on a migrated-from-empty DB
    store.save_persona(
        PersonaProfile(
            name="Lily",
            core="Curious and warm.",
            interests=(Interest("photography", "exact", 0.9),),
            routines=(Routine("morning walk", 0.25, 1.0, 0.9, 0.5),),
        )
    )
    assert store.load_persona().name == "Lily"
    store.upsert_life_arc(
        LifeArc("a1", "photography", "photography", 4, 0.37, "active",
                "practice portraits")
    )
    arc = store.get_life_arc("a1")
    assert arc is not None and arc.progress == 0.37
    store.save_agenda(
        4, DailyAgenda(4, (AgendaItem("i1", 9.0, 10.0, "portraits", "arc", "a1",
                                      0.8, "planned"),))
    )
    assert store.load_agenda(4) is not None
    store.save_proactive_intent(
        ProactiveIntent("p1", "arc progress", "life_arc", "a1", "progress",
                        100.0, 104.0, 0.7, "arc a1 at progress 0.37")
    )
    intent = store.load_proactive_intent("p1")
    assert intent is not None
    resolved = store.resolve_intent_source(intent)
    assert resolved is not None and resolved.id == "a1"
    store.close()
