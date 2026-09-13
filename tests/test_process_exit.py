"""Subprocess-exit regression (Iteration 2, A6).

Asserting that an async function returned does not detect a process kept
alive by a lingering non-daemon thread or an executor that was never shut
down; launching the runtime scenario in a SUBPROCESS does. The parent asserts
exit code 0 within a hard timeout and the child self-checks its thread table.

Negative controls: one child stays alive on purpose, one leaves a daemon
thread at self-check time.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent

#: Hard bound on the child's whole run. The happy path finishes in well
#: under a second; anything close to this bound is a lingering process.
CHILD_TIMEOUT_S = 30


def _child_program(body: str) -> str:
    """Wrap a child main() body as a -c program with the tests dir on path."""
    return (
        "import sys\n"
        f"sys.path.insert(0, {str(TESTS_DIR)!r})\n"
        + body
    )


_CHILD_RUNTIME_RUN = r'''
import asyncio
import threading

import engine.rng as rng_mod
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import DailyAgenda
from harness.judge import ScriptedJudge
from harness.proactive import IntentResolver
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import REASON_SCHEDULE, ProactiveSchedule
from harness.session import Session
from tests.helpers import SeamStore, agenda_item

PERSONA = PersonaParams()
TIMING = TimingParams()
SEED = 12345


def run_runtime_test() -> None:
    """One bounded AsyncRuntime run: a grounded event at 10:00 fires, the
    clock rolls past max_virtual_hours, the current day is finalized and the
    channel stopped. Mirrors tests/test_runtime.py's minimal run shape."""
    store = SeamStore()
    clock = VirtualClock()
    session = Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=SEED,
        client=FakeClient(responses=["ok!"]),
        clock=clock,
        judge=ScriptedJudge(score=0.5).judge_day,
    )
    store.save_agenda(
        0,
        DailyAgenda(0, (agenda_item(item_id="g1", start=9.5, end=10.6, salience=0.8),)),
    )
    store.save_schedule_events(
        SEED, [{"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE}]
    )
    channel = FakeChannel()

    async def record(delay: float) -> None:
        pass  # sleeper — recorded, never waited

    async def main() -> None:
        runtime = AsyncRuntime(
            session,
            ProactiveSchedule.restore(SEED, store),
            channel,
            store=store,
            timing=TIMING,
            seed=SEED,
            time_scale=TimeScale(seconds_per_virtual_hour=0.001),
            max_virtual_hours=11.0,
            resolver=IntentResolver(store, rng=rng_mod.stream_rng(SEED)),
            sleeper=record,
        )
        await runtime.run()

    asyncio.run(main())


def main() -> int:
    run_runtime_test()
    threads = sorted(t.name for t in threading.enumerate())
    print("THREADS_AFTER=" + repr(threads))
    orphans = [name for name in threads if name != "MainThread"]
    if orphans:
        print("FAIL_ORPHAN_THREADS=" + repr(orphans))
        return 1
    print("CHILD_EXIT_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


_CHILD_LINGERING_THREAD = r'''
import sys
import threading

# A non-daemon worker thread blocked forever: even after main() returns the
# interpreter cannot exit. The subprocess harness must detect this hang.
threading.Thread(target=threading.Event().wait, daemon=False,
                 name="llh-lingering").start()
print("CHILD_MAIN_DONE")
sys.exit(0)
'''


_CHILD_ORPHAN_THREAD_DETECTED = r'''
import sys
import threading

# An orphan (daemon) thread at self-check time: the child's own check must
# catch it and exit 1 with a FAIL marker.
threading.Thread(target=threading.Event().wait, daemon=True,
                 name="llh-orphan-daemon").start()
threads = sorted(t.name for t in threading.enumerate())
print("THREADS_AFTER=" + repr(threads))
orphans = [name for name in threads if name != "MainThread"]
if orphans:
    print("FAIL_ORPHAN_THREADS=" + repr(orphans))
    sys.exit(1)
print("CHILD_EXIT_OK")
sys.exit(0)
'''


def _run_child(code: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def test_runtime_subprocess_exits_cleanly():
    """THE regression: a runtime test in a subprocess must exit 0 within the
    hard timeout, with no orphan worker threads.
    """
    proc = _run_child(_child_program(_CHILD_RUNTIME_RUN), CHILD_TIMEOUT_S)

    assert proc.returncode == 0, (
        f"runtime subprocess did not exit cleanly (rc={proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "CHILD_EXIT_OK" in proc.stdout, (
        f"child self-check did not pass\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    # no orphan worker threads, no lingering executor threads
    threads = ast.literal_eval(
        next(line for line in proc.stdout.splitlines()
             if line.startswith("THREADS_AFTER=")).split("=", 1)[1]
    )
    assert threads == ["MainThread"], (
        f"orphan threads/executor left behind: {threads}"
    )


def test_regression_detects_process_that_stays_alive():
    """Negative control: a child that stays alive must trip the timeout, not
    pass silently.
    """
    with pytest.raises(subprocess.TimeoutExpired):
        _run_child(_child_program(_CHILD_LINGERING_THREAD), timeout=5)


def test_child_self_check_detects_orphan_threads():
    """Negative control: the in-child thread check must catch an orphan
    thread and exit 1 with a FAIL marker (the parent then fails)."""
    proc = _run_child(_child_program(_CHILD_ORPHAN_THREAD_DETECTED), CHILD_TIMEOUT_S)
    assert proc.returncode == 1
    assert "FAIL_ORPHAN_THREADS" in proc.stdout
    assert "CHILD_EXIT_OK" not in proc.stdout
