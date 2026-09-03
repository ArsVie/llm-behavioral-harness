"""Companion en vivo — Telegram o CLI (integración it3, workstream del
usuario: "telegram and cli integration, steal the gate for the hermes
agent to test").

Wire del stack completo, cero piezas nuevas en el runtime:
    config.select_channel('telegram'|'cli') -> AsyncRuntime.run()

- El runtime entrega los proactivos por el canal REAL (channel.send,
  proactive=True) y el inbound del canal entra por _on_inbound.
- El token del bot se ROBA de la configuración del agente Hermes
  (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID en ~/.hermes/.env) — el mismo
  canal de mensajería que Hermes ya usa; el harness solo lo consume.
- Tiempo REAL: time_scale = 3600 s/vh (1 hora virtual = 1 hora real) —
  a diferencia de las células aceleradas de la matriz.
- El DB persiste entre sesiones (resume-safe, it2 A5): reabrir con la
  misma ruta continúa la historia sin rewind.

Uso:
    # lane token (LILY_TOKEN) vive en el .env de la raíz del repo:
    set -a; . <repo>/.env; set +a
    .venv/bin/python -m experiments.live_companion --channel telegram \
        --db results/live-companion/companion.db
    .venv/bin/python -m experiments.live_companion --channel cli \
        --db results/live-companion/companion.db

Flags opcionales (misma superficie que sim/run_async):
    --enable-commands   registra el handler de slash-commands (S3).
                        Default OFF -> los comandos se descartan, igual que
                        hoy. Con el flag ON el canal registra el menú de
                        comandos del cliente vía setMyCommands (/state
                        NUNCA se registra: contamina la lectura perceptual).
    --tz <IANA>         ancla el reloj virtual a tiempo real (HARNESS_TZ
                        como fallback; sin ninguno = sin ancla).
    Las demás features UX (debounce HARNESS_DEBOUNCE con ventanas
    HARNESS_DEBOUNCE_TRAILING_S / HARNESS_DEBOUNCE_MAX_WAIT_S, typing
    HARNESS_TYPING, two-phase close HARNESS_TWO_PHASE_CLOSE) se activan por
    env en el canal/sesión compartidos — el entry en vivo ya las consume.

Verificación de compuerta (sin enviar mensajes): el driver imprime el
estado del canal al arrancar; `--check` solo valida el token vía getMe
(no envía nada) y sale.

Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
import sys
import time
from pathlib import Path

from harness.domain import UserProfile
from harness.store import SQLiteStore
from engine.types import MoodVariant, PersonaParams, TimingParams
from experiments.cvs_common import (
    GATE2_USER_INTERESTS,
    REASON_SCHEDULE,
    DeterministicJudge,
    TimeScale,
    VirtualClock,
    make_session,
)
from harness.anchor import anchor_for_fresh_start
from harness.runtime import AsyncRuntime, IntentResolver, load_anchor, persist_anchor
from harness.scheduler import ProactiveSchedule, day_scores
from harness.client import OpenAICompatibleClient
from harness.judge import judge_day
from sim.run_async import CommandBridgeChannel, build_command_callback, _commit_sha
from harness.credentials import load_env_file
from harness.env import env_bool as _env_bool

REPO_ROOT = Path(__file__).resolve().parents[1]

# One virtual hour equals one real hour (live mode; the matrix cells use 0.0004).
LIVE_TIME_SCALE_S_PER_VH = 3600.0

#: Owner identity for a live trial. Without these the bootstrap falls back to
#: the ablation matrix's fixture — a user literally named "User" whose
#: interests are the matrix's four — so the companion spends the trial
#: talking to a stranger she was told she knows.
OWNER_NAME_ENV = "LILY_OWNER_NAME"
OWNER_INTERESTS_ENV = "LILY_OWNER_INTERESTS"       # comma-separated
COMPANION_NAME_ENV = "LILY_COMPANION_NAME"




#: How much unrun virtual time a resume may silently skip. Beyond this the
#: entry refuses: the anchor is a wall-clock line, so a DB parked for a week
#: maps "now" to a virtual day the simulation never lived through, and the
#: first midnight would manufacture every intervening day at once.
MAX_RESUME_GAP_DAYS = 1.0


def check_resume_gap(store: SQLiteStore, anchor, now_epoch_s: float,
                     max_gap_days: float = MAX_RESUME_GAP_DAYS) -> str | None:
    """Describe an unsafe resume gap, or None when the resume is safe.

    ``anchor.t_h_at(now)`` is where the wall clock says the companion should
    be; ``latest_daily_state`` is the last day she actually lived. When the
    first is far ahead of the second, ``Session.ensure_day`` will roll every
    missing day forward at the next midnight — finalising each one through
    the judge — so the trial would start on top of days that never happened.
    Refusing is the point: the operator archives the DB or accepts the gap
    explicitly.
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
    """Send warnings and errors to stderr AND to a file beside the DB.

    Nothing configured a handler before, so a failed turn produced at best a
    line on whatever terminal launched the bot — gone the moment the terminal
    closed. The file is what you read after an unattended night.
    """
    log_path = db_path.parent / "companion.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr),
                  logging.FileHandler(log_path, encoding="utf-8")],
    )
    # httpx logs every request at INFO; the turn traffic is already in the
    # llm_calls ledger, so keep the file readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    return log_path


def build_store(db_path: Path) -> SQLiteStore:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteStore(db_path, audit_mode=True)


def owner_profile() -> UserProfile:
    """The owner's identity from the environment, matrix fixture otherwise.

    ``LILY_OWNER_INTERESTS`` is a comma-separated list; blank entries are
    dropped so a trailing comma is harmless.
    """
    name = (os.environ.get(OWNER_NAME_ENV) or "").strip() or "User"
    raw = os.environ.get(OWNER_INTERESTS_ENV) or ""
    interests = tuple(x.strip() for x in raw.split(",") if x.strip())
    return UserProfile(name=name, interests=interests or GATE2_USER_INTERESTS)


def rename_companion(store: SQLiteStore, new_name: str) -> bool:
    """Rename the persona in place, fixing the name inside the prose core.

    ``harness.persona.build_persona`` hard-codes ``DEFAULT_NAME`` ("Nova")
    and writes it into the core prose ("You are Nova, ..."), so a bot the
    owner knows by another name introduces itself as Nova on turn one.
    Returns True when a rename happened. Identity is otherwise untouched —
    interests, routines and the seed-drawn portfolio are the persona's, not
    the name's.
    """
    persona = store.load_persona()
    if persona is None or not new_name or persona.name == new_name:
        return False
    from dataclasses import replace

    store.save_persona(replace(
        persona,
        name=new_name,
        core=persona.core.replace(persona.name, new_name),
    ))
    return True


def bootstrap(store: SQLiteStore, seed: int, user: UserProfile | None = None,
              companion_name: str | None = None) -> None:
    """Initialise identity once (idempotent), with the owner's real profile."""
    from harness.bootstrap import ensure_companion_initialized

    ensure_companion_initialized(
        store, seed=seed,
        user=user if user is not None else owner_profile(),
        day=0,
    )
    name = companion_name if companion_name is not None else (
        os.environ.get(COMPANION_NAME_ENV) or ""
    ).strip()
    if name:
        rename_companion(store, name)


def build_runtime(store: SQLiteStore, seed: int, condition: str,
                  channel, persona=None, timing=None,
                  max_virtual_hours: float | None = None,
                  client=None, judge=None,
                  time_scale_s_per_vh: float = LIVE_TIME_SCALE_S_PER_VH,
                  anchor=None, clock=None,
                  judge_client=None) -> AsyncRuntime:
    from experiments.cvs_common import stream_rng, rng_mod

    if client is None:
        client = OpenAICompatibleClient(lane="product")
    if judge is None:
        # The REAL judge, not the matrix's DeterministicJudge. The scripted
        # one returns a seed-keyed sinusoid with a hard-coded bad-mood block
        # on days 11-14, and the session runs feedback=True — so with it in
        # place the companion's mood answered a script, never the person she
        # was talking to. See harness/judge.py: the v2 rubric scores the
        # USER's treatment of the companion, and it still owes a monthly
        # separation re-check before its numbers are trusted.
        judge = judge_day
    # Judge lane: LLM judges get a research-lane client so judge spend
    # attributes to the research lane; offline judging keeps the product client.
    if judge_client is None and not isinstance(judge, DeterministicJudge):
        judge_client = OpenAICompatibleClient(lane="research")
    persona = persona or PersonaParams()
    timing = timing or TimingParams()
    variant = MoodVariant.DECOUPLED_OFFSETS
    clock = clock or VirtualClock(0.0)
    session = make_session(condition, seed, store, clock, client, judge,
                           persona, timing, variant, judge_client=judge_client)
    # Day 0 up-front: ensure_day(0) runs before planning, so day 0 is
    # planned with real state. On resume the row exists and planning is a no-op.
    if store.load_daily_state(0) is None:
        session.ensure_day(0)
    ProactiveSchedule.plan_and_persist(
        1, seed, persona, timing, store,
        reason=REASON_SCHEDULE, scores=day_scores(store, 0, timing),
    )
    rt = AsyncRuntime(
        session,
        ProactiveSchedule.restore(seed, store),
        channel=channel,
        store=store,
        timing=timing,
        seed=seed,
        time_scale=TimeScale(time_scale_s_per_vh),
        max_virtual_hours=max_virtual_hours,  # live: None (Ctrl-C to exit)
        resolver=IntentResolver(store, rng=stream_rng(seed, rng_mod.EXPERIMENT_STREAM)),
        sleeper=None,
        anchor=anchor,
        # Live policy: one failed provider response must not end a week-long
        # run. Experiment cells keep the fail-fast default.
        survive_turn_failures=True,
    )
    return rt


async def _amain(channel_name: str, db_path: Path, seed: int,
                 condition: str, check_only: bool, tz: str | None = None,
                 enable_commands: bool = False,
                 accept_resume_gap: bool = False) -> int:
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

    log_path = configure_logging(db_path)
    store = build_store(db_path)
    bootstrap(store, seed)
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
    runtime = build_runtime(store, seed, condition, channel, anchor=anchor,
                            clock=clock)
    print(f"[live] channel={channel_name} condition={condition} "
          f"seed={seed} db={db_path} tz={anchor.tz if anchor else 'none'}"
          + (" commands=on" if enable_commands else ""),
          flush=True)
    print("[live] Ctrl-C to stop; the DB persists between sessions", flush=True)
    print(f"[live] log: {log_path}", flush=True)
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
    parser.add_argument("--condition", type=str, default="FULL")
    parser.add_argument("--check", action="store_true",
                        help="validate the telegram token via getMe (no message sent)")
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
    # WS-C env bootstrap: sources the repo-root .env so the product-lane
    # token (LILY_TOKEN) is available.
    load_env_file(REPO_ROOT / ".env")
    tz = args.tz or os.environ.get("HARNESS_TZ") or None
    try:
        return asyncio.run(_amain(args.channel, Path(args.db), args.seed,
                                  args.condition, args.check, tz,
                                  args.enable_commands,
                                  args.accept_resume_gap))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
