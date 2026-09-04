"""Compact, persistence-ready summary of an
:class:`~patchfrog.intent_verification.domain.IntentVerificationReport`
-- mirrors :mod:`patchfrog.change_intelligence.telemetry`'s own split
exactly: this *persistence* summary carries the one already-bounded,
already-rendered ``intent_coverage_summary_text`` (needed for cross-task
publication -- publication runs as a separate, independently-retriable
Celery task from review generation, so the text must be persisted
rather than recomputed), while the separate telemetry-snapshot type
(:class:`patchfrog.telemetry.domain.IntentVerificationTelemetry`) stays
counts-only, exactly like ``ChangeIntelligenceTelemetry`` never carries
Change Story/Change Map prose. The Intent Story prefix has no text field
here at all -- it is folded directly into the existing
``review_runs.change_story`` text at the review-service integration
point, never a second column."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from patchfrog.intent_verification.domain import IntentCoverageStatus, IntentVerificationReport
from patchfrog.intent_verification.summary import render_intent_coverage_summary


@dataclass(frozen=True, slots=True)
class IntentVerificationSummary:
    version: int
    intent_claim_count: int
    intent_source_kind_counts_json: str
    mapped_intent_claim_count: int
    intent_gap_candidate_count: int
    intent_coverage_summary_rendered: bool
    intent_coverage_summary_text: str | None


def summarize_for_persistence(report: IntentVerificationReport) -> IntentVerificationSummary:
    source_kind_counts = Counter(claim.source.source_kind.value for claim in report.claims)
    mapped_count = sum(1 for c in report.coverage if c.status is not IntentCoverageStatus.INSUFFICIENT_EVIDENCE)
    summary_text = render_intent_coverage_summary(report)

    return IntentVerificationSummary(
        version=report.version,
        intent_claim_count=len(report.claims),
        intent_source_kind_counts_json=json.dumps(dict(sorted(source_kind_counts.items())), separators=(",", ":")),
        mapped_intent_claim_count=mapped_count,
        intent_gap_candidate_count=len(report.gaps),
        intent_coverage_summary_rendered=summary_text is not None,
        intent_coverage_summary_text=summary_text,
    )
