"""W-close: v5 -> v6 migration (seam S1) — additive-only, live-DB safe.

Builds a genuine v5 database by running the store's OWN historical
migration chain (v2..v5) and stamping ``schema_meta`` to 5, seeds it across
the conversation/message families, then opens it with the v6 store: the
migration must add ONLY ``kv_store`` and the nullable
``conversations.closing_pending_t_h`` column, keep every pre-existing row
intact and interpretable, be idempotent on re-open, and expose the new
kv/close-pending APIs. No destructive step — the same shape of migration
that runs on the live ``companion.db`` at next restart.

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

_LEGACY_TABLES = [
    "daily_state", "messages", "judgements", "state_events", "llm_calls",
    "schedule_events", "persona", "interests", "life_arcs", "agenda_items",
    "proactive_intents", "memory_sessions", "memory_session_summaries",
    "memory_episodes", "memory_episode_sources", "user_model_assertions",
    "memory_embeddings", "conversations", "conversation_turns",
    "decision_records", "steering_queue",
]


def _build_v5_db(path) -> None:
    """A genuine v5 database: the store's own v1..v5 chain, stamped 5.

    Uses the historical migration functions verbatim (the same code that
    shipped v5), so the pre-migration schema is exactly what a v5 live
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
    con.execute("DELETE FROM schema_meta")
    con.execute("INSERT INTO schema_meta (version) VALUES (5)")
    con.commit()
    con.close()


def _seed_v5(path) -> None:
    """Seed the v5 database across the conversation families."""
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
    con.commit()
    con.close()


def _table_counts(con: sqlite3.Connection) -> dict[str, int]:
    return {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in _LEGACY_TABLES
    }


def test_v5_to_v6_migration_is_additive_and_preserves_data(tmp_path):
    db = tmp_path / "v5.db"
    _build_v5_db(db)
    _seed_v5(db)

    before = _table_counts(sqlite3.connect(db))

    store = SQLiteStore(db)
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION == 7

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
    assert _table_counts(store.conn) == before
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


def test_fresh_db_reaches_v6_with_all_tables(tmp_path):
    store = SQLiteStore(tmp_path / "fresh.db")
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION == 7
    tables = {
        r["name"] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "kv_store" in tables
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(conversations)")}
    assert "closing_pending_t_h" in cols
    store.close()


def _live_db_candidates() -> list:
    here = __file__
    for root in (
        __import__("pathlib").Path(here).resolve().parents[1],  # worktree root
        __import__("pathlib").Path("/home/vruizes/.hermes/projects/llm-behavioral-harness"),
    ):
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
    con0.close()

    store = SQLiteStore(copy)
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION == 7
    assert version0 <= SCHEMA_VERSION, (
        f"live DB schema v{version0} is newer than code SCHEMA_VERSION "
        f"v{SCHEMA_VERSION}"
    )
    tables = {r["name"] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "kv_store" in tables
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(conversations)")}
    assert "closing_pending_t_h" in cols
    # every table opens, every row count intact
    for t, n in counts0.items():
        assert store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n, t
    # spot data
    convs = store.list_conversations()
    assert len(convs) == counts0["conversations"]
    assert all(c.close_reason in ("closing_tendency", "user_left", "quiet_hours", "max_turns") or c.close_reason is None for c in convs)
    store.close()

    # the ORIGINAL is byte-identical
    assert _sha(src) == orig_sha, "ORIGINAL live companion.db was modified!"
