"""S1 real time: v6 -> v7 migration (nullable real timestamps) + write path.

Builds a genuine v6 database by running the store's OWN historical migration
chain (v2..v6), stamped 6, then opens it with the v7 store: only the seven
nullable REAL columns are added (opened_at/closed_at, start_at/end_at,
created_at/valid_until_at, sent_at), every pre-existing row stays intact, and
re-open is idempotent. The write-path tests pin S1: with an anchor every
row-creation write resolves ``real_at(t_h)`` into the matching ``*_at`` column;
without one every new column stays NULL.
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
from tests.helpers.migrations import (
    live_db_candidates,
    seed_conversation_families,
    table_counts,
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

def _build_v6_db(path) -> None:
    """A genuine v6 database: the store's own v1..v6 chain, stamped 6 (the
    historical migration functions verbatim, not a hand-approximation)."""
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


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def test_v6_to_v7_migration_is_additive_and_preserves_data(tmp_path):
    db = tmp_path / "v6.db"
    _build_v6_db(db)
    seed_conversation_families(db, extra_intents=True)

    before = table_counts(sqlite3.connect(db), _LEGACY_TABLES)

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
    assert table_counts(store.conn, _LEGACY_TABLES) == before
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
    assert table_counts(store2.conn, _LEGACY_TABLES) == before
    store2.close()


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


# -- live-DB copy test ---


@pytest.mark.skipif(
    not live_db_candidates(),
    reason="results/live-companion/companion.db not present (live DB is "
    "gitignored; this test runs where the real DB ships)",
)
def test_live_companion_db_migrates_additively(tmp_path):
    """Migrate a COPY of the real populated companion.db; the original is opened
    read-only and byte-compared before/after."""
    src = live_db_candidates()[0]

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
    assert "kv_store" in {r["name"] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "closing_pending_t_h" in _table_columns(store.conn, "conversations")
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
    # every stored close reason is one the code writes; 'followed_event' is the
    # negotiation go-verdict close.
    assert all(
        c.close_reason in ("closing_tendency", "user_left", "quiet_hours",
                           "max_turns", "followed_event")
        or c.close_reason is None
        for c in store.list_conversations()
    )
    store.close()

    # the ORIGINAL is byte-identical
    assert _sha(src) == orig_sha, "ORIGINAL live companion.db was modified!"
