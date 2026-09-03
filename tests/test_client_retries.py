"""Transport and response retries in the real HTTP client.

The retry budget is the same for three different failures — a retryable
status, a transport error, and a 200 whose body cannot be parsed — and in
every case the LAST attempt raises rather than returning something the
session would persist. Backoff is stubbed so the tests do not sleep.
"""

from __future__ import annotations

import httpx
import pytest

from harness.client import OpenAICompatibleClient


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """The retry backoff is real time.sleep; the schedule is tested
    elsewhere, so here it only has to not happen."""
    monkeypatch.setattr("harness.client.time.sleep", lambda _s: None)


def _client(monkeypatch, responses):
    """A client whose underlying httpx.Client replays `responses` in order.

    Each entry is either an httpx.Response or an exception to raise.
    """
    monkeypatch.setenv("LILY_TOKEN", "tok")
    client = OpenAICompatibleClient(lane="product", base_url="https://x/v1")
    calls = {"n": 0}

    def post(url, **kwargs):
        item = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(client._client, "post", post)
    client.calls = calls
    return client


def _ok(content="hello"):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content},
                           "finish_reason": "stop"}]},
        request=httpx.Request("POST", "https://x/v1/chat/completions"),
    )


def _status(code):
    return httpx.Response(
        code, json={"error": "nope"},
        request=httpx.Request("POST", "https://x/v1/chat/completions"),
    )


# --- transport-level retries --------------------------------------------


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_retryable_status_is_retried_then_succeeds(monkeypatch, code):
    client = _client(monkeypatch, [_status(code), _ok()])
    assert client.chat([{"role": "user", "content": "hi"}]) == "hello"
    assert client.calls["n"] == 2


def test_retryable_status_eventually_raises(monkeypatch):
    client = _client(monkeypatch, [_status(503)])
    with pytest.raises(httpx.HTTPStatusError):
        client.chat([{"role": "user", "content": "hi"}])
    assert client.calls["n"] == client.max_retries + 1


def test_a_client_error_is_also_retried_the_full_budget(monkeypatch):
    """Pins CURRENT behaviour, which is arguably wrong and worth revisiting.

    ``_post`` names 429/500/502/503/504 as the retryable statuses, but the
    ``raise_for_status()`` below raises ``httpx.HTTPStatusError`` — a
    subclass of ``httpx.HTTPError`` — straight into the generic
    ``except httpx.HTTPError`` handler, which retries anything. So a 400
    (bad payload, unsupported_model: deterministic, our bug, never going to
    succeed) is re-sent the whole budget before surfacing, delaying the real
    error and spending against a paid gateway to do it.

    Pinned rather than changed: narrowing the retry set is a behaviour
    change to spend and latency, not a test fix.
    """
    client = _client(monkeypatch, [_status(400)])
    with pytest.raises(httpx.HTTPStatusError):
        client.chat([{"role": "user", "content": "hi"}])
    assert client.calls["n"] == client.max_retries + 1


def test_transport_error_is_retried_then_succeeds(monkeypatch):
    client = _client(monkeypatch, [httpx.ConnectError("boom"), _ok()])
    assert client.chat([{"role": "user", "content": "hi"}]) == "hello"


def test_transport_error_eventually_raises(monkeypatch):
    client = _client(monkeypatch, [httpx.ConnectError("boom")])
    with pytest.raises(httpx.HTTPError):
        client.chat([{"role": "user", "content": "hi"}])
    assert client.calls["n"] == client.max_retries + 1


# --- response-level retries ---------------------------------------------


def _malformed():
    return httpx.Response(
        200, json={"not": "a completion"},
        request=httpx.Request("POST", "https://x/v1/chat/completions"),
    )


def test_malformed_200_is_retried_then_succeeds(monkeypatch):
    """A 200 without `choices` is a real gateway behaviour, not a crash."""
    client = _client(monkeypatch, [_malformed(), _ok()])
    assert client.chat([{"role": "user", "content": "hi"}]) == "hello"


def test_malformed_200_eventually_raises_a_named_error(monkeypatch):
    client = _client(monkeypatch, [_malformed()])
    with pytest.raises(RuntimeError, match="malformed LLM response"):
        client.chat([{"role": "user", "content": "hi"}])


def test_empty_content_is_retried_then_succeeds(monkeypatch):
    client = _client(monkeypatch, [_ok("   "), _ok("hello")])
    assert client.chat([{"role": "user", "content": "hi"}]) == "hello"


def test_empty_content_eventually_raises(monkeypatch):
    client = _client(monkeypatch, [_ok("")])
    with pytest.raises(RuntimeError, match="empty/whitespace-only"):
        client.chat([{"role": "user", "content": "hi"}])


def test_null_content_eventually_raises(monkeypatch):
    client = _client(monkeypatch, [_ok(None)])
    with pytest.raises(RuntimeError, match="null content"):
        client.chat([{"role": "user", "content": "hi"}])


def test_reasoning_only_reply_is_not_retried(monkeypatch):
    """A reasoning model may answer entirely in the reasoning channel.

    That is a legitimate turn with empty content and must round-trip as ""
    — retrying it would burn the budget and then raise on a good response.
    """
    resp = httpx.Response(
        200,
        json={"choices": [{"message": {"content": None,
                                       "reasoning_content": "thinking"},
                           "finish_reason": "stop"}]},
        request=httpx.Request("POST", "https://x/v1/chat/completions"),
    )
    client = _client(monkeypatch, [resp])
    result = client.chat_with_meta([{"role": "user", "content": "hi"}])
    assert result.content == ""
    assert result.reasoning == "thinking"
    assert client.calls["n"] == 1


def test_truncation_is_warned_but_persisted(monkeypatch, caplog):
    """finish_reason=length keeps the content: a truncated reply is still
    the model's turn, and dropping it would lose the transcript."""
    import logging

    resp = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "half a sen"},
                           "finish_reason": "length"}]},
        request=httpx.Request("POST", "https://x/v1/chat/completions"),
    )
    client = _client(monkeypatch, [resp])
    with caplog.at_level(logging.WARNING, logger="harness.client"):
        result = client.chat_with_meta([{"role": "user", "content": "hi"}])
    assert result.content == "half a sen"
    assert result.finish_reason == "length"
    assert "truncated" in caplog.text


def test_close_is_safe(monkeypatch):
    client = _client(monkeypatch, [_ok()])
    client.close()
