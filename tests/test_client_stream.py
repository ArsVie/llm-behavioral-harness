"""Streaming surface tests (2026-09-07): chat_stream on both clients.

Covers SSE parsing on the real client via httpx.MockTransport (content
deltas, [DONE], malformed lines, a non-streaming fallback path), the
chunked slicing on FakeClient, and the calls-log parity between
``chat_stream`` and ``chat_with_meta`` (one entry, identical wire shape).
The existing chat/chat_with_meta suites stay untouched — streaming is an
additive, optional surface (``LLMClient`` declares ``chat_stream`` but
clients without it still conform; consumers duck-type with getattr).
"""

import json

import httpx
import pytest

from harness.client import FakeClient, OpenAICompatibleClient


# --- SSE body helpers ----------------------------------------------------- #


def _sse_event(payload: dict) -> str:
    """One ``data:`` line carrying a JSON chunk, per the SSE framing."""
    return f"data: {json.dumps(payload)}"


def _content_event(piece: str) -> str:
    """A stream chunk with a text content delta (and null finish reason)."""
    return _sse_event(
        {
            "choices": [
                {"delta": {"content": piece}, "finish_reason": None}
            ]
        }
    )


def _role_event() -> str:
    """The opening chunk announcing the assistant role (no content)."""
    return _sse_event(
        {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]}
    )


_DONE = "data: [DONE]"


def _sse_body(*events: str) -> str:
    """The full SSE body: each event line followed by a blank line."""
    return "".join(f"{event}\n\n" for event in events)


def _client_with(handler, **kwargs) -> OpenAICompatibleClient:
    """OpenAICompatibleClient wired to a MockTransport handler."""
    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m", **kwargs
    )
    client._client = httpx.Client(transport=transport)
    return client


def _request_payload(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())


# --- OpenAICompatibleClient: SSE parsing ---------------------------------- #


def test_openai_chat_stream_yields_content_deltas_and_sends_stream_flag():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_request_payload(request))
        return httpx.Response(
            200,
            content=_sse_body(
                _role_event(),
                _content_event("Hello"),
                _content_event(" world"),
                _DONE,
            ).encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    client = _client_with(handler)

    pieces = list(client.chat_stream([{"role": "user", "content": "q"}]))

    # Raw wire deltas, no padding/grouping; the role-only chunk and the
    # [DONE] sentinel are skipped.
    assert pieces == ["Hello", " world"]
    assert "".join(pieces) == "Hello world"
    # The stream request is the usual payload plus stream: true — no
    # json_mode / tools keys were smuggled in.
    assert seen[0]["stream"] is True
    assert seen[0]["model"] == "m"
    assert seen[0]["messages"] == [{"role": "user", "content": "q"}]
    assert "response_format" not in seen[0]


def test_openai_chat_stream_survives_multiline_and_keepalive_noise():
    body = (
        ": keep-alive comment\n\n"
        + _sse_body(_content_event("a"), _DONE)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    client = _client_with(handler)
    assert list(client.chat_stream([{"role": "user", "content": "q"}])) == ["a"]


def test_openai_chat_stream_skips_malformed_lines_without_raising():
    # CRLF framing, empty deltas, a malformed (non-JSON) data line and a
    # malformed event shape are all skipped; content is preserved.
    body = (
        'data: {"choices":[{"delta":{"content":"He"}}]}\r\n\r\n'
        'data: {"choices":[{"delta":{"content":""}}]}\n\n'
        "data: not-json\n\n"
        'data: {"no":"choices"}\n\n'
        'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    client = _client_with(handler)
    assert list(client.chat_stream([{"role": "user", "content": "q"}])) == [
        "He", "llo"
    ]


def test_openai_chat_stream_no_extra_request_when_finish_reason_present():
    # finish_reason=stop on a normal completion body: stream=true was
    # ignored by the endpoint; no deltas -> exactly ONE non-streaming
    # fallback request, whose content is yielded once.
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _request_payload(request)
        requests.append(payload)
        if payload.get("stream"):
            return httpx.Response(
                200,
                content=(
                    'data: {"choices":[{"message":{"content":"full reply"},'
                    '"finish_reason":"stop"}]}\n\n'
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "full reply"},
                               "finish_reason": "stop"}]},
        )

    client = _client_with(handler)
    pieces = list(client.chat_stream([{"role": "user", "content": "q"}]))

    assert pieces == ["full reply"]
    # The fallback request dropped stream: true.
    assert len(requests) == 2
    assert requests[0]["stream"] is True
    assert "stream" not in requests[1]


def test_openai_chat_stream_fallback_after_retryable_status(monkeypatch):
    # A streamed 503 exhausts the bounded retry budget (max_retries=1 →
    # 2 stream attempts), then ONE non-streaming fallback call returns
    # the full reply.
    calls = {"n": 0}
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        payload = _request_payload(request)
        if payload.get("stream"):
            return httpx.Response(503, json={"error": "nope"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "degraded reply"}}]},
        )

    client = _client_with(handler, max_retries=1)
    pieces = list(client.chat_stream([{"role": "user", "content": "q"}]))

    assert pieces == ["degraded reply"]
    assert calls["n"] == 3  # two stream attempts + one fallback
    assert sleeps == [2.0]  # bounded exponential backoff


def test_openai_chat_stream_non_streaming_client_error_is_retried(
    monkeypatch,
):
    # 400 is not a retryable status: _post retries it the full budget as
    # transport-level HTTPStatusError, then the fallback fires.
    calls = {"n": 0}
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        payload = _request_payload(request)
        if payload.get("stream"):
            return httpx.Response(400, json={"error": "nope"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "recovered"}}]},
        )

    client = _client_with(handler, max_retries=1)
    pieces = list(client.chat_stream([{"role": "user", "content": "q"}]))

    assert pieces == ["recovered"]
    assert calls["n"] == 3  # 2 stream attempts + 1 fallback
    assert sleeps == [2.0]


def test_openai_chat_stream_transport_error_propagates(monkeypatch):
    # A connection failure means the endpoint is unreachable: no fallback
    # (the non-streaming call would fail identically), the error raises.
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)
    transport = httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom"))
    )
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m",
        max_retries=1,
    )
    client._client = httpx.Client(transport=transport)

    with pytest.raises(httpx.ConnectError):
        list(client.chat_stream([{"role": "user", "content": "q"}]))
    assert len(sleeps) == 1  # one backoff before the final raise


def test_openai_chat_stream_requires_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = OpenAICompatibleClient(
        base_url="https://x.test", api_key=None, model="m"
    )
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        list(client.chat_stream([{"role": "user", "content": "q"}]))


def test_openai_chat_stream_system_prepend_and_full_params_passthrough():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_request_payload(request))
        return httpx.Response(
            200,
            content=_sse_body(_content_event("ok"), _DONE).encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    client = _client_with(handler)
    tools = [{"type": "function", "function": {"name": "f"}}]
    pieces = list(
        client.chat_stream(
            [{"role": "user", "content": "q"}],
            system="SYS", temperature=0.0, max_tokens=7, tools=tools,
            tool_choice="auto", reasoning_effort="low",
        )
    )

    assert pieces == ["ok"]
    assert seen[0]["stream"] is True
    assert seen[0]["messages"][0] == {"role": "system", "content": "SYS"}
    assert seen[0]["temperature"] == 0.0
    assert seen[0]["max_tokens"] == 7
    assert seen[0]["tools"] == tools
    assert seen[0]["tool_choice"] == "auto"
    assert seen[0]["reasoning_effort"] == "low"


def test_openai_chat_stream_lazy_generator_fires_request_on_first_next():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            content=_sse_body(_content_event("x"), _DONE).encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    client = _client_with(handler)

    gen = client.chat_stream([{"role": "user", "content": "q"}])
    assert calls["n"] == 0  # nothing sent until consumed
    assert next(gen) == "x"
    assert calls["n"] == 1  # one request for the whole stream
    with pytest.raises(StopIteration):
        next(gen)
    assert calls["n"] == 1


# --- FakeClient: chunked slicing ------------------------------------------ #


def test_fake_client_chat_stream_chunks_queued_reply():
    client = FakeClient(responses=["a fairly long scripted reply"])
    pieces = list(
        client.chat_stream(
            [{"role": "user", "content": "q"}], chunk_size=8
        )
    )

    # "a fairly| long sc|ripted r|eply" — 25 chars in 8-char pieces, the
    # partial trailing chunk (3 chars) still delivered.
    assert pieces == ["a fairly", " long sc", "ripted r", "eply"]
    assert "".join(pieces) == "a fairly long scripted reply"


def test_fake_client_chat_stream_short_reply_yielded_whole():
    client = FakeClient(responses=["short", "short"])
    assert list(client.chat_stream([{"role": "user", "content": "q"}])) == [
        "short"
    ]
    # Default chunk_size (64) yields even a long reply whole — the caller
    # always receives the full text.
    assert list(
        client.chat_stream([{"role": "user", "content": "q"}])
    ) == ["short"]


def test_fake_client_chat_stream_dict_response_uses_content_field():
    client = FakeClient(
        responses=[
            {
                "content": "scripted content",
                "reasoning": "think",
                "finish_reason": "stop",
            }
        ]
    )
    pieces = list(
        client.chat_stream(
            [{"role": "user", "content": "q"}], chunk_size=10
        )
    )

    assert pieces == ["scripted c", "ontent"]
    assert "".join(pieces) == "scripted content"
    # The dict entry is consumed exactly as chat_with_meta would.
    assert not client.responses


def test_fake_client_chat_stream_empty_content_yields_nothing():
    client = FakeClient(responses=["", "second"])
    assert list(client.chat_stream([{"role": "user", "content": "q"}])) == []
    assert list(
        client.chat_stream([{"role": "user", "content": "q"}], chunk_size=2)
    ) == ["se", "co", "nd"]


def test_fake_client_chat_stream_echo_chunks():
    client = FakeClient(echo=True)
    pieces = list(
        client.chat_stream(
            [{"role": "user", "content": "hi there"}], chunk_size=5
        )
    )

    assert pieces == ["echo:", " hi t", "here"]
    assert "".join(pieces) == "echo: hi there"


def test_fake_client_chat_stream_default_reply():
    client = FakeClient()
    pieces = list(
        client.chat_stream([{"role": "user", "content": "q"}], chunk_size=7)
    )
    assert pieces == ["FakeCli", "ent rep", "ly."]


def test_fake_client_chat_stream_requires_consumption_for_effect():
    # chat_stream is lazy like the real client: the queued response is
    # only consumed (and the call only recorded) once the iterator is
    # consumed.
    client = FakeClient(responses=["lazy reply"])
    gen = client.chat_stream([{"role": "user", "content": "q"}])
    assert client.calls == []
    assert next(gen) == "lazy reply"
    assert len(client.calls) == 1


# --- FakeClient: calls-log parity ----------------------------------------- #


def test_fake_client_chat_stream_records_one_call_identical_to_chat_with_meta():
    stream_client = FakeClient(responses=["same reply"])
    meta_client = FakeClient(responses=["same reply"])

    list(stream_client.chat_stream([{"role": "user", "content": "q"}]))
    meta_client.chat_with_meta([{"role": "user", "content": "q"}])

    assert len(stream_client.calls) == 1
    assert stream_client.calls[0] == meta_client.calls[0]


def test_fake_client_chat_stream_call_record_full_shape():
    client = FakeClient()
    tools = [{"type": "function", "function": {"name": "decide_event"}}]
    list(
        client.chat_stream(
            [{"role": "user", "content": "q"}],
            system="SYS", temperature=0.0, json_mode=True, max_tokens=5,
            tools=tools, tool_choice="auto", reasoning_effort="high",
        )
    )

    call = client.calls[0]
    assert set(call) == {
        "messages", "system", "temperature", "json_mode", "max_tokens",
        "tools", "tool_choice", "reasoning_effort",
    }
    assert call["messages"][0] == {"role": "user", "content": "q"}
    assert call["system"] == "SYS"
    assert call["temperature"] == 0.0
    assert call["json_mode"] is True
    assert call["max_tokens"] == 5
    assert call["tools"] == tools
    assert call["tool_choice"] == "auto"
    assert call["reasoning_effort"] == "high"


def test_fake_client_chat_stream_call_parity_with_defaults():
    client = FakeClient(responses=["r"])
    list(client.chat_stream([{"role": "user", "content": "q"}]))
    call = client.calls[0]
    assert call["tools"] is None
    assert call["tool_choice"] is None
    assert call["reasoning_effort"] is None
    assert call["temperature"] == 0.8
    assert call["json_mode"] is False
    assert call["max_tokens"] is None


def test_fake_client_chat_stream_system_only_payload_on_empty_transcript():
    client = FakeClient(responses=["r"])
    list(client.chat_stream([], system="SYS"))
    assert client.calls[0]["messages"] == [
        {"role": "system", "content": "SYS"}
    ]
