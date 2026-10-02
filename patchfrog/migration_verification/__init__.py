"""Migration Verification -- M8.

migration plan + generated patch + exact repository state -> verification
plan -> bounded, sandboxed execution -> evidence bundle -> deterministic
outcome. Never a provider call; never treats generated code, a single
passing generic test, or AI output as proof. See
``docs/migration-verification.md``.
"""

from patchfrog.migration_verification.domain import (
    MIGRATION_VERIFICATION_VERSION,
    MigrationEvidenceBundle,
    VerificationOutcome,
)
from patchfrog.migration_verification.service import run_migration_verification

__all__ = [
    "MIGRATION_VERIFICATION_VERSION",
    "MigrationEvidenceBundle",
    "VerificationOutcome",
    "run_migration_verification",
]
