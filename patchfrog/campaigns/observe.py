"""Metric recording for watchers, campaigns and migration PRs (M11.12).

Thin, label-validated wrappers over :mod:`patchfrog.ops.metrics`, so every
caller (the engine's ingest function and Cloud's tasks) increments the same
low-cardinality series. Labels come only from the closed vocabularies below;
an unknown value is folded into ``other`` rather than creating a new series.
Nothing here ever receives a token, URL, repository name or source text.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from patchfrog.campaigns.domain import CampaignState, CompatibilityCampaign, RepositoryRecord
from patchfrog.ops import metrics
from patchfrog.watchers.domain import WatcherSourceKind

FETCH_OUTCOMES = frozenset({"success", "not_modified", "rate_limited", "transient_error", "permanent_error", "unsafe"})
WATCH_RESULTS = frozenset({"baseline", "unchanged", "changed", "duplicate_suppressed", "skipped"})
BACKOFF_REASONS = frozenset({"rate_limited", "transient_error"})
CAMPAIGN_EVENTS = frozenset({"created", "reconciled", "unchanged", "not_launched", "resolved"})
PR_OUTCOMES = frozenset(
    {"eligible", "opened", "updated", "stale_rejected", "duplicate_avoided", "failed", "awaiting_approval", "skipped"}
)
_KINDS = frozenset(k.value for k in WatcherSourceKind)


def _closed(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else "other"


def record_fetch(source_kind: str, outcome: str, *, seconds: float | None = None) -> None:
    kind = _closed(source_kind, _KINDS)
    metrics.watcher_fetch_attempts_total.labels(kind).inc()
    metrics.watcher_fetches_total.labels(kind, _closed(outcome, FETCH_OUTCOMES)).inc()
    if seconds is not None:
        metrics.watcher_fetch_seconds.labels(kind).observe(max(0.0, seconds))


def record_watch_result(source_kind: str, result: str) -> None:
    metrics.watcher_changes_total.labels(_closed(source_kind, _KINDS), _closed(result, WATCH_RESULTS)).inc()


def record_backoff(source_kind: str, reason: str) -> None:
    metrics.watcher_backoffs_total.labels(_closed(source_kind, _KINDS), _closed(reason, BACKOFF_REASONS)).inc()


def record_campaign_event(event: str) -> None:
    metrics.campaigns_total.labels(_closed(event, CAMPAIGN_EVENTS)).inc()


def record_campaign_records(records: Iterable[RepositoryRecord]) -> None:
    for record in records:
        metrics.campaign_repositories_total.labels(record.state.value).inc()


def record_campaign_resolved(campaign: CompatibilityCampaign, *, now: datetime) -> None:
    if campaign.state is CampaignState.RESOLVED:
        metrics.campaign_resolution_seconds.observe(max(0.0, (now - campaign.discovered_at).total_seconds()))


def record_pr_outcome(outcome: str) -> None:
    metrics.migration_prs_total.labels(_closed(outcome, PR_OUTCOMES)).inc()


def pr_outcome_for_status(status: str) -> str:
    """Maps an M9 ``MigrationPRStatus`` value to a metric outcome."""

    return {
        "opened": "opened", "updated": "duplicate_avoided", "no_op_unchanged": "duplicate_avoided",
        "stale_requires_regeneration": "stale_rejected", "failed": "failed", "skipped_not_eligible": "skipped",
    }.get(status, "skipped")


__all__ = [
    "pr_outcome_for_status",
    "record_backoff",
    "record_campaign_event",
    "record_campaign_records",
    "record_campaign_resolved",
    "record_fetch",
    "record_pr_outcome",
    "record_watch_result",
]
