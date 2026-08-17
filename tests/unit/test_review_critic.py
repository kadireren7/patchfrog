from __future__ import annotations

import json

import pytest

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.review.critic import CriticService
from patchfrog.review.domain import (
    AIReviewFinding,
    CriticDecision,
    ReviewCandidate,
    ReviewCandidateReason,
    ValidatedFinding,
    ValidationOutcome,
)
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.validation import ResponseSchemaError

_CANDIDATE = ReviewCandidate(
    file_path="src/billing.py",
    symbol_id=None,
    symbol_name="can_withdraw",
    qualified_name="src.billing.can_withdraw",
    start_line=1,
    end_line=3,
    changed_lines=(2,),
    static_finding_ids=(),
    reason=ReviewCandidateReason.CHANGED_SYMBOL,
)

_FINDING = AIReviewFinding(
    title="bug",
    message="msg",
    category=FindingCategory.CORRECTNESS,
    severity=Severity.HIGH,
    confidence=Confidence.HIGH,
    file_path="src/billing.py",
    start_line=2,
    end_line=2,
    evidence=(),
    reasoning_summary="x",
)
_VALIDATED = ValidatedFinding(finding=_FINDING, outcome=ValidationOutcome.VALID, detail="")


async def test_critic_accepts() -> None:
    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({
            "decision": "accept", "reasoning_summary": "real bug",
            "downgraded_severity": None, "downgraded_confidence": None,
        }))]
    )
    service = CriticService(provider=provider)
    verdict = await service.critique(_VALIDATED, candidate=_CANDIDATE, context_text="x = 1")
    assert verdict.decision == CriticDecision.ACCEPT
    assert verdict.provider == "fake"


async def test_critic_rejects() -> None:
    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({
            "decision": "reject", "reasoning_summary": "hallucinated",
            "downgraded_severity": None, "downgraded_confidence": None,
        }))]
    )
    service = CriticService(provider=provider)
    verdict = await service.critique(_VALIDATED, candidate=_CANDIDATE, context_text="x = 1")
    assert verdict.decision == CriticDecision.REJECT


async def test_critic_downgrades() -> None:
    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({
            "decision": "downgrade", "reasoning_summary": "overstated",
            "downgraded_severity": "low", "downgraded_confidence": "medium",
        }))]
    )
    service = CriticService(provider=provider)
    verdict = await service.critique(_VALIDATED, candidate=_CANDIDATE, context_text="x = 1")
    assert verdict.decision == CriticDecision.DOWNGRADE
    assert verdict.downgraded_severity == Severity.LOW
    assert verdict.downgraded_confidence == Confidence.MEDIUM


async def test_critic_malformed_response_raises_schema_error() -> None:
    provider = FakeLLMProvider([ScriptedResponse(raw_json="not json")])
    service = CriticService(provider=provider)
    with pytest.raises(ResponseSchemaError):
        await service.critique(_VALIDATED, candidate=_CANDIDATE, context_text="x = 1")


async def test_critic_prompt_carries_the_evidence_and_finding() -> None:
    finding = AIReviewFinding(
        title="bug", message="msg", category=FindingCategory.CORRECTNESS, severity=Severity.HIGH,
        confidence=Confidence.HIGH, file_path="src/billing.py", start_line=2, end_line=2,
        evidence=(), reasoning_summary="x",
    )
    validated = ValidatedFinding(finding=finding, outcome=ValidationOutcome.VALID, detail="")
    provider = FakeLLMProvider(
        [ScriptedResponse(raw_json=json.dumps({
            "decision": "accept", "reasoning_summary": "ok",
            "downgraded_severity": None, "downgraded_confidence": None,
        }))]
    )
    service = CriticService(provider=provider)
    await service.critique(validated, candidate=_CANDIDATE, context_text="def can_withdraw(): ...")
    assert len(provider.calls) == 1
    assert "can_withdraw" in provider.calls[0].user_prompt
    assert "def can_withdraw" in provider.calls[0].user_prompt
