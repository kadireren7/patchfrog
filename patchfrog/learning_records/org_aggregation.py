"""Y7/Y8: organization-level learning aggregation.

**The public engine has no concept of "organization" or "workspace" at
all** -- that concept exists only in the private Cloud repository (see
``validation/org_learning_governance/latest-summary.md`` section 1). This
module is therefore a *generic primitive*: every function takes an
explicit ``repository_ids`` tuple, supplied by the caller (Cloud), and
never infers tenant scope itself (spec's own "TENANT ISOLATION" section:
"Public engine generic org-learning primitives must always be scoped by
explicit organization/workspace identity provided by the operator.
Never infer tenant globally.").

Requires **stronger evidence than repository-level learning** (Y7): the
same ``(learning_type, surface_category)`` pattern must be independently
``ESTABLISHED`` in at least :data:`~patchfrog.learning_records.domain.ORG_MIN_ESTABLISHED_REPOSITORIES`
distinct repositories among the given set before an
:class:`~patchfrog.learning_records.domain.OrganizationLearning` is
produced -- a single repository's established learning, however strong,
never becomes an "organization" learning on its own.
"""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.analysis.domain import FindingCategory
from patchfrog.learning_records.domain import (
    ORG_MIN_ESTABLISHED_REPOSITORIES,
    LearningType,
    OrganizationLearning,
    RepositoryLearningRecord,
)
from patchfrog.persistence.repositories.cross_repo import RepositoryRelationRepository
from patchfrog.persistence.repositories.learning import RepositoryLearningRecordRepository


async def aggregate_repository_learnings_for_organization(
    session: AsyncSession, *, repository_ids: tuple[UUID, ...]
) -> tuple[OrganizationLearning, ...]:
    """``repository_ids`` is the caller's own, already-authorized tenant
    scope (e.g. Cloud's ``RepositoryEnrollmentRepository.list_for_workspace``
    resolved to engine repository ids) -- this function performs no
    authorization of its own and trusts the scope it is given exactly
    once, never re-derives or widens it."""

    if len(repository_ids) < ORG_MIN_ESTABLISHED_REPOSITORIES:
        return ()

    established = await RepositoryLearningRecordRepository().list_established_for_repositories(
        session, repository_ids=repository_ids
    )

    by_pattern: dict[tuple[LearningType, FindingCategory], list[RepositoryLearningRecord]] = defaultdict(list)
    for record in established:
        by_pattern[(record.learning_type, record.surface.category)].append(record)

    relation_repo = RepositoryRelationRepository()
    results: list[OrganizationLearning] = []
    for (learning_type, category), records in by_pattern.items():
        contributing_repo_ids = tuple(sorted({r.repository_id for r in records}, key=str))
        if len(contributing_repo_ids) < ORG_MIN_ESTABLISHED_REPOSITORIES:
            continue

        cross_repo_related = False
        contributing_set = set(contributing_repo_ids)
        for repo_id in contributing_repo_ids:
            relations = await relation_repo.list_for_source(session, source_repository_id=repo_id)
            if any(r.active and r.target_repository_id in contributing_set for r in relations):
                cross_repo_related = True
                break

        results.append(
            OrganizationLearning(
                learning_type=learning_type,
                surface_category=category,
                contributing_repository_ids=contributing_repo_ids,
                contributing_records=tuple(records),
                cross_repo_related=cross_repo_related,
            )
        )

    return tuple(results)
