"""Pure derivation (no I/O) of durable :class:`RepositoryLearningRecord`
snapshots from already-computed evidence, plus the idempotent,
session-aware recomputation entry point that upserts them.

Two sources, deliberately never a third:

- ``USEFUL_FINDING_PATTERN`` reuses Milestone O's own
  :func:`patchfrog.repository_learnings.matching.derive_repository_learnings`
  output verbatim -- this package adds no new "is this useful" logic of
  its own.
- ``NOISE_SUPPRESSION`` groups :func:`patchfrog.learning_records.queries.fetch_repeated_noise_feedback`
  rows by exact surface, mirroring O's own grouping discipline (category
  included in identity, one representative per distinct review run,
  ``MIN_SUPPORTING_EVENTS`` floor) exactly.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.analysis.domain import FindingCategory
from patchfrog.historical_regression_memory.domain import HistoricalRegressionRecord
from patchfrog.historical_regression_memory.queries import fetch_trusted_historical_records
from patchfrog.learning_records.domain import (
    MAX_EVIDENCE_PER_RECORD,
    MIN_SUPPORTING_EVENTS,
    LearningEvidenceRef,
    LearningSurface,
    LearningType,
    RepositoryLearningRecord,
    classify_maturity,
)
from patchfrog.learning_records.queries import NoiseFeedbackRow, fetch_repeated_noise_feedback
from patchfrog.persistence.repositories.learning import RepositoryLearningRecordRepository
from patchfrog.repository_learnings.matching import derive_repository_learnings

#: Bounds how many records one recomputation run will ever produce per
#: learning type -- mirrors O's own MAX_LEARNINGS_PER_RUN discipline.
MAX_RECORDS_PER_RECOMPUTATION = 20


def derive_useful_pattern_records(
    *, repository_id: UUID, trusted_records: tuple[HistoricalRegressionRecord, ...]
) -> tuple[RepositoryLearningRecord, ...]:
    """Snapshot of Milestone O's own already-derived, already-independence-
    checked learnings -- never a second derivation of "is this useful"."""

    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    records: list[RepositoryLearningRecord] = []
    for learning in learnings:
        pattern = learning.pattern
        evidence = tuple(
            LearningEvidenceRef(
                finding_id=e.historical_record.historical_finding_id,
                review_run_id=e.historical_record.historical_review_run_id,
                observed_at=e.historical_record.observed_at,
            )
            for e in learning.supporting_evidence[:MAX_EVIDENCE_PER_RECORD]
        )
        records.append(
            RepositoryLearningRecord(
                id=None,
                repository_id=repository_id,
                learning_type=LearningType.USEFUL_FINDING_PATTERN,
                surface=LearningSurface(
                    file_path=pattern.anchor_file_path,
                    qualified_name=pattern.anchor_qualified_name,
                    category=pattern.finding_category,
                ),
                maturity=classify_maturity(learning.support_count),
                support_count=learning.support_count,
                evidence=evidence,
                first_observed_at=learning.first_observed_at,
                last_observed_at=learning.last_observed_at,
            )
        )
    records.sort(key=lambda r: (r.support_count, r.last_observed_at), reverse=True)
    return tuple(records[:MAX_RECORDS_PER_RECOMPUTATION])


def derive_noise_suppression_records(
    *, repository_id: UUID, rows: tuple[NoiseFeedbackRow, ...]
) -> tuple[RepositoryLearningRecord, ...]:
    """Groups repeated-false-positive rows by exact surface -- category
    included in identity, one representative per distinct review run,
    ``MIN_SUPPORTING_EVENTS`` floor -- the same three rules Milestone O
    applies to its own trusted-record grouping (see
    :func:`patchfrog.repository_learnings.matching.derive_repository_learnings`),
    deliberately mirrored rather than imported since the evidence shape
    differs (``NoiseFeedbackRow`` vs. ``HistoricalRegressionRecord``)."""

    by_surface: dict[tuple[str, str, FindingCategory], list[NoiseFeedbackRow]] = defaultdict(list)
    for row in rows:
        if row.qualified_name is None:
            # Mirrors O's own rule: a finding with no stable symbol
            # identity cannot participate -- file-only identity would
            # reintroduce the over-broad match O's own correction round
            # already ruled out.
            continue
        by_surface[(row.file_path, row.qualified_name, row.category)].append(row)

    records: list[RepositoryLearningRecord] = []
    for (file_path, qualified_name, category), surface_rows in by_surface.items():
        # Independence: one representative per distinct review run.
        by_run: dict[UUID, NoiseFeedbackRow] = {}
        for row in surface_rows:
            existing = by_run.get(row.review_run_id)
            if existing is None or row.observed_at > existing.observed_at:
                by_run[row.review_run_id] = row
        support_count = len(by_run)
        if support_count < MIN_SUPPORTING_EVENTS:
            continue

        independent_rows = sorted(by_run.values(), key=lambda r: r.observed_at, reverse=True)
        evidence = tuple(
            LearningEvidenceRef(
                finding_id=r.finding_id, review_run_id=r.review_run_id, observed_at=r.observed_at.isoformat()
            )
            for r in independent_rows[:MAX_EVIDENCE_PER_RECORD]
        )
        records.append(
            RepositoryLearningRecord(
                id=None,
                repository_id=repository_id,
                learning_type=LearningType.NOISE_SUPPRESSION,
                surface=LearningSurface(file_path=file_path, qualified_name=qualified_name, category=category),
                maturity=classify_maturity(support_count),
                support_count=support_count,
                evidence=evidence,
                first_observed_at=min(r.observed_at for r in independent_rows).isoformat(),
                last_observed_at=max(r.observed_at for r in independent_rows).isoformat(),
            )
        )

    records.sort(key=lambda r: (r.support_count, r.last_observed_at), reverse=True)
    return tuple(records[:MAX_RECORDS_PER_RECOMPUTATION])


async def recompute_repository_learnings(
    session: AsyncSession, *, repository_id: UUID, as_of: datetime | None = None
) -> tuple[RepositoryLearningRecord, ...]:
    """The one idempotent entry point: re-derives every learning type
    from current source evidence and upserts durable records --
    **recomputed fresh from evidence every call, never irreversible
    mutable state** (Y11). A surface that no longer meets its floor is
    not re-produced this call; the caller (see
    :meth:`patchfrog.persistence.repositories.learning.RepositoryLearningRecordRepository.retire_missing`)
    retires (never deletes) any existing record that recomputation did
    not reconfirm -- satisfying "contradictory evidence degrades/retires"
    (Y3) and "do not silently overwrite history" together: the retired
    row keeps its last-known support_count/evidence, only its maturity
    and a ``retired_reason`` change.

    ``as_of`` defaults to "now" -- background recomputation is not tied
    to any one review run's own deterministic evidence chain (unlike
    Milestone N/O, which always use the review run's own ``started_at``)."""

    effective_as_of = as_of or datetime.now(UTC)

    trusted_records = await fetch_trusted_historical_records(
        session, repository_id=repository_id, as_of=effective_as_of
    )
    useful_records = derive_useful_pattern_records(repository_id=repository_id, trusted_records=trusted_records)

    noise_rows = await fetch_repeated_noise_feedback(session, repository_id=repository_id, as_of=effective_as_of)
    noise_records = derive_noise_suppression_records(repository_id=repository_id, rows=noise_rows)

    fresh_records = useful_records + noise_records

    repo = RepositoryLearningRecordRepository()
    persisted = await repo.upsert_many(session, records=fresh_records)
    await repo.retire_missing(
        session,
        repository_id=repository_id,
        fresh_surface_keys={(r.learning_type, r.surface.file_path, r.surface.qualified_name, r.surface.category) for r in fresh_records},
        retired_reason="not reconfirmed by the most recent recomputation",
    )
    return persisted
