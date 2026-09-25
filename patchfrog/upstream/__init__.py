"""Upstream change detection + consumer impact / blast radius (M6).

change -> deterministic contract diff -> compatibility classification ->
affected consumers -> blast radius. Deterministic and offline: no
network, no provider call, no LLM. Builds on the M5 dependency registry
(``patchfrog.dependencies``) rather than keeping parallel state. See
``docs/upstream-change-detection.md``.
"""

from patchfrog.upstream.domain import (
    UPSTREAM_CHANGE_VERSION,
    ChangeRisk,
    CompatibilityClass,
    ContractDiffItem,
    DependencyTarget,
    ExternalChangeEvent,
)

__all__ = [
    "UPSTREAM_CHANGE_VERSION",
    "ChangeRisk",
    "CompatibilityClass",
    "ContractDiffItem",
    "DependencyTarget",
    "ExternalChangeEvent",
]
