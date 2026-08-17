from __future__ import annotations

import pytest

from patchfrog.review.provider import ProviderError, ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse

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
