"""``patchfrog telemetry beta-summary`` -- external beta readiness.

Answers, for one operator managing a handful of invited repositories,
the exact questions spec section 23 asks for ("how many reviews ran? how
many succeeded/failed? how many findings published? provider calls/
tokens? user feedback coverage? false-positive reports? latency?") as a
single read-only CLI summary, over an arbitrary time window and
optionally one repository.

Deliberately not a new analytics subsystem: every number here is either
a plain count over already-persisted ``review_runs``/``review_publications``
rows, or produced by re-using :mod:`patchfrog.telemetry.aggregation`'s
existing, already-tested pure functions
(:func:`~patchfrog.telemetry.aggregation.aggregate_snapshots`,
:func:`~patchfrog.telemetry.aggregation.compute_feedback_coverage`) over
:func:`patchfrog.telemetry.collector.collect_review_telemetry` snapshots
-- never a second, divergent SQL aggregation path, and never a composite
score (see that module's own docstring: "never a weighted rate that
mixes telemetry with feedback or benchmark ground truth").

**Known, accepted scale characteristic**: :func:`compute_beta_summary`
calls :func:`~patchfrog.telemetry.collector.collect_review_telemetry`
once per succeeded run in the window -- each of those calls is itself
already query-bound (a small, fixed number of queries regardless of how
many candidates/proposals/findings/feedback events that one run has --
see ``tests/integration/test_telemetry_collector.py``'s own
``test_collector_query_count_does_not_scale_linearly_with_proposal_count``),
but the *total* query count for this function still scales with the
*number of runs* in the window. Fine at beta scale (a handful of
repositories, tens of runs a week -- see
``tests/integration/test_telemetry_beta_summary.py``'s own query-bound
test, which asserts this explicitly rather than leaving it
unverified). Batching ``collect_review_telemetry`` itself across many
run ids in one query pass would be a real rewrite of an already-tested
core telemetry function, not a trivial change -- deliberately left as
documented, on-demand, beta-scale behavior rather than built here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.publishing import ReviewPublicationModel
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.publishing.domain import ReviewPublicationStatus
from patchfrog.review.domain import ReviewRunStatus
from patchfrog.telemetry.aggregation import (
    FeedbackCoverage,
    aggregate_snapshots,
    compute_feedback_coverage,
)
from patchfrog.telemetry.collector import collect_review_telemetry
from patchfrog.telemetry.domain import TelemetryAggregate


@dataclass(frozen=True, slots=True)
class BetaSummary:
    since: datetime
    repository_id: uuid.UUID | None
    runs_total: int
    runs_succeeded: int
    runs_partial: int
    runs_failed: int
    findings_published: int
    aggregate: TelemetryAggregate
    feedback_coverage: FeedbackCoverage


async def compute_beta_summary(
    session: AsyncSession, *, since: datetime, repository_id: uuid.UUID | None = None
) -> BetaSummary:
    run_stmt = select(ReviewRunModel).where(ReviewRunModel.created_at >= since)
    if repository_id is not None:
        run_stmt = run_stmt.where(ReviewRunModel.repository_id == repository_id)
    runs = (await session.execute(run_stmt)).scalars().all()

    status_counts = dict.fromkeys(ReviewRunStatus, 0)
    for run in runs:
        status_counts[run.status] = status_counts.get(run.status, 0) + 1

    succeeded_ids = [run.id for run in runs if run.status is ReviewRunStatus.SUCCEEDED]
    snapshots = []
    for run_id in succeeded_ids:
        snapshot = await collect_review_telemetry(session, review_run_id=run_id)
        if snapshot is not None:
            snapshots.append(snapshot)

    aggregate = aggregate_snapshots(snapshots)
    all_feedback = [f for snapshot in snapshots for f in snapshot.feedback]
    coverage = compute_feedback_coverage(all_feedback)

    publication_stmt = select(func.coalesce(func.sum(ReviewPublicationModel.inline_count + ReviewPublicationModel.summary_only_count), 0)).where(
        ReviewPublicationModel.status == ReviewPublicationStatus.PUBLISHED,
        ReviewPublicationModel.created_at >= since,
    )
    if repository_id is not None:
        publication_stmt = publication_stmt.where(ReviewPublicationModel.repository_id == repository_id)
    findings_published = (await session.execute(publication_stmt)).scalar_one()

    return BetaSummary(
        since=since,
        repository_id=repository_id,
        runs_total=len(runs),
        runs_succeeded=status_counts[ReviewRunStatus.SUCCEEDED],
        runs_partial=status_counts[ReviewRunStatus.PARTIAL],
        runs_failed=status_counts[ReviewRunStatus.FAILED],
        findings_published=int(findings_published),
        aggregate=aggregate,
        feedback_coverage=coverage,
    )


def parse_since(value: str) -> datetime:
    """Accepts either an ISO 8601 timestamp or a simple relative window
    (``7d``, ``24h``, ``30m``) -- the shape an operator actually types on
    a command line, not just what a machine would emit."""

    value = value.strip()
    if value and value[-1] in "dhm" and value[:-1].replace(".", "", 1).isdigit():
        amount = float(value[:-1])
        unit = value[-1]
        seconds = {"d": 86400.0, "h": 3600.0, "m": 60.0}[unit] * amount
        return datetime.now(UTC) - timedelta(seconds=seconds)
    return datetime.fromisoformat(value)
