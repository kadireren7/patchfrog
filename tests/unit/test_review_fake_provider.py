from __future__ import annotations

from dataclasses import replace

import pytest

from patchfrog.review.provider import ProviderError, ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse, route_by_schema_name

_REQUEST = ProviderRequest(
    system_prompt="system", user_prompt="user", json_schema={}, schema_name="s", max_output_tokens=100
)


async def test_fake_provider_returns_scripted_response() -> None:
    provider = FakeLLMProvider([ScriptedResponse(raw_json='{"findings": []}', input_tokens=42, output_tokens=7)])
    result = await provider.generate_structured(_REQUEST)
    assert result.raw_json == '{"findings": []}'
    assert result.usage.input_tokens == 42
    assert result.usage.output_tokens == 7


async def test_fake_provider_records_calls() -> None:
    provider = FakeLLMProvider([ScriptedResponse(raw_json="{}"), ScriptedResponse(raw_json="{}")])
    await provider.generate_structured(_REQUEST)
    await provider.generate_structured(_REQUEST)
    assert len(provider.calls) == 2
    assert provider.calls[0] is _REQUEST


async def test_fake_provider_raises_scripted_exception() -> None:
    boom = RuntimeError("boom")
    provider = FakeLLMProvider([boom])
    with pytest.raises(RuntimeError, match="boom"):
        await provider.generate_structured(_REQUEST)


async def test_fake_provider_raises_when_exhausted() -> None:
    provider = FakeLLMProvider([])
    with pytest.raises(ProviderError):
        await provider.generate_structured(_REQUEST)


async def test_fake_provider_response_factory() -> None:
    def factory(request: ProviderRequest) -> ScriptedResponse:
        return ScriptedResponse(raw_json=f'{{"seen": "{request.schema_name}"}}')

    provider = FakeLLMProvider(response_factory=factory)
    result = await provider.generate_structured(_REQUEST)
    assert result.raw_json == '{"seen": "s"}'


def test_fake_provider_identity() -> None:
    provider = FakeLLMProvider([], provider_name="fake", model_id="fake-model-2")
    assert provider.identity.provider == "fake"
    assert provider.identity.model == "fake-model-2"


async def test_route_by_schema_name_dispatches_deterministically() -> None:
    """Required scenario 26: FakeLLMProvider must allow deterministic
    role-specific scripting keyed on schema_name, not call order."""

    routes = {
        "review_response:correctness": ScriptedResponse(raw_json='{"role": "correctness"}'),
        "review_response:security": ScriptedResponse(raw_json='{"role": "security"}'),
        "critic_verdict": ScriptedResponse(raw_json='{"role": "critic"}'),
    }
    provider = FakeLLMProvider(response_factory=route_by_schema_name(routes))

    correctness_result = await provider.generate_structured(replace(_REQUEST, schema_name="review_response:correctness"))
    security_result = await provider.generate_structured(replace(_REQUEST, schema_name="review_response:security"))
    critic_result = await provider.generate_structured(replace(_REQUEST, schema_name="critic_verdict"))

    assert correctness_result.raw_json == '{"role": "correctness"}'
    assert security_result.raw_json == '{"role": "security"}'
    assert critic_result.raw_json == '{"role": "critic"}'


async def test_route_by_schema_name_falls_back_to_default() -> None:
    provider = FakeLLMProvider(
        response_factory=route_by_schema_name(
            {"review_response:correctness": ScriptedResponse(raw_json='{"role": "correctness"}')},
            default=ScriptedResponse(raw_json='{"role": "default"}'),
        )
    )
    result = await provider.generate_structured(replace(_REQUEST, schema_name="review_response:security"))
    assert result.raw_json == '{"role": "default"}'


async def test_route_by_schema_name_raises_clearly_with_no_route_and_no_default() -> None:
    provider = FakeLLMProvider(response_factory=route_by_schema_name({}))
    with pytest.raises(ProviderError, match="no route"):
        await provider.generate_structured(_REQUEST)


async def test_route_by_schema_name_supports_callable_routes() -> None:
    def callable_route(request: ProviderRequest) -> ScriptedResponse:
        return ScriptedResponse(raw_json=f'{{"user_prompt": "{request.user_prompt}"}}')

    provider = FakeLLMProvider(response_factory=route_by_schema_name({"s": callable_route}))
    result = await provider.generate_structured(replace(_REQUEST, user_prompt="hello"))
    assert result.raw_json == '{"user_prompt": "hello"}'
