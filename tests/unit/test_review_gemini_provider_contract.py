"""Provider contract tests: mock the Gemini API at the HTTP level (via
respx) and verify :class:`GeminiLLMProvider` classifies every response
shape correctly -- success, 401/403, 429, 5xx, timeout/connection error,
invalid request, and a safety refusal. No network access, no real
credentials. Mirrors tests/unit/test_review_anthropic_provider_contract.py
exactly, one respx-mocked endpoint per provider."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from patchfrog.review.provider import ProviderFatalError, ProviderRequest, ProviderTransientError
from patchfrog.review.providers.gemini_provider import GeminiLLMProvider

_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"

_REQUEST = ProviderRequest(
    system_prompt="system", user_prompt="user", json_schema={"type": "object"}, schema_name="s",
    max_output_tokens=100,
)


def _provider() -> GeminiLLMProvider:
    return GeminiLLMProvider(api_key="test-key-not-real", model="gemini-3.6-flash", timeout_seconds=2.0)


def _success_body(
    text: str = '{"findings": []}',
    *,
    finish_reason: str = "STOP",
    prompt_tokens: int = 123,
    candidates_tokens: int = 45,
    thoughts_tokens: int | None = None,
) -> dict[str, object]:
    usage: dict[str, object] = {
        "promptTokenCount": prompt_tokens,
        "candidatesTokenCount": candidates_tokens,
        "totalTokenCount": prompt_tokens + candidates_tokens + (thoughts_tokens or 0),
    }
    if thoughts_tokens is not None:
        usage["thoughtsTokenCount"] = thoughts_tokens
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}], "role": "model"},
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "usageMetadata": usage,
        "modelVersion": "gemini-3.6-flash",
    }


def _error_body(*, code: int, status: str, message: str = "error") -> dict[str, object]:
    return {"error": {"code": code, "message": message, "status": status}}


async def test_success_returns_text_and_usage() -> None:
    with respx.mock:
        respx.post(_GENERATE_URL).mock(return_value=httpx.Response(200, json=_success_body()))
        result = await _provider().generate_structured(_REQUEST)
    assert result.raw_json == '{"findings": []}'
    assert result.usage.input_tokens == 123
    assert result.usage.output_tokens == 45
    assert result.usage.thinking_tokens == 0
    assert result.stop_reason == "STOP"


async def test_thinking_tokens_are_captured_separately_when_present() -> None:
    with respx.mock:
        respx.post(_GENERATE_URL).mock(
            return_value=httpx.Response(200, json=_success_body(thoughts_tokens=77))
        )
        result = await _provider().generate_structured(_REQUEST)
    assert result.usage.thinking_tokens == 77
    assert result.usage.output_tokens == 45  # never folded into output_tokens


async def test_rate_limit_or_quota_is_transient() -> None:
    with respx.mock:
        respx.post(_GENERATE_URL).mock(
            return_value=httpx.Response(429, json=_error_body(code=429, status="RESOURCE_EXHAUSTED"))
        )
        with pytest.raises(ProviderTransientError):
            await _provider().generate_structured(_REQUEST)


async def test_server_error_is_transient() -> None:
    with respx.mock:
        respx.post(_GENERATE_URL).mock(
            return_value=httpx.Response(500, json=_error_body(code=500, status="INTERNAL"))
        )
        with pytest.raises(ProviderTransientError):
            await _provider().generate_structured(_REQUEST)


async def test_deadline_exceeded_5xx_is_transient() -> None:
    with respx.mock:
        respx.post(_GENERATE_URL).mock(
            return_value=httpx.Response(504, json=_error_body(code=504, status="DEADLINE_EXCEEDED"))
        )
        with pytest.raises(ProviderTransientError):
            await _provider().generate_structured(_REQUEST)


async def test_timeout_is_transient() -> None:
    with respx.mock:
        respx.post(_GENERATE_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        with pytest.raises(ProviderTransientError):
            await _provider().generate_structured(_REQUEST)


async def test_connection_error_is_transient() -> None:
    with respx.mock:
        respx.post(_GENERATE_URL).mock(side_effect=httpx.ConnectError("connection reset"))
        with pytest.raises(ProviderTransientError):
            await _provider().generate_structured(_REQUEST)


async def test_invalid_request_is_fatal_never_retried() -> None:
    with respx.mock:
        respx.post(_GENERATE_URL).mock(
            return_value=httpx.Response(400, json=_error_body(code=400, status="INVALID_ARGUMENT"))
        )
        with pytest.raises(ProviderFatalError):
            await _provider().generate_structured(_REQUEST)


async def test_unknown_model_404_is_fatal_never_retried() -> None:
    # Real shape observed live: gemini-2.5-flash returns exactly this once
    # retired -- a deprecated/misconfigured model name must never be
    # silently retried.
    with respx.mock:
        respx.post(_GENERATE_URL).mock(
            return_value=httpx.Response(404, json=_error_body(code=404, status="NOT_FOUND"))
        )
        with pytest.raises(ProviderFatalError):
            await _provider().generate_structured(_REQUEST)


async def test_auth_failure_401_is_fatal_never_retried() -> None:
    with respx.mock:
        respx.post(_GENERATE_URL).mock(
            return_value=httpx.Response(401, json=_error_body(code=401, status="UNAUTHENTICATED"))
        )
        with pytest.raises(ProviderFatalError):
            await _provider().generate_structured(_REQUEST)


async def test_auth_failure_403_is_fatal_never_retried() -> None:
    with respx.mock:
        respx.post(_GENERATE_URL).mock(
            return_value=httpx.Response(403, json=_error_body(code=403, status="PERMISSION_DENIED"))
        )
        with pytest.raises(ProviderFatalError):
            await _provider().generate_structured(_REQUEST)


@pytest.mark.parametrize("finish_reason", ["SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"])
async def test_safety_refusal_finish_reasons_are_fatal(finish_reason: str) -> None:
    with respx.mock:
        respx.post(_GENERATE_URL).mock(
            return_value=httpx.Response(200, json=_success_body(finish_reason=finish_reason))
        )
        with pytest.raises(ProviderFatalError, match="refused"):
            await _provider().generate_structured(_REQUEST)


async def test_max_tokens_finish_reason_is_not_treated_as_refusal() -> None:
    # A truncated response still gets normal text extraction; it fails
    # JSON/schema validation naturally downstream, exactly like a
    # truncated Anthropic response would -- never special-cased here.
    with respx.mock:
        respx.post(_GENERATE_URL).mock(
            return_value=httpx.Response(200, json=_success_body(text="{incomplete", finish_reason="MAX_TOKENS"))
        )
        result = await _provider().generate_structured(_REQUEST)
    assert result.raw_json == "{incomplete"
    assert result.stop_reason == "MAX_TOKENS"


async def test_no_candidates_is_fatal() -> None:
    body = _success_body()
    body["candidates"] = []
    with respx.mock:
        respx.post(_GENERATE_URL).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(ProviderFatalError):
            await _provider().generate_structured(_REQUEST)


async def test_request_uses_structured_output_config() -> None:
    with respx.mock:
        route = respx.post(_GENERATE_URL).mock(return_value=httpx.Response(200, json=_success_body()))
        await _provider().generate_structured(_REQUEST)
        sent = route.calls.last.request
    payload = json.loads(sent.content)
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["generationConfig"]["responseJsonSchema"] == _REQUEST.json_schema
    assert payload["systemInstruction"]["parts"][0]["text"] == "system"
    assert payload["contents"][0]["parts"][0]["text"] == "user"


async def test_thinking_budget_is_capped_leaving_room_for_the_answer() -> None:
    # Reproduced live: an unbounded ("AUTOMATIC") thinking budget can
    # consume nearly all of max_output_tokens, truncating the JSON
    # mid-object. thinking_budget must always leave at least
    # _MIN_RESERVED_OUTPUT_TOKENS of headroom for the visible answer.
    from patchfrog.review.providers.gemini_provider import _MIN_RESERVED_OUTPUT_TOKENS

    request = ProviderRequest(
        system_prompt="system", user_prompt="user", json_schema={"type": "object"}, schema_name="s",
        max_output_tokens=4096,
    )
    with respx.mock:
        route = respx.post(_GENERATE_URL).mock(return_value=httpx.Response(200, json=_success_body()))
        await _provider().generate_structured(request)
        sent = route.calls.last.request
    payload = json.loads(sent.content)
    assert payload["generationConfig"]["thinkingConfig"]["thinking_budget"] == 4096 - _MIN_RESERVED_OUTPUT_TOKENS


async def test_thinking_budget_never_goes_negative_for_a_small_output_budget() -> None:
    request = ProviderRequest(
        system_prompt="system", user_prompt="user", json_schema={"type": "object"}, schema_name="s",
        max_output_tokens=200,  # smaller than _MIN_RESERVED_OUTPUT_TOKENS
    )
    with respx.mock:
        route = respx.post(_GENERATE_URL).mock(return_value=httpx.Response(200, json=_success_body()))
        await _provider().generate_structured(request)
        sent = route.calls.last.request
    payload = json.loads(sent.content)
    assert payload["generationConfig"]["thinkingConfig"]["thinking_budget"] == 0


async def test_api_key_never_appears_in_request_body() -> None:
    with respx.mock:
        route = respx.post(_GENERATE_URL).mock(return_value=httpx.Response(200, json=_success_body()))
        await _provider().generate_structured(_REQUEST)
        sent = route.calls.last.request
    assert b"test-key-not-real" not in sent.content


def test_empty_api_key_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        GeminiLLMProvider(api_key="", model="gemini-3.6-flash")


def test_identity_reports_provider_and_model() -> None:
    provider = _provider()
    assert provider.identity.provider == "gemini"
    assert provider.identity.model == "gemini-3.6-flash"
