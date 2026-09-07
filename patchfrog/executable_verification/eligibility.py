"""Deterministic eligibility: does this candidate have a real,
already-discovered test-file relationship to verify against?

Reuses :func:`patchfrog.test_intelligence.expectations.derive_test_surfaces`
directly -- the exact same pure function Test Intelligence's own service
already calls internally -- never a second, duplicated derivation, and
never a filename-similarity guess (spec section E6's own explicit
prohibition). See
``validation/executable_verification/latest-summary.md`` section 1 for
why this is not exposed on ``TestIntelligenceReport`` today and why
calling it directly here is the correct reuse, not a workaround.
"""

from __future__ import annotations

from patchfrog.change_intelligence.domain import ExpectedCompanionChange
from patchfrog.review.domain import ReviewCandidate
from patchfrog.test_intelligence.expectations import derive_test_surfaces


def determine_verification_target(
    *, candidate: ReviewCandidate, expected_companions: tuple[ExpectedCompanionChange, ...]
) -> str | None:
    """Returns the single, bounded, already-indexed test file path to
    verify against for ``candidate``, or ``None`` if no real test-file
    relationship exists. A module-region candidate (``qualified_name is
    None``) is still eligible -- the relationship is file-level, not
    symbol-level -- but a candidate with no discoverable test surface at
    all is not. Only the first discovered path is ever used (v1 runs at
    most one target per candidate, never a search over several)."""

    surfaces = derive_test_surfaces(
        changed_file_paths=frozenset({candidate.file_path}), expected_companions=expected_companions
    )
    surface = surfaces.get(candidate.file_path)
    if surface is None or not surface.known_test_file_paths:
        return None
    return surface.known_test_file_paths[0]
