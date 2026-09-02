"""Compact, persistence-ready summary of a
:class:`~patchfrog.change_intelligence.domain.ChangeIntelligenceReport`.

The full report (every affected-surface entry, every companion
candidate) is never persisted -- only bounded counts plus the already-
bounded Change Story/Change Map text (see
:mod:`patchfrog.change_intelligence.change_map`'s own size bounds).
This is what :mod:`patchfrog.review.service` writes onto the owning
``review_runs`` row (new columns -- see
``patchfrog/persistence/models/review.py``), which is what makes the
counts recoverable later by
:func:`patchfrog.telemetry.collector.collect_review_telemetry` without
ever re-deriving the graph traversal.

Never persists: raw source bodies, raw context, per-node evidence
strings beyond what the bounded Change Map text already includes, or
any LLM reasoning (there is none -- this whole package is deterministic).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from patchfrog.change_intelligence.domain import ChangeIntelligenceReport, CompanionStatus


@dataclass(frozen=True, slots=True)
class ChangeIntelligenceSummary:
    version: int
    change_unit_count: int
    change_kind_counts_json: str
    affected_surface_count: int
    expected_companion_count: int
    missing_companion_candidate_count: int
    change_map_rendered: bool
    change_map_node_count: int
    change_story: str
    change_map_text: str | None


def summarize_for_persistence(report: ChangeIntelligenceReport) -> ChangeIntelligenceSummary:
    kind_counts = Counter(unit.change_kind.value for unit in report.change_units)
    missing_count = sum(1 for c in report.expected_companions if c.status is CompanionStatus.MISSING)

    return ChangeIntelligenceSummary(
        version=report.version,
        change_unit_count=len(report.change_units),
        change_kind_counts_json=json.dumps(dict(sorted(kind_counts.items())), separators=(",", ":")),
        affected_surface_count=report.affected_surface_count,
        expected_companion_count=len(report.expected_companions),
        missing_companion_candidate_count=missing_count,
        change_map_rendered=report.change_map is not None,
        change_map_node_count=report.change_map.node_count if report.change_map is not None else 0,
        change_story=report.change_story,
        change_map_text=report.change_map.text if report.change_map is not None else None,
    )
