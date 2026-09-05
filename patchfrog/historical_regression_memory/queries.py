"""The one bounded, indexed SQL query this milestone needs -- fetches
trusted historical findings for a repository directly from Phase 9's
own already-persisted ``feedback_assessments`` table joined with the
existing ``ai_findings``/``review_candidates``/``review_runs`` chain.

**No new table, no new history database.** See
``validation/historical_regression_memory/latest-summary.md`` section 1
("How does this reuse Review Memory") for the full audit. One query per
review run -- never a per-current-surface query loop (spec section 13:
"Avoid per-event DB query loops", mirrored from Phase 9's own
:mod:`patchfrog.feedback.queries` discipline).
"""

from __future__ import annotations

import uuid

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.feedback.domain import FEEDBACK_ASSESSMENT_VERSION
from patchfrog.historical_regression_memory.domain import (
    MAX_EVIDENCE_FINGERPRINT_CHARS,
    MAX_HISTORICAL_LOOKBACK_ROWS,
    HistoricalEvidenceStrength,
    HistoricalRegressionRecord,
)
from patchfrog.persistence.models.feedback import FeedbackAssessmentModel
from patchfrog.persistence.models.review import AIFindingModel, ReviewCandidateModel, ReviewRunModel


async def fetch_trusted_historical_records(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    assessment_version: int = FEEDBACK_ASSESSMENT_VERSION,
    limit: int = MAX_HISTORICAL_LOOKBACK_ROWS,
) -> tuple[HistoricalRegressionRecord, ...]:
    """The mandatory eligibility predicate (spec sections 2/11, audited
    in section 2 of the validation doc): ``(explicit_fixed > 0 OR
    explicit_useful > 0) AND explicit_false_positive = 0 AND
    explicit_ignore = 0``, scoped by a mandatory ``repository_id``
    equality filter (never optional -- see the audit's "Repository
    isolation" section). Ordered strongest-trust-first (a confirmed fix
    outranks "useful" alone), then most-recent, then a stable id
    tie-break -- never returns more than ``limit`` rows."""

    trust_rank = case(
        (FeedbackAssessmentModel.explicit_fixed > 0, 0),
        else_=1,
    )

    stmt = (
        select(
            AIFindingModel.id,
            AIFindingModel.file_path,
            AIFindingModel.category,
            AIFindingModel.created_at,
            ReviewCandidateModel.qualified_name,
            ReviewRunModel.id.label("review_run_id"),
            ReviewRunModel.commit_sha,
            FeedbackAssessmentModel.explicit_fixed,
            AIFindingModel.title,
        )
        .select_from(FeedbackAssessmentModel)
        .join(AIFindingModel, AIFindingModel.id == FeedbackAssessmentModel.finding_id)
        .join(ReviewCandidateModel, ReviewCandidateModel.id == AIFindingModel.candidate_id)
        .join(ReviewRunModel, ReviewRunModel.id == AIFindingModel.review_run_id)
        .where(
            ReviewRunModel.repository_id == repository_id,
            FeedbackAssessmentModel.assessment_version == assessment_version,
            or_(
                FeedbackAssessmentModel.explicit_fixed > 0,
                FeedbackAssessmentModel.explicit_useful > 0,
            ),
            FeedbackAssessmentModel.explicit_false_positive == 0,
            FeedbackAssessmentModel.explicit_ignore == 0,
        )
        .order_by(trust_rank, AIFindingModel.created_at.desc(), AIFindingModel.id)
        .limit(limit)
    )

    rows = (await session.execute(stmt)).all()

    records: list[HistoricalRegressionRecord] = []
    for row in rows:
        strength = (
            HistoricalEvidenceStrength.CONFIRMED_FIXED
            if row.explicit_fixed > 0
            else HistoricalEvidenceStrength.CONFIRMED_USEFUL
        )
        title = (row.title or "").strip()
        fingerprint = title[:MAX_EVIDENCE_FINGERPRINT_CHARS]
        records.append(
            HistoricalRegressionRecord(
                historical_finding_id=row.id,
                repository_id=repository_id,
                historical_review_run_id=row.review_run_id,
                historical_commit_sha=row.commit_sha,
                source_file_path=row.file_path,
                source_qualified_name=row.qualified_name,
                finding_category=row.category,
                evidence_strength=strength,
                bounded_evidence_fingerprint=fingerprint,
                observed_at=row.created_at.isoformat(),
            )
        )
    return tuple(records)
