"""Pure domain model for Evidence-Backed Automated Migration PRs -- M9.

A :class:`MigrationPRPlan` is the complete, deterministic output of
deciding whether/how to publish a migration -- everything a dry-run needs
to print, and everything a real publish needs to write, with no further
decisions left to make at write time (mirrors
:class:`patchfrog.publishing.domain.ReviewPublicationPlan`'s own role for
review publishing).

No LLM ever decides eligibility, branch identity, or PR content here --
every field is derived from already-computed M6/M7/M8 evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum

#: Bumped whenever branch-naming, eligibility-policy, dossier-rendering,
#: or idempotency-identity logic changes materially enough that a prior
#: MigrationPRPlan/MigrationPullRequest can no longer be considered
#: equivalent to what re-running now would produce.
MIGRATION_PR_VERSION = 1


class MigrationPREligibility(StrEnum):
    """M9.3's publication eligibility decision -- a pure function of a
    :class:`patchfrog.migration_verification.domain.VerificationOutcome`
    and operator policy. Never silently lowers the bar: a
    ``PARTIALLY_VERIFIED`` migration only ever reaches
    ``OPEN_WITH_OPERATOR_APPROVAL`` when the operator has explicitly opted
    in, and the resulting PR must be visibly marked partial (M9.3)."""

    #: VERIFIED -- may auto-open.
    AUTO_OPEN = "auto_open"
    #: PARTIALLY_VERIFIED and the operator's policy allows it -- must be
    #: visibly marked in the dossier (see :mod:`patchfrog.migration_pr.dossier`).
    OPEN_WITH_OPERATOR_APPROVAL = "open_with_operator_approval"
    #: HUMAN_REQUIRED -- a plan/report may still be produced (dry-run),
    #: but never a code PR by default.
    PLAN_ONLY = "plan_only"
    #: UNVERIFIED / FAILED / REGRESSION_DETECTED, or PARTIALLY_VERIFIED
    #: without operator opt-in -- no PR, no plan-only report either.
    NOT_ELIGIBLE = "not_eligible"


class MigrationPRStatus(StrEnum):
    """Terminal (or in-flight) disposition of one migration PR publication
    attempt -- mirrors :class:`patchfrog.publishing.domain.ReviewPublicationStatus`'s
    own shape."""

    PLANNED = "planned"
    DRY_RUN = "dry_run"
    PUBLISHING = "publishing"
    OPENED = "opened"
    UPDATED = "updated"
    NO_OP_UNCHANGED = "no_op_unchanged"
    SKIPPED_NOT_ELIGIBLE = "skipped_not_eligible"
    STALE_REQUIRES_REGENERATION = "stale_requires_regeneration"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MigrationPRPolicy:
    """Operator-controlled publication policy (M9.3) -- never hardcoded,
    never silently relaxed by this package itself."""

    #: Whether a PARTIALLY_VERIFIED migration may open a (visibly marked)
    #: PR at all. Off by default -- PatchFrog never lowers the bar on its
    #: own.
    allow_partially_verified: bool = False


@dataclass(frozen=True, slots=True)
class MigrationPRLinkage:
    """Everything M9 idempotency, staleness, and integrity checking key
    on -- the migration-PR-specific analogue of
    :class:`patchfrog.migration.domain.PatchLinkage`."""

    change_fingerprint: str
    dependency_key: str | None
    repository: str
    base_commit_sha: str
    plan_fingerprint: str
    patch_fingerprint: str
    verification_plan_fingerprint: str
    bundle_fingerprint: str
    engine_version: int = MIGRATION_PR_VERSION

    def identity_key(self) -> str:
        """The stable identity a duplicate-PR check keys on: same
        repository + same upstream change + same base/engine version --
        deliberately independent of the patch/verification fingerprints,
        so a *legitimately regenerated* patch for the same upstream
        change updates the existing PR (M9.6) rather than opening a
        second one."""

        payload = {
            "repository": self.repository, "change_fingerprint": self.change_fingerprint,
            "engine_version": self.engine_version,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class MigrationPRPlan:
    """The complete, deterministic output of the publication planner."""

    linkage: MigrationPRLinkage
    eligibility: MigrationPREligibility
    eligibility_reason: str
    branch_name: str
    commit_message: str
    pr_title: str
    pr_body: str
    check_title: str
    check_summary: str

    @property
    def may_open_code_pr(self) -> bool:
        return self.eligibility in (
            MigrationPREligibility.AUTO_OPEN, MigrationPREligibility.OPEN_WITH_OPERATOR_APPROVAL,
        )


@dataclass(frozen=True, slots=True)
class MigrationPullRequest:
    """One durable migration-PR publication record."""

    id: str | None
    number: int | None
    html_url: str | None
    status: MigrationPRStatus
    linkage: MigrationPRLinkage
    branch_name: str
    reason: str | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)


__all__ = [
    "MIGRATION_PR_VERSION",
    "MigrationPREligibility",
    "MigrationPRLinkage",
    "MigrationPRPlan",
    "MigrationPRPolicy",
    "MigrationPRStatus",
    "MigrationPullRequest",
]
