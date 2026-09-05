"""Conditional, deterministic Historical Context summary -- a small,
bounded, evidence-only list, never a score/percentage/gamified badge,
never a count like "N past bugs touched this file" (spec section 20).
Mirrors :mod:`patchfrog.intent_verification.summary`'s own discipline:
shown only when there is real, strongly-matched historical evidence to
report."""

from __future__ import annotations

from patchfrog.historical_regression_memory.domain import (
    HistoricalMatchKind,
    HistoricalRegressionReport,
)
from patchfrog.publishing.marker import sanitize_untrusted_text

_MAX_LINES = 5

#: Same eligibility bar as the Story prefix -- see
#: :mod:`patchfrog.historical_regression_memory.story`.
_SUMMARY_ELIGIBLE_KINDS = frozenset(
    {HistoricalMatchKind.SAME_SYMBOL, HistoricalMatchKind.SAME_QUALIFIED_NAME_IN_SAME_FILE}
)


def should_render_historical_summary(report: HistoricalRegressionReport) -> bool:
    return any(c.match_kind in _SUMMARY_ELIGIBLE_KINDS for c in report.candidates)


def render_historical_summary(report: HistoricalRegressionReport) -> str | None:
    strong = [c for c in report.candidates if c.match_kind in _SUMMARY_ELIGIBLE_KINDS]
    if not strong:
        return None

    lines = ["### Historical context", ""]
    for candidate in strong[:_MAX_LINES]:
        label = sanitize_untrusted_text(candidate.current_qualified_name or candidate.current_file_path)
        fingerprint = sanitize_untrusted_text(candidate.historical_record.bounded_evidence_fingerprint)
        lines.append(f"- `{label}`: a previous trusted finding here was resolved ({fingerprint}).")

    return "\n".join(lines)
