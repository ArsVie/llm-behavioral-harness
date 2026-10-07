"""Live integration gate (telegram/cli).

The driver starts the runtime with an injected channel; it delivers inbound -> reply -> send and proactive messages."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


from experiments.live_companion import bootstrap, build_runtime
from harness.channels.base import FakeChannel, InboundMessage
from harness.client import FakeClient

from tests.helpers import no_wait
from tests.helpers.judges import DeterministicJudge


def test_live_runtime_round_trip_reply_and_proactive():
    """Inbound -> reply -> send, and proactive messages reach the channel."""
    async def _run():
        from harness.store import SQLiteStore

        with tempfile.TemporaryDirectory() as td:
            store = SQLiteStore(Path(td) / "companion.db", audit_mode=True)
            bootstrap(store, seed=5001)
            channel = FakeChannel()
            judge = DeterministicJudge(5001)
            runtime = build_runtime(
                store, 5001, channel,
                client=FakeClient(), judge=judge,
                time_scale_s_per_vh=0.0004,  # CI-accelerated time scale
            )
            # response_delay_s are REAL wall-clock seconds (TimeScale does
            # not scale them): without this the gate would sleep ~30 s for real.
            runtime.sleeper = no_wait
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
                # Unlimited horizon (live semantics): cancellation is
                # the Ctrl-C path — the runtime finalizes cleanly.
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


def test_build_runtime_wires_anchor_and_resume_positions(tmp_path):
    """The persisted anchor positions a restart at the real local hour, not at virtual midnight."""
    import time
    import datetime
    import zoneinfo
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
        store, 5001, FakeChannel(),
        client=FakeClient(),
        judge=DeterministicJudge(5001),
        anchor=load_anchor(store),
    )
    assert rt.anchor is not None and rt.anchor.tz == tz

    # resume positioning: t_h_at(now) maps to the real local hour-of-day
    local = datetime.datetime.fromtimestamp(now, zoneinfo.ZoneInfo(tz))
    expected = local.hour + local.minute / 60 + local.second / 3600
    assert abs((rt.anchor.t_h_at(now) % 24.0) - expected) < 0.02
    store.close()


def test_build_runtime_no_tz_keeps_pre_anchor_behavior(tmp_path):
    """Without a persisted anchor and without tz -> anchor=None."""
    from harness.store import SQLiteStore

    store = SQLiteStore(tmp_path / "companion.db", audit_mode=True)
    bootstrap(store, seed=5001)
    rt = build_runtime(
        store, 5001, FakeChannel(),
        client=FakeClient(),
        judge=DeterministicJudge(5001),
    )
    assert rt.anchor is None
    store.close()
