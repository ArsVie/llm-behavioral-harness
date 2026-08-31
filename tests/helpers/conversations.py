"""Conversation-table seam builder (consolidated from test_cvs_preflight +
test_validation).

Both files created the B2 conversations/conversation_turns tables
byte-identically; test_cvs_preflight also seeded one conversation with rows.
``with_rows`` selects that extra seeding.
"""

from __future__ import annotations


def seed_conversation_tables(store, *, with_rows: bool = False) -> None:
    """Create the B2 conversation seam tables on ``store.conn``.

    With ``with_rows=True`` also inserts a single conversation (c1) with
    four turns, exactly as test_cvs_preflight did.
    """
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS conversations ("
        " id TEXT PRIMARY KEY, opened_t_h REAL, closed_t_h REAL,"
        " opened_by TEXT, close_reason TEXT)"
    )
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_turns ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT,"
        " speaker TEXT, text TEXT, t_h REAL, turn_index INTEGER)"
    )
    if with_rows:
        store.conn.execute(
            "INSERT INTO conversations (id, opened_t_h, opened_by) "
            "VALUES ('c1', 0.0, 'user')"
        )
        store.conn.execute(
            "INSERT INTO conversation_turns "
            "(conversation_id, speaker, text, t_h, turn_index) "
            "VALUES ('c1', 'user', 'hello', 0.0, 0), "
            "       ('c1', 'companion', 'hi!', 0.1, 1), "
            "       ('c1', 'user', 'how are you?', 0.2, 2), "
            "       ('c1', 'companion', 'great!', 0.3, 3)"
        )
    store.conn.commit()
