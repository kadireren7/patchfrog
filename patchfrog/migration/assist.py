"""Optional model-assisted migration (M7.4) -- only when a deterministic
strategy could not safely generate the step.

Deliberately the narrowest possible use of a provider:

- called **only** for a ``HUMAN_REQUIRED`` step whose target is a single,
  already-located SDK call (``add_required_parameter`` /
  ``replace_enum_value`` with no deterministic value source) -- never for
  removed APIs, response shape, or auth, where no value proposal could
  ever be safe;
- the prompt gives the model exactly that one call's text and the plan
  step's own facts (current usage, required change) -- never another
  file, never the whole repository;
- the model's entire output is one structured field, the literal value
  to pass -- never source code, never a diff. The character-span edit is
  still produced by the same deterministic rewriter
  (:mod:`patchfrog.migration.python_rewrite` /
  :mod:`patchfrog.migration.js_rewrite`) that a hinted value would use, so
  a hallucinated field name still fails the same
  :class:`~patchfrog.migration.edits.RewriteError` path;
- the result then passes through the exact same
  :func:`~patchfrog.migration.safety.run_safety_gates` a deterministic
  patch does, and is returned as a *separate*
  :class:`~patchfrog.migration.domain.GeneratedPatch` with
  ``origin=PatchOrigin.MODEL_ASSISTED`` -- never merged into, or silently
  trusted alongside, the deterministic one.

Callers opt in explicitly per step; nothing in the deterministic planner
or generator ever calls this module, and no CLI command defaults to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from patchfrog.migration.domain import (
    AutoFixEligibility,
    EditOperation,
    GeneratedPatch,
    MigrationStep,
    MigrationStrategy,
    PatchOrigin,
    StepOutcome,
    StepResult,
)
from patchfrog.migration.edits import NotNeeded, RewriteError, TextEdit, apply_edits, unified_diff
from patchfrog.migration.js_rewrite import js_edits
from patchfrog.migration.python_rewrite import python_edits
from patchfrog.migration.safety import run_safety_gates
from patchfrog.review.provider import LLMProvider, ProviderRequest
from patchfrog.upstream.hints import HintError, check_literal

#: Steps this module ever assists -- an add-keyword or enum-mapping
#: value proposal, both already routed through a deterministic rewriter
#: once a value is known. Nothing else is offered to a provider.
_ASSISTABLE_STRATEGIES = frozenset(
    {MigrationStrategy.ADD_REQUIRED_PARAMETER, MigrationStrategy.REPLACE_ENUM_VALUE}
)

MAX_CONTEXT_CHARS = 400

VALUE_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "can_propose": {
            "type": "boolean",
            "description": "false when no safe literal value can be determined from the given facts alone.",
        },
        "value": {
            "type": ["string", "number", "boolean", "null"],
            "description": "The literal argument value, when can_propose is true. Never a credential, "
                           "never a made-up identifier not present in the given facts.",
        },
        "rationale": {"type": "string", "description": "One sentence, for a human reviewer -- never trusted as code."},
    },
    "required": ["can_propose", "value", "rationale"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You propose exactly one literal argument value for one specific line of already-located code, "
    "so a deterministic rewriter can apply it. You never write code, never see any file beyond the one "
    "line given to you, and never propose a value that looks like a credential, API key, token or secret. "
    "If the given facts do not determine a safe value, set can_propose to false."
)


class AssistError(ValueError):
    """The provider's response could not be trusted for this step."""


@dataclass(frozen=True, slots=True)
class ValueProposal:
    step_id: str
    value_json: str
    rationale: str


def build_request(step: MigrationStep, call_text: str, *, max_output_tokens: int = 512) -> ProviderRequest:
    if step.strategy not in _ASSISTABLE_STRATEGIES:
        raise AssistError(f"{step.strategy.value} is not eligible for model assistance")
    if step.eligibility is not AutoFixEligibility.HUMAN_REQUIRED or step.operation is not None:
        raise AssistError("only steps a deterministic strategy left HUMAN_REQUIRED are eligible")
    user_prompt = (
        f"Current usage:\n{step.current_usage}\n\n"
        f"Line of code:\n{call_text[:MAX_CONTEXT_CHARS]}\n\n"
        f"Required change:\n{step.required_change}\n\n"
        "Propose the literal value, if the facts above determine one safely."
    )
    return ProviderRequest(
        system_prompt=_SYSTEM_PROMPT, user_prompt=user_prompt, json_schema=VALUE_PROPOSAL_SCHEMA,
        schema_name="migration_value_proposal", max_output_tokens=max_output_tokens,
    )


def _parse_response(step_id: str, raw_json: str) -> ValueProposal | None:
    """Never trust the provider output directly: strict shape check, and
    a credential-shaped value is refused exactly like a hinted one."""

    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise AssistError(f"provider response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or "can_propose" not in payload or "value" not in payload:
        raise AssistError("provider response does not match the expected schema")
    if not payload.get("can_propose"):
        return None
    value = payload["value"]
    try:
        check_literal(value, "model-assisted value")
    except HintError as exc:
        raise AssistError(str(exc)) from exc
    rationale = str(payload.get("rationale", ""))[:300]
    return ValueProposal(step_id, json.dumps(value), rationale)


async def propose_value(step: MigrationStep, call_text: str, provider: LLMProvider) -> ValueProposal | None:
    """One provider call for one step. ``None`` means the provider found
    no safe value (an explicit ``can_propose: false``, never a guess)."""

    request = build_request(step, call_text)
    result = await provider.generate_structured(request)
    return _parse_response(step.step_id, result.raw_json)


def _rewrite(step_with_operation: MigrationStep, text: str) -> list[TextEdit]:
    language = step_with_operation.target.language
    if language == "python":
        return python_edits(step_with_operation, text)
    if language == "javascript":
        return js_edits(step_with_operation, text)
    raise RewriteError(f"model assistance is not supported for language {language!r}")


async def assist_step(
    step: MigrationStep,
    *,
    member: str,
    old_enum_value: str | None,
    text: str,
    provider: LLMProvider,
) -> tuple[GeneratedPatch | None, StepResult]:
    """Propose and apply exactly one value for one HUMAN_REQUIRED step.

    ``member``: the argument/parameter name (from ``step.diff_item_kinds``'
    originating diff item -- the caller already has it, since it built the
    plan). ``old_enum_value``: required only for ``REPLACE_ENUM_VALUE``
    (the value being replaced, from the diff item -- never guessed by the
    model).
    """

    lines = text.splitlines()
    if not step.target.line or not (0 < step.target.line <= len(lines)):
        return None, StepResult(step.step_id, StepOutcome.FAILED, "usage site line is out of range for this file")
    call_text = lines[step.target.line - 1]
    try:
        proposal = await propose_value(step, call_text, provider)
    except AssistError as exc:
        return None, StepResult(step.step_id, StepOutcome.FAILED, f"model assistance refused: {exc}")
    if proposal is None:
        return None, StepResult(step.step_id, StepOutcome.SKIPPED, "provider found no safe value")

    if step.strategy is MigrationStrategy.ADD_REQUIRED_PARAMETER:
        operation = EditOperation.of("add_keyword", name=member, value_json=proposal.value_json)
    elif step.strategy is MigrationStrategy.REPLACE_ENUM_VALUE:
        if old_enum_value is None:
            return None, StepResult(step.step_id, StepOutcome.FAILED, "old_enum_value is required")
        new_value = json.loads(proposal.value_json)
        if not isinstance(new_value, str):
            return None, StepResult(step.step_id, StepOutcome.FAILED, "proposed value must be a string for an enum")
        operation = EditOperation.of("replace_keyword_literal", name=member, old=old_enum_value, new=new_value)
    else:
        return None, StepResult(step.step_id, StepOutcome.FAILED, f"{step.strategy.value} is not assistable")

    assisted_step = replace(step, operation=operation)

    try:
        edits = _rewrite(assisted_step, text)
    except NotNeeded as exc:
        return None, StepResult(step.step_id, StepOutcome.NOT_NEEDED, str(exc))
    except RewriteError as exc:
        return None, StepResult(step.step_id, StepOutcome.FAILED, f"proposed value rejected: {exc}")

    after_text = apply_edits(text, edits)
    result = StepResult(step.step_id, StepOutcome.APPLIED, proposal.rationale or "model-assisted value applied",
                        edits=len(edits))
    before = {step.target.file_path: text}
    after = {step.target.file_path: after_text}
    edit_scopes = {step.target.file_path: [(step.step_id, e.scope) for e in edits]}
    safety = run_safety_gates(steps=(assisted_step,), results=(result,), before=before, after=after,
                              edit_scopes=edit_scopes)
    diff = unified_diff(step.target.file_path, text, after_text)
    patch = GeneratedPatch(
        origin=PatchOrigin.MODEL_ASSISTED, unified_diff=diff, modified_files=(step.target.file_path,),
        new_contents={step.target.file_path: after_text}, step_results=(result,), safety=safety,
    )
    return patch, result


__all__ = [
    "MAX_CONTEXT_CHARS",
    "VALUE_PROPOSAL_SCHEMA",
    "AssistError",
    "ValueProposal",
    "assist_step",
    "build_request",
    "propose_value",
]
