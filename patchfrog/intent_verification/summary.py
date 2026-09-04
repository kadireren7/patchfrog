"""Conditional, deterministic Intent Coverage summary (spec section 22)
-- a small, bounded, evidence-only list, never a score/percentage/
gamified badge. Eligibility is deterministic, never an LLM judgment
call, mirroring
:func:`patchfrog.change_intelligence.change_map.should_render_change_map`'s
own discipline: shown only when there's real, multi-surface evidence to
report, never for every PR with *any* intent text."""

from __future__ import annotations

from patchfrog.intent_verification.domain import (
    IntentCoverage,
    IntentCoverageStatus,
    IntentVerificationReport,
)
from patchfrog.publishing.marker import sanitize_untrusted_text

#: A claim needs at least this many total surfaces (covered + uncovered)
#: to be worth a summary block -- a single-surface claim adds nothing
#: beyond what the Change Story sentence already says.
_MIN_SURFACES_FOR_SUMMARY = 2
_MAX_LINES = 8


def should_render_intent_coverage_summary(report: IntentVerificationReport) -> bool:
    return _eligible_coverage(report) is not None


def render_intent_coverage_summary(report: IntentVerificationReport) -> str | None:
    coverage = _eligible_coverage(report)
    if coverage is None:
        return None

    lines = ["### Intent coverage", ""]
    shown = 0
    for surface in coverage.covered_surfaces:
        if shown >= _MAX_LINES:
            break
        lines.append(f"- `{sanitize_untrusted_text(surface)}`: changed")
        shown += 1
    for ref in coverage.potentially_uncovered_surfaces:
        if shown >= _MAX_LINES:
            break
        label = ref.qualified_name or ref.file_path
        lines.append(f"- `{sanitize_untrusted_text(label)}`: unchanged")
        shown += 1

    return "\n".join(lines)


def _eligible_coverage(report: IntentVerificationReport) -> IntentCoverage | None:
    for coverage in report.coverage:
        if coverage.status is IntentCoverageStatus.INSUFFICIENT_EVIDENCE:
            continue
        total_surfaces = len(coverage.covered_surfaces) + len(coverage.potentially_uncovered_surfaces)
        if total_surfaces >= _MIN_SURFACES_FOR_SUMMARY:
            return coverage
    return None
