"""Auxiliary calls reach the ledger without changing the call.

``MeteredClient`` records them; these tests pin the recording and the
non-interference: same result, same gates, same exceptions.
"""

from __future__ import annotations

import pytest

from harness.bootstrap import ensure_companion_initialized
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import UserProfile
from harness.metering import MeteredClient, render_prompt
from tests.helpers.store import make_session, make_store

SEED = 20260912

REPLY = "{\"source\": \"model\"}"


class _BareClient:
    """A client with no ``chat_with_meta``: a capability gate must see that."""

    supports_json = False
    model = "bare-model"

    def chat(self, messages, **kwargs):  # pragma: no cover - never called here
        return "bare"


def _rows(store):
    """Ledger rows as the SCHEMA actually stores them.

    There is no ``prompt`` column: the ledger keeps ``prompt_hash`` always and
    the payload only in ``repro_json`` under audit mode (privacy mode drops it),
    so a test asserts the hash, never the text.
    """
    return store.conn.execute(
        "SELECT day, t_h, role, prompt_hash, response FROM llm_calls ORDER BY id"
    ).fetchall()


def test_a_forwarded_call_lands_one_row_and_returns_the_same_result(tmp_path):
    store = make_store(tmp_path)
    messages = [{"role": "user", "content": "propose routines"}]
    direct = FakeClient(responses=[REPLY]).chat_with_meta(
        messages, system="You return JSON only."
    )

    metered = MeteredClient(
        FakeClient(responses=[REPLY]), store, VirtualClock(48.5),
        "aux_routine_setup",
    )
    via_proxy = metered.chat_with_meta(messages, system="You return JSON only.")

    rows = _rows(store)
    store.close()
    assert len(rows) == 1, "one call, one row"
    day, t_h, role, prompt_hash, response = rows[0]
    assert (day, role) == (2, "aux_routine_setup")
    assert t_h == 48.5
    assert prompt_hash, "the prompt still reaches the ledger, as a hash"
    assert response == direct.content
    assert via_proxy.content == direct.content, (
        "the caller gets exactly what the wrapped client would have returned"
    )


def test_the_plain_chat_surface_is_metered_too(tmp_path):
    """The judge calls ``chat``; the planner, extension and setup call the rich one."""
    store = make_store(tmp_path)
    metered = MeteredClient(
        FakeClient(responses=["0.5"]), store, VirtualClock(24.0), "aux_judge"
    )
    assert metered.chat([{"role": "user", "content": "score this"}]) == "0.5"
    metered.chat([{"role": "user", "content": "and this instead"}])

    rows = _rows(store)
    store.close()
    assert [row[2] for row in rows] == ["aux_judge", "aux_judge"]
    assert rows[0][3] and rows[1][3]
    assert rows[0][3] != rows[1][3], "a different prompt is a different hash"


def test_capability_gates_and_attributes_still_answer_truthfully(tmp_path):
    store = make_store(tmp_path)
    rich = MeteredClient(FakeClient(), store, VirtualClock(0.0), "aux_x")
    assert rich.supports_json is True and rich.supports_tools is True
    assert hasattr(rich, "chat_with_meta") and hasattr(rich, "chat_stream")

    bare = MeteredClient(_BareClient(), store, VirtualClock(0.0), "aux_x")
    assert not hasattr(bare, "chat_with_meta"), (
        "a capability gate must not be fooled by the proxy"
    )
    assert bare.supports_json is False and bare.model == "bare-model"
    store.close()


def test_a_raising_call_passes_through_and_records_nothing(tmp_path):
    class _Boom:
        supports_json = True
        model = "boom"

        def chat(self, messages, **kwargs):
            raise RuntimeError("gateway down")

    store = make_store(tmp_path)
    metered = MeteredClient(_Boom(), store, VirtualClock(0.0), "aux_judge")
    with pytest.raises(RuntimeError):
        metered.chat([{"role": "user", "content": "hi"}])
    assert _rows(store) == [], "only completed calls are recorded"
    store.close()


def test_a_ledger_without_the_usage_columns_still_takes_the_row():
    """Older stores lose the usage kwargs — never the row, never the call."""

    class _BareStore:
        def __init__(self):
            self.rows = []

        def log_llm_call(self, day, t_h, role, prompt, response, model,
                         meta=None, *, repro=None):
            self.rows.append((day, t_h, role, response))

    store = _BareStore()
    metered = MeteredClient(
        FakeClient(responses=["ok"]), store, VirtualClock(12.0), "aux_day_planner"
    )
    metered.chat_with_meta([{"role": "user", "content": "plan"}])
    assert store.rows == [(0, 12.0, "aux_day_planner", "ok")]


def test_a_store_with_no_ledger_is_a_no_op():
    class _NoLedger:
        pass

    metered = MeteredClient(
        FakeClient(responses=["ok"]), _NoLedger(), VirtualClock(0.0), "aux_x"
    )
    assert metered.chat([{"role": "user", "content": "hi"}]) == "ok"


def test_render_prompt_names_roles_and_the_system_line():
    assert render_prompt(
        [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}], "sys"
    ) == "system: sys\nuser: a\nuser: b"
    assert render_prompt(None) == ""


def test_a_call_from_a_worker_thread_still_lands(tmp_path):
    """The row must land even when the call runs in a worker thread."""
    import concurrent.futures

    store = make_store(tmp_path)
    metered = MeteredClient(
        FakeClient(responses=["ok"]), store, VirtualClock(6.0),
        "aux_interest_extension",
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            metered.chat_with_meta, [{"role": "user", "content": "worker"}]
        ).result(timeout=10)

    rows = _rows(store)
    store.close()
    assert result.content == "ok", "the call itself is unaffected"
    assert [row[2] for row in rows] == ["aux_interest_extension"]
    assert (rows[0][0], rows[0][1]) == (0, 6.0)


def test_a_broken_ledger_never_breaks_the_call():
    """The aux callers fall back to their heuristics on ANY client exception, so
    an unwritable ledger must degrade to a report, never to a raised error."""
    seen = []

    class _AngryStore:
        def log_llm_call(self, *args, **kwargs):
            raise RuntimeError("disk full")

    metered = MeteredClient(
        FakeClient(responses=["fine"]), _AngryStore(), VirtualClock(0.0),
        "aux_judge", logger=seen.append,
    )
    assert metered.chat([{"role": "user", "content": "x"}]) == "fine"
    assert seen and "ledger write failed" in seen[0]


# --- wiring: the four callers actually go through the proxy -------------------


def test_the_session_meters_its_judge_and_planner_clients(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    session = make_session(store, client=FakeClient())
    assert isinstance(session.judge_client, MeteredClient), (
        "the judge burns tokens every finalize"
    )
    assert session._planner_client() is None, "off by default: byte parity"

    monkeypatch.setenv("HARNESS_DAY_PLANNER", "1")
    assert isinstance(session._planner_client(), MeteredClient)
    store.close()


def test_onboarding_model_calls_land_in_the_ledger(tmp_path):
    """The extension and the routine catalog are the two onboarding callers."""
    store = make_store(tmp_path, "onboard.db")
    ensure_companion_initialized(
        store,
        seed=SEED,
        user=UserProfile(name="tx", interests=("quantum harmonica repair",)),
        client=FakeClient(responses=["{}", "{}"]),
    )
    roles = [row[2] for row in _rows(store)]
    store.close()
    assert "aux_interest_extension" in roles, (
        "an off-catalog interest asks the model — that call must be visible"
    )
    assert "aux_routine_setup" in roles
