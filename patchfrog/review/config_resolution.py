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

from patchfrog.config.settings import Settings
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


def apply_operator_hard_caps(repo_config: ReviewConfig, *, settings: Settings) -> ReviewConfig:
    """Quality + Cost Guard trust boundary: a repository may *reduce*
    its own review cost/candidate ceilings below the operator's hard
    caps, but may never exceed them -- ``effective = min(repo_intent,
    operator_hard_cap)`` for each capped field, independently.

    Milestone C protected provider/model selection from repository
    control; this closes a related trust/cost gap it explicitly did not
    cover -- a repository's own ``.patchfrog.yml`` could otherwise still
    request an arbitrarily large ``max_candidates``/
    ``max_total_input_tokens``/etc. and force the operator to spend
    accordingly. Operator hard caps are environment-only (never
    ``.patchfrog.yml``-controlled -- see :mod:`patchfrog.config.settings`),
    exactly like provider/model credentials.

    The returned :class:`ReviewConfig` is what canonical run identity
    (:meth:`ReviewConfig.fingerprint`) is computed from downstream -- a
    repository asking for more than the operator allows is never
    silently reused as if it got what it asked for; the *effective*
    (possibly capped) behavior is what participates in identity.
    """

    return ReviewConfig(
        critic_enabled=repo_config.critic_enabled,
        max_candidates=min(repo_config.max_candidates, settings.review_max_candidates),
        max_input_tokens_per_candidate=repo_config.max_input_tokens_per_candidate,
        max_output_tokens_per_candidate=min(
            repo_config.max_output_tokens_per_candidate, settings.review_max_output_tokens_per_candidate
        ),
        max_total_input_tokens=min(repo_config.max_total_input_tokens, settings.review_max_total_input_tokens),
        max_concurrent_requests=min(
            repo_config.max_concurrent_requests, settings.review_max_concurrent_requests
        ),
        min_final_confidence=repo_config.min_final_confidence,
        max_retries=min(repo_config.max_retries, settings.review_max_retries),
    )
