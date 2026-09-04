"""Pure domain model for Test Intelligence -- no I/O, no LLM, no database
session (mirrors :mod:`patchfrog.change_intelligence.domain`/
:mod:`patchfrog.contract_intelligence.domain`/
:mod:`patchfrog.intent_verification.domain`'s own role).

Reuses :mod:`patchfrog.change_intelligence.domain` types directly where
the shape already fits (``CompanionStatus`` for a ``TestExpectation``'s
own OBSERVED/MISSING state, ``ExpectedCompanionChange`` referenced --
never copied -- to dedup against J's own ``TEST_NOT_UPDATED`` companions)
-- see this package's own docstring and the audit in
``validation/test_intelligence/latest-summary.md`` section 1 for why
nothing here duplicates J/K/L's own candidate model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar

from patchfrog.change_intelligence.domain import CompanionStatus

#: Bumped whenever expectation-derivation/gap logic changes materially
#: enough that a prior report can no longer be considered equivalent to
#: what re-running now would produce. Independent of
#: CHANGE_INTELLIGENCE_VERSION/CONTRACT_INTELLIGENCE_VERSION/
#: INTENT_VERIFICATION_VERSION (none of those packages' own logic
#: changes because of this milestone -- see docs/test-intelligence.md).
TEST_INTELLIGENCE_VERSION = 1

#: Same defensive-bound rationale as
#: patchfrog.change_intelligence.service.MAX_CANDIDATES_CONSIDERED /
#: patchfrog.contract_intelligence.domain.MAX_CANDIDATES_CONSIDERED /
#: patchfrog.intent_verification.domain.MAX_CANDIDATES_CONSIDERED.
MAX_CANDIDATES_CONSIDERED = 150

#: Never surface more than this many gap candidates for one ChangeUnit --
#: bounded, matching every other engine's per-unit/per-claim cap in this
#: codebase.
MAX_TEST_GAPS_PER_UNIT = 5


class TestExpectationReasonCode(StrEnum):
    """Exactly two genuinely new, structural signals -- see
    ``validation/test_intelligence/latest-summary.md`` section 1 for why
    neither overlaps J's own ``CompanionReasonCode.TEST_NOT_UPDATED``."""

    #: Not a pytest test class despite the name -- see the identical
    #: note on ``TestSurface``/``TestExpectation`` below.
    __test__ = False

    #: A BEHAVIOR-kind ChangeUnit's changed file has zero discoverable
    #: test file at all -- not merely an existing test file that wasn't
    #: touched (that is J's own TEST_NOT_UPDATED territory).
    NO_TEST_SURFACE_FOUND = "no_test_surface_found"
    #: A test file genuinely touched in this diff shows a structural
    #: erosion signal -- net assertion-marker count decreased, or a
    #: skip/xfail marker was newly added. Never an NLP/semantic judgment
    #: about test quality.
    TEST_TOUCHED_BUT_WEAKENED = "test_touched_but_weakened"


@dataclass(frozen=True, slots=True)
class TestSurface:
    """The discovered behavioral test-file linkage for one changed file,
    derived purely by cross-referencing that file against J's own
    ``TEST_NOT_UPDATED`` companions -- never a new repository-graph
    query (see :func:`patchfrog.test_intelligence.expectations.derive_test_surfaces`).
    ``known_test_file_paths`` empty means genuinely no test file was
    ever discoverable for this file, not merely unchecked."""

    #: Not a pytest test class despite the name -- pytest's default
    #: collection only matches on the *class* name prefix, so this
    #: silences the otherwise-spurious "cannot collect ... because it
    #: has an __init__ constructor" warning for every module that
    #: imports this dataclass into a test file's namespace.
    __test__: ClassVar[bool] = False

    file_path: str
    known_test_file_paths: tuple[str, ...] = field(default_factory=tuple)

    @property
    def discovered(self) -> bool:
        return len(self.known_test_file_paths) > 0


@dataclass(frozen=True, slots=True)
class TestEvidence:
    """The bounded, already-rendered structural evidence behind one
    :class:`TestExpectation` -- e.g. an exact assertion/skip-marker
    count comparison. Never raw LLM prose, never a paraphrase."""

    #: See the identical note on ``TestSurface`` above.
    __test__: ClassVar[bool] = False

    reason_code: TestExpectationReasonCode
    bounded_text: str


@dataclass(frozen=True, slots=True)
class TestExpectation:
    """One candidate -- never a published finding on its own, exactly
    like :class:`~patchfrog.change_intelligence.domain.ExpectedCompanionChange`.
    ``status`` mirrors that type's own OBSERVED/MISSING split: this
    milestone only ever constructs ``MISSING`` expectations (there is no
    positive-evidence "test surface confirmed present and unweakened"
    case worth recording, since J's own companions already cover the
    positive case) -- the field is still reused as-is (never a parallel
    two-value enum) so any future caller can treat every Intelligence
    package's candidates uniformly."""

    #: See the identical note on ``TestSurface`` above.
    __test__: ClassVar[bool] = False

    change_unit_id: str
    source_qualified_name: str
    source_file_path: str
    reason_code: TestExpectationReasonCode
    reason: str
    evidence: TestEvidence
    status: CompanionStatus


@dataclass(frozen=True, slots=True)
class PotentialTestGap:
    """One evidence-backed candidate surfaced to review, never a finding
    on its own. Constructed 1:1 from a ``MISSING`` :class:`TestExpectation`
    -- never a second, independently-derived object (spec: no duplicate
    candidate models)."""

    change_unit_id: str
    expectation: TestExpectation


@dataclass(frozen=True, slots=True)
class TestIntelligenceReport:
    """The complete, deterministic output for one review run. Never
    itself sent to an LLM in bulk -- only small, bounded per-candidate
    slices are (see
    :func:`patchfrog.test_intelligence.evidence.evidence_text_for_candidate`)."""

    #: See the identical note on ``TestSurface`` above.
    __test__: ClassVar[bool] = False

    version: int
    expectations: tuple[TestExpectation, ...]
    gaps: tuple[PotentialTestGap, ...]
    test_story: str

    @property
    def gap_count(self) -> int:
        return len(self.gaps)

    @property
    def reason_code_counts(self) -> dict[TestExpectationReasonCode, int]:
        counts: dict[TestExpectationReasonCode, int] = {}
        for gap in self.gaps:
            counts[gap.expectation.reason_code] = counts.get(gap.expectation.reason_code, 0) + 1
        return counts
