"""SQLite persistence for the harness (W-E1).

Pattern follows Hermes session storage: single SQLite file, WAL mode, append-
only trace tables alongside canonical state tables. Canonical tables hold the
current truth (daily_state, messages, judgements); `state_events` and
`llm_calls` are the audit/replay log (model, prompt hash, seed, clock time,
state version recorded per call).

Tables (slice scope of DESIGN.md data model):
  - daily_state(day PK, M, m, g, p, arg, mu, eta, cycle_day, phase_label,
    seed, score)
  - messages(id PK, role, content, t_h, day, proactive, meta)
  - judgements(day PK, score, justification, model, shadow)
  - state_events(id PK, day, t_h, event, detail)
  - llm_calls(id PK, day, t_h, role, model, prompt_hash, response, meta)

All writes go through `conn` transactions; reads are plain SELECTs. No
secrets are stored (credentials stay in the environment).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

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
"""


def _hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


class SQLiteStore:
    """Thin wrapper over sqlite3 with the harness schema."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

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
        self, role: str, content: str, t_h: float, day: int, proactive: bool = False
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages (role, content, t_h, day, proactive) "
            "VALUES (?, ?, ?, ?, ?)",
            (role, content, t_h, day, int(proactive)),
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
    ) -> None:
        self.conn.execute(
            "INSERT INTO llm_calls (day, t_h, role, model, prompt_hash, response, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                day,
                t_h,
                role,
                model,
                _hash(prompt),
                response,
                json.dumps(meta) if meta else None,
            ),
        )
        self.conn.commit()

    def events_since(self, day: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM state_events WHERE day >= ? ORDER BY id", (day,)
        ).fetchall()
        return [dict(r) for r in rows]
