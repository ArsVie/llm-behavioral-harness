"""The behavioural-signature exporter.

The contract that matters is read-only access: the live trial DB is opened
with ``mode=ro`` and must never be written, checkpointed or copied. These
tests build a throwaway DB with the same two tables and assert both the
happy path and that the exporter cannot mutate what it reads.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from behavioral_signature.export import DEFAULT_OUT, export_conv, main


def _conv_db(tmp_path, *, conv_id="conv-3", turns=3, closed=6.0):
    """A minimal DB with the two tables the exporter reads."""
    path = tmp_path / "companion.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, opened_t_h REAL, "
        "closed_t_h REAL)"
    )
    con.execute(
        "CREATE TABLE conversation_turns (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "conversation_id TEXT, speaker TEXT, text TEXT, t_h REAL, "
        "turn_index INTEGER)"
    )
    con.execute(
        "INSERT INTO conversations VALUES (?, ?, ?)", (conv_id, 1.0, closed)
    )
    for i in range(turns):
        con.execute(
            "INSERT INTO conversation_turns "
            "(conversation_id, speaker, text, t_h, turn_index) "
            "VALUES (?, ?, ?, ?, ?)",
            (conv_id, "user" if i % 2 == 0 else "companion",
             f"line {i}", 1.0 + i, i),
        )
    con.commit()
    con.close()
    return path


def test_export_reads_the_conversation_in_order(tmp_path):
    db = _conv_db(tmp_path, turns=4)
    record = export_conv(str(db), "conv-3")
    assert record.conversation_id == "conv-3"
    assert [t.turn_index for t in record.turns] == [0, 1, 2, 3]
    assert [t.speaker for t in record.turns] == [
        "user", "companion", "user", "companion"
    ]
    assert record.opened_t_h == 1.0 and record.closed_t_h == 6.0


def test_export_tolerates_null_times(tmp_path):
    """An open conversation has no closed_t_h; turns may lack a timestamp."""
    db = _conv_db(tmp_path, turns=1, closed=None)
    con = sqlite3.connect(db)
    con.execute("UPDATE conversation_turns SET t_h = NULL, turn_index = NULL")
    con.commit()
    con.close()
    record = export_conv(str(db), "conv-3")
    assert record.closed_t_h is None
    assert record.turns[0].t_h is None
    assert record.turns[0].turn_index is None


def test_export_of_an_empty_conversation(tmp_path):
    db = _conv_db(tmp_path, turns=0)
    assert export_conv(str(db), "conv-3").turns == ()


def test_missing_conversation_exits_with_a_named_error(tmp_path):
    db = _conv_db(tmp_path)
    with pytest.raises(SystemExit, match="conv-99"):
        export_conv(str(db), "conv-99")


def test_export_never_writes_to_the_live_db(tmp_path):
    """The read-only contract: opening for export must not touch the file.

    The trial DB is irreplaceable evidence, so the exporter opens it
    ``mode=ro`` and a write through that handle is refused by SQLite itself.
    """
    db = _conv_db(tmp_path)
    before = db.stat().st_mtime_ns
    export_conv(str(db), "conv-3")
    assert db.stat().st_mtime_ns == before
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM conversation_turns")
    finally:
        con.close()


# --- the CLI -------------------------------------------------------------


def test_main_writes_json_and_prints_the_signature(tmp_path, capsys):
    db = _conv_db(tmp_path, turns=4)
    out = tmp_path / "log.json"
    assert main(["--db", str(db), "--conv", "conv-3", "--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["conversation_id"] == "conv-3"
    assert len(payload["turns"]) == 4
    printed = capsys.readouterr().out
    assert f"wrote {out}" in printed and "4 turns" in printed
    # The signature itself is printed as JSON on the trailing lines.
    assert printed.rstrip().endswith("}")


def test_main_requires_db_and_conv(tmp_path):
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "x.db")])


def test_default_out_is_the_documented_fixture_path():
    """The default path is referenced from the module docstring and the
    fixture it feeds; a silent change would strand both."""
    assert DEFAULT_OUT == "tests/fixtures/conv3_log.json"
