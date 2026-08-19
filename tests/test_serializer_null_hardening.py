"""WS-E serializer null-hardening tests (reasoning-only turns).

DeepSeek-compatible v4-flash can answer ENTIRELY in the reasoning channel:
``message.reasoning_content`` is populated while ``content`` comes back
null/empty (the null-content brick — alpha finding 2026-08-16). If such a
turn is serialized verbatim into the next request the gateway 400s it.
These tests pin the serializer rule: **content on the wire is always a
string — ``""`` for reasoning-only turns, never ``null``** — on both the
request side (payload construction) and the response side (ChatResult
round-trip). No network: the real client runs against ``httpx.MockTransport``
and scripted fakes, per tests/test_client.py patterns.
"""

from __future__ import annotations

import json

import httpx

from harness.client import ChatResult, FakeClient, OpenAICompatibleClient


def _client_with(handler, **kwargs):
    """OpenAICompatibleClient wired to a MockTransport (tests/test_client shape)."""
    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m", **kwargs
    )
    client._client = httpx.Client(transport=transport)
    return client


def _assert_no_content_null(payload: dict) -> None:
    """Every message content in a payload is a string — never None (WS-E)."""
    for msg in payload["messages"]:
        assert isinstance(msg.get("content"), str), (
            f"message content serialized as {msg.get('content')!r}: "
            f"would 400 on a DeepSeek-compatible gateway"
        )


# -- request side: payload construction ------------------------------------- #


def test_reasoning_only_turn_serializes_as_empty_content():
    """A reasoning-only assistant turn (content None / reasoning_content set)
    must reach the wire as content:"" — never content:null."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        seen.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _client_with(handler)
    client.chat(
        [
            {"role": "user", "content": "deep question"},
            # v4-flash quirk: answered entirely in the reasoning channel.
            {"role": "assistant", "content": None, "reasoning_content": "hidden"},
        ],
        system="SYS",
    )

    _assert_no_content_null(seen[0])
    assistant = seen[0]["messages"][-1]
    assert assistant["content"] == ""
    assert assistant["reasoning_content"] == "hidden"  # extraction path intact


def test_empty_string_content_stays_empty_string():
    """An already-empty content ("") must NOT be turned into null — and stays
    "" (the protocol-safe DeepSeek-compatible form)."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        seen.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _client_with(handler)
    client.chat(
        [{"role": "assistant", "content": "", "reasoning_content": "only thought"}],
        system="SYS",
    )

    _assert_no_content_null(seen[0])
    assert seen[0]["messages"][-1]["content"] == ""


def test_chatresult_content_none_fixture_serializes_cleanly_via_fake():
    """A ChatResult fixture with content=None (or '') + reasoning must
    serialize without emitting content:null — FakeClient records mirror the
    wire shape (WS-E mirror guard)."""
    for fixture_content in (None, ""):
        # The fixture intentionally violates ChatResult's str typing: a
        # caller holding a None content (e.g. from a pre-WS-E stored turn)
        # is exactly the leak this guard serializes defensively.
        result = ChatResult(
            content=fixture_content,  # type: ignore[arg-type]
            reasoning="thoughts only",
        )
        client = FakeClient()
        client.chat(
            [
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "content": result.content,
                    "reasoning_content": result.reasoning,
                },
            ]
        )
        recorded = client.calls[0]["messages"]
        assert recorded[-1]["content"] == ""
        assert recorded[-1]["reasoning_content"] == "thoughts only"


# -- response side: ChatResult round-trip ------------------------------------ #


def test_reasoning_only_response_round_trips_without_retry(monkeypatch):
    """content null/absent + reasoning_content present is a LEGITIMATE reply:
    ChatResult(content='', reasoning='...') on the FIRST attempt — no null
    retry storm, no empty-content retry, no sleep."""
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "reasoning_content": "the whole answer lives here",
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = _client_with(handler, max_retries=2)
    result = client.chat_with_meta([{"role": "user", "content": "q"}])

    assert calls["n"] == 1  # accepted on the first attempt
    assert sleeps == []  # never burned backoff on a legitimate reply
    assert result.content == ""
    assert result.reasoning == "the whole answer lives here"
    assert result.finish_reason == "stop"


def test_missing_content_key_with_reasoning_round_trips():
    """A response message with NO content key at all + reasoning_content is
    not the malformed-200 path — it round-trips as ChatResult(content='', ...)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"reasoning_content": "no content key at all"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = _client_with(handler, max_retries=2)
    result = client.chat_with_meta([{"role": "user", "content": "q"}])

    assert result.content == ""
    assert result.reasoning == "no content key at all"


def test_reasoning_only_turn_serializes_cleanly_on_next_request():
    """Full round trip: a reasoning-only response becomes the next request's
    assistant turn and serializes with content:"" — never content:null."""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        payloads.append(payload)
        # First call: reasoning-only reply. Second call: a normal reply.
        if len(payloads) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "reasoning_content": "deep reasoning",
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "answered"}}]}
        )

    client = _client_with(handler, max_retries=2)
    first = client.chat_with_meta([{"role": "user", "content": "q"}])
    assert first.content == "" and first.reasoning == "deep reasoning"

    # The reasoning-only turn is fed back as the assistant turn of the
    # next request — it must serialize as "".
    client.chat(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": first.content,
                "reasoning_content": first.reasoning,
            },
        ]
    )

    _assert_no_content_null(payloads[0])
    _assert_no_content_null(payloads[1])
    assert payloads[1]["messages"][-1]["content"] == ""
    assert payloads[1]["messages"][-1]["reasoning_content"] == "deep reasoning"


def test_reasoning_never_contaminates_content():
    """The reasoning-content extraction stays intact (client.py ~206): a
    reasoning-only reply yields content="" and reasoning=... — the two are
    never swapped, and content never carries the reasoning text."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "reasoning_content": "the hidden thinking",
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = _client_with(handler)
    result = client.chat_with_meta([{"role": "user", "content": "q"}])

    assert result.content == ""
    assert result.reasoning == "the hidden thinking"
    assert "hidden thinking" not in result.content


# -- the narrowed exemptions stay narrow ------------------------------------- #


def test_null_content_without_reasoning_still_retried(monkeypatch):
    """WS-E exempts ONLY reasoning-only replies: null content with NO tools
    and NO reasoning remains a broken reply — retried, then raises."""
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"choices": [{"message": {"content": None}}]}
        )

    client = _client_with(handler, max_retries=1)
    try:
        client.chat_with_meta([{"role": "user", "content": "q"}])
        raise AssertionError("expected RuntimeError for null content")
    except RuntimeError as exc:
        assert "null content" in str(exc)
    assert calls["n"] == 2  # capped retry budget, never a hang


def test_empty_content_without_reasoning_still_retried(monkeypatch):
    """Empty content WITHOUT reasoning keeps the B1 generation-integrity
    retry — the WS-E exemption is scoped to reasoning-only replies only."""
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"choices": [{"message": {"content": ""}}]}
        )

    client = _client_with(handler, max_retries=1)
    try:
        client.chat_with_meta([{"role": "user", "content": "q"}])
        raise AssertionError("expected RuntimeError for empty content")
    except RuntimeError as exc:
        assert "empty/whitespace-only content" in str(exc)
    assert calls["n"] == 2