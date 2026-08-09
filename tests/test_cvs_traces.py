"""Smoke tests del generador de causal traces (plan §13, deliverable 10)."""

import json
import sqlite3

from experiments.cvs_traces import build_traces


def _db(tmp_path) -> str:
    db = tmp_path / "cell.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, role TEXT, content TEXT, t_h REAL,
            day INTEGER, proactive INTEGER, meta TEXT, session_id TEXT,
            intent_id TEXT
        );
        CREATE TABLE proactive_intents (
            id TEXT PRIMARY KEY, reason TEXT, source_type TEXT, source_id TEXT,
            hook TEXT, created_t_h REAL, valid_until_t_h REAL, salience REAL,
            evidence TEXT, status TEXT
        );
        CREATE TABLE agenda_items (
            id TEXT PRIMARY KEY, day INTEGER, start_t_h REAL, end_t_h REAL,
            activity TEXT, source_type TEXT, source_id TEXT, salience REAL,
            status TEXT
        );
        CREATE TABLE life_arcs (
            id TEXT PRIMARY KEY, name TEXT, interest TEXT, started_day INTEGER,
            progress REAL, status TEXT, next_intention TEXT
        );
        CREATE TABLE interests (
            name TEXT PRIMARY KEY, bucket TEXT, salience REAL
        );
        CREATE TABLE schedule_events (
            id INTEGER PRIMARY KEY, seed INTEGER, t_h REAL, day INTEGER,
            reason TEXT, status TEXT, fired_t_h REAL
        );
        CREATE TABLE memory_episodes (
            id TEXT PRIMARY KEY, kind TEXT, day INTEGER, summary TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO messages VALUES (1, 'assistant', 'hi', 15.3, 0, 1, '', 's', "
        "'pi_agenda_item_ag_0_i_movies_15.300')")
    conn.execute(
        "INSERT INTO proactive_intents VALUES "
        "('pi_agenda_item_ag_0_i_movies_15.300', 'schedule', 'agenda_item', "
        "'ag_0_i_movies', 'Agenda: movies', 15.0, 18.0, 0.8, "
        "'agenda_item:ag_0_i_movies activity=try a movie source=interest:movies "
        "ep_mem_1', 'fired')")
    conn.execute(
        "INSERT INTO agenda_items VALUES "
        "('ag_0_i_movies', 0, 15.0, 16.0, 'try a small movies exercise', "
        "'interest', 'movies', 0.78, 'completed')")
    conn.execute("INSERT INTO interests VALUES ('movies', 'exact', 0.8)")
    conn.execute(
        "INSERT INTO schedule_events VALUES (1, 5001, 15.300111, 0, 'schedule', "
        "'fired', 15.300111)")
    conn.execute(
        "INSERT INTO memory_episodes VALUES ('ep_mem_1', 'L4', 0, 'movie talk')")
    conn.commit()
    conn.close()
    return str(db)


def test_build_traces_full_chain(tmp_path):
    db = _db(tmp_path)
    records = tmp_path / "records.json"
    records.write_text(json.dumps({
        "controls_by_message": {"1": {"max_tokens": 560, "response_delay_s": 3.7,
                                      "closing_tendency": 0.4}},
        "directives_by_message": {"1": {"initiative": 0.4,
                                        "response_length_scale": 0.9}},
    }), encoding="utf-8")
    traces = build_traces(db, str(records), 5)
    assert len(traces) == 1
    t = traces[0]
    walk = [(s["type"], s["id"]) for s in t["walk"]]
    assert walk == [
        ("OutgoingMessage", 1),
        ("ProactiveIntent", "pi_agenda_item_ag_0_i_movies_15.300"),
        ("AgendaItem", "ag_0_i_movies"),
        ("IndependentInterest", "movies"),
    ]
    assert t["timing"]["delay_h"] == 0.0
    assert t["behavior"]["max_tokens"] == 560
    assert t["memory_context"][0]["id"] == "ep_mem_1"
    assert t["persisted_intent_id"] == "pi_agenda_item_ag_0_i_movies_15.300"
