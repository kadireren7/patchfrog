"""Deterministic derivation of :class:`~patchfrog.test_intelligence.domain.TestExpectation`\\ s
-- the two genuinely new structural signals (spec section 1's audit;
see ``validation/test_intelligence/latest-summary.md`` section 1 for
the full reuse/dedup rationale).

Entirely synchronous and session-free: every input is already-computed,
in-memory evidence (:class:`~patchfrog.change_intelligence.domain.ChangeUnit`\\ s,
:class:`~patchfrog.change_intelligence.domain.ExpectedCompanionChange`\\ s,
and the PR's own already-parsed :class:`~patchfrog.diff.models.DiffFile`\\ s)
-- no repository-graph query of its own, no base-commit fetch, zero LLM
calls.
"""

from __future__ import annotations

import re

from patchfrog.change_intelligence.domain import (
    ChangeKind,
    ChangeUnit,
    CompanionReasonCode,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.diff.models import DiffFile, DiffLine
from patchfrog.test_intelligence.domain import (
    MAX_TEST_GAPS_PER_UNIT,
    PotentialTestGap,
    TestEvidence,
    TestExpectation,
    TestExpectationReasonCode,
    TestSurface,
)

#: Structural assertion-like markers -- checked against the start of a
#: (stripped) diff line, never a substring anywhere in it, so e.g. a
#: variable literally named ``self_assert_count`` never matches.
_ASSERT_RE = re.compile(r"^(assert\b|self\.assert\w*\(|pytest\.raises\(|with\s+(pytest\.)?raises\()")

#: Structural skip/xfail markers -- a substring match is fine here since
#: these are always call/decorator forms, never plausible as an
#: unrelated identifier prefix.
_SKIP_RE = re.compile(r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|pytest\.importorskip\(")


def derive_test_surfaces(
    *, changed_file_paths: frozenset[str], expected_companions: tuple[ExpectedCompanionChange, ...]
) -> dict[str, TestSurface]:
    """Cross-reference each changed file against J's own
    ``TEST_NOT_UPDATED`` companions -- the *only* place "was a test file
    ever discoverable for this file" is answered, never a new query."""

    known_by_file: dict[str, list[str]] = {path: [] for path in changed_file_paths}
    for companion in expected_companions:
        if companion.reason_code is not CompanionReasonCode.TEST_NOT_UPDATED:
            continue
        if companion.source_file_path not in known_by_file:
            continue
        known_by_file[companion.source_file_path].append(companion.expected_file_path)

    return {
        path: TestSurface(file_path=path, known_test_file_paths=tuple(sorted(set(known))))
        for path, known in known_by_file.items()
    }


def derive_no_test_surface_expectations(
    *, change_units: tuple[ChangeUnit, ...], expected_companions: tuple[ExpectedCompanionChange, ...]
) -> tuple[TestExpectation, ...]:
    """One expectation per changed file in a BEHAVIOR-kind ChangeUnit
    that has zero discoverable test file at all (see the audit's "Scope
    restriction" section for why the unit-kind gate is BEHAVIOR-only)."""

    out: list[TestExpectation] = []
    for unit in change_units:
        if unit.change_kind is not ChangeKind.BEHAVIOR:
            continue

        eligible_files: dict[str, str] = {}  # file_path -> a representative qualified_name
        for candidate in unit.changed_candidates:
            if candidate.symbol_id is None or candidate.qualified_name is None:
                continue
            eligible_files.setdefault(candidate.file_path, candidate.qualified_name)
        if not eligible_files:
            continue

        surfaces = derive_test_surfaces(
            changed_file_paths=frozenset(eligible_files), expected_companions=expected_companions
        )
        for file_path, qualified_name in sorted(eligible_files.items()):
            surface = surfaces[file_path]
            if surface.discovered:
                continue  # J already found a real (even if stale) test link
            out.append(
                TestExpectation(
                    change_unit_id=unit.id,
                    source_qualified_name=qualified_name,
                    source_file_path=file_path,
                    reason_code=TestExpectationReasonCode.NO_TEST_SURFACE_FOUND,
                    reason=(
                        f"{qualified_name!r} is a changed behavioral symbol with no discoverable "
                        "test file (no file_tests_file graph edge found)"
                    ),
                    evidence=TestEvidence(
                        reason_code=TestExpectationReasonCode.NO_TEST_SURFACE_FOUND,
                        bounded_text=f"no likely test file found for {file_path!r}",
                    ),
                    status=CompanionStatus.MISSING,
                )
            )
    return tuple(out)


def _count_markers(lines: list[DiffLine]) -> tuple[int, int]:
    assert_count = 0
    skip_count = 0
    for line in lines:
        stripped = line.content.strip()
        if _ASSERT_RE.search(stripped):
            assert_count += 1
        if _SKIP_RE.search(stripped):
            skip_count += 1
    return assert_count, skip_count


def derive_weakened_test_expectations(
    *, expected_companions: tuple[ExpectedCompanionChange, ...], diff_files: tuple[DiffFile, ...]
) -> tuple[TestExpectation, ...]:
    """One expectation per genuinely-touched test file whose structural
    assertion signal weakened -- net assertion markers decreased, or a
    skip/xfail marker was newly added. See the audit's "Which structural
    markers count as 'weakened'" section for exactly why removing a
    skip marker never flags (that is strengthening).

    **Anchored to a real, same-PR production change** (spec's own
    "Test Intelligence is not an inverse feature detector" requirement,
    and the mandatory test-only negative case): a test file is only
    ever eligible here when J's own companions already confirm, via a
    real ``TEST_NOT_UPDATED`` companion, that it is linked to a changed
    *production* file. Companions are only ever derived from a changed
    *production* candidate's own test-file lookup
    (:func:`patchfrog.change_intelligence.companions._test_staleness`
    iterates the production side, never the reverse) -- a PR that
    touches only test files therefore produces zero such companions of
    any status, so this signal structurally cannot fire without a
    real, same-PR production change. See
    ``validation/test_intelligence/latest-summary.md`` section 1
    ("Test-only PRs stay quiet") for the full proof.

    Deliberately does **not** filter on ``companion.status is OBSERVED``:
    that status is itself derived from ``all_changed_file_paths``, a
    set built from the *candidates* generated for this diff -- and
    candidate generation is added-lines-only
    (:func:`patchfrog.review.candidates._extract_added_lines`), so a
    test file whose only change is a pure deletion never produces a
    candidate and is therefore reported ``MISSING`` by J even though it
    really was touched. Whether the test file was genuinely touched is
    instead answered directly and more precisely by real membership in
    ``diff_files`` (the same already-parsed diff every review run
    builds) -- the companion is used *only* to establish the
    correlation to a changed production file, never to gate on
    "touched"."""

    diff_by_path = {d.path: d for d in diff_files}
    out: list[TestExpectation] = []
    seen_files: set[str] = set()

    for companion in expected_companions:
        if companion.reason_code is not CompanionReasonCode.TEST_NOT_UPDATED:
            continue
        file_path = companion.expected_file_path
        if file_path in seen_files:
            continue
        seen_files.add(file_path)

        diff_file = diff_by_path.get(file_path)
        if diff_file is None:
            continue  # not present in this diff at all -- genuinely untouched, not this signal's concern

        added_assert, added_skip = _count_markers(diff_file.added_lines)
        removed_assert, removed_skip = _count_markers(diff_file.deleted_lines)
        net_assert = added_assert - removed_assert
        net_skip = added_skip - removed_skip
        if net_assert >= 0 and net_skip <= 0:
            continue

        evidence_parts: list[str] = []
        if net_assert < 0:
            evidence_parts.append(f"assertion markers: {removed_assert} removed, {added_assert} added")
        if net_skip > 0:
            evidence_parts.append(f"skip/xfail markers newly added: {net_skip}")

        out.append(
            TestExpectation(
                change_unit_id=companion.change_unit_id,
                source_qualified_name=companion.expected_qualified_name,
                source_file_path=file_path,
                reason_code=TestExpectationReasonCode.TEST_TOUCHED_BUT_WEAKENED,
                reason=(
                    f"{file_path!r} is linked to changed production symbol "
                    f"{companion.source_qualified_name!r} but its structural test signal weakened"
                ),
                evidence=TestEvidence(
                    reason_code=TestExpectationReasonCode.TEST_TOUCHED_BUT_WEAKENED,
                    bounded_text="; ".join(evidence_parts),
                ),
                status=CompanionStatus.MISSING,
            )
        )
    return tuple(out)


def derive_gaps(expectations: tuple[TestExpectation, ...]) -> tuple[PotentialTestGap, ...]:
    counts: dict[str, int] = {}
    gaps: list[PotentialTestGap] = []
    for expectation in expectations:
        if expectation.status is not CompanionStatus.MISSING:
            continue
        count = counts.get(expectation.change_unit_id, 0)
        if count >= MAX_TEST_GAPS_PER_UNIT:
            continue
        counts[expectation.change_unit_id] = count + 1
        gaps.append(PotentialTestGap(change_unit_id=expectation.change_unit_id, expectation=expectation))
    return tuple(gaps)
