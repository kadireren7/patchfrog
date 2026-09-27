"""Evidence/patch integrity check (M9.8).

Before a :class:`~patchfrog.migration_pr.domain.MigrationPRPlan` is even
built, verify that the evidence bundle a caller is about to publish from
actually corresponds to the exact upstream change, patch, and repository
state it claims to. A :class:`~patchfrog.migration_verification.domain.MigrationEvidenceBundle`
computed for one patch must never be silently treated as evidence for a
different one -- this is a programming-error guard (a caller passing
mismatched arguments), not a normal outcome, so it raises rather than
degrading to a weaker status.
"""

from __future__ import annotations

from patchfrog.migration.domain import GeneratedPatch
from patchfrog.migration_verification.domain import MigrationEvidenceBundle
from patchfrog.upstream.domain import ExternalChangeEvent


class MigrationPRIntegrityError(Exception):
    """Raised when an evidence bundle does not correspond to the exact
    upstream change / patch / repository state a migration PR plan is
    about to be built from."""


def verify_evidence_integrity(
    *,
    event: ExternalChangeEvent,
    patch: GeneratedPatch | None,
    bundle: MigrationEvidenceBundle,
    base_commit_sha: str,
) -> None:
    if bundle.change_fingerprint != event.fingerprint:
        raise MigrationPRIntegrityError(
            f"evidence bundle was computed for upstream change {bundle.change_fingerprint!r}, "
            f"not the requested change {event.fingerprint!r}"
        )

    expected_patch_fingerprint = patch.fingerprint if patch is not None else ""
    if bundle.patch_fingerprint != expected_patch_fingerprint:
        raise MigrationPRIntegrityError(
            f"evidence bundle was computed for patch {bundle.patch_fingerprint!r}, "
            f"not the patch about to be published ({expected_patch_fingerprint!r}) -- "
            "a VERIFIED bundle for one patch must never be reused for another"
        )

    if bundle.repository_head_sha != base_commit_sha:
        raise MigrationPRIntegrityError(
            f"evidence bundle was computed against repository head {bundle.repository_head_sha!r}, "
            f"not the base commit this migration PR would publish against ({base_commit_sha!r}) -- "
            "regenerate the evidence rather than publishing stale evidence"
        )


__all__ = ["MigrationPRIntegrityError", "verify_evidence_integrity"]
