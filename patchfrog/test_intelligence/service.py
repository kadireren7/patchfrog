"""Top-level Test Intelligence orchestrator.

:func:`build_test_intelligence_report` is the one entry point everything
else in this package composes into. Called once per review run, after
Change Intelligence (whose already-built ``ChangeUnit``s/
``ExpectedCompanionChange``s it consumes) -- see
:mod:`patchfrog.review.service`'s integration point.

Deliberately synchronous and session-free, exactly like
:mod:`patchfrog.intent_verification.service` -- see
``validation/test_intelligence/latest-summary.md`` section 1 for why
neither of this milestone's two signals needs a repository-graph query
or any new I/O at all. Zero LLM calls.
"""

from __future__ import annotations

from patchfrog.change_intelligence.domain import ChangeUnit, ExpectedCompanionChange
from patchfrog.diff.models import DiffFile
from patchfrog.test_intelligence.domain import TEST_INTELLIGENCE_VERSION, TestIntelligenceReport
from patchfrog.test_intelligence.expectations import (
    derive_gaps,
    derive_no_test_surface_expectations,
    derive_weakened_test_expectations,
)
from patchfrog.test_intelligence.story import build_test_story_prefix


def build_test_intelligence_report(
    *,
    change_units: tuple[ChangeUnit, ...] = (),
    expected_companions: tuple[ExpectedCompanionChange, ...] = (),
    diff_files: tuple[DiffFile, ...] = (),
) -> TestIntelligenceReport:
    expectations = derive_no_test_surface_expectations(
        change_units=change_units, expected_companions=expected_companions
    ) + derive_weakened_test_expectations(expected_companions=expected_companions, diff_files=diff_files)

    gaps = derive_gaps(expectations)
    test_story = build_test_story_prefix(gaps)

    return TestIntelligenceReport(
        version=TEST_INTELLIGENCE_VERSION,
        expectations=expectations,
        gaps=gaps,
        test_story=test_story,
    )
