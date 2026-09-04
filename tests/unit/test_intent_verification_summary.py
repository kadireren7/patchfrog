"""Unit tests for :mod:`patchfrog.intent_verification.summary`/
:mod:`patchfrog.intent_verification.story` -- the conditional, bounded
user-facing Intent Coverage block (spec section 22) and the Change Story
prefix (spec section 21). Pure/synchronous, no database, no LLM."""

from __future__ import annotations

import uuid

from patchfrog.change_intelligence.domain import (
    AffectedRelation,
    AffectedSymbolRef,
    ChangeKind,
    ChangeUnit,
)
from patchfrog.intent_verification.domain import (
    IntentCoverage,
    IntentCoverageStatus,
    IntentVerificationReport,
)
from patchfrog.intent_verification.extraction import extract_claims_from_pr_metadata
from patchfrog.intent_verification.service import build_intent_verification_report
from patchfrog.intent_verification.story import build_intent_story_prefix
from patchfrog.intent_verification.summary import (
    render_intent_coverage_summary,
    should_render_intent_coverage_summary,
)
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason


def _candidate(*, file_path: str, qualified_name: str) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=uuid.uuid4(), symbol_name=qualified_name.rsplit(".", 1)[-1],
        qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _affected(*, file_path: str, qualified_name: str) -> AffectedSymbolRef:
    return AffectedSymbolRef(
        file_path=file_path, qualified_name=qualified_name, symbol_name=qualified_name.rsplit(".", 1)[-1],
        relation=AffectedRelation.DIRECTLY_DEPENDENT, distance=1, reason="directly calls the changed symbol",
    )


def test_no_claims_produces_no_story_prefix() -> None:
    assert build_intent_story_prefix(()) == ""


def test_story_prefix_uses_first_claim_verbatim() -> None:
    claims = extract_claims_from_pr_metadata(title="Prevent duplicate webhook processing", body=None)
    prefix = build_intent_story_prefix(claims)
    assert prefix == "Intent: Prevent duplicate webhook processing"


def test_empty_report_never_renders_summary() -> None:
    report = IntentVerificationReport(version=1, claims=(), coverage=(), gaps=())
    assert should_render_intent_coverage_summary(report) is False
    assert render_intent_coverage_summary(report) is None


def test_single_surface_claim_does_not_render_summary() -> None:
    """A single covered surface with no uncovered surface adds nothing
    beyond the Change Story sentence -- deliberately not shown."""

    coverage = IntentCoverage(
        intent_claim_id="c1", status=IntentCoverageStatus.SUPPORTED,
        mapped_change_unit_ids=("u1",), covered_surfaces=("process_payment",),
    )
    report = IntentVerificationReport(version=1, claims=(), coverage=(coverage,), gaps=())
    assert should_render_intent_coverage_summary(report) is False


def test_multi_surface_partial_evidence_renders_summary() -> None:
    unit = ChangeUnit(
        id="u1", title="retry payment idempotency", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
        affected_surface=(_affected(file_path="retry_worker.py", qualified_name="RetryWorker.run"),),
    )
    report = build_intent_verification_report(
        title="Prevent duplicate retry payment processing", body=None, change_units=(unit,)
    )
    assert should_render_intent_coverage_summary(report) is True
    text = render_intent_coverage_summary(report)
    assert text is not None
    assert "### Intent coverage" in text
    assert "process_payment" in text
    assert "RetryWorker.run" in text
    assert "changed" in text
    assert "unchanged" in text
    # Never a numeric score/percentage anywhere in the block.
    assert "%" not in text
