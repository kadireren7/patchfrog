"""Shared repository/``.patchfrog.yml`` incremental-review config
resolution -- mirrors :mod:`patchfrog.review.config_resolution` exactly,
so the CLI and the production Celery task can never diverge in what
:class:`~patchfrog.review_memory.config.IncrementalConfig` a given
repository/commit resolves to."""

from __future__ import annotations

from pathlib import Path

from patchfrog.repository.snapshot import RepositorySnapshotProvider
from patchfrog.review_memory.config import IncrementalConfig, load_incremental_config


async def resolve_repository_incremental_config(
    *,
    local: bool,
    commit_sha: str,
    repository_full_name: str,
    root_path: Path | None = None,
    clone_url: str | None = None,
    token: str | None = None,
    snapshot_provider: RepositorySnapshotProvider | None = None,
) -> IncrementalConfig:
    """Resolve the exact repository/commit's ``review.incremental`` /
    ``review.suppress_already_reported`` / ``memory.enabled`` settings.

    Unlike :func:`patchfrog.review.config_resolution.resolve_repository_review_config`,
    this is never strict -- :func:`~patchfrog.review_memory.config.load_incremental_config`
    always degrades to safe defaults on a malformed file (memory enabled,
    ``auto`` mode), matching its own documented "decide per-run whether
    memory is safely usable, never hard-fail" contract. A malformed
    ``.patchfrog.yml`` is already surfaced as a real failure via the
    strict ``review:`` resolution this always runs alongside -- this
    function never needs to raise a second time for the same file.
    """

    if local:
        if root_path is None:
            raise ValueError("root_path is required when local=True")
        return load_incremental_config(root_path)

    if clone_url is None:
        raise ValueError("clone_url is required when local=False")
    provider = snapshot_provider or RepositorySnapshotProvider()
    with provider.acquire(
        clone_url=clone_url,
        commit_sha=commit_sha,
        repository_full_name=repository_full_name,
        token=token,
    ) as snapshot:
        return load_incremental_config(snapshot.root_path)
