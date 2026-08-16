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

Verificación de compuerta (sin enviar mensajes): el driver imprime el
estado del canal al arrancar; `--check` solo valida el token vía getMe
(no envía nada) y sale.

Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

import argparse
import asyncio
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
from harness.credentials import load_env_file

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 1 hora virtual = 1 hora real (modo en vivo; la matriz usa 0.0004).
LIVE_TIME_SCALE_S_PER_VH = 3600.0

#: Constante de persona (misma que las células de la matriz).
DEFAULT_PERSONA = "Ana"


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
                  anchor=None) -> AsyncRuntime:
    from experiments.cvs_common import stream_rng, rng_mod

    if client is None:
        client = OpenAICompatibleClient(lane="product")
    if judge is None:
        judge = DeterministicJudge(seed, block_start=BLOCK_START_D, block_end=BLOCK_END_D)
    persona = persona or PersonaParams()
    timing = timing or TimingParams()
    variant = MoodVariant.DECOUPLED_OFFSETS
    clock = VirtualClock(0.0)
    session = make_session(condition, seed, store, clock, client, judge,
                           persona, timing, variant)
    # Día 0 up-front (misma razón que run_cell): el primer replan del
    # runtime ocurre en la medianoche del día 1 y planificaría el día 0
    # retroactivamente. El plan nace con ESTADO real: si el store aún no
    # tiene la fila daily_state del día 0 (arranque limpio), se dibuja el
    # mood con ensure_day(0) ANTES de planificar (el mismo paso del
    # rollover de medianoche) y se pasa day_scores reales — nunca
    # scores=None (espejo del _replan de producción). En resume la fila ya
    # existe y el plan es un no-op (INSERT OR IGNORE nunca toca filas
    # fired/expired).
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
        max_virtual_hours=max_virtual_hours,  # en vivo: None (Ctrl-C para salir)
        resolver=IntentResolver(store, rng=stream_rng(seed, rng_mod.EXPERIMENT_STREAM)),
        sleeper=None,
        anchor=anchor,
    )
    return rt


async def _amain(channel_name: str, db_path: Path, seed: int,
                 condition: str, check_only: bool, tz: str | None = None) -> int:
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
    # Real-time anchor (S2): a persisted anchor (resume) wins; otherwise a
    # fresh anchor is drawn from --tz/HARNESS_TZ and persisted. anchor=None
    # keeps the pre-anchor behavior. AsyncRuntime uses it for absolute sleeps
    # and resume repositioning — restart no longer lands at virtual midnight
    # (quiet hours) regardless of the real launch time.
    anchor = load_anchor(store)
    if anchor is None and tz:
        try:
            anchor = anchor_for_fresh_start(time.time(), tz)
        except Exception as exc:  # noqa: BLE001 - bad IANA name -> clean exit
            print(f"[live] invalid timezone {tz!r}: {exc}", flush=True)
            store.close()
            return 2
        persist_anchor(store, anchor)
    # S1 write path: the store resolves real timestamps (opened_at etc.)
    # from the anchor at row creation; without an anchor all *_at columns
    # stay NULL (pre-anchor behavior, replay parity).
    if anchor is not None:
        store.attach_anchor(anchor)
    channel = select_channel(channel_name)
    runtime = build_runtime(store, seed, condition, channel, anchor=anchor)
    print(f"[live] channel={channel_name} condition={condition} "
          f"seed={seed} db={db_path} tz={anchor.tz if anchor else 'none'}",
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
    args = parser.parse_args(argv)
    # WS-C env bootstrap (owned by the tokens lane): source the repo-root
    # .env so the product-lane token (LILY_TOKEN) is available even when the
    # launcher did not export it. Values are never printed. The launcher
    # recipe remains the canonical path: set -a; . "$REPO/.env"; set +a.
    load_env_file(REPO_ROOT / ".env")
    tz = args.tz or os.environ.get("HARNESS_TZ") or None
    try:
        return asyncio.run(_amain(args.channel, Path(args.db), args.seed,
                                  args.condition, args.check, tz))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
