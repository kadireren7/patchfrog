"""Compact, persistence-ready summary of a
:class:`~patchfrog.historical_regression_memory.domain.HistoricalRegressionReport`
-- mirrors every other Intelligence package's own telemetry-split
exactly: this *persistence* summary carries the already-bounded,
already-rendered ``historical_summary_text`` (needed for cross-task
publication), while the separate telemetry-snapshot type
(:class:`patchfrog.telemetry.domain.HistoricalRegressionMemoryTelemetry`)
stays counts-only. The Historical Story prefix has no text field here
at all -- it is folded directly into the existing
``review_runs.change_story`` text at the review-service integration
point, never a second column."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from patchfrog.historical_regression_memory.domain import HistoricalRegressionReport
from patchfrog.historical_regression_memory.summary import render_historical_summary


@dataclass(frozen=True, slots=True)
class HistoricalRegressionMemorySummary:
    version: int
    historical_trusted_record_count: int
    historical_match_kind_counts_json: str
    historical_regression_candidate_count: int
    historical_summary_rendered: bool
    historical_summary_text: str | None


def summarize_for_persistence(report: HistoricalRegressionReport) -> HistoricalRegressionMemorySummary:
    match_kind_counts = Counter(c.match_kind.value for c in report.candidates)
    summary_text = render_historical_summary(report)

    return HistoricalRegressionMemorySummary(
        version=report.version,
        historical_trusted_record_count=len(report.trusted_records_considered),
        historical_match_kind_counts_json=json.dumps(dict(sorted(match_kind_counts.items())), separators=(",", ":")),
        historical_regression_candidate_count=len(report.candidates),
        historical_summary_rendered=summary_text is not None,
        historical_summary_text=summary_text,
    )
