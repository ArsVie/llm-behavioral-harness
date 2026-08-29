"""Client tests (W-E1): FakeClient + OpenAICompatibleClient payload shape."""

import json

import httpx
import pytest

from harness.client import FakeClient, OpenAICompatibleClient


def test_fake_client_cycles_responses():
    client = FakeClient(responses=["a", "b"])
    assert client.chat([{"role": "user", "content": "x"}]) == "a"
    assert client.chat([{"role": "user", "content": "x"}]) == "b"
    assert client.chat([{"role": "user", "content": "x"}]) == "FakeClient reply."
    assert len(client.calls) == 3


def test_fake_client_echo():
    client = FakeClient(echo=True)
    out = client.chat([{"role": "user", "content": "hi there"}])
    assert out == "echo: hi there"


def test_fake_client_records_max_tokens_per_call():
    client = FakeClient()
    client.chat([{"role": "user", "content": "x"}], max_tokens=123)
    client.chat([{"role": "user", "content": "y"}])

    assert client.calls[0]["max_tokens"] == 123
    assert client.calls[1]["max_tokens"] is None
    # The call record carries the full request (WS3): tools/tool_choice/
    # reasoning_effort are recorded even when absent (None).
    assert set(client.calls[0]) == {
        "messages", "system", "temperature", "json_mode", "max_tokens",
        "tools", "tool_choice", "reasoning_effort",
    }


def test_openai_client_payload_and_system_prepend():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        payload = json.loads(body)
        assert "/chat/completions" in request.url.path
        assert "Bearer sekrit" in request.headers["authorization"]
        assert payload["model"] == "m1"
        assert payload["messages"][0]["role"] == "system"  # system prepended
        assert "response_format" not in payload  # json_mode off
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1",
        api_key="sekrit",
        model="m1",
    )
    client._client = httpx.Client(transport=transport)
    out = client.chat(
        [{"role": "user", "content": "q"}], system="SYS", temperature=0.0
    )
    assert out == "ok"


def test_openai_client_json_mode_flag():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        payload = json.loads(body)
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"s": 1}'}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m"
    )
    client._client = httpx.Client(transport=transport)
    assert client.chat([{"role": "user", "content": "q"}], json_mode=True) == '{"s": 1}'


def test_openai_client_max_tokens_payload():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        seen.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m"
    )
    client._client = httpx.Client(transport=transport)

    client.chat([{"role": "user", "content": "q"}], max_tokens=777)
    client.chat([{"role": "user", "content": "q"}])

    assert seen[0]["max_tokens"] == 777
    assert "max_tokens" not in seen[1]


def test_openai_client_requires_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = OpenAICompatibleClient(base_url="https://x.test", api_key=None, model="m")
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        client.chat([{"role": "user", "content": "q"}])


# -- iteration-3 B1: generation integrity ---------------------------------- #
# Empty/whitespace completions are retried with bounded backoff; a persistent
# empty raises; truncation (finish_reason=length) is recorded, not treated as
# an empty reply (it2 F1: 579/2090 blank assistant turns were persisted).


def _empty_then_real_client(empties: int = 2, max_retries: int = 2):
    """OpenAICompatibleClient whose transport returns empty content `empties`
    times before a real reply."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= empties:
            return httpx.Response(
                200, json={"choices": [{"message": {"content": ""}}]}
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "real content"}}]}
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m",
        max_retries=max_retries,
    )
    client._client = httpx.Client(transport=transport)
    return client, calls


def test_openai_client_retries_empty_content_then_succeeds(monkeypatch, caplog):
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)
    client, calls = _empty_then_real_client(empties=2, max_retries=2)

    out = client.chat([{"role": "user", "content": "q"}])

    assert out == "real content"
    assert calls["n"] == 3  # two empties, one real
    assert sleeps == [2.0, 4.0]  # bounded exponential backoff (2.0 * 2**i)
    assert "empty content" in caplog.text


def test_openai_client_retries_whitespace_only_content(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "   \n\t"}}]}
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m", max_retries=1
    )
    client._client = httpx.Client(transport=transport)

    assert client.chat([{"role": "user", "content": "q"}]) == "ok"
    assert calls["n"] == 2


def test_openai_client_persistent_empty_raises(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("harness.client.time.sleep", sleeps.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"choices": [{"message": {"content": ""}}]}
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m", max_retries=1
    )
    client._client = httpx.Client(transport=transport)

    with pytest.raises(RuntimeError, match="empty/whitespace-only content"):
        client.chat([{"role": "user", "content": "q"}])
    assert calls["n"] == 2  # capped at max_retries + 1 — never hangs


def test_openai_client_truncation_finish_reason_records_marker(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "partial reply"},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m", max_retries=1
    )
    client._client = httpx.Client(transport=transport)

    # Truncated-but-non-empty content is returned (not retried, not raised)
    # and the truncation is recorded as a marker log line.
    assert client.chat([{"role": "user", "content": "q"}]) == "partial reply"
    assert "truncated (finish_reason=length" in caplog.text


# -- WS3: tools, reasoning, ChatResult -------------------------------------- #


def _client_with(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1", api_key="k", model="m", **kwargs
    )
    client._client = httpx.Client(transport=transport)
    return client


def test_openai_client_tools_passthrough():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        seen.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _client_with(handler)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "decide_event",
                "description": "Decide whether to initiate the event.",
                "parameters": {"type": "object", "properties": {"initiate": {"type": "boolean"}}},
            },
        }
    ]
    client.chat_with_meta([{"role": "user", "content": "q"}], tools=tools, tool_choice="auto")
    assert seen[0]["tools"] == tools
    assert seen[0]["tool_choice"] == "auto"

    # Without tools: keys absent from the payload (never sent empty).
    client.chat_with_meta([{"role": "user", "content": "q"}])
    assert "tools" not in seen[1]
    assert "tool_choice" not in seen[1]


def test_openai_client_parses_tool_calls():
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
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "decide_event",
                                        "arguments": '{"event": "gym", "initiate": true}',
                                    },
                                },
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {"name": "decide_reply", "arguments": "{}"},
                                },
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    client = _client_with(handler, max_retries=2)
    result = client.chat_with_meta([{"role": "user", "content": "q"}])

    # Tool-call-only reply: no text, no raise, NO retry.
    assert result.content == ""
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [
        {"id": "call_1", "name": "decide_event", "arguments_json": '{"event": "gym", "initiate": true}'},
        {"id": "call_2", "name": "decide_reply", "arguments_json": "{}"},
    ]
    assert calls["n"] == 1
    # chat() stays a thin wrapper: plain content string even for tool calls.
    assert client.chat([{"role": "user", "content": "q"}]) == ""


def test_openai_client_tool_calls_stay_raw_json_strings():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "deciding",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "decide_event", "arguments": "not json!"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    client = _client_with(handler)
    result = client.chat_with_meta([{"role": "user", "content": "q"}])

    # arguments_json is passed through verbatim; semantic parsing (and the
    # loud failure on invalid JSON) belongs to WS2's runner.
    assert result.tool_calls[0]["arguments_json"] == "not json!"


def test_openai_client_reasoning_effort_and_extraction():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        seen.append(payload)
        reasoning = payload.get("reasoning_effort")
        message = {"content": "visible reply"}
        if reasoning == "high":
            message["reasoning_content"] = "hidden thinking"
        return httpx.Response(200, json={"choices": [{"message": message}]})

    client = _client_with(handler)

    result = client.chat_with_meta([{"role": "user", "content": "q"}], reasoning_effort="high")
    assert seen[0]["reasoning_effort"] == "high"
    assert result.reasoning == "hidden thinking"
    assert result.content == "visible reply"  # reasoning never contaminates content

    # Not provided → not emitted; no reasoning in response → None.
    client.chat_with_meta([{"role": "user", "content": "q"}])
    assert "reasoning_effort" not in seen[1]


def test_openai_client_reasoning_alternate_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "c", "reasoning": "alternate placement"}}
                ]
            },
        )

    client = _client_with(handler)
    result = client.chat_with_meta([{"role": "user", "content": "q"}])
    assert result.reasoning == "alternate placement"


def test_openai_client_supports_tools_capability_flag():
    # Endpoint capability, not model capability: the flag gates whether the
    # tools param is SENT; model-level failures are WS2's parse path.
    assert OpenAICompatibleClient.supports_tools is True
    assert FakeClient.supports_tools is True

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        seen.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _client_with(handler)
    client.supports_tools = False
    client.chat_with_meta(
        [{"role": "user", "content": "q"}],
        tools=[{"type": "function", "function": {"name": "f"}}],
        tool_choice="auto",
    )
    assert "tools" not in seen[0]
    assert "tool_choice" not in seen[0]


def test_fake_client_scripts_dict_responses():
    client = FakeClient(
        responses=[
            {
                "content": "text",
                "reasoning": "think",
                "tool_calls": [{"id": "c1", "name": "decide_event", "arguments_json": "{}"}],
                "finish_reason": "tool_calls",
            },
            "plain",
        ]
    )
    r1 = client.chat_with_meta(
        [{"role": "user", "content": "q"}], tools=[{"type": "function"}], reasoning_effort="low"
    )
    assert r1.content == "text"
    assert r1.reasoning == "think"
    assert r1.tool_calls == [{"id": "c1", "name": "decide_event", "arguments_json": "{}"}]
    assert r1.finish_reason == "tool_calls"
    # Plain-string entries keep working; chat() returns content only.
    assert client.chat([{"role": "user", "content": "q"}]) == "plain"


def test_fake_client_records_full_request():
    client = FakeClient()
    tools = [{"type": "function", "function": {"name": "decide_event"}}]
    client.chat_with_meta(
        [{"role": "user", "content": "q"}],
        tools=tools,
        tool_choice="auto",
        reasoning_effort="medium",
    )
    call = client.calls[0]
    assert call["tools"] == tools
    assert call["tool_choice"] == "auto"
    assert call["reasoning_effort"] == "medium"


def test_fake_client_chat_with_meta_records_defaults():
    client = FakeClient()
    client.chat_with_meta([{"role": "user", "content": "q"}])
    call = client.calls[0]
    assert call["tools"] is None
    assert call["tool_choice"] is None
    assert call["reasoning_effort"] is None
    assert call["temperature"] == 0.8
    assert call["json_mode"] is False


# -- WS-C: two-lane credential split ---------------------------------------- #
# The client resolves api_key/base_url through harness.credentials by lane:
# product -> LILY_TOKEN/LILY_BASE_URL, research -> JUDGE_GENERATOR_TOKEN/
# JUDGE_GENERATOR_BASE_URL. Explicit args always win; lane=None keeps the
# legacy LLM_* env behavior. Values are never logged — only the env var NAME.


def test_lane_product_resolves_token_and_stamp(monkeypatch):
    monkeypatch.setenv("LILY_TOKEN", "lily-key")
    client = OpenAICompatibleClient(lane="product", model="m")
    assert client.api_key == "lily-key"
    assert client.lane == "product"
    # Lane base URL: LILY_BASE_URL absent -> LLM_BASE_URL absent -> default.
    assert client.base_url == "https://api.commandcode.ai/provider/v1"


def test_lane_research_resolves_token_and_stamp(monkeypatch):
    monkeypatch.setenv("JUDGE_GENERATOR_TOKEN", "judge-key")
    client = OpenAICompatibleClient(lane="research", model="m")
    assert client.api_key == "judge-key"
    assert client.lane == "research"


def test_lane_explicit_api_key_wins_over_resolver(monkeypatch):
    monkeypatch.setenv("LILY_TOKEN", "lily-key")
    client = OpenAICompatibleClient(lane="product", api_key="explicit", model="m")
    assert client.api_key == "explicit"


def test_lane_missing_token_fails_loudly_at_construction(monkeypatch):
    monkeypatch.delenv("LILY_TOKEN", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="product lane credential missing"):
        OpenAICompatibleClient(lane="product", model="m")
    # Same for the research lane — and the message names the env var, not a
    # fallback: there is NO silent fallback to LLM_API_KEY/OPENCODE_GO_API_KEY.
    monkeypatch.delenv("JUDGE_GENERATOR_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="JUDGE_GENERATOR_TOKEN"):
        OpenAICompatibleClient(lane="research", model="m")


def test_lane_resolution_never_falls_back_to_opencode_key(monkeypatch):
    # A stray OPENCODE_GO_API_KEY in the environment must NOT satisfy a lane.
    monkeypatch.delenv("LILY_TOKEN", raising=False)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "opencode-key")
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    with pytest.raises(RuntimeError, match="LILY_TOKEN"):
        OpenAICompatibleClient(lane="product", model="m")


def test_lane_base_url_precedence(monkeypatch):
    monkeypatch.setenv("LILY_TOKEN", "k")
    monkeypatch.setenv("LILY_BASE_URL", "https://lily.example/v1")
    monkeypatch.setenv("LLM_BASE_URL", "https://generic.example/v1")
    client = OpenAICompatibleClient(lane="product", model="m")
    assert client.base_url == "https://lily.example/v1"

    # Lane var absent -> generic LLM_BASE_URL (documented fallback).
    monkeypatch.delenv("LILY_BASE_URL")
    client = OpenAICompatibleClient(lane="product", model="m")
    assert client.base_url == "https://generic.example/v1"

    # Both absent -> current gateway default; explicit arg wins over all.
    monkeypatch.delenv("LLM_BASE_URL")
    client = OpenAICompatibleClient(lane="product", model="m")
    assert client.base_url == "https://api.commandcode.ai/provider/v1"
    client = OpenAICompatibleClient(lane="product", base_url="https://x.test", model="m")
    assert client.base_url == "https://x.test"


def test_lane_redacted_label_never_renders_value(monkeypatch, caplog):
    """The resolver logs the LANE + env var NAME — never the token value."""
    import logging

    import harness.credentials as creds

    monkeypatch.setenv("LILY_TOKEN", "sekrit-lily-value")
    with caplog.at_level(logging.INFO, logger="harness.credentials"):
        creds.resolve_credentials("product")
    assert "product lane" in caplog.text
    assert "LILY_TOKEN" in caplog.text
    assert "sekrit-lily-value" not in caplog.text


def test_lane_stamp_default_none_for_unlaned_client(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "legacy-key")
    client = OpenAICompatibleClient(model="m")
    assert client.lane is None
    assert client.api_key == "legacy-key"
    assert FakeClient().lane is None
    assert FakeClient(lane="research").lane == "research"
