"""Deterministic targeted-test selection for migration verification (M8.3).

Reuses :class:`patchfrog.upstream.blast_radius.BlastRadius` -- already
computed by M6/M7 for every migration plan -- rather than re-deriving a
second test-to-symbol mapping. ``BlastRadius.related_tests`` already
carries a confidence and a reason per test node; this module only
translates that existing evidence into the bounded
:class:`~patchfrog.migration_verification.domain.TestSelection` taxonomy
M8.3 asks for, and explains why every selection was made.

Never a filename-similarity guess (the same discipline
:mod:`patchfrog.executable_verification.eligibility` already applies for
review candidates): a file with no test evidence in the blast radius at
all is ``UNKNOWN``, never given an invented test path.
"""

from __future__ import annotations

from patchfrog.dependencies.domain import DetectionConfidence
from patchfrog.migration.domain import MigrationPlan
from patchfrog.migration_verification.domain import (
    MAX_TARGETED_TESTS_PER_PLAN,
    TestSelection,
    TestSelectionClass,
)
from patchfrog.upstream.blast_radius import BlastRadius


def _classify(confidence: DetectionConfidence, *, is_direct_site: bool) -> TestSelectionClass:
    if is_direct_site and confidence is DetectionConfidence.HIGH:
        return TestSelectionClass.DIRECT
    if confidence in (DetectionConfidence.HIGH, DetectionConfidence.MEDIUM):
        return TestSelectionClass.TRANSITIVE
    return TestSelectionClass.FALLBACK


def select_targeted_tests(
    plan: MigrationPlan, blast_radii: tuple[BlastRadius, ...]
) -> tuple[TestSelection, ...]:
    """One :class:`TestSelection` per distinct related-test file across
    every :class:`BlastRadius` the migration's dependency touches,
    classified DIRECT/TRANSITIVE/FALLBACK by the same confidence the
    blast radius already computed. Bounded to
    ``MAX_TARGETED_TESTS_PER_PLAN`` -- the highest-confidence selections
    are kept first (never "run everything" by default, M8.2's own rule).

    A plan with automatic steps but zero related-test evidence anywhere
    in its blast radii yields no selections at all -- the caller (see
    :mod:`patchfrog.migration_verification.requirements`) must then treat
    ``UNIT_TESTS_PASS`` as a requirement with no discoverable evidence,
    never invent one."""

    if not plan.automatic_steps:
        return ()

    seen: dict[str, TestSelection] = {}
    for radius in blast_radii:
        direct_files = {n.file_path for n in radius.direct}
        for related in radius.related_tests:
            if related.file_path in seen:
                continue
            classification = _classify(related.confidence, is_direct_site=related.covers in direct_files or any(
                related.covers.startswith(f) for f in direct_files
            ))
            seen[related.file_path] = TestSelection(
                test_path=related.file_path,
                classification=classification,
                reason=f"{related.reason} (confidence={related.confidence.value})",
            )

    ordered = sorted(
        seen.values(),
        key=lambda s: (
            {"direct": 0, "transitive": 1, "fallback": 2, "unknown": 3}[s.classification.value],
            s.test_path,
        ),
    )
    return tuple(ordered[:MAX_TARGETED_TESTS_PER_PLAN])


__all__ = ["select_targeted_tests"]
