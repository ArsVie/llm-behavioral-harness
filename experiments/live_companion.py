"""Live companion — Telegram or CLI.

Full-stack wiring, zero new pieces in the runtime:
    config.select_channel('telegram'|'cli') -> AsyncRuntime.run()

- The runtime delivers proactive messages over the REAL channel (channel.send,
  proactive=True) and the channel's inbound enters through _on_inbound.
- The bot token is STOLEN from the Hermes agent configuration
  (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in ~/.hermes/.env) — the same
  messaging channel Hermes already uses; the harness merely consumes it.
- REAL time: time_scale = 3600 s/vh.
- The DB persists between sessions (resume-safe): reopening with the same
  path continues the story without rewind.

Usage:
    # lane token (LILY_TOKEN) lives in the repo-root .env:
    set -a; . <repo>/.env; set +a
    .venv/bin/python -m experiments.live_companion --channel telegram \
        --db results/live-companion/companion.db
    .venv/bin/python -m experiments.live_companion --channel cli \
        --db results/live-companion/companion.db

Optional flags (same surface as sim/run_async):
    --enable-commands   registers the slash-command handler.
                        Default OFF -> commands are dropped. With the
                        flag ON the channel registers the client command
                        menu via setMyCommands (/state is NEVER registered).
    --tz <IANA>         anchors the virtual clock to real time (HARNESS_TZ
                        as fallback; neither = no anchor).
    The other UX features (debounce HARNESS_DEBOUNCE with windows
    HARNESS_DEBOUNCE_TRAILING_S / HARNESS_DEBOUNCE_MAX_WAIT_S, typing
    HARNESS_TYPING, two-phase close HARNESS_TWO_PHASE_CLOSE) are enabled by
    env in the shared channel/session — the live entry already consumes them.

Gate check (without sending messages): the driver prints the channel
state at startup; `--check` only validates the token via getMe
(sends nothing) and exits.

Repo convention: English docstrings, English identifiers.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

from engine.rng import EXPERIMENT_STREAM, stream_rng
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.anchor import anchor_for_fresh_start
from harness.bootstrap import (
    DEFAULT_USER_INTERESTS,
    DEFAULT_USER_NAME,
    ensure_companion_initialized,
)
from harness.client import OpenAICompatibleClient
from harness.clock import VirtualClock
from harness.credentials import load_env_file
from harness.domain import UserProfile
from harness.env import env_bool as _env_bool
from harness.gateway import KV_SESSION_ID, new_session_id
from harness.judge import judge_day
from harness.memory import MemoryAgent, recall_embedder
from harness.runtime import (
    AsyncRuntime,
    IntentResolver,
    TimeScale,
    load_anchor,
    persist_anchor,
)
from harness.scheduler import REASON_SCHEDULE, ProactiveSchedule, day_scores
from harness.session import Session
from harness.store import SQLiteStore
from sim.run_async import CommandBridgeChannel, _commit_sha, build_command_callback

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Held poller locks (one fd per telegram profile): the OS releases the
#: flock when the fd closes, so the references must outlive the process.
_POLLER_LOCKS: list = []

# One virtual hour equals one real hour (live mode; the matrix cells use 0.0004).
LIVE_TIME_SCALE_S_PER_VH = 3600.0

#: Owner identity for a live trial; unset, the bootstrap falls back to the
#: ablation matrix fixture.
OWNER_NAME_ENV = "LILY_OWNER_NAME"
OWNER_INTERESTS_ENV = "LILY_OWNER_INTERESTS"       # comma-separated
COMPANION_NAME_ENV = "LILY_COMPANION_NAME"

#: How much unrun virtual time a resume may silently skip; beyond this the
#: entry refuses (--accept-resume-gap continues anyway).
MAX_RESUME_GAP_DAYS = 1.0


def check_resume_gap(store: SQLiteStore, anchor, now_epoch_s: float,
                     max_gap_days: float = MAX_RESUME_GAP_DAYS) -> str | None:
    """Describe an unsafe resume gap, or None when the resume is safe.

    ``anchor.t_h_at(now)`` is where the wall clock says she should be;
    ``latest_daily_state`` is the last day she lived. A gap beyond
    ``max_gap_days`` means ``Session.ensure_day`` would roll every missing
    day forward at the next midnight, finalising each through the judge.
    """
    latest = store.latest_daily_state()
    reached_day = int(latest["day"]) if latest else 0
    now_day = anchor.t_h_at(now_epoch_s) / 24.0
    gap = now_day - reached_day
    if gap <= max_gap_days:
        return None
    return (
        f"stale resume: the anchor maps now to virtual day {now_day:.1f} but "
        f"the store only reached day {reached_day} — {gap:.1f} days would be "
        f"manufactured at the first midnight (ensure_day finalises every missing "
        f"day through the judge). Archive the DB and start fresh, or pass "
        f"--accept-resume-gap to continue anyway."
    )


def configure_logging(db_path: Path) -> Path:
    """Send warnings and errors to stderr AND to a file beside the DB."""
    log_path = db_path.parent / "companion.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr),
                  logging.FileHandler(log_path, encoding="utf-8")],
    )
    # httpx logs every request at INFO; the turn traffic is already in the
    # llm_calls ledger.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    return log_path


def build_store(db_path: Path) -> SQLiteStore:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteStore(db_path, audit_mode=True)


def owner_profile() -> UserProfile:
    """The owner's identity from the environment, matrix fixture otherwise.

    ``LILY_OWNER_INTERESTS`` is a comma-separated list; blank entries
    are dropped.
    """
    name = (os.environ.get(OWNER_NAME_ENV) or "").strip() or DEFAULT_USER_NAME
    raw = os.environ.get(OWNER_INTERESTS_ENV) or ""
    interests = tuple(x.strip() for x in raw.split(",") if x.strip())
    return UserProfile(name=name, interests=interests or DEFAULT_USER_INTERESTS)


def rename_companion(store: SQLiteStore, new_name: str) -> bool:
    """Rename the persona in place, fixing the name inside the prose core.

    ``harness.persona.build_persona`` hard-codes ``DEFAULT_NAME`` ("Nova")
    and writes it into the core prose ("You are Nova, ..."), so the name is
    replaced there too. Returns True when a rename happened; only the name
    and the prose core change.
    """
    persona = store.load_persona()
    if persona is None or not new_name or persona.name == new_name:
        return False
    store.save_persona(replace(
        persona,
        name=new_name,
        core=persona.core.replace(persona.name, new_name),
    ))
    return True


def bootstrap(store: SQLiteStore, seed: int, client=None) -> None:
    """Initialise identity once (idempotent), with the owner's real profile.

    ``client`` is used ONCE, on a cold start, to place the owner's
    off-catalog interests into the interest graph; without it onboarding
    stays offline and falls back to the heuristic extension.
    """
    ensure_companion_initialized(
        store, seed=seed,
        user=owner_profile(),
        day=0,
        client=client,
        logger=lambda line: print(f"[setup] {line}", flush=True),
    )
    name = (os.environ.get(COMPANION_NAME_ENV) or "").strip()
    if name:
        rename_companion(store, name)


def build_session(store: SQLiteStore, seed: int, clock: VirtualClock, client,
                  judge, persona: PersonaParams, timing: TimingParams, *,
                  judge_client=None) -> Session:
    """The live companion's session. The persona profile comes from the
    store (the bootstrap is its source of truth), not from an argument."""
    return Session(
        store, persona=persona, timing=timing,
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=seed,
        client=client, clock=clock, judge=judge,
        feedback=True, synthetic_score=False,
        memory=MemoryAgent(store, embedder=recall_embedder),
        judge_client=judge_client,
        # Each character's cycle starts where her seed puts it, not all on
        # day 1 of her period.
        random_cycle_start=True,
    )


def run_session_id(store) -> str:
    """The run's provider session id, drawn once and saved in the DB. A reset
    clears it, so a new run gets a new session; a bad backend rotates it
    (``harness.gateway``)."""
    saved = store.get_kv(KV_SESSION_ID)
    if saved:
        return saved
    sid = new_session_id()
    store.set_kv(KV_SESSION_ID, sid)
    return sid


def build_runtime(store: SQLiteStore, seed: int, channel, *,
                  client=None, judge=None,
                  time_scale_s_per_vh: float = LIVE_TIME_SCALE_S_PER_VH,
                  anchor=None, clock=None,
                  enable_commands: bool = False) -> AsyncRuntime:
    if client is None:
        client = OpenAICompatibleClient(lane="product")
    if judge is None:
        judge = judge_day
    # Judge lane: the model judge gets a research-lane client so its spend
    # attributes there; a scripted judge makes no call and needs none.
    judge_client = OpenAICompatibleClient(lane="research") if judge is judge_day else None
    persona = PersonaParams()
    timing = TimingParams()
    clock = clock or VirtualClock(0.0)
    session = build_session(store, seed, clock, client, judge, persona, timing,
                            judge_client=judge_client)
    # Day 0 up-front: ensure_day(0) runs before planning, so day 0 is
    # planned with real state. On resume the row exists and planning is a no-op.
    if store.load_daily_state(0) is None:
        session.ensure_day(0)
    ProactiveSchedule.plan_and_persist(
        1, seed, persona, timing, store,
        reason=REASON_SCHEDULE, scores=day_scores(store, 0, timing),
    )
    return AsyncRuntime(
        session,
        ProactiveSchedule.restore(seed, store),
        channel=channel,
        store=store,
        timing=timing,
        seed=seed,
        time_scale=TimeScale(time_scale_s_per_vh),
        max_virtual_hours=None,  # live: Ctrl-C to exit
        resolver=IntentResolver(store, rng=stream_rng(seed, EXPERIMENT_STREAM)),
        sleeper=None,
        anchor=anchor,
        # Live policy: one failed provider response must not end a week-long run.
        survive_turn_failures=True,
        # A restart after a slept-through midnight still plans that day.
        plan_on_start=True,
        # The runtime's own command dispatch (full hook set: call, hangup,
        # presence, day edits). Without it the launcher's older bridge
        # callback serves commands and the newer hooks read as unavailable.
        enable_commands=enable_commands,
    )


def with_web_chat(channel, db_path: Path):
    """Telegram plus the web chat (/chat): one session, one history. Replies
    go back where the message came from; proactive messages stay on
    Telegram (``harness.channels.multi``)."""
    import sqlite3

    from harness.channels.multi import MultiChannel
    from harness.channels.web import WebChannel

    def newest_id() -> int:
        # Its own read-only connection: called on the event loop while a
        # turn may be writing on the store's.
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            return int(conn.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0)

    def presence_mode() -> str:
        # Same read-only pattern; the /chat page shows "here" vs "away" from
        # this. Unset or unrecognized reads as apart, matching presence_mode()
        # on the session.
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key = 'life.presence'").fetchone()
        return "together" if row is not None and row[0] == "together" else "apart"

    web = WebChannel(watermark=newest_id, presence=presence_mode)
    print(f"[live] web chat on http://127.0.0.1:{web.port}/web/", flush=True)
    return MultiChannel(channel, web)


async def _amain(channel_name: str, db_path: Path, seed: int,
                 check_only: bool, tz: str | None = None,
                 enable_commands: bool = False,
                 accept_resume_gap: bool = False,
                 defer_bootstrap: bool = False,
                 day_planner: bool | None = None) -> int:
    from harness.config import select_channel

    if check_only:
        if channel_name == "telegram":
            from harness.channels.telegram import TelegramChannel

            channel = TelegramChannel.from_env()
            ok = await channel.check_token()
            print(f"telegram token check: {'OK' if ok else 'FAILED'}")
            return 0 if ok else 1
        print("--check only applies to the telegram channel")
        return 2

    # The planner is opt-in in the session; the live launcher turns it on
    # via HARNESS_DAY_PLANNER.
    if day_planner is not None:
        os.environ["HARNESS_DAY_PLANNER"] = "1" if day_planner else "0"

    log_path = configure_logging(db_path)
    store = build_store(db_path)
    # One provider session per run, not per process: OpenCode Go routes (and
    # caches) by it, so a restart keeps the run's cache.
    os.environ.setdefault("LLM_SESSION_ID", run_session_id(store))
    if channel_name == "telegram" and os.environ.get("TELEGRAM_BOT_TOKEN"):
        from harness.channels.telegram import acquire_poller_lock
        try:
            _POLLER_LOCKS.append(
                acquire_poller_lock(os.environ["TELEGRAM_BOT_TOKEN"]))
        except RuntimeError as exc:
            print(f"[live] {exc}", flush=True)
            return 4
    if defer_bootstrap and store.load_persona() is None:
        # Blank DB and the operator asked to onboard interactively: leave
        # identity uninitialized so /setup has something to do.
        print("[live] defer-bootstrap: companion NOT initialized — send "
              "/setup on the channel to onboard.", flush=True)
    else:
        bootstrap(store, seed, client=OpenAICompatibleClient(lane="product"))
    # Real-time anchor: a persisted (resume) anchor wins; otherwise a fresh
    # one is drawn from --tz/HARNESS_TZ and persisted.
    anchor = load_anchor(store)
    if anchor is None and tz:
        try:
            anchor = anchor_for_fresh_start(time.time(), tz)
        except Exception as exc:  # noqa: BLE001 - bad IANA name -> clean exit
            print(f"[live] invalid timezone {tz!r}: {exc}", flush=True)
            store.close()
            return 2
        persist_anchor(store, anchor)
    # Store write path: real timestamps resolve from the anchor at row
    # creation; without an anchor the *_at columns stay NULL.
    if anchor is not None:
        problem = check_resume_gap(store, anchor, time.time())
        if problem is not None and not accept_resume_gap:
            print(f"[live] {problem}", flush=True)
            store.close()
            return 3
        if problem is not None:
            print(f"[live] WARNING (--accept-resume-gap): {problem}", flush=True)
        store.attach_anchor(anchor)
    channel = select_channel(channel_name)
    if channel_name == "telegram" and _env_bool("HARNESS_WEB_CHAT"):
        channel = with_web_chat(channel, db_path)
    clock = VirtualClock(0.0)
    if enable_commands:
        if "on_command" not in inspect.signature(channel.start).parameters:
            print(
                "WARNING: --enable-commands given but the selected channel has no "
                "command seam (S3) — commands will be dropped.",
                flush=True,
            )
        else:
            # Same pattern as sim/run_async: CommandBridgeChannel wraps the
            # launcher callback; the runtime's own on_command wins when passed.
            channel = CommandBridgeChannel(
                channel,
                build_command_callback(
                    store,
                    clock,
                    seed=seed,
                    channel=channel,
                    anchor=anchor,
                    flags={"debug": _env_bool("HARNESS_DEBUG_COMMANDS")},
                    commit_sha=_commit_sha(),
                    request_tz_change=lambda name: store.set_kv(
                        "cmd.tz.pending", name
                    ),
                    request_mute=lambda hours: store.set_kv(
                        "cmd.mute.until_t_h", f"{clock.now_h() + hours:.3f}"
                    ),
                ),
            )
    runtime = build_runtime(store, seed, channel, anchor=anchor,
                            clock=clock, enable_commands=enable_commands)
    print(f"[live] channel={channel_name} "
          f"seed={seed} db={db_path} tz={anchor.tz if anchor else 'none'}"
          + (" commands=on" if enable_commands else ""),
          flush=True)
    print("[live] Ctrl-C to stop; the DB persists between sessions", flush=True)
    print(f"[live] log: {log_path}", flush=True)
    # systemd stops with SIGTERM, which by default kills the process before
    # any finally runs: turn it into the same cancellation Ctrl-C gives, so
    # the channel stops and the store closes cleanly.
    main_task = asyncio.current_task()
    if main_task is not None:
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGTERM, main_task.cancel)
    try:
        await runtime.run()
    except asyncio.CancelledError:
        print("\n[live] stopped — state persisted", flush=True)
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live companion — telegram or CLI.")
    parser.add_argument("--channel", choices=("cli", "telegram"), default="cli")
    parser.add_argument("--db", type=str, default="results/live-companion/companion.db")
    parser.add_argument("--seed", type=int, default=5001)
    parser.add_argument("--check", action="store_true",
                        help="validate the telegram token via getMe (no message sent)")
    parser.add_argument(
        "--day-planner", dest="day_planner", action="store_true", default=None,
        help="plan each day's activities with one model call at rollover "
             "(sets HARNESS_DAY_PLANNER=1). Without it the agenda uses the "
             "verb-template pool. --no-day-planner forces it off.")
    parser.add_argument(
        "--no-day-planner", dest="day_planner", action="store_false",
        help=argparse.SUPPRESS)
    parser.add_argument(
        "--defer-bootstrap", action="store_true",
        help="on a BLANK database, do NOT initialize at startup — send "
             "/setup on the channel to onboard instead (requires "
             "--enable-commands). Ignored once a persona row exists.")
    parser.add_argument("--tz", type=str, default=None,
                        help="IANA timezone for the real-time anchor (e.g. "
                             "America/Mexico_City); default HARNESS_TZ env; "
                             "neither = no anchor (pre-anchor behavior)")
    parser.add_argument(
        "--enable-commands", action="store_true",
        help="register the slash-command handler (S3). Default OFF -> "
             "start(on_message=..., on_command=None) -> commands are dropped, "
             "exactly like today. /state stays debug-only "
             "(HARNESS_DEBUG_COMMANDS=1) and is never registered in the "
             "client command menu.",
    )
    parser.add_argument(
        "--accept-resume-gap", action="store_true",
        help="resume even when the persisted anchor is far ahead of the last "
             "day the store actually reached (the intervening days are "
             "manufactured at the first midnight). Default: refuse.",
    )
    args = parser.parse_args(argv)
    # Sources the repo-root .env so the product-lane token (LILY_TOKEN) is
    # available.
    load_env_file(REPO_ROOT / ".env")
    tz = args.tz or os.environ.get("HARNESS_TZ") or None
    try:
        return asyncio.run(_amain(args.channel, Path(args.db), args.seed,
                                  args.check, tz,
                                  args.enable_commands,
                                  args.accept_resume_gap,
                                  args.defer_bootstrap,
                                  args.day_planner))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
