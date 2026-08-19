"""Shared repository/``.patchfrog.yml`` ``publish:`` section resolution.

Mirrors :mod:`patchfrog.review.config_resolution` exactly, including why it
exists as a standalone module: both the CLI and the Celery publish task
(:mod:`apps.worker.tasks.publish_review`) must resolve the exact same
publication config for a given repository/commit, so a repository's
``publish.enabled`` (or any other publication setting) can never diverge
between the two entry points.

Never executes repository code -- only ever reads one text file off a
checked-out (local) or freshly, narrowly cloned-at-exactly-``commit_sha``
(remote) working tree, discarded immediately after the one read.
"""

from __future__ import annotations

from pathlib import Path

from patchfrog.publishing.config import PublicationConfig, load_publication_config
from patchfrog.repository.snapshot import RepositorySnapshotProvider


async def resolve_repository_publication_config(
    *,
    local: bool,
    commit_sha: str,
    repository_full_name: str,
    root_path: Path | None = None,
    clone_url: str | None = None,
    token: str | None = None,
    snapshot_provider: RepositorySnapshotProvider | None = None,
) -> PublicationConfig:
    if local:
        if root_path is None:
            raise ValueError("root_path is required when local=True")
        return load_publication_config(root_path)

    if clone_url is None:
        raise ValueError("clone_url is required when local=False")
    provider = snapshot_provider or RepositorySnapshotProvider()
    with provider.acquire(
        clone_url=clone_url,
        commit_sha=commit_sha,
        repository_full_name=repository_full_name,
        token=token,
    ) as snapshot:
        return load_publication_config(snapshot.root_path)
