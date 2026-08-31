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
import os
import sys
import time
from pathlib import Path

from harness.channels.base import InboundMessage
from harness.domain import UserProfile
from harness.session import Session
from harness.store import SQLiteStore
from engine.types import MoodVariant, PersonaParams, TimingParams
from experiments.cvs_common import (
    BLOCK_END_D,
    BLOCK_START_D,
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
from sim.run_async import CommandBridgeChannel, build_command_callback, _commit_sha
from harness.credentials import load_env_file

REPO_ROOT = Path(__file__).resolve().parents[1]

# One virtual hour equals one real hour (live mode; the matrix cells use 0.0004).
LIVE_TIME_SCALE_S_PER_VH = 3600.0

# Persona constant (same as the matrix cells).
DEFAULT_PERSONA = "Ana"


def _env_bool(name: str, default: bool = False) -> bool:
    """Env bool with the harness convention (mirrors tools._env_bool):
    unset/empty -> default; truthy = 1/true/yes/on."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_store(db_path: Path) -> SQLiteStore:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteStore(db_path, audit_mode=True)


def bootstrap(store: SQLiteStore, seed: int) -> None:
    from harness.bootstrap import ensure_companion_initialized

    ensure_companion_initialized(
        store, seed=seed,
        user=UserProfile(name="User", interests=GATE2_USER_INTERESTS),
        day=0,
    )


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
        judge = DeterministicJudge(seed, block_start=BLOCK_START_D, block_end=BLOCK_END_D)
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
    )
    return rt


async def _amain(channel_name: str, db_path: Path, seed: int,
                 condition: str, check_only: bool, tz: str | None = None,
                 enable_commands: bool = False) -> int:
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
    args = parser.parse_args(argv)
    # WS-C env bootstrap: sources the repo-root .env so the product-lane
    # token (LILY_TOKEN) is available.
    load_env_file(REPO_ROOT / ".env")
    tz = args.tz or os.environ.get("HARNESS_TZ") or None
    try:
        return asyncio.run(_amain(args.channel, Path(args.db), args.seed,
                                  args.condition, args.check, tz,
                                  args.enable_commands))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
