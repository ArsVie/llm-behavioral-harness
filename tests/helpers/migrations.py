"""Migration-test helpers (consolidated from test_store_migrations_v6/v7).

``table_counts`` and ``live_db_candidates`` were byte-identical (modulo
line-wrapping) in test_store_migrations_v6.py and v7.py. The table list is
passed in because each file names its own ``_LEGACY_TABLES``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def table_counts(con: sqlite3.Connection, tables) -> dict[str, int]:
    return {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in tables
    }


def live_db_candidates() -> list:
    here = __file__
    root = Path(here).resolve().parents[2]  # worktree root
    p = root / "results" / "live-companion" / "companion.db"
    if p.exists():
        return [p]
    return []


def seed_conversation_families(path, *, extra_intents: bool = False) -> None:
    """Seed the conversation/message/daily_state families on a v5/v6 DB.

    Consolidated from test_store_migrations_v6._seed_v5 and
    test_store_migrations_v7._seed_v6 — the v7 variant additionally inserts
    an agenda item + proactive intent (``extra_intents=True``).
    """
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
    if extra_intents:
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
