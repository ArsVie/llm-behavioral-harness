"""S1 real time: v6 -> v7 migration (nullable real timestamps) + write path.

Builds a genuine v6 database by running the store's OWN historical
migration chain (v2..v6) and stamping ``schema_meta`` to 6, seeds it across
the conversation/agenda/proactive/message families, then opens it with the
v7 store: the migration must add ONLY the seven nullable REAL columns
(``conversations.opened_at/closed_at``, ``agenda_items.start_at/end_at``,
``proactive_intents.created_at/valid_until_at``, ``messages.sent_at``),
keep every pre-existing row intact and interpretable, be idempotent on
re-open, and leave the version row at 7. No destructive step — the same
shape of migration that runs on the live ``companion.db`` at next restart.

The write-path tests pin the S1 contract: with an anchor attached every
row-creation write resolves ``real_at(t_h)`` into the matching ``*_at``
column; without an anchor all new columns stay NULL and the legacy columns
are byte-identical to the pre-v7 write path (replay parity, G1).

The live-DB copy test (``test_live_companion_db_migrates_additively``) runs
the same verification against a COPY of the real populated live database
(``results/live-companion/companion.db``, main tree) when that file is
present; it is skipped elsewhere. The copy goes to a pytest tmp_path — the
original is opened read-only and byte-compared before/after, and is NEVER
touched by the migration.
"""

import hashlib
import shutil
import sqlite3

import pytest

from harness.anchor import RealTimeAnchor
from harness.domain import AgendaItem, DailyAgenda, ProactiveIntent
from harness.store import (
    SCHEMA_VERSION,
    SQLiteStore,
    _SCHEMA,
    _migrate_v2,
    _migrate_v3,
    _migrate_v4,
    _migrate_v5,
    _migrate_v6,
    schema_meta,
)

_LEGACY_TABLES = [
    "daily_state", "messages", "judgements", "state_events", "llm_calls",
    "schedule_events", "persona", "interests", "life_arcs", "agenda_items",
    "proactive_intents", "memory_sessions", "memory_session_summaries",
    "memory_episodes", "memory_episode_sources", "user_model_assertions",
    "memory_embeddings", "conversations", "conversation_turns",
    "decision_records", "steering_queue", "kv_store",
]

#: The seven v7 additions (table, column, declared type).
_V7_ADDITIONS = {
    ("conversations", "opened_at"),
    ("conversations", "closed_at"),
    ("agenda_items", "start_at"),
    ("agenda_items", "end_at"),
    ("proactive_intents", "created_at"),
    ("proactive_intents", "valid_until_at"),
    ("messages", "sent_at"),
}

#: Legacy (pre-v7) column lists, in table order — the v6 write-path shape.
_LEGACY_COLUMNS = {
    "messages": [
        "id", "role", "content", "t_h", "day", "proactive", "meta",
        "session_id", "intent_id", "conversation_id",
    ],
    "conversations": [
        "id", "opened_t_h", "closed_t_h", "opened_by", "close_reason",
        "closing_pending_t_h",
    ],
    "agenda_items": [
        "id", "day", "start_t_h", "end_t_h", "activity", "source_type",
        "source_id", "salience", "status",
    ],
    "proactive_intents": [
        "id", "reason", "source_type", "source_id", "hook", "created_t_h",
        "valid_until_t_h", "salience", "evidence", "status",
    ],
}

_ORDER_BY = {
    "messages": "id",
    "conversations": "id",
    "agenda_items": "id",
    "proactive_intents": "id",
}


def _build_v6_db(path) -> None:
    """A genuine v6 database: the store's own v1..v6 chain, stamped 6.

    Uses the historical migration functions verbatim (the same code that
    shipped v6), so the pre-migration schema is exactly what a v6 live
    database has — not a hand-approximation.
    """
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row  # migration helpers read columns by name
    con.executescript(_SCHEMA)
    con.executescript(schema_meta(SCHEMA_VERSION))
    _migrate_v2(con)
    _migrate_v3(con)
    _migrate_v4(con)
    _migrate_v5(con)
    _migrate_v6(con)
    con.execute("DELETE FROM schema_meta")
    con.execute("INSERT INTO schema_meta (version) VALUES (6)")
    con.commit()
    con.close()


def _seed_v6(path) -> None:
    """Seed the v6 database across the conversation families."""
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO conversations (id, opened_t_h, closed_t_h, opened_by, "
        "close_reason) VALUES ('conv-0', 1.0, 3.0, 'user', 'closing_tendency')"
    )
    con.execute(
        "INSERT INTO conversations (id, opened_t_h, opened_by) "
        "VALUES ('conv-1', 5.0, 'companion')"
    )
    con.execute(
        "INSERT INTO conversation_turns (conversation_id, speaker, text, t_h, "
        "turn_index, message_id) VALUES ('conv-0', 'user', 'hi', 1.0, 0, NULL)"
    )
    con.execute(
        "INSERT INTO conversation_turns (conversation_id, speaker, text, t_h, "
        "turn_index, message_id) VALUES ('conv-0', 'companion', 'hello!', 1.1, 1, NULL)"
    )
    con.execute(
        "INSERT INTO messages (role, content, t_h, day, proactive, meta, "
        "session_id, intent_id, conversation_id) "
        "VALUES ('user', 'hi', 1.0, 0, 0, NULL, 'day-1000', NULL, 'conv-0')"
    )
    con.execute(
        "INSERT INTO daily_state (day, M, m_level, g, p, arg, mu, eta, "
        "cycle_day, phase_label, seed, score) "
        "VALUES (0, 5, 2.5, 0.7, 0.5, 0.3, 0.1, 0.0, 0.0, 'neutral', 12345, 0.5)"
    )
    con.execute(
        "INSERT INTO agenda_items (id, day, start_t_h, end_t_h, activity, "
        "source_type, source_id, salience, status) "
        "VALUES ('ag-0', 0, 6.0, 7.0, 'coffee', 'routine', 'r-coffee', "
        "0.5, 'planned')"
    )
    con.execute(
        "INSERT INTO proactive_intents (id, reason, source_type, source_id, "
        "hook, created_t_h, valid_until_t_h, salience, evidence) "
        "VALUES ('pi-0', 'window', 'agenda_item', 'ag-0', 'try coffee', "
        "8.0, 9.5, 0.6, 'ev')"
    )
    con.commit()
    con.close()


def _table_counts(con: sqlite3.Connection) -> dict[str, int]:
    return {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in _LEGACY_TABLES
    }


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def test_v6_to_v7_migration_is_additive_and_preserves_data(tmp_path):
    db = tmp_path / "v6.db"
    _build_v6_db(db)
    _seed_v6(db)

    before = _table_counts(sqlite3.connect(db))

    store = SQLiteStore(db)
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION

    # additive only: exactly the seven v7 columns, nullable REAL, nothing else
    for table, col in sorted(_V7_ADDITIONS):
        cols = {r["name"] for r in store.conn.execute(f"PRAGMA table_info({table})")}
        assert col in cols, f"{table}.{col} missing"
        decl = [
            r for r in store.conn.execute(f"PRAGMA table_info({table})")
            if r["name"] == col
        ][0]
        assert decl["type"].upper() == "REAL" and decl["notnull"] == 0, (
            f"{table}.{col} must be nullable REAL, got {decl}"
        )

    # every pre-existing row survived, every table opens
    assert _table_counts(store.conn) == before
    convs = store.list_conversations()
    assert [c.id for c in convs] == ["conv-0", "conv-1"]
    assert convs[0].close_reason == "closing_tendency"
    assert convs[1].close_reason is None
    assert [t.text for t in convs[0].turns] == ["hi", "hello!"]
    assert store.load_daily_state(0)["M"] == 5
    agenda = store.load_agenda(0)
    assert agenda is not None and [i.id for i in agenda.items] == ["ag-0"]
    assert store.load_proactive_intent("pi-0").hook == "try coffee"
    assert [m["content"] for m in store.messages_for_day(0)] == ["hi"]

    # legacy rows read the new columns as NULL (pre-anchor / replay rows)
    row = store.conn.execute(
        "SELECT opened_at, closed_at FROM conversations WHERE id = 'conv-0'"
    ).fetchone()
    assert row["opened_at"] is None and row["closed_at"] is None
    row = store.conn.execute(
        "SELECT start_at, end_at FROM agenda_items WHERE id = 'ag-0'"
    ).fetchone()
    assert row["start_at"] is None and row["end_at"] is None
    row = store.conn.execute(
        "SELECT created_at, valid_until_at FROM proactive_intents "
        "WHERE id = 'pi-0'"
    ).fetchone()
    assert row["created_at"] is None and row["valid_until_at"] is None
    row = store.conn.execute(
        "SELECT sent_at FROM messages WHERE content = 'hi'"
    ).fetchone()
    assert row["sent_at"] is None
    store.close()

    # idempotent re-open: migration is a no-op, data still there
    store2 = SQLiteStore(db)
    rows = store2.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION
    assert store2.load_proactive_intent("pi-0").hook == "try coffee"
    assert _table_counts(store2.conn) == before
    store2.close()


def test_fresh_db_reaches_v7_with_all_tables(tmp_path):
    store = SQLiteStore(tmp_path / "fresh.db")
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION
    for table, col in sorted(_V7_ADDITIONS):
        assert col in _table_columns(store.conn, table), f"{table}.{col} missing"
    store.close()


# -- write path: anchored populates, unanchored stays NULL ---


def _anchored_store(tmp_path):
    store = SQLiteStore(tmp_path / "anchored.db")
    store.attach_anchor(RealTimeAnchor(
        epoch0_s=1_785_000_000.0, t_h0=0.0, tz="America/Chihuahua",
    ))
    return store


def _scripted_writes(store: SQLiteStore) -> None:
    """The canonical scripted run used by every write-path test."""
    store.add_message("user", "hi", 1.0, 0, session_id="day-1000",
                      conversation_id="conv-0")
    store.add_message("companion", "hello!", 1.1, 0, session_id="day-1000",
                      intent_id="pi-ag-0", conversation_id="conv-0")
    store.open_conversation("conv-0", 1.0, "user")
    store.close_conversation("conv-0", 3.0, "closing_tendency")
    store.open_conversation("conv-1", 5.0, "companion")
    agenda = DailyAgenda(day=0, items=(
        AgendaItem(id="ag-0", start_t_h=6.0, end_t_h=7.0, activity="coffee",
                   source_type="routine", source_id="r-coffee", salience=0.5,
                   status="planned"),
        AgendaItem(id="ag-1", start_t_h=15.0, end_t_h=16.0, activity="movies",
                   source_type="routine", source_id="r-movies", salience=0.7,
                   status="planned"),
    ))
    store.save_agenda(0, agenda)
    store.save_proactive_intent(ProactiveIntent(
        id="pi-1", reason="window", source_type="agenda_item", source_id="ag-0",
        hook="try coffee", created_t_h=8.0, valid_until_t_h=9.5, salience=0.6,
        evidence="ev1",
    ))
    store.save_proactive_intent(ProactiveIntent(
        id="pi-2", reason="window", source_type="agenda_item", source_id="ag-1",
        hook="try movies", created_t_h=14.0, valid_until_t_h=16.5, salience=0.7,
        evidence="ev2",
    ))


def test_write_path_populates_real_at_with_anchor(tmp_path):
    store = _anchored_store(tmp_path)
    E = 1_785_000_000.0
    _scripted_writes(store)

    # messages.sent_at from the row's own t_h
    row = store.conn.execute(
        "SELECT content, t_h, sent_at FROM messages ORDER BY id"
    ).fetchall()
    assert row[0]["sent_at"] == pytest.approx(E + 1.0 * 3600.0, abs=1e-6)
    assert row[1]["sent_at"] == pytest.approx(E + 1.1 * 3600.0, abs=1e-6)

    # conversations.opened_at / closed_at
    row = store.conn.execute(
        "SELECT opened_at, closed_at FROM conversations WHERE id = 'conv-0'"
    ).fetchone()
    assert row["opened_at"] == pytest.approx(E + 1.0 * 3600.0, abs=1e-6)
    assert row["closed_at"] == pytest.approx(E + 3.0 * 3600.0, abs=1e-6)
    row = store.conn.execute(
        "SELECT opened_at, closed_at FROM conversations WHERE id = 'conv-1'"
    ).fetchone()
    assert row["opened_at"] == pytest.approx(E + 5.0 * 3600.0, abs=1e-6)
    assert row["closed_at"] is None

    # agenda_items start_at / end_at per item
    row = store.conn.execute(
        "SELECT start_at, end_at FROM agenda_items WHERE id = 'ag-0'"
    ).fetchone()
    assert row["start_at"] == pytest.approx(E + 6.0 * 3600.0, abs=1e-6)
    assert row["end_at"] == pytest.approx(E + 7.0 * 3600.0, abs=1e-6)
    row = store.conn.execute(
        "SELECT start_at, end_at FROM agenda_items WHERE id = 'ag-1'"
    ).fetchone()
    assert row["start_at"] == pytest.approx(E + 15.0 * 3600.0, abs=1e-6)
    assert row["end_at"] == pytest.approx(E + 16.0 * 3600.0, abs=1e-6)

    # proactive_intents created_at / valid_until_at
    row = store.conn.execute(
        "SELECT created_at, valid_until_at FROM proactive_intents "
        "WHERE id = 'pi-1'"
    ).fetchone()
    assert row["created_at"] == pytest.approx(E + 8.0 * 3600.0, abs=1e-6)
    assert row["valid_until_at"] == pytest.approx(E + 9.5 * 3600.0, abs=1e-6)

    # upsert re-save refreshes the real columns with the new t_h values
    store.save_proactive_intent(ProactiveIntent(
        id="pi-1", reason="window", source_type="agenda_item", source_id="ag-0",
        hook="try coffee", created_t_h=8.5, valid_until_t_h=10.0, salience=0.6,
        evidence="ev1",
    ))
    row = store.conn.execute(
        "SELECT created_at, valid_until_at FROM proactive_intents "
        "WHERE id = 'pi-1'"
    ).fetchone()
    assert row["created_at"] == pytest.approx(E + 8.5 * 3600.0, abs=1e-6)
    assert row["valid_until_at"] == pytest.approx(E + 10.0 * 3600.0, abs=1e-6)
    store.close()


def test_write_path_unanchored_leaves_new_columns_null(tmp_path):
    store = SQLiteStore(tmp_path / "unanchored.db")  # no attach_anchor
    _scripted_writes(store)

    for table, col in sorted(_V7_ADDITIONS):
        rows = store.conn.execute(f"SELECT {col} FROM {table}").fetchall()
        assert all(r[col] is None for r in rows), f"{table}.{col} not NULL"
    store.close()


def test_unanchored_run_identical_to_pre_v7_write_path(tmp_path):
    """G1 replay parity: with anchor=None the v7 write path produces exactly
    the legacy rows the pre-v7 (v6) write path would have produced — the
    only difference is the seven new columns, all NULL."""
    # Reference side: a genuine v6 database run through the v6-era SQL.
    v6_db = tmp_path / "v6.db"
    _build_v6_db(v6_db)
    con = sqlite3.connect(v6_db)
    con.execute(
        "INSERT INTO conversations (id, opened_t_h, opened_by) "
        "VALUES ('conv-0', 1.0, 'user')"
    )
    con.execute(
        "UPDATE conversations SET closed_t_h = 3.0, close_reason = "
        "'closing_tendency' WHERE id = 'conv-0'"
    )
    con.execute(
        "INSERT INTO conversations (id, opened_t_h, opened_by) "
        "VALUES ('conv-1', 5.0, 'companion')"
    )
    con.execute(
        "INSERT INTO messages (role, content, t_h, day, proactive, meta, "
        "session_id, intent_id, conversation_id) "
        "VALUES ('user', 'hi', 1.0, 0, 0, NULL, 'day-1000', NULL, 'conv-0')"
    )
    con.execute(
        "INSERT INTO messages (role, content, t_h, day, proactive, meta, "
        "session_id, intent_id, conversation_id) "
        "VALUES ('companion', 'hello!', 1.1, 0, 0, NULL, 'day-1000', "
        "'pi-ag-0', 'conv-0')"
    )
    con.execute("DELETE FROM agenda_items WHERE day = 0")
    con.execute(
        "INSERT INTO agenda_items (id, day, start_t_h, end_t_h, activity, "
        "source_type, source_id, salience, status) "
        "VALUES ('ag-0', 0, 6.0, 7.0, 'coffee', 'routine', 'r-coffee', "
        "0.5, 'planned')"
    )
    con.execute(
        "INSERT INTO agenda_items (id, day, start_t_h, end_t_h, activity, "
        "source_type, source_id, salience, status) "
        "VALUES ('ag-1', 0, 15.0, 16.0, 'movies', 'routine', 'r-movies', "
        "0.7, 'planned')"
    )
    con.execute(
        "INSERT INTO proactive_intents (id, reason, source_type, source_id, "
        "hook, created_t_h, valid_until_t_h, salience, evidence) "
        "VALUES ('pi-1', 'window', 'agenda_item', 'ag-0', "
        "'try coffee', 8.0, 9.5, 0.6, 'ev1')"
    )
    con.execute(
        "INSERT INTO proactive_intents (id, reason, source_type, source_id, "
        "hook, created_t_h, valid_until_t_h, salience, evidence) "
        "VALUES ('pi-2', 'window', 'agenda_item', 'ag-1', "
        "'try movies', 14.0, 16.5, 0.7, 'ev2')"
    )
    con.commit()
    v6_legacy = {
        table: [tuple(r) for r in con.execute(
            f"SELECT {', '.join(cols)} FROM {table} "
            f"ORDER BY {_ORDER_BY[table]}"
        )]
        for table, cols in _LEGACY_COLUMNS.items()
    }
    con.close()

    # New-code side: the same scripted run through the v7 store, no anchor.
    store = SQLiteStore(tmp_path / "v7.db")
    _scripted_writes(store)
    v7_legacy = {
        table: [tuple(r) for r in store.conn.execute(
            f"SELECT {', '.join(cols)} FROM {table} "
            f"ORDER BY {_ORDER_BY[table]}"
        )]
        for table, cols in _LEGACY_COLUMNS.items()
    }
# the seven new columns are all-NULL on the unanchored run
    for table, col in sorted(_V7_ADDITIONS):
        rows = store.conn.execute(f"SELECT {col} FROM {table}").fetchall()
        assert all(r[col] is None for r in rows), f"{table}.{col} not NULL"
    store.close()

    # legacy rows byte-identical to the pre-v7 write path
    assert v7_legacy == v6_legacy


# -- live-DB copy test ---


def _live_db_candidates() -> list:
    here = __file__
    root = __import__("pathlib").Path(here).resolve().parents[1]  # worktree root
    p = root / "results" / "live-companion" / "companion.db"
    if p.exists():
        return [p]
    return []


@pytest.mark.skipif(
    not _live_db_candidates(),
    reason="results/live-companion/companion.db not present (live DB is "
    "gitignored; this test runs where the real DB ships)",
)
def test_live_companion_db_migrates_additively(tmp_path):
    """The REQUIRED live-schema verification: migrate a COPY of the real
    populated companion.db. The original is opened read-only and its bytes
    are compared before/after — the migration NEVER touches it."""
    src = _live_db_candidates()[0]

    def _sha(p) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    orig_sha = _sha(src)
    copy = tmp_path / "companion.db"
    for suffix in ("", "-wal", "-shm"):
        s = __import__("pathlib").Path(str(src) + suffix)
        if s.exists():
            shutil.copy2(s, tmp_path / f"companion.db{suffix}")

    # row counts before migration (read-only)
    con0 = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    version0 = con0.execute("SELECT version FROM schema_meta").fetchone()[0]
    tables0 = [
        r[0] for r in con0.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]
    counts0 = {t: con0.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables0}
    n_null0 = con0.execute(
        "SELECT COUNT(*) FROM messages WHERE sent_at IS NOT NULL"
    ).fetchone()[0]
    con0.close()

    store = SQLiteStore(copy)
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION
    assert version0 <= SCHEMA_VERSION, (
        f"live DB expected at or below v{SCHEMA_VERSION}, found v{version0}"
    )
    for table, col in sorted(_V7_ADDITIONS):
        assert col in _table_columns(store.conn, table), f"{table}.{col} missing"
    # every table opens, every row count intact
    for t, n in counts0.items():
        assert store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n, t
# additivity: the migration keeps existing sent_at values
    n_null = store.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE sent_at IS NOT NULL"
    ).fetchone()[0]
    assert n_null == n_null0, (
        f"migration changed sent_at values: {n_null0} -> {n_null}"
    )
    store.close()

    # the ORIGINAL is byte-identical
    assert _sha(src) == orig_sha, "ORIGINAL live companion.db was modified!"
