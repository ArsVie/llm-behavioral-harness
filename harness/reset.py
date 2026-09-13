"""Reset a live run without losing the onboarding it paid for.

A run database holds two very different things at once:

- the TRIAL's own record — messages, decisions, steering, state events, calls,
  mood, memories, the anchor. This is what a reset is FOR.
- the ONBOARDING results — who the user is, the interest graph with their
  off-catalog interests placed into it, the built persona (which carries the
  routine catalog in ``routines_json``). Those cost model calls, and losing
  them means re-asking the provider and letting a second roll of the dice
  decide the user's hobbies.

Nothing could tell them apart, so resetting a run meant hand-editing the DB or
throwing the onboarding away with it. This module keeps the second list and
clears the first.

Guarantees:

- **Nothing is deleted.** The old database and its WAL sidecars are MOVED to
  ``<db_dir>/archive/pre-reset-<stamp>/``; the fresh database is built beside
  them and swapped in only after it is complete, so a failure mid-reset leaves
  the old run where it was.
- **A live writer is refused.** If a process has the database open (the bot
  service), the reset stops and says so unless ``force`` is set. Stopping the
  service is the caller's job (``reset_lily.sh`` does it).
- **Older databases still copy.** Only the columns both schemas share are
  carried over, so ageless tables do not break the move.
- ``--dry-run`` prints the whole plan and touches nothing.

The proposal cache the extension and the routine setup use is a FILE cache
(``~/.cache/harness/setup-proposals``), outside the database, so it survives a
reset by construction.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__all__ = [
    "PRESERVE_TABLES",
    "LEARNED_TABLES",
    "ResetPlan",
    "ResetRefused",
    "active_writer",
    "apply_reset",
    "common_columns",
    "plan_reset",
    "render_plan",
]

#: Onboarding results a reset KEEPS.
PRESERVE_TABLES: tuple[str, ...] = (
    "persona",              # name, authored core, routines_json (the catalog)
    "user_profile",         # who the user is, their stated interests
    "interests",            # the portfolio: bucket + salience per interest
    "interest_relations",   # the graph, incl. off-catalog interests placed by
                            # the onboarding model call (origin='model')
)

#: Learned during DIALOGUE rather than onboarding, and pointing at memory rows
#: the reset removes. Cleared by default; ``--keep-learned-facts`` keeps them.
LEARNED_TABLES: tuple[str, ...] = ("user_model_assertions",)

#: Every table the reset clears. Listed rather than inferred so a new table is
#: a visible decision instead of a silent survivor: anything the schema grows
#: that is on NEITHER list still gets cleared (a reset means a clean run) but it
#: is printed as unlisted, and ``tests/test_reset.py`` fails when the schema
#: grows a table this list does not know about.
CLEAR_TABLES: tuple[str, ...] = (
    "messages", "conversation_turns", "conversations", "decision_records",
    "steering_queue", "state_events", "schedule_events", "judgements",
    "llm_calls", "agenda_items", "proactive_intents", "daily_state",
    "life_arcs", "kv_store", "memory_episodes", "memory_episode_sources",
    "memory_embeddings", "memory_sessions", "memory_session_summaries",
)

#: Tables seeded by ``init_life`` / the engine from the seed alone, so clearing
#: them costs nothing: the same seed rebuilds them byte-for-byte.
REGENERATED_TABLES: tuple[str, ...] = ("life_arcs",)


class ResetRefused(RuntimeError):
    """The reset was not safe to run; nothing was touched."""


@dataclass
class ResetPlan:
    """What a reset would do, and to which files."""

    db: Path
    archive_dir: Path
    keep: list[str] = field(default_factory=list)
    learned: list[str] = field(default_factory=list)
    clear: list[str] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)
    unlisted: list[str] = field(default_factory=list)
    writer: str | None = None
    bytes_before: int = 0

    @property
    def keep_learned_facts(self) -> bool:
        return bool(self.learned)

    def as_dict(self) -> dict:
        return {
            "db": str(self.db),
            "archive_dir": str(self.archive_dir),
            "keep": list(self.keep),
            "learned": list(self.learned),
            "clear": list(self.clear),
            "rows": dict(self.rows),
            "writer": self.writer,
            "bytes_before": self.bytes_before,
        }


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [
        row["name"] for row in conn.execute(
            "select name from sqlite_master where type='table'"
            " and name not like 'sqlite_%' order by name"
        )
    ]


def file_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """The columns a table actually has (never assumed from the schema)."""
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def common_columns(old: list[str], new: list[str]) -> list[str]:
    """Columns present on BOTH sides, in the new table's order.

    A reset can meet a database written by an older schema; carrying a column
    the target does not have would abort the whole move, and dropping one the
    source lacks is the same. Copy the intersection.
    """
    keep = set(old)
    return [name for name in new if name in keep]


def active_writer(db_path: Path) -> str | None:
    """A live process holding this database, as its command line, or None.

    Two signals, because either alone is blind to a real case:

    - **Open file descriptors** (``/proc/<pid>/fd``) — the truth. The service
      launches the bot with a RELATIVE ``--db results/live-companion/...``, so a
      command-line comparison sees nothing while the bot has the file open.
      Matching the sidecars too catches a WAL writer between checkpoints.
    - **Command line** — names a database that is not open yet (a just-started
      process), which the fd scan would miss.

    The store takes no advisory lock, so there is nothing cheaper to ask.
    """
    try:
        target = db_path.resolve()
    except OSError:
        target = db_path.absolute()
    watched = {str(target), f"{target}-wal", f"{target}-shm"}
    mine = os.getpid()

    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == mine:
            continue
        pid = entry.name
        command = ""
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            ).strip()
        except OSError:
            pass
        if command and Path(command.split()[0]).name.startswith("python") and str(target) in command:
            return command
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                opened = os.readlink(fd)
            except OSError:
                continue
            if opened in watched:
                return command or f"pid {pid} holds {opened}"
    return None


def plan_reset(
    db_path: str | Path,
    *,
    keep_learned_facts: bool = False,
    archive_dir: str | Path | None = None,
    stamp: str | None = None,
) -> ResetPlan:
    """Describe the reset. Reads the database; changes nothing."""
    db = Path(db_path)
    if not db.exists():
        raise ResetRefused(f"no database at {db}")
    when = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    plan = ResetPlan(
        db=db,
        archive_dir=Path(archive_dir) if archive_dir else db.parent / "archive" / f"pre-reset-{when}",
        keep=[t for t in PRESERVE_TABLES],
        learned=list(LEARNED_TABLES) if keep_learned_facts else [],
        bytes_before=sum(
            f.stat().st_size for f in _sidecars(db) if f.exists()
        ),
    )
    with _ro(db) as conn:
        present = _tables(conn)
        plan.keep = [t for t in plan.keep if t in present]
        plan.learned = [t for t in plan.learned if t in present]
        documented = list(PRESERVE_TABLES) + list(LEARNED_TABLES) + list(CLEAR_TABLES)
        plan.clear = [t for t in present if t not in plan.keep + plan.learned
                      and t != "schema_meta"]
        plan.unlisted = [t for t in present if t not in documented and t != "schema_meta"]
        for table in plan.keep + plan.learned + plan.clear:
            plan.rows[table] = conn.execute(
                f"select count(*) as n from {table}"
            ).fetchone()["n"]
    plan.writer = active_writer(db)
    return plan


def _sidecars(db: Path) -> list[Path]:
    return [db, Path(f"{db}-wal"), Path(f"{db}-shm")]


def apply_reset(plan: ResetPlan, *, force: bool = False, writer: str | None = None) -> dict:
    """Do it: build the fresh database, archive the old one, swap.

    The new database is completed BEFORE anything moves, so an error here
    leaves the run exactly as it was. Nothing is deleted.
    """
    from harness.store import SQLiteStore

    db = plan.db
    writer = plan.writer if writer is None else writer
    if writer and not force:
        raise ResetRefused(
            f"a live process has this database open:\n  {writer}\n"
            "stop it first (systemctl --user stop lily-telegram.service) "
            "or pass force=True / --force"
        )
    if plan.archive_dir.exists():
        raise ResetRefused(f"archive target already exists: {plan.archive_dir}")

    staging = db.with_name(f"{db.name}.reset-staging")
    for leftover in _sidecars(staging):
        leftover.unlink(missing_ok=True)

    copied: dict[str, int] = {}
    try:
        target = SQLiteStore(staging)          # schema + migrations, current version
        try:
            with _ro(db) as source:
                for table in plan.keep + plan.learned:
                    if table not in _tables(source):
                        continue
                    columns = common_columns(
                        file_columns(source, table), file_columns(target.conn, table)
                    )
                    if not columns:
                        continue
                    rows = source.execute(f"select {', '.join(columns)} from {table}").fetchall()
                    if rows:
                        target.conn.executemany(
                            f"insert into {table} ({', '.join(columns)})"
                            f" values ({', '.join('?' * len(columns))})",
                            [tuple(row) for row in rows],
                        )
                        target.conn.commit()
                    copied[table] = len(rows)
        finally:
            target.close()

        plan.archive_dir.mkdir(parents=True, exist_ok=False)
        for sidecar in _sidecars(db):
            if sidecar.exists():
                shutil.move(str(sidecar), str(plan.archive_dir / sidecar.name))
        shutil.move(str(staging), str(db))
        for leftover in _sidecars(staging):
            leftover.unlink(missing_ok=True)
    except Exception:
        for leftover in _sidecars(staging):
            leftover.unlink(missing_ok=True)
        raise

    return {
        "db": str(db),
        "archive_dir": str(plan.archive_dir),
        "copied": copied,
        "cleared": [t for t in plan.clear],
        "bytes_before": plan.bytes_before,
        "bytes_after": sum(f.stat().st_size for f in _sidecars(db) if f.exists()),
    }


def render_plan(plan: ResetPlan) -> str:
    """The dry-run text: what is kept, what is dropped, where the old file goes."""
    lines = [
        f"database      {plan.db}  ({plan.bytes_before / 1024:.0f} KB incl. sidecars)",
        f"archive to    {plan.archive_dir}   (MOVED, never deleted)",
        "",
        "kept (onboarding paid for these):",
    ]
    for table in plan.keep + plan.learned:
        note = "  <- learned in dialogue, kept on request" if table in plan.learned else ""
        note = note or ("  <- carries the routine catalog" if table == "persona" else "")
        lines.append(f"  {table:<24} {plan.rows.get(table, 0):>5} rows{note}")
    lines.append("")
    lines.append("cleared (the trial's own record):")
    for table in plan.clear:
        mark = "  <- UNLISTED: add it to CLEAR_TABLES or PRESERVE_TABLES" \
            if table in plan.unlisted else ""
        lines.append(f"  {table:<24} {plan.rows.get(table, 0):>5} rows{mark}")
    lines.append("")
    if plan.writer:
        lines.append("a live process holds this database, so the reset would refuse")
        lines.append("until it stops:")
        lines.append(f"  {plan.writer}")
    else:
        lines.append("no live process holds this database")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m harness.reset",
        description="Reset a run database, keeping the onboarding cache.",
    )
    parser.add_argument("--db", required=True, help="the run database to reset")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and touch nothing")
    parser.add_argument("--force", action="store_true",
                        help="reset even if a process has the database open")
    parser.add_argument("--keep-learned-facts", action="store_true",
                        help="also keep user_model_assertions (dialogue-derived)")
    parser.add_argument("--archive-dir", default=None,
                        help="override the archive target")
    args = parser.parse_args(argv)

    plan = plan_reset(
        args.db,
        keep_learned_facts=args.keep_learned_facts,
        archive_dir=args.archive_dir,
    )
    if args.dry_run:
        print(render_plan(plan))
        print("\n(dry run: nothing was written)")
        return 0

    report = apply_reset(plan, force=args.force)
    print(render_plan(plan))
    print()
    print(f"reset done: {report['db']}")
    print(f"  archived  {report['archive_dir']}")
    print(f"  kept      {', '.join(f'{k}={v}' for k, v in report['copied'].items()) or 'nothing'}")
    print(f"  cleared   {len(report['cleared'])} tables")
    print(f"  size      {report['bytes_before'] / 1024:.0f} KB -> {report['bytes_after'] / 1024:.0f} KB")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    try:
        sys.exit(main())
    except ResetRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        sys.exit(2)
