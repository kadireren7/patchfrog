"""Provider contract tests: mock the Claude API at the HTTP level (via
respx) and verify :class:`AnthropicLLMProvider` classifies every response
shape correctly -- success, 429, 500, timeout, invalid JSON body, and a
policy refusal. No network access, no real credentials."""

from __future__ import annotations

import httpx
import pytest
import respx

from patchfrog.review.provider import ProviderFatalError, ProviderRequest, ProviderTransientError
from patchfrog.review.providers.anthropic_provider import AnthropicLLMProvider

_MESSAGES_URL = "https://api.anthropic.com/v1/messages"

_REQUEST = ProviderRequest(
    system_prompt="system", user_prompt="user", json_schema={"type": "object"}, schema_name="s",
    max_output_tokens=100,
)


def _provider() -> AnthropicLLMProvider:
    return AnthropicLLMProvider(api_key="test-key-not-real", model="claude-opus-5", timeout_seconds=2.0)


def _success_body(text: str = '{"findings": []}') -> dict[str, object]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 123, "output_tokens": 45},
    }


@respx.mock
async def test_success_returns_text_and_usage() -> None:
    respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(200, json=_success_body()))
    result = await _provider().generate_structured(_REQUEST)
    assert result.raw_json == '{"findings": []}'
    assert result.usage.input_tokens == 123
    assert result.usage.output_tokens == 45
    assert result.stop_reason == "end_turn"


@respx.mock
async def test_rate_limit_is_transient() -> None:
    respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            headers={"retry-after": "1"},
        )
    )
    with pytest.raises(ProviderTransientError):
        await _provider().generate_structured(_REQUEST)


@respx.mock
async def test_server_error_is_transient() -> None:
    respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(
            500, json={"type": "error", "error": {"type": "api_error", "message": "oops"}}
        )
    )
    with pytest.raises(ProviderTransientError):
        await _provider().generate_structured(_REQUEST)


@respx.mock
async def test_bad_gateway_is_transient() -> None:
    respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(
            502, json={"type": "error", "error": {"type": "api_error", "message": "bad gateway"}}
        )
    )
    with pytest.raises(ProviderTransientError):
        await _provider().generate_structured(_REQUEST)


@respx.mock
async def test_timeout_is_transient() -> None:
    respx.post(_MESSAGES_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    with pytest.raises(ProviderTransientError):
        await _provider().generate_structured(_REQUEST)


@respx.mock
async def test_connection_error_is_transient() -> None:
    respx.post(_MESSAGES_URL).mock(side_effect=httpx.ConnectError("connection reset"))
    with pytest.raises(ProviderTransientError):
        await _provider().generate_structured(_REQUEST)


@respx.mock
async def test_bad_request_is_fatal_never_retried() -> None:
    respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad schema"}}
        )
    )
    with pytest.raises(ProviderFatalError):
        await _provider().generate_structured(_REQUEST)


@respx.mock
async def test_auth_failure_is_fatal_never_retried() -> None:
    respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(
            401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
        )
    )
    with pytest.raises(ProviderFatalError):
        await _provider().generate_structured(_REQUEST)


@respx.mock
async def test_refusal_stop_reason_is_fatal() -> None:
    body = _success_body(text="")
    body["content"] = []
    body["stop_reason"] = "refusal"
    respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(ProviderFatalError, match="refusal"):
        await _provider().generate_structured(_REQUEST)


@respx.mock
async def test_no_text_content_block_is_fatal() -> None:
    body = _success_body()
    body["content"] = [{"type": "thinking", "thinking": ""}]
    respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(ProviderFatalError):
        await _provider().generate_structured(_REQUEST)


@respx.mock
async def test_request_uses_structured_output_config() -> None:
    route = respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(200, json=_success_body()))
    await _provider().generate_structured(_REQUEST)
    sent = route.calls.last.request
    import json as _json

    payload = _json.loads(sent.content)
    assert payload["output_config"]["format"]["type"] == "json_schema"
    assert payload["output_config"]["format"]["schema"] == _REQUEST.json_schema
    assert payload["model"] == "claude-opus-5"
    assert payload["system"] == "system"
