"""Shared repository/``.patchfrog.yml`` config resolution.

The single code path both the CLI (:mod:`patchfrog.cli`) and the
production Celery task (:mod:`apps.worker.tasks.review_pull_request`)
call, so they can never diverge in what :class:`~patchfrog.review.config.ReviewConfig`
a given repository/commit resolves to -- the exact bug this module fixes
("the production task always used ``ReviewConfig()`` defaults and ignored
the repository's committed config").

This resolves only repository-controlled review *behavior*
(:class:`~patchfrog.review.config.ReviewConfig`) -- max candidates, token
budgets, confidence thresholds, and so on. Provider/model selection is a
separate, operator-controlled concern resolved independently by
:func:`patchfrog.review.runtime_config.resolve_review_runtime_config`
from trusted `Settings`, never from this function's result; a caller
needs both before constructing a
:class:`~patchfrog.review.service.PullRequestReviewService` (which
requires an already-constructed provider). That is why this lives as a
standalone function callable by both entry points, rather than inside
the service itself.
"""

from __future__ import annotations

from pathlib import Path

from patchfrog.repository.snapshot import RepositorySnapshotProvider
from patchfrog.review.config import ReviewConfig, load_review_config


async def resolve_repository_review_config(
    *,
    local: bool,
    commit_sha: str,
    repository_full_name: str,
    root_path: Path | None = None,
    clone_url: str | None = None,
    token: str | None = None,
    snapshot_provider: RepositorySnapshotProvider | None = None,
) -> ReviewConfig:
    """Resolve the exact repository/commit's ``.patchfrog.yml`` ``review:``
    section.

    Always strict (``on_malformed="raise"``) -- both callers represent a
    real review attempt (or a ``--dry-run`` preview of one), never a
    silent, best-effort fallback, so a bad committed config must surface
    as a clear failure rather than an unnoticed default. Raises
    :class:`~patchfrog.review.config.MalformedReviewConfigError`; see
    :func:`patchfrog.review.service.persist_malformed_config_failure` for
    how callers turn that into a persisted, queryable failure.

    Never executes repository code -- this only ever reads one text file
    off a checked-out (local) or freshly, narrowly cloned-at-exactly-
    ``commit_sha`` (remote) working tree. The remote snapshot is acquired
    solely for this one read and then discarded immediately -- never
    cached or reused across commits, so a stale local checkout or an
    earlier commit's clone can never supply another commit's config.
    """

    if local:
        if root_path is None:
            raise ValueError("root_path is required when local=True")
        return load_review_config(root_path, on_malformed="raise")

    if clone_url is None:
        raise ValueError("clone_url is required when local=False")
    provider = snapshot_provider or RepositorySnapshotProvider()
    with provider.acquire(
        clone_url=clone_url,
        commit_sha=commit_sha,
        repository_full_name=repository_full_name,
        token=token,
    ) as snapshot:
        return load_review_config(snapshot.root_path, on_malformed="raise")
