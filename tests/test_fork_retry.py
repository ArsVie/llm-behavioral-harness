"""An empty fork reply is asked again on another backend, and on record.

Live 2026-10-01: at the rollover the judge, the memory fork and the day
planner all came back empty, the retries on the same gateway session too,
while the same requests answered 17 of 17 times on other sessions.
"""

from __future__ import annotations

from harness.client import ChatResult, fresh_session
from harness.day_planner import _call_within_budget
from harness.judge import judge_day
from harness.memory_fork import remember_day
from harness.metering import MeteredClient

FORK = lambda task: ("SYS", [{"role": "system", "content": task}], None)  # noqa: E731


class Sessions:
    """Answers empty on the session it was built with, text on any other."""

    supports_json = True
    model = "m"

    def __init__(self, text: str):
        self.session_id = "run"
        self.text = text
        self.seen: list[str] = []

    def chat_with_meta(self, messages, **kwargs):
        self.seen.append(self.session_id)
        content = "" if self.session_id == "run" else self.text
        return ChatResult(content=content, reasoning="hm" if not content else None,
                          finish_reason="stop")


class Ledger:
    def __init__(self):
        self.rows = []

    def log_llm_call(self, day, t_h, role, prompt, response, model, meta, **kw):
        self.rows.append((role, response, meta))
        return len(self.rows)


def test_fresh_session_restores_the_id_and_skips_clients_without_one():
    client = Sessions("x")
    with fresh_session(MeteredClient(client, None, None, "aux_judge"), "judge1"):
        assert client.session_id == "run-judge1"
    assert client.session_id == "run"
    plain = object()
    with fresh_session(plain, "t"):
        pass


def test_the_judge_asks_again_on_another_session():
    client = Sessions('{"score": 0.4, "justification": "fine"}')
    result = judge_day("", client, fork=FORK)
    assert result.score == 0.4
    assert client.seen == ["run", "run-judge1"]
    assert client.session_id == "run"


def test_the_memory_fork_asks_again_on_another_session():
    client = Sessions('{"items": []}')
    remember_day(client, FORK, known=[], his_turns=[])
    assert client.seen == ["run", "run-memory1"]


def test_the_planner_asks_again_on_another_session():
    client = Sessions('{"activities": ["reading"]}')
    assert "reading" in _call_within_budget(client, "plan", 10.0, FORK)
    assert client.seen == ["run", "run-planner1"]


def test_the_ledger_row_says_what_an_empty_reply_was():
    ledger = Ledger()
    metered = MeteredClient(Sessions("x"), ledger, None, "aux_judge")
    metered.chat_with_meta([{"role": "user", "content": "q"}])
    [(role, response, meta)] = ledger.rows
    assert response == ""
    assert meta == {"aux": True, "role": "aux_judge",
                    "finish_reason": "stop", "reasoning_chars": 2}
