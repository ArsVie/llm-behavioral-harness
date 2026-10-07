"""W-close: v5 -> v6 migration (seam S1) — additive-only, live-DB safe.

Builds a genuine v5 database by running the store's OWN historical migration
chain (v2..v5) and stamping ``schema_meta`` to 5, then opens it with the v6
store: only ``kv_store`` and the nullable ``conversations.closing_pending_t_h``
column are added, every pre-existing row stays intact, re-open is idempotent,
and the new kv/close-pending APIs work. The live-DB copy test lives in
test_store_migrations_v7.
"""

import sqlite3

from harness.store import (
    SCHEMA_VERSION,
    SQLiteStore,
    _SCHEMA,
    _migrate_v2,
    _migrate_v3,
    _migrate_v4,
    _migrate_v5,
    schema_meta,
)
from tests.helpers.migrations import seed_conversation_families, table_counts

_LEGACY_TABLES = [
    "daily_state", "messages", "judgements", "state_events", "llm_calls",
    "schedule_events", "persona", "interests", "life_arcs", "agenda_items",
    "proactive_intents", "memory_sessions", "memory_session_summaries",
    "memory_episodes", "memory_episode_sources", "user_model_assertions",
    "memory_embeddings", "conversations", "conversation_turns",
    "decision_records", "steering_queue",
]


def _build_v5_db(path) -> None:
    """A genuine v5 database: the store's own v1..v5 chain, stamped 5 (the
    historical migration functions verbatim, not a hand-approximation)."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row  # migration helpers read columns by name
    con.executescript(_SCHEMA)
    con.executescript(schema_meta(SCHEMA_VERSION))
    _migrate_v2(con)
    _migrate_v3(con)
    _migrate_v4(con)
    _migrate_v5(con)
    con.execute("DELETE FROM schema_meta")
    con.execute("INSERT INTO schema_meta (version) VALUES (5)")
    con.commit()
    con.close()


def test_v5_to_v6_migration_is_additive_and_preserves_data(tmp_path):
    db = tmp_path / "v5.db"
    _build_v5_db(db)
    seed_conversation_families(db)

    before = table_counts(sqlite3.connect(db), _LEGACY_TABLES)

    store = SQLiteStore(db)
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION

    # additive only: exactly the two v6 additions
    tables = {
        r["name"] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "kv_store" in tables
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(conversations)")}
    assert "closing_pending_t_h" in cols
    coltype = [
        r for r in store.conn.execute("PRAGMA table_info(conversations)")
        if r["name"] == "closing_pending_t_h"
    ][0]
    assert coltype["type"].upper() == "REAL" and coltype["notnull"] == 0

    # every pre-existing row survived, every table opens
    assert table_counts(store.conn, _LEGACY_TABLES) == before
    convs = store.list_conversations()
    assert [c.id for c in convs] == ["conv-0", "conv-1"]
    assert convs[0].close_reason == "closing_tendency"
    assert convs[1].close_reason is None
    assert [t.text for t in convs[0].turns] == ["hi", "hello!"]
    assert store.load_daily_state(0)["M"] == 5
    # legacy rows read the new column as NULL
    assert store.conversation_closing_pending("conv-0") is None
    assert store.conversation_closing_pending("conv-1") is None

    # new APIs work
    assert store.get_kv("nope") is None
    store.set_kv("k", "v1")
    store.set_kv("k", "v2")  # INSERT OR REPLACE
    assert store.get_kv("k") == "v2"
    store.set_conversation_closing_pending("conv-1", 6.5)
    assert store.conversation_closing_pending("conv-1") == 6.5
    store.set_conversation_closing_pending("conv-1", None)
    assert store.conversation_closing_pending("conv-1") is None
    store.close()

    # idempotent re-open: migration is a no-op, data still there
    store2 = SQLiteStore(db)
    rows = store2.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION
    assert store2.get_kv("k") == "v2"
    assert store2.conversation_closing_pending("conv-0") is None
    store2.close()
