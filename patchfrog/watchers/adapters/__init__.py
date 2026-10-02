"""Upstream source adapters (M11.2)."""

from __future__ import annotations

from patchfrog.watchers.adapters.base import SourceAdapter
from patchfrog.watchers.adapters.github_releases import GitHubReleasesAdapter
from patchfrog.watchers.adapters.registries import NpmAdapter, PyPIAdapter
from patchfrog.watchers.adapters.spec_and_feed import ChangelogFeedAdapter, OpenApiUrlAdapter
from patchfrog.watchers.domain import WatcherSourceKind


def default_adapters() -> dict[WatcherSourceKind, SourceAdapter]:
    adapters: list[SourceAdapter] = [
        PyPIAdapter(), NpmAdapter(), GitHubReleasesAdapter(), OpenApiUrlAdapter(), ChangelogFeedAdapter(),
    ]
    return {adapter.kind: adapter for adapter in adapters}


__all__ = [
    "ChangelogFeedAdapter",
    "GitHubReleasesAdapter",
    "NpmAdapter",
    "OpenApiUrlAdapter",
    "PyPIAdapter",
    "SourceAdapter",
    "default_adapters",
]
