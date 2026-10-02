"""Watch registry (M11.1): what *should* be watched.

Derived from the dependency identities workspaces actually use -- never "every
API on the internet". Pure functions over plain data (the hosted service
feeds them rows read from the M5 registry), so the aggregation and
de-duplication rules are inspectable and testable here.

Two inputs:

* automatic sources, derived from a discovered dependency's ecosystem and
  package (a PyPI or npm package gets a registry watcher);
* configured sources a workspace added explicitly (a GitHub repository's
  releases, an OpenAPI URL, a changelog feed).

Sources are de-duplicated by :attr:`WatcherSource.key`, so fifty workspaces
using one package cause one upstream fetch; the resulting change is fanned
out to the subscribing workspaces, each of which then evaluates it against
its own dependency evidence (an irrelevant change simply matches nothing).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from patchfrog.dependencies.domain import Ecosystem
from patchfrog.upstream.domain import DependencyTarget, normalize_package_name
from patchfrog.watchers.domain import WatcherSource, WatcherSourceKind

#: Dependency kinds (``ExternalDependencyKind`` values) that have a package identity.
PACKAGE_KINDS = frozenset({"sdk", "package"})


@dataclass(frozen=True, slots=True)
class DependencySubscription:
    """One active dependency of one repository, as plain data."""

    workspace_key: str
    repository: str
    kind: str
    ecosystem: str
    package_name: str | None


@dataclass(frozen=True, slots=True)
class ConfiguredSource:
    workspace_key: str
    source: WatcherSource


@dataclass(frozen=True, slots=True)
class WatchRequirement:
    source: WatcherSource
    subscribers: tuple[str, ...]
    #: ``dependency`` (derived) or ``configured`` (added explicitly).
    origin: str


def automatic_source(subscription: DependencySubscription) -> WatcherSource | None:
    if subscription.kind not in PACKAGE_KINDS or not subscription.package_name:
        return None
    name = subscription.package_name.strip()
    if subscription.ecosystem == Ecosystem.PYPI.value:
        kind, ecosystem = WatcherSourceKind.PYPI, Ecosystem.PYPI
        name = normalize_package_name(name)
    elif subscription.ecosystem == Ecosystem.NPM.value:
        kind, ecosystem = WatcherSourceKind.NPM, Ecosystem.NPM
    else:
        return None
    return WatcherSource(
        kind=kind, locator=name, target=DependencyTarget(ecosystem=ecosystem, package_name=name, display_name=name)
    )


def required_watchers(
    subscriptions: Iterable[DependencySubscription], configured: Iterable[ConfiguredSource] = ()
) -> tuple[WatchRequirement, ...]:
    sources: dict[str, WatcherSource] = {}
    subscribers: dict[str, set[str]] = {}
    origins: dict[str, str] = {}
    for subscription in subscriptions:
        source = automatic_source(subscription)
        if source is None:
            continue
        sources.setdefault(source.key, source)
        subscribers.setdefault(source.key, set()).add(subscription.workspace_key)
        origins.setdefault(source.key, "dependency")
    for item in configured:
        key = item.source.key
        # an explicitly configured source carries a more specific target than a derived one
        sources[key] = item.source if origins.get(key) != "configured" else sources[key]
        subscribers.setdefault(key, set()).add(item.workspace_key)
        origins[key] = "configured"
    return tuple(
        WatchRequirement(source=sources[key], subscribers=tuple(sorted(subscribers[key])), origin=origins[key])
        for key in sorted(sources)
    )


def subscribers_for(requirements: Iterable[WatchRequirement], source_key: str) -> tuple[str, ...]:
    for requirement in requirements:
        if requirement.source.key == source_key:
            return requirement.subscribers
    return ()


__all__ = [
    "PACKAGE_KINDS",
    "ConfiguredSource",
    "DependencySubscription",
    "WatchRequirement",
    "automatic_source",
    "required_watchers",
    "subscribers_for",
]
