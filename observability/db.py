"""Read-only access layer: run discovery, liveness and typed row reads.

Every connection this package opens goes through :func:`open_run`, which
uses a ``mode=ro`` URI — safe against a bot that is writing the database
right now (WAL included) and never able to migrate or repair anything.
"""

from __future__ import annotations

import fnmatch
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

#: A run whose files moved this recently counts as live (the only liveness
#: signal a file-backed run can offer without holding its lock).
ACTIVE_WINDOW_S: float = 180.0

#: Directories that never hold run databases.
SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".venv", "__pycache__", "node_modules", ".pytest_cache",
    ".ruff_cache", ".complexipy_cache",
})


@dataclass(frozen=True)
class RunRef:
    """One discovered run database on disk."""

    path: Path
    mtime: float
    wal_mtime: float | None

    @property
    def label(self) -> str:
        """Short display name: parent folder + file stem."""
        return f"{self.path.parent.name}/{self.path.stem}"

    def idle_seconds(self, now: float) -> float:
        """Seconds since the newest write to the db or its WAL sidecar."""
        newest = max([self.mtime] + ([self.wal_mtime] if self.wal_mtime else []))
        return max(0.0, now - newest)

    def is_active(self, now: float, window_s: float = ACTIVE_WINDOW_S) -> bool:
        return self.idle_seconds(now) <= window_s


def repo_root() -> Path:
    """The harness checkout this package ships inside."""
    return Path(__file__).resolve().parent.parent


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def find_runs(root: Path | None = None, *, limit: int = 60) -> list[RunRef]:
    """Every run database under ``root``, newest write first."""
    base = root or repo_root()
    found: list[RunRef] = []
    for path in base.rglob("*.db"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        mtime = _mtime(path)
        if mtime is None:
            continue
        found.append(RunRef(path=path, mtime=mtime,
                            wal_mtime=_mtime(path.with_name(path.name + "-wal"))))
    found.sort(key=lambda ref: ref.mtime, reverse=True)
    return found[:limit]


def live_processes(pattern: str = "live_companion") -> dict[str, int]:
    """Running ``pattern`` processes, keyed by the run database they serve.

    The file mtime says whether the run is *writing*; this says whether its
    process still exists, so a quiet bot can be told apart from a dead one.
    A relative ``--db`` resolves against this checkout, which is where the
    launcher runs from. Returns an empty map when ``ps`` is unavailable.
    """
    try:
        completed = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True,
                                   text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[str, int] = {}
    for line in completed.stdout.splitlines()[1:]:
        pid_text, _, args = line.strip().partition(" ")
        if pattern not in args or "--db" not in args:
            continue
        tail = args.split("--db", 1)[1].strip()
        raw = tail.split(" ")[0] if tail else ""
        if not raw:
            continue
        path = Path(raw) if raw.startswith("/") else repo_root() / raw
        found[str(path.resolve())] = int(pid_text) if pid_text.isdigit() else 0
    return found


#: Where the launchers that deploy a run for real live.
LAUNCHER_DIR: Path = Path.home() / ".hermes" / "scripts"

_DB_ASSIGN = re.compile(r'^\s*([A-Z_]+)="([^"]*)"', re.MULTILINE)
_DB_ARG = re.compile(r'--db\s+(?:"([^"]+)"|(\S+))')
_CHANNEL_ARG = re.compile(r"--channel\s+(\w+)")
_VARIABLE = re.compile(r"\$\{?(\w+)\}?")


@dataclass(frozen=True)
class Deployment:
    """One launcher script and the run database it points at."""

    channel: str
    script: str
    pattern: str

    def matches(self, path: Path | str) -> bool:
        return fnmatch.fnmatch(str(path), self.pattern)


def _expand(value: str, variables: dict[str, str], root: Path) -> str:
    """Resolve $REPO and friends; anything still unknown becomes a wildcard."""
    out = value
    for name, replacement in variables.items():
        out = out.replace(f"${{{name}}}", replacement).replace(f"${name}", replacement)
    out = _VARIABLE.sub("*", out)
    # Launchers cd into the checkout, so a relative --db is repo-relative.
    return out if out.startswith("/") else str(root / out)


def deployments(scripts_dir: Path | None = None,
                root: Path | None = None) -> list[Deployment]:
    """Every ``live_*.sh`` launcher, as a channel plus a run-path pattern.

    A deployed run is one a launcher points at, which is the only honest
    definition available: the run database itself records no channel. The
    launcher's ``--db`` value is read directly, and a value that comes from a
    shell variable is resolved when the script assigns it, otherwise the
    variable becomes a wildcard (the CLI launcher keeps its db under
    ``results/profiles/$PROFILE/``).
    """
    base = root or repo_root()
    where = scripts_dir or LAUNCHER_DIR
    found: list[Deployment] = []
    # Path.glob swallows filesystem errors, so the only failure left is a
    # script that cannot be read — guarded per script below.
    scripts = sorted(path for path in where.glob("live_*.sh")
                     if ".bak" not in path.name)
    for script in scripts:
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        channel = (_CHANNEL_ARG.search(text) or [None, "unknown"])[1]
        # The checkout the launcher runs from always wins for $REPO.
        variables = {name: value for name, value in _DB_ASSIGN.findall(text)}
        variables["REPO"] = str(base)
        arg = _DB_ARG.search(text)
        if arg is None:
            continue
        value = arg.group(1) or arg.group(2)
        found.append(Deployment(channel=str(channel), script=script.name,
                                pattern=_expand(value, variables, base)))
    return found


def deployed_runs(scripts_dir: Path | None = None,
                  root: Path | None = None) -> dict[str, Deployment]:
    """Run-database paths (as written by the launchers) to their deployment."""
    listed = deployments(scripts_dir=scripts_dir, root=root)
    base = root or repo_root()
    candidates = [path for path in base.glob("**/*.db")
                  if not any(part in SKIP_DIRS for part in path.parts)]
    resolved: dict[str, Deployment] = {}
    for path in candidates:
        for deployment in listed:
            if deployment.matches(path):
                resolved[str(path.resolve())] = deployment
                break
    return resolved


def deployment_for(path: Path | str, known: dict[str, Deployment] | None = None,
                   ) -> Deployment | None:
    """The deployment a run belongs to, or None when it is not deployed."""
    table = known if known is not None else deployed_runs()
    return table.get(str(Path(path).resolve()))


@contextmanager
def open_run(path: Path | str) -> Iterator[sqlite3.Connection]:
    """A read-only connection; never opens the live file for writing."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def rows(conn: sqlite3.Connection, sql: str,
         params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Tolerant query: a table missing from an older run reads as empty."""
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    except sqlite3.Error:
        return []


def count(conn: sqlite3.Connection, table: str) -> int:
    """Row count for ``table`` (0 when the table does not exist)."""
    try:
        row = conn.execute(f"select count(*) as n from {table}").fetchone()
    except sqlite3.Error:
        return 0
    return int(row["n"] if row is not None else 0)
