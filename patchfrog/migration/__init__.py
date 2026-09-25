"""Migration planner + generated fix (M7).

upstream change + consumer impact (``patchfrog.upstream``) -> structured
migration plan -> deterministic patch -> deterministic safety gates.
No runtime verification here (M8). See ``docs/migration-planner.md``.
"""

from patchfrog.migration.domain import (
    MIGRATION_ENGINE_VERSION,
    AutoFixEligibility,
    MigrationPlan,
    MigrationResult,
    MigrationStatus,
)

__all__ = [
    "MIGRATION_ENGINE_VERSION",
    "AutoFixEligibility",
    "MigrationPlan",
    "MigrationResult",
    "MigrationStatus",
]
