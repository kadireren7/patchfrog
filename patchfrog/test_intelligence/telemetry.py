"""Compact, persistence-ready summary of a
:class:`~patchfrog.test_intelligence.domain.TestIntelligenceReport` --
mirrors :mod:`patchfrog.intent_verification.telemetry`'s own split
exactly: this *persistence* summary carries the already-bounded,
already-rendered ``test_coverage_summary_text`` (needed for cross-task
publication), while the separate telemetry-snapshot type
(:class:`patchfrog.telemetry.domain.TestIntelligenceTelemetry`) stays
counts-only. The Test Story prefix has no text field here at all -- it
is folded directly into the existing ``review_runs.change_story`` text
at the review-service integration point, never a second column."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from patchfrog.test_intelligence.domain import TestIntelligenceReport
from patchfrog.test_intelligence.summary import render_test_gap_summary


@dataclass(frozen=True, slots=True)
class TestIntelligenceSummary:
    version: int
    test_expectation_count: int
    test_reason_code_counts_json: str
    test_gap_candidate_count: int
    test_coverage_summary_rendered: bool
    test_coverage_summary_text: str | None


def summarize_for_persistence(report: TestIntelligenceReport) -> TestIntelligenceSummary:
    reason_code_counts = Counter(expectation.reason_code.value for expectation in report.expectations)
    summary_text = render_test_gap_summary(report)

    return TestIntelligenceSummary(
        version=report.version,
        test_expectation_count=len(report.expectations),
        test_reason_code_counts_json=json.dumps(dict(sorted(reason_code_counts.items())), separators=(",", ":")),
        test_gap_candidate_count=len(report.gaps),
        test_coverage_summary_rendered=summary_text is not None,
        test_coverage_summary_text=summary_text,
    )
