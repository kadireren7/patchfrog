"""Compact, persistence-ready summary of a
:class:`~patchfrog.contract_intelligence.domain.ContractIntelligenceReport`
-- counts only, mirroring :mod:`patchfrog.change_intelligence.telemetry`'s
own role exactly. The Contract Story addendum is folded into the
existing ``review_runs.change_story`` text at the review-service
integration point (never persisted as a second text column -- see
``docs/contract-intelligence.md``'s Persistence section) and the
Contract Map reuses the existing ``review_runs.change_map_*`` columns
(the *same* Change Map, not a second one) -- so neither needs a field
here."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from patchfrog.change_intelligence.domain import CompanionStatus
from patchfrog.contract_intelligence.domain import ContractIntelligenceReport


@dataclass(frozen=True, slots=True)
class ContractIntelligenceSummary:
    version: int
    contract_delta_count: int
    contract_kind_counts_json: str
    potentially_breaking_delta_count: int
    impacted_consumer_count: int
    stale_consumer_candidate_count: int


def summarize_for_persistence(report: ContractIntelligenceReport) -> ContractIntelligenceSummary:
    kind_counts = Counter(d.kind.value for d in report.descriptors)
    stale_count = sum(1 for c in report.stale_consumers if c.status is CompanionStatus.MISSING)

    return ContractIntelligenceSummary(
        version=report.version,
        contract_delta_count=len(report.deltas),
        contract_kind_counts_json=json.dumps(dict(sorted(kind_counts.items())), separators=(",", ":")),
        potentially_breaking_delta_count=len(report.potentially_breaking_deltas),
        impacted_consumer_count=report.impacted_consumer_count,
        stale_consumer_candidate_count=stale_count,
    )
