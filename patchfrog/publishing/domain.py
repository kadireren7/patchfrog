"""Internal domain models for GitHub review publishing (Phase 6).

Mirrors :mod:`patchfrog.review.domain`'s role for the AI Reviewer: the
stable, pure boundary everything in :mod:`patchfrog.publishing` shares.
Reuses :class:`patchfrog.analysis.domain.Severity`/``FindingCategory``/
``Confidence`` rather than inventing a parallel taxonomy -- a finding's
severity means the same thing whether it is being displayed, aggregated,
or published.

Deliberately GitHub-transport-free: nothing here knows about GitHub's JSON
shapes, REST paths, or its ``LEFT``/``RIGHT`` vocabulary (see
:class:`DiffSide` instead, translated to GitHub's own
:class:`patchfrog.domain.github_review.GitHubDiffSide` only at the
transport boundary in :mod:`patchfrog.publishing.github_publisher`). This
is what keeps :mod:`patchfrog.publishing.planner` and
:mod:`patchfrog.publishing.diff_mapper` fully unit-testable without
network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity


class ReviewPublicationMode(StrEnum):
    """Whether a publication attempt is allowed to perform a real GitHub
    write. The planner runs identically either way (see the module
    docstring of :mod:`patchfrog.publishing.planner`) -- ``mode`` only
    ever gates the final write, never the planning logic itself."""

    DRY_RUN = "dry_run"
    PUBLISH = "publish"


class ReviewPublicationStatus(StrEnum):
    """Terminal (or in-flight) disposition of one publication attempt.

    ``PUBLISHING`` is the durable marker persisted *before* the GitHub
    write (see :mod:`patchfrog.publishing.service`) -- if the process
    crashes between the GitHub write and the following DB commit, a retry
    finds a row stuck in ``PUBLISHING`` and reconciles against GitHub
    rather than blindly writing again (see :func:`ReviewPublicationStatus`
    usage in :mod:`patchfrog.publishing.service` and the module docstring
    there for the full reconciliation design).
    """

    PLANNED = "planned"
    DRY_RUN = "dry_run"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    SKIPPED_NO_FINDINGS = "skipped_no_findings"
    SKIPPED_DISABLED = "skipped_disabled"
    STALE = "stale"
    FAILED = "failed"
    RECONCILED = "reconciled"


class DiffSide(StrEnum):
    """Which half of a unified diff a mapped line belongs to -- the
    diff-mapper's own vocabulary, independent of GitHub's ``LEFT``/``RIGHT``
    (see the module docstring)."""

    OLD = "old"
    NEW = "new"


class UnmappableReason(StrEnum):
    """Why a finding could not be mapped to a publishable inline diff
    position. Always recorded (never silently dropped) -- see
    :class:`ReviewPublicationComment.reason`."""

    FILE_NOT_IN_DIFF = "file_not_in_diff"
    FILE_DELETED = "file_deleted"
    BINARY_OR_NO_PATCH = "binary_or_no_patch"
    LINE_NOT_IN_DIFF = "line_not_in_diff"
    PATH_UNSAFE = "path_unsafe"


class PublicationDisposition(StrEnum):
    """What ultimately happens to one finding in a publication plan."""

    INLINE = "inline"
    SUMMARY_ONLY = "summary_only"
    OMITTED = "omitted"
    #: Suppressed before any selection/threshold logic runs, because
    #: Phase 7 (:mod:`patchfrog.review_memory`) determined this finding is
    #: the same underlying issue as one already published on this PR in a
    #: previous review -- see :meth:`patchfrog.publishing.planner.PublicationPlanner.build_plan`'s
    #: ``already_reported_finding_ids``. Deliberately distinct from
    #: ``OMITTED`` (which means "didn't meet the threshold"): this
    #: finding met every threshold, it just isn't news.
    ALREADY_REPORTED = "already_reported"


@dataclass(frozen=True, slots=True)
class MappedPosition:
    """A finding's location translated into a GitHub-commentable diff
    position -- always a position that actually exists within the PR's
    diff hunks (see :mod:`patchfrog.publishing.diff_mapper`), never a
    guessed "nearest" line."""

    path: str
    side: DiffSide
    line: int
    start_side: DiffSide | None = None
    start_line: int | None = None


@dataclass(frozen=True, slots=True)
class PublishableFinding:
    """One AI finding as consumed by the publication planner -- a plain,
    persistence-independent projection of
    :class:`patchfrog.persistence.models.review.AIFindingModel`, so the
    planner never needs a database session to be unit-tested (see
    :func:`patchfrog.publishing.queries.publishable_finding_from_model`
    for the conversion)."""

    finding_id: UUID
    title: str
    message: str
    category: FindingCategory
    severity: Severity
    confidence: Confidence
    file_path: str
    start_line: int
    end_line: int
    reasoning_summary: str
    suggested_fix: str | None = None
    impact: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewPublicationComment:
    """One finding's disposition within a publication plan -- whatever
    that disposition turns out to be. Every finding fed to the planner
    produces exactly one of these, so nothing is ever silently dropped
    without a recorded reason."""

    finding_id: UUID
    fingerprint: str
    path: str
    body: str
    severity: Severity
    disposition: PublicationDisposition
    reason: str
    position: MappedPosition | None = None


@dataclass(frozen=True, slots=True)
class ReviewInputSnapshot:
    """Enough identity to prove exactly which PR revision a review run
    covers -- immutable SHA identity, never a branch name (see the module
    docstring of :mod:`patchfrog.publishing.service`)."""

    repository_id: UUID
    repository_full_name: str
    pull_request_number: int
    review_run_id: UUID
    head_sha: str
    base_sha: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewPublicationPlan:
    """The complete, deterministic output of the publication planner --
    everything needed to either print a dry-run report or perform a real
    GitHub write, with no further decisions left to make at write time."""

    snapshot: ReviewInputSnapshot
    mode: ReviewPublicationMode
    status: ReviewPublicationStatus
    reason: str | None
    inline_comments: tuple[ReviewPublicationComment, ...] = field(default_factory=tuple)
    summary_only: tuple[ReviewPublicationComment, ...] = field(default_factory=tuple)
    omitted: tuple[ReviewPublicationComment, ...] = field(default_factory=tuple)
    already_reported: tuple[ReviewPublicationComment, ...] = field(default_factory=tuple)
    summary_body: str = ""

    @property
    def is_publishable(self) -> bool:
        return self.status in (ReviewPublicationStatus.PLANNED, ReviewPublicationStatus.DRY_RUN)


@dataclass(frozen=True, slots=True)
class ReviewPublicationResult:
    """Structured outcome of one publication attempt (dry-run or real)."""

    status: ReviewPublicationStatus
    mode: ReviewPublicationMode
    repository_id: UUID
    pull_request_number: int
    head_sha: str
    publication_id: UUID | None
    github_review_id: int | None
    planned_inline: int
    published_inline: int
    summary_only: int
    skipped: int
    omitted: int
    reconciled: bool
    errors: tuple[str, ...] = field(default_factory=tuple)
