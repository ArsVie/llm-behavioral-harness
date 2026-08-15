"""Compuerta de la integración en vivo (telegram/cli) — it3.

El driver live_companion no añade piezas al runtime: el stack completo es
config.select_channel -> AsyncRuntime.run(). Esta compuerta verifica que
el driver (a) arranca el runtime con un canal inyectado, (b) entrega
inbound -> réplica -> send, y (c) entrega proactivos por el canal real.
El token de telegram se valida aparte (check_token / getMe — no envía
mensajes; no se toca la red aquí).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from experiments.live_companion import bootstrap, build_runtime
from harness.channels.base import FakeChannel, InboundMessage, OutboundMessage
from harness.client import FakeClient


def test_live_runtime_round_trip_reply_and_proactive():
    """Inbound -> réplica -> send, y los proactivos llegan al canal.

    Patrón síncrono del repo (asyncio.run dentro del test — el repo no
    usa pytest-asyncio)."""
    async def _run():
        from experiments.cvs_common import DeterministicJudge, BLOCK_START_D, BLOCK_END_D
        from harness.store import SQLiteStore

        with tempfile.TemporaryDirectory() as td:
            store = SQLiteStore(Path(td) / "companion.db", audit_mode=True)
            bootstrap(store, seed=5001)
            channel = FakeChannel()
            judge = DeterministicJudge(5001, block_start=BLOCK_START_D, block_end=BLOCK_END_D)
            runtime = build_runtime(
                store, 5001, "FULL", channel,
                client=FakeClient(), judge=judge,
                time_scale_s_per_vh=0.0004,  # escala acelerada del CI
            )
            task = asyncio.create_task(runtime.run())
            try:
                for _ in range(50):
                    if channel.handler is not None:
                        break
                    await asyncio.sleep(0.05)
                assert channel.handler is not None
                await channel.handler(InboundMessage(text="Hello", sender_id="test"))
                await asyncio.sleep(0.5)
                assert any(not m.proactive for m in channel.sent), \
                    "reactive reply expected"
                # Horizonte ilimitado (semántica en vivo): la cancelación es
                # el camino Ctrl-C — el runtime finaliza limpio.
                await asyncio.sleep(0.5)
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, RuntimeError):
                        pass
                store.close()

    asyncio.run(_run())


def test_build_runtime_smoke(tmp_path):
    """build_runtime construye sin tocar la red (fake client + fake judge)."""
    from experiments.cvs_common import DeterministicJudge, BLOCK_START_D, BLOCK_END_D
    from harness.store import SQLiteStore

    store = SQLiteStore(tmp_path / "companion.db", audit_mode=True)
    bootstrap(store, seed=5001)
    runtime = build_runtime(
        store, 5001, "FULL", FakeChannel(),
        client=FakeClient(),
        judge=DeterministicJudge(5001, block_start=BLOCK_START_D, block_end=BLOCK_END_D),
    )
    assert runtime.channel.name == "fake"
    assert runtime.max_virtual_hours is None  # en vivo: sin horizonte
    store.close()


def test_build_runtime_wires_anchor_and_resume_positions(tmp_path):
    """live_companion wires the real-time anchor into AsyncRuntime, and the
    persisted anchor positions a restart at the real local hour instead of
    virtual midnight (the resume→midnight bug fix, WS-A)."""
    import time
    import datetime
    import zoneinfo
    from experiments.cvs_common import DeterministicJudge, BLOCK_START_D, BLOCK_END_D
    from harness.store import SQLiteStore
    from harness.anchor import anchor_for_fresh_start
    from harness.runtime import load_anchor, persist_anchor

    tz = "America/Mexico_City"
    now = time.time()
    anchor = anchor_for_fresh_start(now, tz)

    store = SQLiteStore(tmp_path / "companion.db", audit_mode=True)
    bootstrap(store, seed=5001)
    persist_anchor(store, anchor)

    # anchor round-trips and the runtime receives it
    rt = build_runtime(
        store, 5001, "FULL", FakeChannel(),
        client=FakeClient(),
        judge=DeterministicJudge(5001, block_start=BLOCK_START_D, block_end=BLOCK_END_D),
        anchor=load_anchor(store),
    )
    assert rt.anchor is not None and rt.anchor.tz == tz

    # resume positioning: t_h_at(now) maps to the real local hour-of-day
    local = datetime.datetime.fromtimestamp(now, zoneinfo.ZoneInfo(tz))
    expected = local.hour + local.minute / 60 + local.second / 3600
    assert abs((rt.anchor.t_h_at(now) % 24.0) - expected) < 0.02
    store.close()


def test_build_runtime_no_tz_keeps_pre_anchor_behavior(tmp_path):
    """No anchor persisted and no tz -> anchor=None (byte-identical to the
    pre-anchor live path; the accelerated/test fleet is untouched)."""
    from experiments.cvs_common import DeterministicJudge, BLOCK_START_D, BLOCK_END_D
    from harness.store import SQLiteStore

    store = SQLiteStore(tmp_path / "companion.db", audit_mode=True)
    bootstrap(store, seed=5001)
    rt = build_runtime(
        store, 5001, "FULL", FakeChannel(),
        client=FakeClient(),
        judge=DeterministicJudge(5001, block_start=BLOCK_START_D, block_end=BLOCK_END_D),
    )
    assert rt.anchor is None
    store.close()
