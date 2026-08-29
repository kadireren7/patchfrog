"""Second-stage LLM critic.

Runs only on findings that already passed deterministic validation (see
:mod:`patchfrog.review.validation`) -- there is no point spending a critic
call on a finding that was already provably hallucinated, so the
deterministic gate always runs first and is strictly cheaper. When the
critic is disabled (:attr:`patchfrog.review.config.ReviewConfig.critic_enabled`
is ``False``), every validated finding is accepted with no critic verdict
attached -- cost-gated, not silently skipped.
"""

from __future__ import annotations

import json

from patchfrog.analysis.domain import Confidence, Severity
from patchfrog.review.domain import (
    AIReviewFinding,
    CriticDecision,
    CriticVerdict,
    ReviewCandidate,
    ValidatedFinding,
)
from patchfrog.review.prompt import build_critic_prompt
from patchfrog.review.provider import LLMProvider, ProviderRequest
from patchfrog.review.schemas import CRITIC_RESPONSE_SCHEMA
from patchfrog.review.validation import ResponseSchemaError


class CriticService:
    def __init__(self, *, provider: LLMProvider, max_output_tokens: int = 1024) -> None:
        self._provider = provider
        self._max_output_tokens = max_output_tokens

    async def critique(
        self,
        validated: ValidatedFinding,
        *,
        candidate: ReviewCandidate,
        context_text: str,
        conflicting_finding: AIReviewFinding | None = None,
    ) -> CriticVerdict:
        system_prompt, user_prompt = build_critic_prompt(
            candidate=candidate,
            context_text=context_text,
            finding=validated.finding,
            conflicting_finding=conflicting_finding,
        )
        request = ProviderRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_schema=CRITIC_RESPONSE_SCHEMA,
            schema_name="critic_verdict",
            max_output_tokens=self._max_output_tokens,
        )
        result = await self._provider.generate_structured(request)

        try:
            payload = json.loads(result.raw_json)
            decision = CriticDecision(payload["decision"])
            reasoning_summary = str(payload.get("reasoning_summary", ""))
            downgraded_severity = payload.get("downgraded_severity")
            downgraded_confidence = payload.get("downgraded_confidence")
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            raise ResponseSchemaError(f"critic response did not match schema: {exc}") from exc

        return CriticVerdict(
            decision=decision,
            reasoning_summary=reasoning_summary,
            downgraded_severity=Severity(downgraded_severity) if downgraded_severity else None,
            downgraded_confidence=Confidence(downgraded_confidence) if downgraded_confidence else None,
            provider=self._provider.identity.provider,
            model=self._provider.identity.model,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            latency_ms=result.latency_ms,
        )
