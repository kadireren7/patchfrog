"""Event-level change classification (M6.4) -- deterministic rules only.

Per-item compatibility comes from the diff engines; this module folds it
into one :class:`ChangeRisk` plus a machine-readable reason list:

- any ``BREAKING`` item                     -> ``BREAKING``
- any ``POTENTIALLY_BREAKING``/``UNKNOWN``  -> ``REVIEW_REQUIRED``
- only non-breaking items beyond a patch bump (additions, deprecations,
  minor bumps)                              -> ``LOW_RISK``
- nothing, or only a patch bump             -> ``SAFE``

A major version bump with no structural contract evidence stays
``REVIEW_REQUIRED`` (never ``BREAKING``) and says so in its reasons.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from types import MappingProxyType

from patchfrog.upstream.domain import (
    ChangeClassification,
    ChangeRisk,
    CompatibilityClass,
    ContractDiffItem,
    DiffItemKind,
    most_severe,
)

_SAFE_ONLY = frozenset({DiffItemKind.PACKAGE_PATCH_BUMP})


def classify(items: Sequence[ContractDiffItem], *, has_structural_evidence: bool) -> ChangeClassification:
    counts = Counter(item.compatibility.value for item in items)
    reasons: set[str] = set()
    for item in items:
        if item.compatibility is not CompatibilityClass.NON_BREAKING:
            reasons.add(f"{item.compatibility.value}:{item.kind.value}")
    kinds = {item.kind for item in items}
    if DiffItemKind.PACKAGE_MAJOR_BUMP in kinds and not has_structural_evidence:
        reasons.add("major_version_bump_without_structural_proof")
    if has_structural_evidence and not any(
        not item.kind.value.startswith("package_") for item in items
    ):
        reasons.add("no_structural_contract_change")

    compatibility = most_severe(*(item.compatibility for item in items))
    if compatibility is CompatibilityClass.BREAKING:
        risk = ChangeRisk.BREAKING
    elif compatibility in (CompatibilityClass.POTENTIALLY_BREAKING, CompatibilityClass.UNKNOWN):
        risk = ChangeRisk.REVIEW_REQUIRED
    elif kinds - _SAFE_ONLY:
        risk = ChangeRisk.LOW_RISK
        reasons.add("non_breaking_changes_only")
    else:
        risk = ChangeRisk.SAFE
        reasons.add("no_compatibility_relevant_change" if not items else "patch_version_only")

    return ChangeClassification(
        risk=risk,
        compatibility=compatibility,
        reasons=tuple(sorted(reasons)),
        counts=MappingProxyType({**{c.value: counts.get(c.value, 0) for c in CompatibilityClass}, "total": len(items)}),
        has_structural_evidence=has_structural_evidence,
    )


__all__ = ["classify"]
