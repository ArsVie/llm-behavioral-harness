"""The run's gateway session moves off a backend that went bad.

Live 2026-10-01: after the backend behind the pinned id switched, warm
requests hit ~30% cache instead of ~99%, decisions stopped parsing, and the
run stayed on it for days.
"""

from __future__ import annotations

import json
import os

import httpx

from harness.client import OpenAICompatibleClient, fresh_session
from harness.gateway import KV_SESSION_ID, STRIKES, GatewayWatch, WATCH
from harness.store import SQLiteStore


class _Clock:
    def __init__(self):
        self.s = 0.0

    def __call__(self):
        return self.s


def test_warm_cache_misses_rotate_the_session(monkeypatch):
    monkeypatch.setenv("LLM_SESSION_ID", "run-a")
    clock = _Clock()
    watch = GatewayWatch(clock)
    watch.note_call(11_000, 10_900)                  # healthy
    for _ in range(STRIKES):
        clock.s += 60
        watch.note_call(11_000, 3_200)               # warm, ~30% hit
    [rotation] = watch.drain()
    assert rotation.old == "run-a"
    assert rotation.new != "run-a"
    assert "cache miss" in rotation.reason
    assert os.environ["LLM_SESSION_ID"] == rotation.new


def test_a_healthy_call_resets_the_count_and_cold_misses_do_not_count(monkeypatch):
    monkeypatch.setenv("LLM_SESSION_ID", "run-a")
    clock = _Clock()
    watch = GatewayWatch(clock)
    watch.note_call(11_000, 10_900)
    for _ in range(STRIKES - 1):
        clock.s += 60
        watch.note_call(11_000, 3_200)
    clock.s += 60
    watch.note_call(11_000, 10_900)                  # healthy: count resets
    clock.s += 60
    watch.note_call(11_000, 3_200)
    clock.s += 3 * 3600                              # the cache is cold anyway
    watch.note_call(11_000, 0)
    clock.s += 3 * 3600
    watch.note_call(11_000, 0)
    assert watch.drain() == []


def test_decision_failures_rotate_once_per_cooldown(monkeypatch):
    monkeypatch.setenv("LLM_SESSION_ID", "run-a")
    clock = _Clock()
    watch = GatewayWatch(clock)
    for _ in range(2 * STRIKES):
        watch.note_decision(False)
    assert len(watch.drain()) == 1                   # the second set waits


def test_rotation_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("HARNESS_SESSION_ROTATION", "0")
    monkeypatch.setenv("LLM_SESSION_ID", "run-a")
    watch = GatewayWatch(_Clock())
    for _ in range(STRIKES):
        watch.note_decision(False)
    assert watch.drain() == []


def _usage(prompt, cached):
    return {"prompt_tokens": prompt, "completion_tokens": 1,
            "total_tokens": prompt + 1, "prompt_cache_hit_tokens": cached,
            "prompt_cache_miss_tokens": prompt - cached}


def test_lane_clients_follow_the_rotated_session(monkeypatch):
    """Every lane client without an explicit id moves with the run; calls on
    a forked id do not count against the run's backend."""
    monkeypatch.setenv("LILY_TOKEN", "tok")
    monkeypatch.setenv("LLM_SESSION_ID", "run-a")
    client = OpenAICompatibleClient(lane="product", base_url="https://x/v1")
    sent: list[str] = []
    hits = iter([10_900] + [3_200] * 10)

    def post(url, **kwargs):
        sent.append(kwargs["headers"]["x-opencode-session"])
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hi"}}],
                       "usage": _usage(11_000, next(hits))},
            request=httpx.Request("POST", url))

    monkeypatch.setattr(client._client, "post", post)
    msgs = [{"role": "user", "content": "hi"}]
    client.chat_with_meta(msgs)
    with fresh_session(client, "judge1"):
        for _ in range(STRIKES):
            client.chat_with_meta(msgs)              # another backend's misses
    assert WATCH.drain() == []
    for _ in range(STRIKES):
        client.chat_with_meta(msgs)
    [rotation] = WATCH.drain()
    client.chat_with_meta(msgs)
    assert sent[0] == "run-a" and sent[1] == "run-a-judge1"
    assert sent[-1] == rotation.new


def test_the_session_saves_the_new_id_and_logs_it(tmp_path, monkeypatch):
    from engine.types import MoodVariant, PersonaParams, TimingParams
    from harness.client import FakeClient
    from harness.clock import VirtualClock
    from harness.judge import ScriptedJudge
    from harness.session import Session

    monkeypatch.setenv("LLM_SESSION_ID", "run-a")
    store = SQLiteStore(tmp_path / "s.db")
    store.set_kv(KV_SESSION_ID, "run-a")
    session = Session(
        store, persona=PersonaParams(), timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS, seed=1,
        client=FakeClient(responses=["hey"]), clock=VirtualClock(t_h=10.0),
        judge=ScriptedJudge(0.5).judge_day,
    )
    for _ in range(STRIKES):
        WATCH.note_decision(False)
    session.on_message("hi")
    new = store.get_kv(KV_SESSION_ID)
    assert new not in (None, "run-a")
    [event] = [e for e in store.events_since(0)
               if e["event"] == "gateway_session_rotated"]
    assert json.loads(event["detail"])["new"] == new
    store.close()
