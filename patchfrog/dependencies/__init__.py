"""External dependency discovery + contract registry (M5).

Given a repository, identify which external APIs/SDKs it depends on --
with package/version evidence, usage sites and normalized contract
fingerprints -- from static repository evidence only, never reading a
secret value. Seed for M6 (upstream change detection + consumer impact),
which is not implemented yet. See ``docs/dependency-discovery.md``.
"""

from patchfrog.dependencies.discovery import discover_dependencies
from patchfrog.dependencies.domain import (
    DependencyInventory,
    ExternalDependency,
    ExternalDependencyKind,
)

__all__ = ["DependencyInventory", "ExternalDependency", "ExternalDependencyKind", "discover_dependencies"]
