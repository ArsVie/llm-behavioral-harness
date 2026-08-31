"""Simulación en vivo ACELERADA de dos semanas — el usuario eres tú (Ars).

Reproduce tu estilo de chat del análisis de la novia de 2024-2025: abres
conversación, hablas un rato, CORTAS a mitad de tema, y saltas al siguiente
día en que tenías pensado hablarle — mientras el companion actúa con su
motor completo (mood + ciclo + proactividad real vía Weibull hazard).

Diferencias clave vs `experiments/live_companion.py`:
  - Tiempo ACELERADO: time_scale configurable (--time-scale, default
    60 s por hora virtual ⇒ 2 semanas ≈ 28 min reales). live_companion
    usa 3600 s/vh (tiempo real); esto es una simulación rápida.
  - El usuario lo pone UN AGENTE GUIONADO por semilla en TU estilo
    (aperturas, cortes a mitad de conversación, seguimientos) — no un
    humano en el terminal. Los tiempos de apertura siguen el patrón
    real de los chats: mayormente tarde-noche (17:00–21:00).
  - Un solo run, 14 días, sin checkpoints ni perturbación preregistrada.
  - Todo persiste en un DB (resume-safe): reabrir con --db continúa.

Modelo: ox-alpha vía OpenRouter (lane product; OPENROUTER_API_KEY del
.env de Hermes o del repo). Sin clave → fallo fuerte al construir el
cliente (nunca silencioso).

Uso:
    set -a; . ~/.hermes/.env; set +a   # o el .env de la raíz del repo
    .venv/bin/python -m experiments.live_two_weeks \
        --db results/live-two-weeks/companion.db \
        --time-scale 60

Mientras corre verás cada turno: [USER] / [COMPANION] / [PROACTIVE],
con la marca del día y hora virtual. Ctrl-C para salir (el estado
persiste).

Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

# User script — Ars-style (derived from real chat analysis)

#: Horas locales (reales) de apertura típicas: tarde-noche, pico 18-21h.
OPENING_HOURS = (17.0, 21.0)

APERTURAS = [
    "Hey you. Tell me something about your day.",
    "Hey. What are you up to?",
    "Hi. How's it going?",
    "Hey! What did you end up doing today?",
    "Hola. How was your day?",
    "Hey there. What's happening?",
    "Hi! Tell me about your day.",
]

FOLLOW_UPS = [
    "That sounds nice. How long did that take?",
    "Nice. And how did that go?",
    "Oh? Tell me more.",
    "Did it work out in the end?",
    "Haha, classic. What happened next?",
    "Okay and? What else?",
]

CUT_LINES = [  # mid-conversation cut (topic abandonment)
    "Anyway. Gotta go, talk later!",
    "Ok I need to run, bfn!",
    "Later! Something came up.",
]

# Days (1-indexed) with no conversation (a day of silence).
SILENT_DAYS = {3, 9}

# Days (1-indexed) with two openings (brief morning + night).
DOUBLE_DAYS = {6}


def _opening_hour(rng: random.Random) -> float:
    """Hora local de apertura: uniforme en la ventana tarde-noche."""
    lo, hi = OPENING_HOURS
    return rng.uniform(lo, hi)


def user_plan(seed: int, days: int) -> list[dict]:
    """Plan del usuario: eventos {"kind", "day", "t_h", "text"} ordenados.

    Tipos: ``open`` (abre), ``followup`` (tras réplica del companion),
    ``cut`` (cierra a mitad de tema). Determinista por semilla.
    """
    rng = random.Random(f"user-plan-{seed}")
    events: list[dict] = []
    for d in range(days):
        day = d + 1
        if day in SILENT_DAYS:
            continue
        hours_today = (
            [_opening_hour(rng)]
        )
        if day in DOUBLE_DAYS:
            # brief morning greeting plus an evening opening
            hours_today = [rng.uniform(8.5, 10.0), _opening_hour(rng)]
        for hour in hours_today:
            events.append({
                "kind": "open",
                "day": day,
                "t_h": (day - 1) * 24.0 + hour,
                "text": rng.choice(APERTURAS),
            })
    return sorted(events, key=lambda e: e["t_h"])


# Driver
#


def _fmt(t_h: float) -> str:
    hh = int(t_h % 24)
    mm = int(round((t_h % 1.0) * 60)) % 60
    return f"d{int(t_h // 24) + 1} {hh:02d}:{mm:02d}"


async def amain(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live accelerated two-week simulation (you as the user)."
    )
    parser.add_argument("--db", type=Path,
                        default=Path("results/live-two-weeks/companion.db"))
    parser.add_argument("--seed", type=int, default=6001)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--time-scale", type=float, default=60.0,
                        help="segundos reales por hora virtual (60 ⇒ 14 días "
                             "= ~5.6 h reales)")
    parser.add_argument("--model", default=None,
                        help="modelo del companion (default $LLM_MODEL o "
                             "stealth/ox-alpha vía OpenRouter)")
    parser.add_argument("--max-turn-seconds", type=float, default=180.0,
                        help="timeout por generación (thinking models tardan)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    # heavy imports after arg parsing (fast startup)
    from engine.types import MoodVariant, PersonaParams, TimingParams
    from harness.bootstrap import ensure_companion_initialized
    from harness.channels.base import InboundMessage
    from harness.client import OpenAICompatibleClient
    from harness.credentials import load_env_file
    from harness.domain import UserProfile
    from harness.runtime import AsyncRuntime, IntentResolver
    from harness.scheduler import ProactiveSchedule, day_scores
    from harness.session import Session
    from harness.store import SQLiteStore
    from sim.run_async import CommandBridgeChannel  # noqa: F401 (CLI parity)

    from experiments.cvs_common import (
        BLOCK_END_D,
        BLOCK_START_D,
        GATE2_USER_INTERESTS,
        REASON_SCHEDULE,
        DeterministicJudge,
        TimeScale,
        VirtualClock,
        make_session,
        stream_rng,
    )
    import importlib
    rng_mod = importlib.import_module("engine.rng")

    load_env_file(REPO_ROOT / ".env")
    load_env_file(Path.home() / ".hermes" / ".env")

    db_path = args.db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db_path, audit_mode=True)
    seed = args.seed
    from harness.bootstrap import ensure_companion_initialized as _e  # noqa

    ensure_companion_initialized(
        store, seed=seed,
        user=UserProfile(name="User", interests=GATE2_USER_INTERESTS),
        day=0,
    )

    persona = PersonaParams()
    timing = TimingParams()
    variant = MoodVariant.DECOUPLED_OFFSETS
    clock = VirtualClock(0.0)
    # ox-alpha via OpenRouter: explicit env key/base; explicit args win.
    import os as _os
    _key = _os.environ.get("OPENROUTER_API_KEY")
    if not _key:
        print("[live] OPENROUTER_API_KEY not set — cannot run live", flush=True)
        return 2
    client = OpenAICompatibleClient(
        api_key=_key,
        base_url=_os.environ.get("OPENROUTER_BASE_URL",
                                 "https://openrouter.ai/api/v1"),
        model=args.model or _os.environ.get("LLM_MODEL", "stealth/ox-alpha"),
    )
    judge = DeterministicJudge(seed, block_start=BLOCK_START_D,
                               block_end=BLOCK_END_D)
    session = make_session("FULL", seed, store, clock, client, judge,
                           persona, timing, variant)
    if store.load_daily_state(0) is None:
        session.ensure_day(0)
    ProactiveSchedule.plan_and_persist(
        1, seed, persona, timing, store,
        reason=REASON_SCHEDULE, scores=day_scores(store, 0, timing),
    )

    scale = TimeScale(args.time_scale)

    from harness.channels.base import OutboundMessage

    class _PrintChannel:
        """Canal mínimo (protocolo Channel completo): imprime cada turno y
        alimenta el runtime."""

        name = "cli"

        async def start(self, on_message, on_command=None):
            self.handler = on_message

        async def stop(self):
            pass

        async def send(self, message: OutboundMessage) -> None:
            tag = "PROACTIVE" if message.proactive else "COMPANION"
            if message.reason and message.proactive:
                print(f"[{_fmt(session.clock.now_h())}] [{tag}] "
                      f"({message.reason}) {message.text}", flush=True)
            else:
                print(f"[{_fmt(session.clock.now_h())}] [{tag}] {message.text}",
                      flush=True)

        async def inbound(self, text: str, t_h: float) -> None:
            print(f"[{_fmt(t_h)}] [USER] {text}", flush=True)
            await self.handler(InboundMessage(text=text, sender_id="ars",
                                              t_h=t_h))

    channel = _PrintChannel()

    rt = AsyncRuntime(
        session,
        ProactiveSchedule.restore(seed, store),
        channel=channel,
        store=store,
        timing=timing,
        seed=seed,
        time_scale=scale,
        max_virtual_hours=args.days * 24.0,
        resolver=IntentResolver(store, rng=stream_rng(seed, rng_mod.EXPERIMENT_STREAM)),
        sleeper=None,
    )

    plan = user_plan(seed, args.days)
    t0 = time.time()
    task = asyncio.create_task(rt.run())
    # Wait for the runtime to register the channel handler before feeding.
    for _ in range(200):
        if task.done():
            exc = task.exception()
            print(f"[live] runtime died at startup: {exc!r}", flush=True)
            return 1
        if getattr(channel, "handler", None) is not None:
            break
        await asyncio.sleep(0.05)
    else:
        print("[live] runtime never registered the channel handler", flush=True)
        return 1

    print(f"== two-week live simulation: seed={seed} days={args.days} "
          f"time_scale={args.time_scale}s/vh "
          f"(~{args.days * 24 * args.time_scale / 3600:.1f} h real) ==",
          flush=True)
    for ev in plan:
        t_h = ev["t_h"]
        # Feed pacing uses real proportional time (t_h * scale); the runtime
        # clock jumps ahead, so this loop owns t_h ordering.
        target_wall = t_h * scale.seconds_per_virtual_hour
        while True:
            if task.done():
                exc = task.exception()
                print(f"[live] runtime ended before feed at {time.time() - t0:.1f}s "
                      f"(clock={session.clock.now_h():.2f}vh): {exc!r}", flush=True)
                return 1
            remaining = target_wall - (time.time() - t0)
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 5.0))
        if task.done():
            break  # run ended: do not feed a stopped runtime
        if task.done():
            exc = task.exception()
            print(f"[live] runtime ended before feed at {time.time() - t0:.1f}s "
                  f"(clock={session.clock.now_h():.2f}vh): {exc!r}", flush=True)
            return 1
        try:
            await channel.inbound(ev["text"], t_h)
        except RuntimeError as exc:
            if "not running" in str(exc):
                print("[live] runtime ended mid-feed — stopping gracefully",
                      flush=True)
                break
            raise
        # one follow-up per opening, after the companion reply
        if ev["kind"] == "open" and not task.done():
            fu = random.Random(f"fu-{seed}-{ev['day']}").choice(FOLLOW_UPS)
            await asyncio.sleep(min(2.0 * scale.seconds_per_virtual_hour, 20.0))
            try:
                await channel.inbound(fu, max(session.clock.now_h(), t_h + 1.0))
            except RuntimeError as exc:
                if "not running" not in str(exc):
                    raise
                print("[live] runtime ended before follow-up — done", flush=True)
            cut = random.Random(f"cut-{seed}-{ev['day']}").random()
            if cut < 0.45 and not task.done():
                # Ars corta casi la mitad de las conversaciones
                await asyncio.sleep(min(scale.seconds_per_virtual_hour / 6.0, 25.0))
                print(f"[{_fmt(session.clock.now_h())}] [USER] (leaves mid-conversation)",
                      flush=True)

    if not task.done():
        remaining_wall = (args.days * 24 - session.clock.now_h()) * scale.seconds_per_virtual_hour
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining_wall + 120.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    n_msgs = len(store.recent_messages(limit=10000))
    print(f"== done: {n_msgs} messages persisted in {db_path} ==", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        print("\n[live] interrupted — state persists; reopen with the same --db",
              flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
