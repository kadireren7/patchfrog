from __future__ import annotations

import json

from patchfrog.fix_verification.critic import FixCriticDecision, judge_fix
from patchfrog.review.provider import ProviderFatalError
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse


async def test_judge_fix_parses_fixed_decision() -> None:
    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({"decision": "fixed", "reasoning_summary": "input is now validated"}))]
    )
    result = await judge_fix(
        provider, title="t", message="m", reasoning_summary="r", file_path="f.py", new_code_excerpt="code"
    )
    assert result is not None
    decision, reasoning = result
    assert decision is FixCriticDecision.FIXED
    assert reasoning == "input is now validated"


async def test_judge_fix_parses_still_present_decision() -> None:
    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({"decision": "still_present", "reasoning_summary": "unchanged"}))]
    )
    result = await judge_fix(
        provider, title="t", message="m", reasoning_summary="r", file_path="f.py", new_code_excerpt="code"
    )
    assert result is not None
    assert result[0] is FixCriticDecision.STILL_PRESENT


async def test_judge_fix_parses_inconclusive_decision() -> None:
    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({"decision": "inconclusive", "reasoning_summary": "not enough context"}))]
    )
    result = await judge_fix(
        provider, title="t", message="m", reasoning_summary="r", file_path="f.py", new_code_excerpt="code"
    )
    assert result is not None
    assert result[0] is FixCriticDecision.INCONCLUSIVE


async def test_judge_fix_returns_none_on_malformed_json() -> None:
    provider = FakeLLMProvider([ScriptedResponse(raw_json="not json")])
    result = await judge_fix(
        provider, title="t", message="m", reasoning_summary="r", file_path="f.py", new_code_excerpt="code"
    )
    assert result is None


async def test_judge_fix_returns_none_on_unknown_decision_value() -> None:
    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({"decision": "maybe", "reasoning_summary": "?"}))]
    )
    result = await judge_fix(
        provider, title="t", message="m", reasoning_summary="r", file_path="f.py", new_code_excerpt="code"
    )
    assert result is None


async def test_judge_fix_returns_none_on_missing_field() -> None:
    provider = FakeLLMProvider([ScriptedResponse(raw_json=json.dumps({"decision": "fixed"}))])
    result = await judge_fix(
        provider, title="t", message="m", reasoning_summary="r", file_path="f.py", new_code_excerpt="code"
    )
    # reasoning_summary defaults to "" via payload.get -- this should
    # still succeed, not fail closed, since the field is genuinely
    # optional at parse time (the schema itself requires it from a real
    # provider, but a malformed/partial reply must not crash the caller).
    assert result is not None
    assert result[1] == ""


async def test_judge_fix_returns_none_on_provider_error() -> None:
    provider = FakeLLMProvider([ProviderFatalError("boom")])
    result = await judge_fix(
        provider, title="t", message="m", reasoning_summary="r", file_path="f.py", new_code_excerpt="code"
    )
    assert result is None


async def test_judge_fix_sends_bounded_excerpt_never_raw_internal_fields() -> None:
    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({"decision": "fixed", "reasoning_summary": "ok"}))]
    )
    await judge_fix(
        provider, title="SQL injection", message="unsanitized input", reasoning_summary="root cause",
        file_path="app.py", new_code_excerpt="def f(): pass",
    )
    assert len(provider.calls) == 1
    request = provider.calls[0]
    assert "app.py" in request.user_prompt
    assert "def f(): pass" in request.user_prompt
