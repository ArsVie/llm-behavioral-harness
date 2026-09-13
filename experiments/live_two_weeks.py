"""Simulación en vivo ACELERADA de dos semanas — el usuario eres tú (Ars).

El usuario lo pone UN AGENTE GUIONADO por semilla en tu estilo (aperturas,
cortes a mitad de conversación, seguimientos) — no un humano en el
terminal. Los tiempos de apertura siguen el patrón real de los chats:
mayormente tarde-noche (17:00–21:00). El companion actúa con su motor
completo (mood + ciclo + proactividad real vía Weibull hazard).

  - Tiempo ACELERADO: time_scale configurable (--time-scale, default
    60 s por hora virtual ⇒ 2 semanas ≈ 28 min reales).
  - Un solo run, 14 días, sin checkpoints ni perturbación preregistrada.
  - Todo persiste en un DB (resume-safe): reabrir con --db continúa.

Modelo: lane product vía commandcode (LILY_TOKEN del .env del repo).
Sin clave → fallo fuerte al construir el cliente (nunca silencioso).

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

def plan_params(seed: int, days: int) -> dict:
    """Parámetros del usuario guionado, derivados frescos de la semilla.

    Cada run sortea días de silencio, días dobles, ventanas de apertura y
    tasa de corte de su propio stream (``user-plan-params-{seed}``),
    determinista por semilla. Tasas calibradas al protocolo seed-6001
    (~14% silencio, ~1 día doble por 14 días, corte ~0.45).
    """
    rng = random.Random(f"user-plan-params-{seed}")
    n_silent = max(1, days * 2 // 14)
    silent = set(rng.sample(range(1, days + 1), min(n_silent, days)))
    rest = [d for d in range(1, days + 1) if d not in silent]
    n_double = max(1, days // 14)
    double = set(rng.sample(rest, min(n_double, len(rest)))) if rest else set()
    lo = rng.uniform(16.0, 18.0)
    hi = rng.uniform(20.0, 22.0)
    mlo = rng.uniform(8.0, 9.0)
    mhi = rng.uniform(9.5, 10.5)
    return {
        "silent_days": silent,
        "double_days": double,
        "opening_hours": (lo, hi),
        "morning_hours": (mlo, mhi),
        "cut_p": rng.uniform(0.35, 0.55),
    }


def _opening_hour(rng: random.Random, lo: float, hi: float) -> float:
    """Hora local de apertura: uniforme en la ventana tarde-noche."""
    return rng.uniform(lo, hi)


def user_plan(seed: int, days: int, params: dict | None = None) -> list[dict]:
    """Plan del usuario: eventos {"kind", "day", "t_h", "text"} ordenados.

    Tipos: ``open`` (abre), ``followup`` (tras réplica del companion),
    ``cut`` (cierra a mitad de tema). Determinista por semilla; los
    parámetros salen de ``plan_params`` salvo override explícito.
    """
    p = params if params is not None else plan_params(seed, days)
    rng = random.Random(f"user-plan-{seed}")
    events: list[dict] = []
    for d in range(days):
        day = d + 1
        if day in p["silent_days"]:
            continue
        lo, hi = p["opening_hours"]
        hours_today = (
            [_opening_hour(rng, lo, hi)]
        )
        if day in p["double_days"]:
            # brief morning greeting plus an evening opening
            mlo, mhi = p["morning_hours"]
            hours_today = [rng.uniform(mlo, mhi), _opening_hour(rng, lo, hi)]
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
    # lane product vía commandcode (LILY_TOKEN/LILY_BASE_URL del .env del
    # repo); explicit args win.
    import os as _os
    from harness.credentials import resolve_credentials
    try:
        _key, _base = resolve_credentials("product")
    except RuntimeError as exc:
        print(f"[live] product lane sin credenciales — {exc}", flush=True)
        return 2
    client = OpenAICompatibleClient(
        api_key=_key,
        base_url=_base,
        model=args.model or _os.environ.get("LLM_MODEL", "deepseek/deepseek-v4-flash"),
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

    params = plan_params(seed, args.days)
    plan = user_plan(seed, args.days, params)
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
    lo, hi = params["opening_hours"]
    print(f"[live] user-plan: silent={sorted(params['silent_days'])} "
          f"double={sorted(params['double_days'])} cut_p={params['cut_p']:.2f} "
          f"evenings={lo:.1f}-{hi:.1f}h",
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
            print(f"[live] turn fallido ({exc}) — registrado, continúa",
                  flush=True)
            continue
        # one follow-up per opening, after the companion reply
        if ev["kind"] == "open" and not task.done():
            fu = random.Random(f"fu-{seed}-{ev['day']}").choice(FOLLOW_UPS)
            await asyncio.sleep(min(2.0 * scale.seconds_per_virtual_hour, 20.0))
            try:
                await channel.inbound(fu, max(session.clock.now_h(), t_h + 1.0))
            except RuntimeError as exc:
                if "not running" in str(exc):
                    print("[live] runtime ended before follow-up — done", flush=True)
                else:
                    print(f"[live] follow-up fallido ({exc}) — registrado, continúa",
                          flush=True)
            cut = random.Random(f"cut-{seed}-{ev['day']}").random()
            if cut < params["cut_p"] and not task.done():
                # corte a mitad de conversación (tasa sorteada por run)
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
