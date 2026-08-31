"""CLI exporter: conversation log from the LIVE trial DB → canonical JSON.

Read-only contract: the live DB is opened with ``sqlite3.connect(f"file:{db}?"
"mode=ro", uri=True)`` only — never read-write, never checkpointed, never
copied. The exporter is product surface: any conversation log → canonical
JSON (see ``log.log_to_json``).

Usage (from the worktree root):

    .venv/bin/python \\
        -m behavioral_signature.export \\
        --db results/live-companion/companion.db \\
        --conv conv-3

Writes ``tests/fixtures/conv3_log.json`` (override with ``--out``) and prints
the computed 8-metric signature to stdout.
"""

from __future__ import annotations

import argparse
import sqlite3

from .log import LogRecord, LogTurn, log_to_json
from .metrics import compute_signature, signature_to_json

DEFAULT_OUT = "tests/fixtures/conv3_log.json"


def export_conv(db_path: str, conv_id: str) -> LogRecord:
    """Read one conversation from the live DB (read-only) as a LogRecord."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        row = cur.execute(
            "SELECT id, opened_t_h, closed_t_h FROM conversations WHERE id = ?",
            (conv_id,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"conversation {conv_id!r} not found in {db_path}")
        turns = []
        for speaker, text, t_h, turn_index in cur.execute(
            "SELECT speaker, text, t_h, turn_index FROM conversation_turns "
            "WHERE conversation_id = ? ORDER BY turn_index",
            (conv_id,),
        ):
            turns.append(
                LogTurn(
                    speaker=speaker,
                    text=text,
                    t_h=None if t_h is None else float(t_h),
                    turn_index=None if turn_index is None else int(turn_index),
                    conversation_id=conv_id,
                )
            )
    finally:
        con.close()
    return LogRecord(
        conversation_id=conv_id,
        turns=tuple(turns),
        opened_t_h=None if row[1] is None else float(row[1]),
        closed_t_h=None if row[2] is None else float(row[2]),
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m behavioral_signature.export",
        description="Export a conversation from the live trial DB (read-only) "
        "to canonical JSON and print its behavioral signature.",
    )
    ap.add_argument("--db", required=True, help="path to the live companion.db")
    ap.add_argument("--conv", required=True, help="conversation id, e.g. conv-3")
    ap.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help=f"output JSON path (default: {DEFAULT_OUT})",
    )
    args = ap.parse_args(argv)

    record = export_conv(args.db, args.conv)
    payload = log_to_json(record)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(payload)
    sig = compute_signature(record)
    print(f"wrote {args.out} ({len(record.turns)} turns, {record.conversation_id})")
    print(signature_to_json(sig), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
