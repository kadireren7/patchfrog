"""Conditional, deterministic Test Impact summary -- a small, bounded,
evidence-only list, never a score/percentage/gamified badge. Named
"Test impact", not "Test coverage" -- PatchFrog does not measure
line/branch coverage, and the latter heading would misleadingly imply
it does. Mirrors :mod:`patchfrog.intent_verification.summary`'s own
discipline: shown only when there is real gap evidence to report."""

from __future__ import annotations

from patchfrog.publishing.marker import sanitize_untrusted_text
from patchfrog.test_intelligence.domain import TestIntelligenceReport

_MAX_LINES = 8


def should_render_test_gap_summary(report: TestIntelligenceReport) -> bool:
    return bool(report.gaps)


def render_test_gap_summary(report: TestIntelligenceReport) -> str | None:
    if not report.gaps:
        return None

    lines = ["### Test impact", ""]
    for gap in report.gaps[:_MAX_LINES]:
        label = sanitize_untrusted_text(gap.expectation.source_qualified_name or gap.expectation.source_file_path)
        lines.append(f"- `{label}`: {sanitize_untrusted_text(gap.expectation.reason)}")

    return "\n".join(lines)
