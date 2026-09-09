"""One bounded LLM fallback call for Fix Verification -- Milestone T (T3),
Part AA/X.

Invoked *only* when every deterministic signal in
:mod:`patchfrog.fix_verification.service` was unavailable or inconclusive
(the finding is not statically corroborated, has no Executable
Verification target, and its flagged file region did change between the
two commits -- so "unchanged code" can't answer it either). Reuses the
exact same provider-neutral :class:`~patchfrog.review.provider.LLMProvider`/
:class:`~patchfrog.review.provider.ProviderRequest` typed-proposal-flow
primitives the reviewer/critic already use -- never a new provider
abstraction.

**Not** :class:`patchfrog.review.critic.CriticService` -- that service
asks "is this newly-proposed finding real," a materially different
question from "does this specific, already-confirmed historical finding's
condition still hold in this new code." Reusing it directly would send a
semantically wrong prompt. This is also **not** a new specialist agent --
no new :class:`~patchfrog.review.agents.roles.AgentRole` is added; it is a
narrow, single-purpose judgment call, structurally analogous to how the
critic itself is documented as "a distinct second-stage check... not a
peer specialist producing its own proposals."
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from patchfrog.review.provider import LLMProvider, ProviderError, ProviderRequest

_FIX_VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["fixed", "still_present", "inconclusive"]},
        "reasoning_summary": {
            "type": "string",
            "description": "1-3 sentences, grounded only in the code shown, explaining the decision.",
        },
    },
    "required": ["decision", "reasoning_summary"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You are checking whether a specific, already-confirmed code issue has "
    "been resolved in a new version of the code. You are not proposing new "
    "findings and not evaluating overall code quality -- answer only "
    "whether the exact described condition still holds. If the new code "
    "no longer contains the described condition, decide fixed. If it "
    "still does, decide still_present. If you cannot tell from the given "
    "code alone, decide inconclusive -- never guess."
)


class FixCriticDecision(StrEnum):
    FIXED = "fixed"
    STILL_PRESENT = "still_present"
    INCONCLUSIVE = "inconclusive"


def build_fix_verification_prompt(
    *, title: str, message: str, reasoning_summary: str, file_path: str, new_code_excerpt: str
) -> str:
    return (
        f"Original finding: {title}\n"
        f"Condition: {message}\n"
        f"Mechanism: {reasoning_summary}\n\n"
        f"Current content of {file_path} at the location under review:\n"
        f"```\n{new_code_excerpt}\n```\n\n"
        "Does the described condition still hold in this current code?"
    )


async def judge_fix(
    provider: LLMProvider,
    *,
    title: str,
    message: str,
    reasoning_summary: str,
    file_path: str,
    new_code_excerpt: str,
) -> tuple[FixCriticDecision, str] | None:
    """Returns ``(decision, reasoning_summary)``, or ``None`` on any
    provider failure -- the caller always treats ``None`` as
    ``INCONCLUSIVE``, never a crash and never a guess."""

    request = ProviderRequest(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=build_fix_verification_prompt(
            title=title,
            message=message,
            reasoning_summary=reasoning_summary,
            file_path=file_path,
            new_code_excerpt=new_code_excerpt,
        ),
        json_schema=_FIX_VERIFICATION_SCHEMA,
        schema_name="fix_verification_verdict",
        max_output_tokens=512,
    )
    try:
        result = await provider.generate_structured(request)
        payload = json.loads(result.raw_json)
        decision = FixCriticDecision(payload["decision"])
        reasoning = str(payload.get("reasoning_summary", ""))
    except (ProviderError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None
    return decision, reasoning
