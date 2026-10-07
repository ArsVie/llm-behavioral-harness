"""Guards for the defects the 2026-10-03 audit of the live run found.

From day 4 the gateway backend stopped answering decisions with the tool, and
the fallback paths turned those failures into messages he received: a proactive
"[neutral] ..." after a verdict that never parsed, the verdict's ``reason`` as
her words, a heads-up for an event that had already ended. Around them: a
retry loop that nested 7 x 7 posts, a planner that kept its templates on prose,
steers that outlived their day, and a forked session id shared across threads.
"""

from __future__ import annotations

import threading

import httpx
import pytest

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.client import ChatResult, FakeClient, OpenAICompatibleClient, fresh_session
from harness.clock import VirtualClock
from harness.day_planner import _call_within_budget
from harness.domain import ProactiveIntent
from harness.judge import ScriptedJudge
from harness.negotiation_state import NegotiationPhase, NegotiationState
from harness.session import Session, _wordless
from harness.steering import KIND_EVENT_POPUP, MAX_ATTEMPTS, SteeringQueue
from harness.store import SQLiteStore
from harness.tools import DecisionConfig
from tests.helpers.store import booted

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345


def _intent(intent_id: str = "pi_1", valid_until: float = 13.0) -> ProactiveIntent:
    return ProactiveIntent(
        id=intent_id, reason="schedule", source_type="agenda_item",
        source_id="ag_0", hook="Finished: pottery class", created_t_h=10.0,
        valid_until_t_h=valid_until, salience=0.5, evidence="agenda_item:ag_0",
    )


def _session(store, client, clock):
    return Session(
        store, persona=PERSONA, timing=TIMING, variant=VARIANT, seed=SEED,
        client=client, clock=clock, judge=ScriptedJudge(score=0.5).judge_day,
        decision_config=DecisionConfig(),
    )


def _steer_status(store, steer_id: int) -> str:
    return store.conn.execute(
        "SELECT status FROM steering_queue WHERE id = ?", (steer_id,)
    ).fetchone()["status"]


# -- a proactive fire needs a yes --------------------------------------------


def test_a_proactive_verdict_that_never_parsed_sends_nothing(tmp_path):
    """Live 2026-10-02: steer 83 failed to parse and "[neutral] ..." went out
    anyway. No yes is not a yes: nothing is generated, and the fire is not
    re-asked at a later boundary about a moment that has passed."""
    store = SQLiteStore(tmp_path / "s.db")
    client = FakeClient(responses=["I'd rather not.", "still prose", "hello!"])
    session = _session(store, client, VirtualClock(t_h=10.0))
    store.save_proactive_intent(_intent())
    result = session.fire_proactive("pi_1")
    assert result.reply == ""
    assert [m for m in store.messages_for_day(0)
            if m["role"] == "assistant" and not m.get("tool_calls_json")] == []
    assert all("hello!" not in str(c) for c in client.calls)
    statuses = [r["status"] for r in store.conn.execute(
        "SELECT status FROM steering_queue")]
    assert statuses == ["abandoned"]
    events = [e["event"] for e in store.events_since(0)]
    assert "proactive_undecided_turn" in events
    store.close()


def test_an_expired_proactive_steer_is_abandoned_without_a_call(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock(t_h=10.0)
    client = FakeClient(responses=["hey you"])
    session = _session(store, client, clock)
    store.save_proactive_intent(_intent(valid_until=11.0))
    session.enqueue_proactive_decision("pi_1")
    clock.advance_hours(2.0)                       # past valid_until
    session.on_message("hi")
    assert store.decisions_for_day(0) == []
    assert len(client.calls) == 1                   # the reply, no decide call
    assert [r["status"] for r in store.conn.execute(
        "SELECT status FROM steering_queue")] == ["abandoned"]
    assert "proactive_expired" in [e["event"] for e in store.events_since(0)]
    store.close()


# -- a reply needs words ------------------------------------------------------


@pytest.mark.parametrize("text,empty", [
    ("[neutral] ...", True), ("…", True), ("  — ", True), ("", True),
    ("hi", False), ("♡", False), ("[smug] no.", False),
])
def test_wordless(text, empty):
    assert _wordless(text) is empty


def test_a_reply_of_dots_is_re_asked_then_dropped(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    client = FakeClient(responses=["[neutral] ...", "..."])
    session = _session(store, client, VirtualClock(t_h=10.0))
    result = session.on_message("you there?")
    assert result.reply == ""
    assert len(client.calls) == 2
    assert "empty_reply_dropped" in [e["event"] for e in store.events_since(0)]
    store.close()


def test_an_empty_completion_reaches_the_re_ask(tmp_path):
    """Live 2026-09-30: message 218 got no reply — the empty generation raised
    before the bounded re-ask could run."""
    store = SQLiteStore(tmp_path / "s.db")
    client = FakeClient(responses=[{"content": ""}, "sorry, here"])
    session = _session(store, client, VirtualClock(t_h=10.0))
    result = session.on_message("you there?")
    assert "sorry, here" in result.reply
    store.close()


# -- steers do not outlive their budget or their day --------------------------


def test_the_last_requeue_abandons_the_steer(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    queue = SteeringQueue(store)
    steer_id = queue.enqueue(KIND_EVENT_POPUP, {"event_id": "e"}, 0, 1.0)
    for _ in range(MAX_ATTEMPTS - 1):
        queue.requeue(steer_id)
        assert _steer_status(store, steer_id) == "pending"
    queue.requeue(steer_id)
    assert _steer_status(store, steer_id) == "abandoned"
    store.close()


def test_finalizing_a_day_expires_its_pop_ups_but_not_his_messages(tmp_path):
    store = booted(SQLiteStore(tmp_path / "s.db"))
    session = _session(store, FakeClient(responses=["ok"] * 5),
                       VirtualClock(t_h=10.0))
    session.on_message("hey")                    # starts day 0
    queue = session._steering
    popup = queue.enqueue(KIND_EVENT_POPUP, {"event_id": "e"}, 0, 20.0)
    held = queue.enqueue("user_message_mid_turn", {"text": "hey"}, 0, 20.0)
    session.finalize_day(0)
    assert _steer_status(store, popup) == "abandoned"
    assert _steer_status(store, held) == "pending"
    assert "steers_expired" in [e["event"] for e in store.events_since(0)]
    store.close()


# -- no heads-up for an event that is over ------------------------------------


class _CountingRunner:
    def __init__(self):
        self.calls = 0

    def execute(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("no decision call expected")


def test_an_inform_after_the_window_is_skipped_without_a_call(tmp_path):
    """Live 2026-10-02 14:25: an inform ran at the event's end and its reason,
    "missed the window; not going to start it now", went to him."""
    store = booted(SQLiteStore(tmp_path / "s.db"))
    session = _session(store, FakeClient(responses=["ok"]),
                       VirtualClock(t_h=11.5))
    runner = _CountingRunner()
    session._decision = runner  # type: ignore[assignment]
    st = NegotiationState(item_id="ag1", activity="gym", source_type="arc",
                          start_t_h=9.0, end_t_h=11.0, salience=0.8,
                          phase=NegotiationPhase.INFORM.value)
    session._negotiations["ag1"] = st
    out: list = []
    session._run_inform(st, 0, 11.5, steer=None, proactive_out=out)
    assert runner.calls == 0 and out == []
    assert st.informed is True
    store.close()


# -- the client's retries ------------------------------------------------------


def _usage(prompt, cached):
    return {"prompt_tokens": prompt, "completion_tokens": 1,
            "total_tokens": prompt + 1, "prompt_cache_hit_tokens": cached,
            "prompt_cache_miss_tokens": prompt - cached}


def test_content_re_asks_are_bounded_and_every_one_is_billed(monkeypatch):
    monkeypatch.setattr("harness.client.time.sleep", lambda _s: None)
    monkeypatch.setenv("LILY_TOKEN", "tok")
    client = OpenAICompatibleClient(lane="product", base_url="https://x/v1")
    posts = {"n": 0}

    def post(url, **kwargs):
        posts["n"] += 1
        return httpx.Response(
            200, json={"choices": [{"message": {"content": None}}],
                       "usage": _usage(100, 60)},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(client._client, "post", post)
    with pytest.raises(RuntimeError):
        client.chat_with_meta([{"role": "user", "content": "hi"}])
    assert posts["n"] == 3                      # not 7, and never 7 x 7

    replies = iter([None, "hello"])

    def post_then_ok(url, **kwargs):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": next(replies)}}],
                       "usage": _usage(100, 60)},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(client._client, "post", post_then_ok)
    result = client.chat_with_meta([{"role": "user", "content": "hi"}])
    assert result.content == "hello" and result.attempts == 2
    assert result.usage.prompt_tokens == 200
    assert result.usage.cached_tokens == 120


# -- the forked session id ----------------------------------------------------


def test_a_fresh_session_is_local_to_its_thread(monkeypatch):
    monkeypatch.setenv("LILY_TOKEN", "tok")
    client = OpenAICompatibleClient(lane="product", base_url="https://x/v1",
                                    session_id="run")
    inside = threading.Event()
    release = threading.Event()
    seen: dict = {}

    def fork():
        with fresh_session(client, "judge1"):
            seen["fork"] = client._headers()
            inside.set()
            release.wait(5)

    worker = threading.Thread(target=fork)
    worker.start()
    inside.wait(5)
    seen["main"] = client._headers()
    release.set()
    worker.join(5)
    assert "run-judge1" in str(seen["fork"])
    assert "run-judge1" not in str(seen["main"]) and "run" in str(seen["main"])
    assert client.session_id == "run"


# -- the planner asks again on prose -------------------------------------------


class _ProseThenPlan:
    supports_json = True
    model = "m"

    def __init__(self):
        self.session_id = "run"
        self.seen: list[str] = []

    def chat_with_meta(self, messages, **kwargs):
        self.seen.append(self.session_id)
        if self.session_id == "run":
            return ChatResult(content="[smug] A plan? Fine, dummy.")
        return ChatResult(content='{"activities": ["sketch"]}')


def test_the_planner_asks_again_on_prose():
    client = _ProseThenPlan()
    reply = _call_within_budget(client, "plan", 5.0)
    assert '"sketch"' in reply
    assert client.seen == ["run", "run-planner1"]


# -- the cold-point idle clock reads when a call went out ---------------------


def test_the_idle_clock_reads_when_a_late_fork_actually_ran(tmp_path):
    """Live 2026-10-01: the rollover forks ran at 01:14 but were stamped at the
    boundary (t=96.0); the first turn at t=98.04 measured 2.04 h idle against a
    real 0.8 h and pruned a warm cache."""
    from harness.anchor import RealTimeAnchor

    store = SQLiteStore(tmp_path / "s.db")
    anchor = RealTimeAnchor(epoch0_s=0.0, t_h0=0.0, tz="UTC")
    store.attach_anchor(anchor)
    store._wall = lambda: anchor.epoch_of(97.24)         # ran late
    store.log_llm_call(4, 96.0, "aux_judge", "p", "r", "m")
    assert store.last_llm_call_t_h(98.045) == pytest.approx(97.24)
    # an unanchored store, and a wall reading past the request, keep t_h
    assert store.last_llm_call_t_h(97.0) == 96.0
    store._anchor = None
    assert store.last_llm_call_t_h(98.045) == 96.0
    store.close()


# -- a reach-out reads back as one ---------------------------------------------


def test_a_reach_out_decision_reads_back_as_texting_him():
    """Live 2026-10-03 15:23: her yes to a reach-out read back as "started
    the event"."""
    row = {"popup_kind": "tool_decide_proactive", "t_h": 15.38}
    assert Session._decision_tool_result(row, {"initiate": True}) == (
        "recorded at 15:23: decided to text him.")
    assert Session._decision_tool_result(row, {"initiate": False}) == (
        "recorded at 15:23: decided not to text him.")


# -- the plan names whose it is -------------------------------------------------


@pytest.mark.parametrize("header", ["Nova's Thursday plan:", "Thursday's plan:"])
def test_the_day_end_forks_find_the_plan_under_either_header(tmp_path, header):
    """The plan row opens "Lily's Saturday plan:" from 2026-10-03; a day
    written before opens "Saturday's plan:". Both still bound the day."""
    from harness.anchor import RealTimeAnchor

    store = SQLiteStore(tmp_path / "s.db")
    store.attach_anchor(RealTimeAnchor(epoch0_s=0.0, t_h0=0.0, tz="UTC"))
    session = _session(store, FakeClient(responses=[]), VirtualClock(t_h=10.0))
    session._persist_row("system", f"{header}\n- gym (09:00–10:00)", 9.0, 0)
    assert session._day_marker(0) == header
    store.close()
