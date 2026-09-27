"""Evidence-Backed Automated Migration Pull Requests -- M9.

Given a sufficiently verified migration (M8), decides whether PatchFrog
may open a GitHub PR at all, builds a deterministic branch/commit/PR plan
bound to the exact evidence it was verified against, and publishes it
through the existing GitHub Check infrastructure -- reusing
:mod:`patchfrog.publishing.checks`'s Protocol, never a second publishing
system. No LLM ever decides eligibility or content; no automatic merge.
See ``docs/migration-pr.md``.
"""

from patchfrog.migration_pr.domain import (
    MIGRATION_PR_VERSION,
    MigrationPREligibility,
    MigrationPRPlan,
    MigrationPRPolicy,
    MigrationPRStatus,
    MigrationPullRequest,
)
from patchfrog.migration_pr.integrity import MigrationPRIntegrityError
from patchfrog.migration_pr.planner import build_pr_plan
from patchfrog.migration_pr.publisher import MigrationPRPublicationMode, MigrationPRPublisher

__all__ = [
    "MIGRATION_PR_VERSION",
    "MigrationPREligibility",
    "MigrationPRIntegrityError",
    "MigrationPRPlan",
    "MigrationPRPolicy",
    "MigrationPRPublicationMode",
    "MigrationPRPublisher",
    "MigrationPRStatus",
    "MigrationPullRequest",
    "build_pr_plan",
]
