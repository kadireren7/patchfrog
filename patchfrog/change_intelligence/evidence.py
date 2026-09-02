"""Bounded, per-candidate evidence text for the existing reviewer's own
prompt (spec section 13: "narrowest integration that improves
correctness review... do not substantially inflate every prompt").

Deliberately produces an **empty string** (never included in the
prompt at all -- see :func:`patchfrog.review.prompt.build_agent_prompt`)
for the common case where this candidate's logical change has no
missing-companion evidence worth surfacing. Quality + Cost Guard
remains authoritative: this never adds tokens to a LIGHT-tier candidate
that doesn't already have something specific and actionable to show.
"""

from __future__ import annotations

from patchfrog.change_intelligence.domain import (
    ChangeIntelligenceReport,
    ChangeUnit,
    CompanionStatus,
)
from patchfrog.review.domain import ReviewCandidate

#: Never more than this many missing-companion lines in one candidate's
#: evidence text -- bounded, same discipline as everything else in this
#: package.
_MAX_MISSING_LINES = 3


def _unit_for_candidate(report: ChangeIntelligenceReport, candidate: ReviewCandidate) -> ChangeUnit | None:
    fingerprint = candidate.fingerprint()
    for unit in report.change_units:
        if any(c.fingerprint() == fingerprint for c in unit.changed_candidates):
            return unit
    return None


def evidence_text_for_candidate(report: ChangeIntelligenceReport, candidate: ReviewCandidate) -> str:
    unit = _unit_for_candidate(report, candidate)
    if unit is None:
        return ""

    missing = [
        c
        for c in report.expected_companions
        if c.change_unit_id == unit.id
        and c.status is CompanionStatus.MISSING
        and c.source_qualified_name == (candidate.qualified_name or candidate.file_path)
    ]
    if not missing:
        return ""

    lines = [f"This change belongs to a logical unit classified as {unit.change_kind.value!r}."]
    lines.append("The following related surface was not touched in this diff (verify independently, never assume):")
    for c in missing[:_MAX_MISSING_LINES]:
        lines.append(f"- {c.expected_qualified_name} ({c.expected_file_path}) -- {c.reason}")
    if len(missing) > _MAX_MISSING_LINES:
        lines.append(f"- (+{len(missing) - _MAX_MISSING_LINES} more)")
    return "\n".join(lines)
