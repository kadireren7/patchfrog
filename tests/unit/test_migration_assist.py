"""M7.4: the optional model-assisted migration path -- fake provider
only, no live calls (see CLAUDE.md security rules)."""

from __future__ import annotations

import json

import pytest

from patchfrog.migration.assist import AssistError, assist_step, build_request
from patchfrog.migration.domain import (
    AutoFixEligibility,
    MigrationStep,
    MigrationStrategy,
    MigrationTarget,
    PatchOrigin,
    StepOutcome,
)
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse

SOURCE = "import kit\nc = kit.Client()\n\ndef send(x):\n    return c.jobs.run(task=x)\n"


def _step(strategy: MigrationStrategy = MigrationStrategy.ADD_REQUIRED_PARAMETER) -> MigrationStep:
    target = MigrationTarget("r", "svc.py", "send", 5, "jobs.run", "sdk_call", "python")
    return MigrationStep(
        step_id="s1", target=target, strategy=strategy, eligibility=AutoFixEligibility.HUMAN_REQUIRED,
        diff_item_keys=("d1",), diff_item_kinds=("sdk_parameter_added_required",),
        current_usage="sdk call `jobs.run` at svc.py:5", required_change="new required argument `queue`",
        proposed_change="", tests_to_update=(), residual_uncertainty="no deterministic source", operation=None,
    )


def test_build_request_refuses_non_assistable_steps() -> None:
    ineligible = _step(MigrationStrategy.REMOVE_ARGUMENT)
    with pytest.raises(AssistError):
        build_request(ineligible, "c.jobs.run(task=x)")

    from dataclasses import replace

    from patchfrog.migration.domain import EditOperation

    already_deterministic = replace(_step(), operation=EditOperation.of("add_keyword", name="queue", value="x"))
    with pytest.raises(AssistError):
        build_request(already_deterministic, "c.jobs.run(task=x)")


async def test_successful_proposal_is_applied_and_marked_model_assisted() -> None:
    provider = FakeLLMProvider([ScriptedResponse(
        raw_json=json.dumps({"can_propose": True, "value": "default", "rationale": "the API default queue"})
    )])
    patch, result = await assist_step(_step(), member="queue", old_enum_value=None, text=SOURCE, provider=provider)
    assert result.outcome is StepOutcome.APPLIED
    assert patch is not None and patch.origin is PatchOrigin.MODEL_ASSISTED
    assert patch.modified_files == ("svc.py",)
    assert "queue='default'" in patch.new_contents["svc.py"]
    assert patch.is_candidate
    assert len(provider.calls) == 1
    # The provider never receives any file content but the one target line.
    assert "def send" not in provider.calls[0].user_prompt
    assert "c.jobs.run(task=x)" in provider.calls[0].user_prompt


async def test_provider_declining_is_skipped_not_forced() -> None:
    provider = FakeLLMProvider([ScriptedResponse(
        raw_json=json.dumps({"can_propose": False, "value": None, "rationale": "no safe default"})
    )])
    patch, result = await assist_step(_step(), member="queue", old_enum_value=None, text=SOURCE, provider=provider)
    assert patch is None and result.outcome is StepOutcome.SKIPPED


@pytest.mark.parametrize("value", ["sk-live-0123456789abcdefghij", "ghp_0123456789abcdefghijklmnop"])
async def test_credential_shaped_proposal_is_refused(value: str) -> None:
    provider = FakeLLMProvider([ScriptedResponse(
        raw_json=json.dumps({"can_propose": True, "value": value, "rationale": "x"})
    )])
    patch, result = await assist_step(_step(), member="queue", old_enum_value=None, text=SOURCE, provider=provider)
    assert patch is None and result.outcome is StepOutcome.FAILED
    assert value not in result.detail


async def test_malformed_response_fails_the_step_without_a_crash() -> None:
    provider = FakeLLMProvider([ScriptedResponse(raw_json="not json")])
    patch, result = await assist_step(_step(), member="queue", old_enum_value=None, text=SOURCE, provider=provider)
    assert patch is None and result.outcome is StepOutcome.FAILED

    provider2 = FakeLLMProvider([ScriptedResponse(raw_json=json.dumps({"unexpected": "shape"}))])
    patch2, result2 = await assist_step(_step(), member="queue", old_enum_value=None, text=SOURCE, provider=provider2)
    assert patch2 is None and result2.outcome is StepOutcome.FAILED


async def test_proposed_value_is_rendered_as_a_literal_never_as_code() -> None:
    """A value containing quote/injection-shaped text is escaped by the
    deterministic rewriter's repr(), never spliced in as executable code."""

    hostile = "x'); import os; os.system('id"
    provider = FakeLLMProvider([ScriptedResponse(
        raw_json=json.dumps({"can_propose": True, "value": hostile, "rationale": "x"})
    )])
    patch, result = await assist_step(_step(), member="queue", old_enum_value=None, text=SOURCE, provider=provider)
    assert result.outcome is StepOutcome.APPLIED and patch is not None
    new_source = patch.new_contents["svc.py"]
    import ast

    tree = ast.parse(new_source)  # must still parse; the hostile text stays inside one string literal
    calls_with_queue = [
        n for n in ast.walk(tree) if isinstance(n, ast.Call) and any(k.arg == "queue" for k in n.keywords)
    ]
    (call,) = calls_with_queue
    (kw,) = [k for k in call.keywords if k.arg == "queue"]
    assert isinstance(kw.value, ast.Constant) and kw.value.value == hostile


async def test_enum_replacement_path_uses_the_diff_items_old_value_not_the_model() -> None:
    source = "import kit\nc = kit.Client()\n\ndef send(x):\n    return c.jobs.run(mode='fast')\n"
    step = _step(MigrationStrategy.REPLACE_ENUM_VALUE)
    provider = FakeLLMProvider([ScriptedResponse(
        raw_json=json.dumps({"can_propose": True, "value": "speed", "rationale": "renamed value"})
    )])
    patch, result = await assist_step(step, member="mode", old_enum_value="fast", text=source, provider=provider)
    assert result.outcome is StepOutcome.APPLIED and patch is not None
    assert "mode='speed'" in patch.new_contents["svc.py"]

    # A non-string proposed value for an enum is refused, not coerced.
    provider2 = FakeLLMProvider([ScriptedResponse(
        raw_json=json.dumps({"can_propose": True, "value": 42, "rationale": "x"})
    )])
    patch2, result2 = await assist_step(step, member="mode", old_enum_value="fast", text=source, provider=provider2)
    assert patch2 is None and result2.outcome is StepOutcome.FAILED


async def test_stale_line_number_fails_cleanly_without_calling_the_provider() -> None:
    """A step whose recorded usage-site line no longer exists in the
    given text (e.g. the file changed since the plan was built) must fail
    the step, never crash and never still spend a provider call."""

    provider = FakeLLMProvider([ScriptedResponse(
        raw_json=json.dumps({"can_propose": True, "value": "x", "rationale": "r"})
    )])
    target = MigrationTarget("r", "svc.py", "send", 99, "jobs.run", "sdk_call", "python")  # beyond the file's lines
    step = MigrationStep("s1", target, MigrationStrategy.ADD_REQUIRED_PARAMETER, AutoFixEligibility.HUMAN_REQUIRED,
                         ("d1",), ("sdk_parameter_added_required",), "", "", "", (), "", operation=None)
    patch, result = await assist_step(step, member="queue", old_enum_value=None, text=SOURCE, provider=provider)
    assert patch is None and result.outcome is StepOutcome.FAILED
    assert provider.calls == []
