"""Integration A — bubble backend streaming through session + runtime.

End-to-end wiring of the optional ``chat_stream`` surface (2026-09-07):

- With ``HARNESS_BUBBLE_STREAM=1`` (which requires ``HARNESS_BUBBLES=1``)
  AND a client exposing ``chat_stream``, ``Session._chat`` routes through
  ``_generate_stream``: the wire chunks feed a ``BubbleStreamer``, bubbles
  release incrementally as their boundaries parse, and the JOINED raw
  stream is the canonical persisted reply (one message row, one llm_call
  row — the single-persist invariant).
- Streamed replies carry no usage accounting (the stream yields text
  only): ``usage=None`` / ``raw_cost=None`` / ``reasoning=None`` degrade
  to NULL ledger columns, same as gateways without usage.
- OFF (or a client without ``chat_stream``) takes the EXACT canonical
  path — ``_generate`` + post-hoc ``_split_into_bubbles`` — byte parity:
  same seed, same scripted reply, identical ``TurnResult.reply`` and
  ``TurnResult.bubbles``.
- ``TurnResult.streamed`` is a data-origin MARKER only: the runtime's
  paced multi-send in ``_send_turn_outputs`` is byte-identical for both
  origins (sequential send_message per bubble, same gap heuristic).

Plain pytest only (no pytest-asyncio): async behavior is driven with
``asyncio.run`` inside sync tests.
"""

import asyncio
import json
from functools import partial
from unittest.mock import patch

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.bubbles import parse_bubbles
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import Interest, PersonaProfile
from harness.runtime import AsyncRuntime
from harness.scheduler import ProactiveSchedule
from harness.session import Session, TurnResult
from harness.store import SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
PROF = PersonaProfile(
    name="Nova",
    core="warm",
    interests=(Interest("chat", "exact", 0.8),),
    routines=(),
)

#: A reply the model would emit under the bubble instruction: three
#: sentence-complete pieces split by blank lines.
REPLY = "Hello there.\n\nHow are you today?\n\nI am doing well."
BUBBLES = ("Hello there.", "How are you today?", "I am doing well.")

STREAM_ON = {"HARNESS_BUBBLES": "1", "HARNESS_BUBBLE_STREAM": "1"}
BUBBLES_ONLY = {"HARNESS_BUBBLES": "1"}
ALL_OFF = {"HARNESS_BUBBLES": "0", "HARNESS_BUBBLE_STREAM": "0"}


def _session(seed: int, client: FakeClient) -> Session:
    """Session over an in-memory store (mirrors tests/test_bubbles.py)."""
    sess = Session(
        store=SQLiteStore(":memory:"),
        persona=PERSONA,
        timing=TIMING,
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=seed,
        client=client,
        clock=VirtualClock(),
    )
    sess._profile = PROF
    return sess


def _env(monkeypatch, env: dict) -> None:
    """Set exactly the HARNESS_BUBBLES*/HARNESS_BUBBLES env flags."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if "HARNESS_BUBBLES" not in env:
        monkeypatch.delenv("HARNESS_BUBBLES", raising=False)
    if "HARNESS_BUBBLE_STREAM" not in env:
        monkeypatch.delenv("HARNESS_BUBBLE_STREAM", raising=False)


def _assistant_rows(store) -> list[dict]:
    msgs = store.messages_for_day(store.latest_daily_state()["day"])
    return [m for m in msgs if m["role"] == "assistant"]


def _llm_rows(store) -> list[dict]:
    return [dict(r) for r in store.conn.execute("SELECT * FROM llm_calls")]


# --- session-level: joined reply + incremental bubbles ---------------------- #


def test_stream_on_joined_reply_equals_non_stream_reply(monkeypatch):
    """The same scripted reply yields a byte-identical TurnResult.reply on
    the streamed path (chunks re-joined) and the canonical _generate path."""
    # Streamed run: bubbles+stream on, small chunks so separators and
    # sentences land across wire pieces.
    _env(monkeypatch, STREAM_ON)
    cli_on = FakeClient(responses=[REPLY])
    r_on = _session(seed=11, client=cli_on).on_message("hi")

    # Canonical run: bubbles on, stream off -> _generate + post-hoc split.
    _env(monkeypatch, BUBBLES_ONLY)
    cli_off = FakeClient(responses=[REPLY])
    r_off = _session(seed=11, client=cli_off).on_message("hi")

    assert r_on.reply == r_off.reply == REPLY
    # The streamed reply is the raw chunks joined: separators intact.
    assert "\n\n" in r_on.reply
    assert r_on.bubbles == BUBBLES
    assert r_off.bubbles == BUBBLES
    assert r_on.streamed is True
    assert r_off.streamed is False


def test_stream_on_bubbles_equal_parse_bubbles_of_reply(monkeypatch):
    """Incremental feed+flush releases exactly parse_bubbles(joined reply)."""
    _env(monkeypatch, STREAM_ON)
    r = _session(seed=22, client=FakeClient(responses=[REPLY])).on_message("hi")
    assert r.bubbles == tuple(parse_bubbles(r.reply))
    assert r.streamed is True


def _chunked_stream(pieces: list[str], messages, **kw):
    """Side-effect factory: yield pre-sliced chunks (no loop-variable
    closure, so ruff B023 stays quiet)."""
    return iter(pieces)


def test_stream_join_is_byte_identical_across_chunk_sizes(monkeypatch):
    """Chunking must never alter the joined reply (mid-sentence wraps,
    separators split across chunks, trailing newline)."""
    _env(monkeypatch, STREAM_ON)
    texts = [
        "One sentence.",
        "Two.\n\nThree.",
        "Line wrap\ncontinues here.\n\nNext bubble!",
        "A.\n\nB.\n\nC.",
        REPLY + "\n",
    ]
    for i, text in enumerate(texts):
        sess = _session(seed=100 + i, client=FakeClient(responses=[text]))
        pieces = [text[j:j + 3] for j in range(0, len(text), 3)]
        # chunk_size=3 forces every boundary and many words across chunks.
        with patch.object(sess.client, "chat_stream", autospec=True,
                          side_effect=partial(_chunked_stream, pieces)):
            r = sess.on_message("hi")
        assert r.reply == text, f"text {i!r}: joined != original"
        parsed = parse_bubbles(text)
        # A single-part reply stays bubbles=None (parity with the post-hoc
        # splitter); a multi-part reply equals the incremental release.
        if len(parsed) >= 2:
            assert r.bubbles == tuple(parsed), f"text {i!r}"
        else:
            assert r.bubbles is None, f"text {i!r}"


def test_stream_single_bubble_is_not_streamed(monkeypatch):
    """A reply with no split stays bubbles=None and streamed=False — the
    runtime single-send path, exactly like the canonical path."""
    _env(monkeypatch, STREAM_ON)
    sess = _session(seed=31, client=FakeClient(responses=["One message only."]))
    r = sess.on_message("hi")
    assert r.reply == "One message only."
    assert r.bubbles is None
    assert r.streamed is False


# --- persistence: single joined reply, usage NULL tolerated ----------------- #


def test_stream_persists_single_joined_reply_and_usage_null(monkeypatch):
    """Streamed turn: exactly one assistant message + one llm_call row, the
    response is the JOINED reply, and usage/reasoning columns are NULL even
    when the scripted response carried usage (stream yields text only)."""
    scripted = {
        "content": REPLY,
        "usage": {"prompt_tokens": 40, "completion_tokens": 9,
                  "total_tokens": 49},
        "cost": 0.0012,
        "reasoning": "secret trace",
    }
    _env(monkeypatch, STREAM_ON)
    sess = _session(seed=42, client=FakeClient(responses=[scripted]))
    r = sess.on_message("hi")
    day = sess.clock.day()

    msgs = sess.store.messages_for_day(day)
    assistant = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"] == r.reply == REPLY
    # day-start block (system) + the user turn + one joined assistant turn
    assert len(msgs) == 3

    calls = _llm_rows(sess.store)
    assert len(calls) == 1
    assert calls[0]["response"] == REPLY
    # Usage accounting is unknown on the streamed path: NULL columns, no
    # reasoning meta — graceful degradation, not an error.
    assert calls[0]["prompt_tokens"] is None
    assert calls[0]["completion_tokens"] is None
    assert calls[0]["total_tokens"] is None
    assert calls[0]["lane"] is None
    assert calls[0]["raw_cost"] is None
    assert calls[0]["meta"] is None  # reasoning=None -> no meta dict
    assert r.bubbles == BUBBLES


def test_usage_columns_do_populate_on_non_stream_path(monkeypatch):
    """Control for the NULL test: the same store DOES persist usage when
    the canonical (non-streamed) path runs with a scripted usage — proving
    the streamed NULLs come from the degradation rule, not the store."""
    scripted = {
        "content": REPLY,
        "usage": {"prompt_tokens": 40, "completion_tokens": 9,
                  "total_tokens": 49},
        "cost": 0.0012,
        "reasoning": "secret trace",
    }
    _env(monkeypatch, BUBBLES_ONLY)
    sess = _session(seed=42, client=FakeClient(responses=[scripted]))
    r = sess.on_message("hi")

    calls = _llm_rows(sess.store)
    assert len(calls) == 1
    assert calls[0]["response"] == r.reply == REPLY
    assert calls[0]["prompt_tokens"] == 40
    assert calls[0]["total_tokens"] == 49
    assert calls[0]["raw_cost"] == 0.0012
    # The raw row stores meta as JSON text; parse before comparing.
    assert json.loads(calls[0]["meta"]) == {"reasoning": "secret trace"}
    assert calls[0]["lane"] is None


# --- flag-off parity -------------------------------------------------------- #


def test_stream_off_is_exact_generate_path_identical_result(monkeypatch):
    """Bubbles on + stream off: the canonical _generate/_split_into_bubbles
    path runs and the TurnResult is identical to the streamed one except
    for the origin marker."""
    _env(monkeypatch, STREAM_ON)
    cli_on = FakeClient(responses=[REPLY])
    sess_on = _session(seed=55, client=cli_on)
    r_on = sess_on.on_message("hi")

    _env(monkeypatch, BUBBLES_ONLY)
    cli_off = FakeClient(responses=[REPLY])
    sess_off = _session(seed=55, client=cli_off)
    r_off = sess_off.on_message("hi")

    assert r_off.reply == r_on.reply == REPLY
    assert r_off.bubbles == r_on.bubbles == BUBBLES
    assert r_off.streamed is False
    # One client call in both modes, identical wire shape (FakeClient
    # records chat_stream through chat_with_meta — parity pinned in
    # test_client_stream).
    assert len(cli_on.calls) == len(cli_off.calls) == 1
    assert cli_on.calls[0] == cli_off.calls[0]


def test_flags_fully_off_zero_behavior_change(monkeypatch):
    """Neither flag set: bubbles stay None, streamed False, the reply is
    untouched and the system prompt carries no bubble instruction."""
    _env(monkeypatch, ALL_OFF)
    cli = FakeClient(responses=[REPLY])
    sess = _session(seed=66, client=cli)
    r = sess.on_message("hi")
    assert r.reply == REPLY
    assert r.bubbles is None
    assert r.streamed is False
    assert "bubbles" not in cli.calls[0]["system"].lower()
    assert "split" not in cli.calls[0]["system"].lower()


def test_client_without_chat_stream_falls_back_to_generate(monkeypatch):
    """A client WITHOUT the optional chat_stream surface (getattr -> None)
    takes the canonical path even with the stream flag on."""

    class _NoStreamClient(FakeClient):
        chat_stream = None

    _env(monkeypatch, STREAM_ON)
    cli = _NoStreamClient(responses=[REPLY])
    sess = _session(seed=77, client=cli)
    r = sess.on_message("hi")
    assert r.reply == REPLY
    assert r.bubbles == BUBBLES
    assert r.streamed is False  # fell back: post-hoc split, not streamed
    assert len(cli.calls) == 1


# --- runtime: paced multi-send of streamed bubbles -------------------------- #


def _run_delivery(r: TurnResult, ch: FakeChannel, sleeps: list[float],
                  sess: Session, seed: int) -> None:
    """Drive the real AsyncRuntime._send_turn_outputs with a recording
    sleeper."""
    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    rt = AsyncRuntime(
        session=sess,
        channel=ch,
        store=sess.store,
        timing=TIMING,
        seed=seed,
        schedule=ProactiveSchedule.restore(seed, sess.store),
        resolver=None,
        sleeper=sleeper,
    )
    asyncio.run(rt._send_turn_outputs(r, proactive=False))


def _expected_gap(part: str) -> float:
    """The runtime's bubble gap heuristic, mirrored for the assertion."""
    gap = 1.0 + 0.5 * len(part) / 80.0
    return max(0.6, min(gap, 2.5))


def test_runtime_sends_streamed_bubbles_in_order_with_pacing(monkeypatch):
    """A streamed TurnResult fans out as a paced multi-send: every bubble
    in order, one gap sleep before each bubble after the first."""
    _env(monkeypatch, STREAM_ON)
    sess = _session(seed=88, client=FakeClient(responses=[REPLY]))
    r = sess.on_message("hi")
    assert r.streamed is True and r.bubbles is not None

    ch = FakeChannel()
    sleeps: list[float] = []
    _run_delivery(r, ch, sleeps, sess, seed=88)

    assert [m.text for m in ch.sent] == list(r.bubbles) == list(BUBBLES)
    assert len(sleeps) == len(BUBBLES) - 1
    expected = [_expected_gap(part) for part in BUBBLES[1:]]
    assert sleeps == expected


def test_runtime_delivery_identical_for_streamed_and_posthoc(monkeypatch):
    """The streamed marker does NOT change delivery: the same bubbles send
    with the same order, texts and pacing whether origin is streamed or
    post-hoc (sequential send_message, no SSE/edit)."""
    # Streamed origin.
    _env(monkeypatch, STREAM_ON)
    sess_on = _session(seed=99, client=FakeClient(responses=[REPLY]))
    r_on = sess_on.on_message("hi")
    # Post-hoc origin (bubbles on, stream off).
    _env(monkeypatch, BUBBLES_ONLY)
    sess_off = _session(seed=99, client=FakeClient(responses=[REPLY]))
    r_off = sess_off.on_message("hi")

    assert r_on.bubbles == r_off.bubbles == BUBBLES
    assert r_on.streamed is True and r_off.streamed is False

    ch_on, ch_off = FakeChannel(), FakeChannel()
    sleeps_on: list[float] = []
    sleeps_off: list[float] = []
    _run_delivery(r_on, ch_on, sleeps_on, sess_on, seed=99)
    _run_delivery(r_off, ch_off, sleeps_off, sess_off, seed=99)

    assert [m.text for m in ch_on.sent] == [m.text for m in ch_off.sent]
    assert sleeps_on == sleeps_off
    # Outbound flags match the reactive turn.
    assert all(m.proactive is False for m in ch_on.sent)
    assert all(m.reason is None for m in ch_on.sent)
