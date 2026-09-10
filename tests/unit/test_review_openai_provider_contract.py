"""Provider contract tests: mock the OpenAI Responses API at the
transport level and verify :class:`OpenAILLMProvider` classifies every
response shape correctly -- success, refusal, incomplete/content-filter,
401/403/400, 429, 5xx, timeout/connection error. No network access, no
real credentials.

The installed ``openai`` SDK (3.x) is built on ``httpx2``, a distinct
package from the classic ``httpx`` that ``respx`` (used for the
Anthropic/Gemini contract tests) mocks -- so this file uses
``httpx2.MockTransport`` directly, injected via
:class:`~patchfrog.review.providers.openai_provider.OpenAILLMProvider`'s
test-only ``http_client`` constructor parameter, instead. Otherwise
mirrors ``tests/unit/test_review_anthropic_provider_contract.py`` and
``tests/unit/test_review_gemini_provider_contract.py`` exactly.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx2
import openai
import pytest

from patchfrog.review.provider import ProviderFatalError, ProviderRequest, ProviderTransientError
from patchfrog.review.providers.openai_provider import OpenAILLMProvider, _sanitize_schema_name

_MODEL = "gpt-6-astra"

_REQUEST = ProviderRequest(
    system_prompt="system", user_prompt="user", json_schema={"type": "object"},
    schema_name="review_response:correctness", max_output_tokens=100,
)

_Handler = Callable[[httpx2.Request], httpx2.Response]


def _provider(*, handler: _Handler, model: str = _MODEL) -> OpenAILLMProvider:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAILLMProvider(api_key="test-key-not-real", model=model, timeout_seconds=2.0, http_client=client)


def _success_body(
    text: str = '{"findings": []}',
    *,
    status: str = "completed",
    input_tokens: int = 123,
    output_tokens: int = 45,
    reasoning_tokens: int | None = None,
    content: list[dict[str, object]] | None = None,
    incomplete_reason: str | None = None,
) -> dict[str, object]:
    usage: dict[str, object] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if reasoning_tokens is not None:
        usage["output_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    body: dict[str, object] = {
        "id": "resp_abc123",
        "model": _MODEL,
        "status": status,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": content if content is not None else [{"type": "output_text", "text": text}],
            }
        ],
        "usage": usage,
    }
    if incomplete_reason is not None:
        body["incomplete_details"] = {"reason": incomplete_reason}
    return body


def _multi_message_body(
    *, messages: list[list[dict[str, object]]], status: str = "completed",
    input_tokens: int = 123, output_tokens: int = 45,
) -> dict[str, object]:
    """Builds a response body with one output ``message`` item per
    element of ``messages`` -- each element is that message's own
    content-block list, so ordering/placement of a refusal relative to
    ``output_text`` across separate messages (not just within one) can
    be exercised explicitly."""

    return {
        "id": "resp_abc123", "model": _MODEL, "status": status,
        "output": [
            {"type": "message", "role": "assistant", "status": "completed", "content": content}
            for content in messages
        ],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _handler_returning(status_code: int, json_body: dict[str, object]) -> _Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status_code, json=json_body)

    return handler


def _handler_raising(exc: Exception) -> _Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc

    return handler


async def test_success_returns_text_and_usage() -> None:
    provider = _provider(handler=_handler_returning(200, _success_body()))
    result = await provider.generate_structured(_REQUEST)
    assert result.raw_json == '{"findings": []}'
    assert result.usage.input_tokens == 123
    assert result.usage.output_tokens == 45
    assert result.usage.thinking_tokens == 0
    assert result.stop_reason == "completed"


async def test_reasoning_tokens_are_captured_separately_when_present() -> None:
    provider = _provider(handler=_handler_returning(200, _success_body(reasoning_tokens=77)))
    result = await provider.generate_structured(_REQUEST)
    assert result.usage.thinking_tokens == 77
    assert result.usage.output_tokens == 45  # never folded into output_tokens


async def test_rate_limit_is_transient() -> None:
    provider = _provider(
        handler=_handler_returning(429, {"error": {"message": "rate limited", "type": "rate_limit_error"}})
    )
    with pytest.raises(ProviderTransientError):
        await provider.generate_structured(_REQUEST)


async def test_server_error_is_transient() -> None:
    provider = _provider(
        handler=_handler_returning(500, {"error": {"message": "server error", "type": "server_error"}})
    )
    with pytest.raises(ProviderTransientError):
        await provider.generate_structured(_REQUEST)


async def test_timeout_is_transient() -> None:
    provider = _provider(handler=_handler_raising(httpx2.TimeoutException("timed out")))
    with pytest.raises(ProviderTransientError):
        await provider.generate_structured(_REQUEST)


async def test_connection_error_is_transient() -> None:
    provider = _provider(handler=_handler_raising(httpx2.ConnectError("connection reset")))
    with pytest.raises(ProviderTransientError):
        await provider.generate_structured(_REQUEST)


async def test_invalid_request_400_is_fatal_never_retried() -> None:
    provider = _provider(
        handler=_handler_returning(400, {"error": {"message": "bad request", "type": "invalid_request_error"}})
    )
    with pytest.raises(ProviderFatalError):
        await provider.generate_structured(_REQUEST)


async def test_auth_failure_401_is_fatal_never_retried() -> None:
    provider = _provider(
        handler=_handler_returning(401, {"error": {"message": "unauthorized", "type": "auth_error"}})
    )
    with pytest.raises(ProviderFatalError):
        await provider.generate_structured(_REQUEST)


async def test_permission_denied_403_is_fatal_never_retried() -> None:
    provider = _provider(
        handler=_handler_returning(403, {"error": {"message": "forbidden", "type": "permission_error"}})
    )
    with pytest.raises(ProviderFatalError):
        await provider.generate_structured(_REQUEST)


async def test_unknown_model_404_is_fatal_never_retried() -> None:
    provider = _provider(
        handler=_handler_returning(404, {"error": {"message": "model not found", "type": "invalid_request_error"}})
    )
    with pytest.raises(ProviderFatalError):
        await provider.generate_structured(_REQUEST)


async def test_refusal_content_item_is_fatal() -> None:
    provider = _provider(
        handler=_handler_returning(200, _success_body(content=[{"type": "refusal", "refusal": "I can't help with that"}]))
    )
    with pytest.raises(ProviderFatalError, match="refused"):
        await provider.generate_structured(_REQUEST)


async def test_output_text_then_refusal_in_same_message_is_still_refused() -> None:
    # Security correction: refusal must win regardless of block ordering
    # within one message -- previously the first output_text block was
    # returned immediately, never reaching the refusal block after it.
    provider = _provider(
        handler=_handler_returning(
            200,
            _success_body(
                content=[
                    {"type": "output_text", "text": '{"looks": "valid"}'},
                    {"type": "refusal", "refusal": "actually refusing"},
                ]
            ),
        )
    )
    with pytest.raises(ProviderFatalError, match="refused"):
        await provider.generate_structured(_REQUEST)


async def test_refusal_then_output_text_in_same_message_is_refused() -> None:
    provider = _provider(
        handler=_handler_returning(
            200,
            _success_body(
                content=[
                    {"type": "refusal", "refusal": "refusing"},
                    {"type": "output_text", "text": '{"looks": "valid"}'},
                ]
            ),
        )
    )
    with pytest.raises(ProviderFatalError, match="refused"):
        await provider.generate_structured(_REQUEST)


async def test_output_text_in_earlier_message_and_refusal_in_later_message_is_refused() -> None:
    # The exact bug scenario: text in message 1, refusal only in message
    # 2 -- a per-message-first-match walk would have returned message
    # 1's text without ever inspecting message 2.
    provider = _provider(
        handler=_handler_returning(
            200,
            _multi_message_body(
                messages=[
                    [{"type": "output_text", "text": '{"looks": "valid"}'}],
                    [{"type": "refusal", "refusal": "refusing after all"}],
                ]
            ),
        )
    )
    with pytest.raises(ProviderFatalError, match="refused"):
        await provider.generate_structured(_REQUEST)


async def test_multiple_output_text_blocks_are_concatenated_via_official_helper() -> None:
    # response.output_text (the official SDK helper) aggregates every
    # output_text block into one string -- never silently returning only
    # the first one found, since the API does not guarantee a single
    # block.
    provider = _provider(
        handler=_handler_returning(
            200,
            _success_body(
                content=[
                    {"type": "output_text", "text": '{"a": 1, '},
                    {"type": "output_text", "text": '"b": 2}'},
                ]
            ),
        )
    )
    result = await provider.generate_structured(_REQUEST)
    assert result.raw_json == '{"a": 1, "b": 2}'


async def test_malformed_json_text_is_passed_through_unmodified() -> None:
    # The adapter never validates/parses JSON itself -- exactly like the
    # Anthropic/Gemini adapters, malformed text is returned as-is and
    # fails downstream in patchfrog.review's own schema validation.
    provider = _provider(handler=_handler_returning(200, _success_body(text="{not valid json")))
    result = await provider.generate_structured(_REQUEST)
    assert result.raw_json == "{not valid json"


async def test_incomplete_content_filter_is_fatal() -> None:
    provider = _provider(
        handler=_handler_returning(
            200, _success_body(status="incomplete", incomplete_reason="content_filter", text="")
        )
    )
    with pytest.raises(ProviderFatalError, match="refused"):
        await provider.generate_structured(_REQUEST)


async def test_incomplete_max_output_tokens_is_not_treated_as_refusal() -> None:
    # A truncated response still gets normal text extraction; it fails
    # JSON/schema validation naturally downstream, exactly like a
    # truncated Anthropic/Gemini response would -- never special-cased.
    provider = _provider(
        handler=_handler_returning(
            200,
            _success_body(status="incomplete", incomplete_reason="max_output_tokens", text="{incomplete"),
        )
    )
    result = await provider.generate_structured(_REQUEST)
    assert result.raw_json == "{incomplete"
    assert result.stop_reason == "incomplete"


async def test_no_message_output_is_fatal() -> None:
    provider = _provider(handler=_handler_returning(200, {**_success_body(), "output": []}))
    with pytest.raises(ProviderFatalError):
        await provider.generate_structured(_REQUEST)


async def test_request_uses_structured_output_config() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["payload"] = json.loads(request.content)
        return httpx2.Response(200, json=_success_body())

    provider = _provider(handler=handler)
    await provider.generate_structured(_REQUEST)
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["schema"] == _REQUEST.json_schema
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["name"] == "review_response_correctness"
    assert payload["input"][0] == {"role": "system", "content": "system"}
    assert payload["input"][1] == {"role": "user", "content": "user"}


async def test_api_key_never_appears_in_request_body() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["body"] = request.content
        return httpx2.Response(200, json=_success_body())

    provider = _provider(handler=handler)
    await provider.generate_structured(_REQUEST)
    assert b"test-key-not-real" not in captured["body"]  # type: ignore[operator]


def test_empty_api_key_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        OpenAILLMProvider(api_key="", model=_MODEL)


def test_identity_reports_provider_and_model() -> None:
    provider = _provider(handler=_handler_returning(200, _success_body()))
    assert provider.identity.provider == "openai"
    assert provider.identity.model == _MODEL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("review_response:correctness", "review_response_correctness"),
        ("review_response:security", "review_response_security"),
        ("critic_verdict", "critic_verdict"),
        ("fix-verification:v1", "fix-verification_v1"),
    ],
)
def test_sanitize_schema_name_strips_unsafe_characters(raw: str, expected: str) -> None:
    assert _sanitize_schema_name(raw) == expected


async def test_sdk_retries_disabled_so_transient_failure_surfaces_once() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(429, json={"error": {"message": "rate limited", "type": "rate_limit_error"}})

    provider = _provider(handler=handler)
    with pytest.raises(ProviderTransientError):
        await provider.generate_structured(_REQUEST)
    assert calls == 1


async def test_api_timeout_error_is_transient_via_connection_error_branch() -> None:
    # openai.APITimeoutError subclasses openai.APIConnectionError -- this
    # asserts the adapter's single except clause actually catches it
    # (not just the more generic httpx2.TimeoutException at transport
    # level, exercised separately above).
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise openai.APITimeoutError(request=httpx2.Request("POST", "https://api.openai.com/v1/responses"))

    provider = _provider(handler=handler)
    with pytest.raises(ProviderTransientError):
        await provider.generate_structured(_REQUEST)
