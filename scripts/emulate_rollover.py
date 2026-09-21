#!/usr/bin/env python
"""Emulate a day rollover against a COPY of a live trial DB.

Why: the aux-cache fix (2026-09-20, commit 80f68de) changed what the nightly
judge and the day planner send — they now mirror the mainline tool menu so
the whole prefix banks on the provider cache. Waiting a day per verification
is slow, so this script replays the rollover on demand, against the REAL
provider, WITHOUT touching the live trial:

  1. back up the source DB (opened read-only) into a scratch workdir;
  2. rewind the copy: delete ``daily_state`` rows past ``--day`` so the
     session resume lands on ``--day`` (the evening the rollover should run);
  3. build the session exactly as the live launcher would (same condition,
     seed, lanes, persona file, anchor);
  4. run one real mainline turn (the day's last user message) — this warms
     the provider cache for [system][tool schemas][conversation] exactly as
     the live trial does;
  5. fire the OLD-shape judge (fork without tools) — the pre-fix cache break;
  6. fire the FIXED judge via ``session._aux_task_request`` — banks the
     mainline prefix, and records the tool menu on the ledger row;
  7. fire the day planner (``session._generate_agenda``) — banks it too.

Numbers come from the copy's ``llm_calls`` ledger (the rows the observability
app reads) plus the wire dumps under ``<workdir>/wire/``. The source DB is
never opened for writing.

Usage:
  .venv/bin/python scripts/emulate_rollover.py [--db PATH] [--day N]
      [--workdir DIR] [--skip-turn] [--skip-ab] [--skip-planner]

Credentials: loads ``~/.hermes/.env`` then ``<repo>/.env`` (lane tokens and
base URLs); values are never printed. HARNESS_* defaults mirror the live
launcher (``~/.hermes/scripts/live_telegram.sh``) unless already set.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> int:
    """Source KEY=VALUE lines into os.environ (source semantics, no echo)."""
    if not path.is_file():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        os.environ[key] = value.strip().strip("'\"")
        loaded += 1
    return loaded


def mirror_launcher_env() -> None:
    """The live launcher's HARNESS_* exports, overridable from the shell."""
    os.environ.setdefault("HARNESS_TZ", "America/Chihuahua")
    os.environ.setdefault("LILY_COMPANION_NAME", "Lily")
    os.environ.setdefault("LILY_OWNER_NAME", "Ars")
    os.environ.setdefault(
        "HARNESS_PERSONA_FILE", str(Path.home() / ".hermes" / "personas" / "lily.md")
    )
    os.environ.setdefault("LILY_OWNER_INTERESTS", "mathematics,lifting,anime,history")
    os.environ.setdefault("HARNESS_THINKING_EFFORT", "low")
    os.environ.setdefault("HARNESS_TWO_PHASE_CLOSE", "1")
    os.environ["HARNESS_DAY_PLANNER"] = "1"


def backup_readonly(src: Path, dst: Path) -> None:
    """Consistent copy of a possibly-WAL source, opened strictly read-only."""
    reader = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    writer = sqlite3.connect(str(dst))
    try:
        reader.backup(writer)
    finally:
        reader.close()
        writer.close()


def ledger_rows(db: Path, limit: int) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, day, role, model, prompt_tokens, cached_tokens, "
            "cache_miss_tokens, completion_tokens, repro_json "
            "FROM llm_calls ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def describe(row: dict) -> str:
    prompt = int(row.get("prompt_tokens") or 0)
    cached = int(row.get("cached_tokens") or 0)
    pct = f"{100.0 * cached / prompt:5.1f}%" if prompt else "  n/a "
    repro = {}
    try:
        repro = json.loads(row.get("repro_json") or "{}")
    except ValueError:
        pass
    kw = repro.get("kwargs") or {}
    tools = repro.get("tool_names") or kw.get("tool_names") or []
    if not tools and str(row.get("role") or "").startswith(("chat", "tool_decide")):
        # Mainline rows written after the tool-name record was dropped ride
        # the constant menu; read it from the code that defines it.
        from harness.tools import TOOL_SCHEMAS

        tools = [t["name"] for t in TOOL_SCHEMAS]
    has_tools = kw.get("tools") is not None or bool(tools)
    return (
        f"#{row['id']:<4} {str(row.get('role')):<22} prompt={prompt:>6} "
        f"cached={cached:>6} ({pct}) miss={int(row.get('cache_miss_tokens') or 0):>6} "
        f"tools={'yes' if has_tools else 'no '} "
        f"names={len(tools) if isinstance(tools, list) else '?'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="results/live-companion/companion.db")
    parser.add_argument(
        "--day", type=int, default=None,
        help="day to finalize/emulate (default: latest recorded day minus one)",
    )
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--seed", type=int, default=8001)
    parser.add_argument("--condition", default="FULL")
    parser.add_argument("--skip-turn", action="store_true",
                        help="do not run the warming mainline turn")
    parser.add_argument("--skip-ab", action="store_true",
                        help="skip the old-shape (no-tools) judge call")
    parser.add_argument("--skip-planner", action="store_true")
    args = parser.parse_args()

    src = (REPO / args.db).resolve()
    if not src.is_file():
        print(f"source DB not found: {src}")
        return 2

    stamp = time.strftime("%Y%m%d_%H%M%S")
    workdir = Path(
        args.workdir
        or Path.home() / ".hermes" / "cache" / "scratch" / f"emulate-rollover-{stamp}"
    ).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    copy_db = workdir / "companion.db"
    assert copy_db != src, "the emulation must run on the copy, never the source"

    env_home = load_env_file(Path.home() / ".hermes" / ".env")
    env_repo = load_env_file(REPO / ".env")
    mirror_launcher_env()
    print(f"workdir: {workdir}")
    print(f"env loaded: ~/.hermes/.env={env_home} keys, repo/.env={env_repo} keys (values hidden)")

    # 1. backup
    backup_readonly(src, copy_db)
    print(f"backup: {copy_db.name} ({copy_db.stat().st_size} bytes)")

    # 2. rewind the copy
    conn = sqlite3.connect(str(copy_db))
    latest = conn.execute("SELECT MAX(day) FROM daily_state").fetchone()[0]
    target = args.day if args.day is not None else int(latest) - 1
    if target < 0:
        print(f"no completed day to emulate (latest={latest})")
        return 2
    deleted = conn.execute("DELETE FROM daily_state WHERE day > ?", (target,)).rowcount
    conn.commit()
    conn.close()
    print(f"rewind: latest day was {latest}; deleted {deleted} daily_state row(s) "
          f"past day {target}; resume will land on day {target}")

    # 3. build the session exactly as the launcher would
    sys.path.insert(0, str(REPO))
    from engine.types import MoodVariant, PersonaParams, TimingParams
    from experiments.cvs_common import make_session
    from experiments.live_companion import bootstrap, build_store
    from harness.client import OpenAICompatibleClient
    from harness.clock import VirtualClock
    from harness.judge import judge_day
    from harness.runtime import load_anchor

    store = build_store(copy_db)
    bootstrap(store, args.seed, client=OpenAICompatibleClient(lane="product"))
    anchor = load_anchor(store)
    if anchor is not None:
        store.attach_anchor(anchor)
    client = OpenAICompatibleClient(lane="product")
    judge_client = OpenAICompatibleClient(lane="research")
    session = make_session(
        args.condition, args.seed, store,
        VirtualClock(float(target) * 24.0 + 23.95),
        client, judge_day, PersonaParams(), TimingParams(),
        MoodVariant.DECOUPLED_OFFSETS, judge_client=judge_client,
    )
    print(f"session: day={session.current_day} t_h={session.clock.now_h():.2f} "
          f"anchor={'attached' if anchor else 'none'}")

    # 4. warm the mainline cache with one real turn
    if not args.skip_turn:
        row = store.conn.execute(
            "SELECT content FROM messages WHERE day = ? AND role = 'user' "
            "ORDER BY id DESC LIMIT 1", (target,),
        ).fetchone()
        if row is None:
            print(f"no user message recorded on day {target}; cannot warm the cache")
            return 2
        text = row["content"]
        print(f"turn: replaying the day's last user message ({len(text)} chars)")
        result = session.on_message(text)
        reply = getattr(result, "reply", None) or getattr(result, "text", "") or ""
        print(f"turn: reply {len(str(reply))} chars; "
              f"stable_system={len(session._last_stable_system)} chars; "
              f"request_tools={len(session._last_request_tools or [])} schemas")
        if not session._last_stable_system or session._last_request_tools is None:
            print("FATAL: the turn did not prime the fork (no stable system/tools)")
            return 2

    transcript = session._transcript_for(target)

    # 5. old-shape judge (the pre-fix break)
    if not args.skip_ab:
        def fork_no_tools(task):
            system, messages, _tools = session._aux_task_request(task)
            return system, messages, None

        print("judge[old shape]: fork WITHOUT tools")
        old = session.judge(transcript, session.judge_client,
                            model=session.judge_model, fork=fork_no_tools)
        print(f"judge[old shape]: score={old.score:.3f}")

    # 6. fixed judge
    print("judge[fixed]: fork WITH the mirrored menu")
    fixed = session.judge(transcript, session.judge_client,
                          model=session.judge_model, fork=session._aux_task_request)
    print(f"judge[fixed]: score={fixed.score:.3f}")

    # 7. planner
    if not args.skip_planner:
        print(f"planner: generate agenda for day {target + 1}")
        session._generate_agenda(target + 1)

    # 8. report
    print("\n=== ledger (newest first; the rows the obs app reads) ===")
    rows = ledger_rows(copy_db, 12)
    report_lines = [describe(row) for row in rows]
    print("\n".join(report_lines))
    wire_dir = workdir / "wire"
    if wire_dir.is_dir():
        wires = sorted(wire_dir.iterdir())[-8:]
        print("\nwire dumps:")
        print("\n".join(f"  {w.name}" for w in wires))
    (workdir / "report.txt").write_text(
        "\n".join([f"source={src}", f"target_day={target}",
                   f"latest_before_rewind={latest}"] + report_lines) + "\n",
        encoding="utf-8",
    )
    print(f"\nreport: {workdir / 'report.txt'}")
    print("EMULATE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
