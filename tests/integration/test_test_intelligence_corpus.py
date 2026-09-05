"""Controlled corpus for Test Intelligence (spec section 31, minimum 18
behavioral scenarios) -- real git repository, real indexing, real
diff-driven :class:`~patchfrog.review.domain.ReviewCandidate`
generation, real
:func:`~patchfrog.change_intelligence.service.build_change_intelligence_report`
for real `ChangeUnit`s, then real
:func:`~patchfrog.test_intelligence.service.build_test_intelligence_report`.
Zero LLM involvement anywhere -- ground truth is always what a real git
diff against a real fixture repository actually produced, never
FakeLLM output.

Each case is a real, independent commit against a shared base fixture
repository, with explicit ground truth recorded directly in the test.

**Accounting** (see ``validation/test_intelligence/latest-summary.md``
section 8 for the full matrix against the spec's 18 named scenarios):
this file contains **21 behavioral corpus scenarios** (numbered
1-21 in the section comments below, including the mandatory test-only
negative cases and the exact-head stale-gap regression) plus **3
supporting integration/structural tests** (real `review_local` pipeline
persistence, telemetry/versioning round trip, a structural
zero-`AsyncSession`-import proof) that are deliberately **not** counted
toward the behavioral total. One spec matrix item --
"negative/error-path test missing" (does the test surface exercise a
*specific* code path, e.g. an error branch) -- is explicitly **DEFERRED**:
this milestone's signals operate at file-existence and gross
assertion-count granularity, never per-branch/per-path coverage, so
"was the error path tested" cannot be answered without a semantic/
path-coverage analysis this milestone does not (and should not, per its
own non-goals) attempt. Marked DEFERRED, never implied as passing.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.change_intelligence.domain import (
    ChangeIntelligenceReport,
    ChangeKind,
    CompanionReasonCode,
    CompanionStatus,
)
from patchfrog.change_intelligence.service import build_change_intelligence_report
from patchfrog.contract_intelligence.service import build_contract_intelligence_report
from patchfrog.diff.models import DiffFile
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.intent_verification.service import build_intent_verification_report
from patchfrog.persistence.repositories import RepositoryIndexRepository, RepositoryRepository
from patchfrog.review.candidates import ReviewCandidateGenerator
from patchfrog.review.domain import ReviewCandidate
from patchfrog.review.local_diff import diff_against_base
from patchfrog.test_intelligence.domain import TestExpectationReasonCode
from patchfrog.test_intelligence.service import build_test_intelligence_report
from tests.support.git_repo import commit_all, init_git_repo

_README = "# scratch repo\n"


async def _make_repo(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=0,
        )
        await session.commit()
        return repo.id


async def _index_and_group(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    root: Path,
    full_name: str,
    base_sha: str,
) -> tuple[ChangeIntelligenceReport, list[DiffFile]]:
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    diff_files = diff_against_base(root, base_sha)
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        candidates: list[ReviewCandidate] = list(
            await ReviewCandidateGenerator().generate(
                session, repository_index_id=index.id, diff_files=diff_files, static_findings=[], max_candidates=40,
            )
        )
        change_report = await build_change_intelligence_report(session, candidates=candidates)
    return change_report, diff_files


def _setup_base(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text(_README)
    init_git_repo(root)
    return root


# ---- 1. NO_TEST_SURFACE_FOUND: genuinely new, entirely untested behavior ----


async def test_case_no_test_surface_found_for_new_untested_behavior(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-no-surface"
    root = _setup_base(tmp_path)
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "add loyalty discount pricing rule")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert gap.expectation.reason_code is TestExpectationReasonCode.NO_TEST_SURFACE_FOUND
    assert gap.expectation.source_file_path == "pricing.py"


# ---- 2. Dedup: J already found a (stale) test link -> never re-flagged ----


async def test_case_existing_test_file_missing_dedup(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-existing-missing"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n"
    )
    base_sha = commit_all(root, "base with existing test")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "add loyalty discount pricing rule, forgot the test")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    assert any(
        c.reason_code is CompanionReasonCode.TEST_NOT_UPDATED and c.status is CompanionStatus.MISSING
        for c in change_report.expected_companions
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert report.gaps == ()  # J's own TEST_NOT_UPDATED already covers this -- never duplicated


# ---- 3. Existing test touched normally, no weakening -> completely clean ----


async def test_case_existing_test_touched_healthy_no_gap(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-healthy"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n"
    )
    base_sha = commit_all(root, "base with existing test")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n\n\n"
        "def test_apply_discount_loyalty():\n"
        "    assert apply_discount({'total': 100, 'loyalty_years': 6}) == 90\n"
    )
    commit_all(root, "add loyalty discount pricing rule, with a test")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert report.gaps == ()


# ---- 4. TEST_TOUCHED_BUT_WEAKENED: production changed + related test weakened ----


async def test_case_weakened_assertions_removed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The anchored positive case: apply_discount (production) AND its
    linked test_pricing.py both change in the same PR -- a real OBSERVED
    TEST_NOT_UPDATED companion exists, so the weakened assertion is
    correctly attributed to that companion's own ChangeUnit."""

    full_name = "test/ti-weakened-assert"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    result = apply_discount({'total': 100})\n"
        "    assert result == 100\n"
        "    assert isinstance(result, int)\n"
    )
    base_sha = commit_all(root, "base with a thorough test")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    result = apply_discount({'total': 100})\n"
        "    assert result == 100\n"
    )
    commit_all(root, "add loyalty discount; simplify test_apply_discount, dropping the type assertion")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    # A real TEST_NOT_UPDATED companion names test_pricing.py -- the
    # correlation this signal anchors on. Its own status happens to be
    # MISSING here (test_pricing.py's edit is a pure deletion, so it
    # never produced a ReviewCandidate and is therefore absent from
    # J's own all_changed_file_paths accounting) -- irrelevant to this
    # milestone's own, more precise "genuinely in diff_files" touch
    # check (see expectations.derive_weakened_test_expectations).
    assert any(
        c.reason_code is CompanionReasonCode.TEST_NOT_UPDATED and c.expected_file_path == "test_pricing.py"
        for c in change_report.expected_companions
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert len(report.gaps) == 1
    assert report.gaps[0].expectation.reason_code is TestExpectationReasonCode.TEST_TOUCHED_BUT_WEAKENED
    assert report.gaps[0].expectation.source_file_path == "test_pricing.py"


# ---- 4a. MANDATORY NEGATIVE: test-only assertion removal, no production change -> quiet ----


async def test_case_test_only_assertion_removal_stays_quiet(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The mandatory test-only negative case (spec's own "Test
    Intelligence is not an inverse feature detector" requirement):
    exactly the same test-side edit as the positive case above, but
    pricing.py itself never changes in this PR -- zero
    TEST_NOT_UPDATED companions exist at all (companions.py only ever
    looks up test files *from* a changed production candidate), so
    TEST_TOUCHED_BUT_WEAKENED structurally cannot fire."""

    full_name = "test/ti-test-only-assert"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    result = apply_discount({'total': 100})\n"
        "    assert result == 100\n"
        "    assert isinstance(result, int)\n"
    )
    base_sha = commit_all(root, "base with a thorough test")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    result = apply_discount({'total': 100})\n"
        "    assert result == 100\n"
    )
    commit_all(root, "simplify test_apply_discount, dropping the type assertion")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    assert not any(
        c.reason_code is CompanionReasonCode.TEST_NOT_UPDATED for c in change_report.expected_companions
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert report.gaps == ()


# ---- 5. Assertions strengthened (production+test present) -> never a gap ----


async def test_case_strengthened_assertions_no_gap(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-strengthened"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n"
    )
    base_sha = commit_all(root, "base with a thin test")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    result = apply_discount({'total': 100})\n"
        "    assert result == 100\n"
        "    assert isinstance(result, int)\n"
    )
    commit_all(root, "add loyalty discount; strengthen test_apply_discount with a type assertion")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert report.gaps == ()


# ---- 6. Only imports/mocks changed, assertion count neutral -> no gap ----


async def test_case_neutral_test_change_no_gap(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-neutral"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "import pricing\nfrom pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n"
    )
    base_sha = commit_all(root, "base with an unused import")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n"
    )
    commit_all(root, "add loyalty discount; remove unused import in test_pricing")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert report.gaps == ()


# ---- 7. A skip marker newly added (production changed too) -> flagged ----


async def test_case_skip_marker_added_flagged(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-skip-added"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n"
    )
    base_sha = commit_all(root, "base with a passing test")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    (root / "test_pricing.py").write_text(
        "import pytest\nfrom pricing import apply_discount\n\n\n@pytest.mark.skip(reason='flaky in CI')\n"
        "def test_apply_discount():\n    assert apply_discount({'total': 100}) == 100\n"
    )
    commit_all(root, "add loyalty discount; skip test_apply_discount, flaky in CI")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert len(report.gaps) == 1
    assert report.gaps[0].expectation.reason_code is TestExpectationReasonCode.TEST_TOUCHED_BUT_WEAKENED
    assert "skip" in report.gaps[0].expectation.evidence.bounded_text


# ---- 7a. MANDATORY NEGATIVE: test-only skip/xfail addition, no production change -> quiet ----


async def test_case_test_only_skip_marker_addition_stays_quiet(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-test-only-skip"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n"
    )
    base_sha = commit_all(root, "base with a passing test")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "test_pricing.py").write_text(
        "import pytest\nfrom pricing import apply_discount\n\n\n@pytest.mark.skip(reason='flaky in CI')\n"
        "def test_apply_discount():\n    assert apply_discount({'total': 100}) == 100\n"
    )
    commit_all(root, "skip test_apply_discount, flaky in CI")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    assert not any(
        c.reason_code is CompanionReasonCode.TEST_NOT_UPDATED for c in change_report.expected_companions
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert report.gaps == ()


# ---- 8. A skip marker removed (un-skip, production changed too) -> never flagged ----


async def test_case_skip_marker_removed_not_flagged(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-skip-removed"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "import pytest\nfrom pricing import apply_discount\n\n\n@pytest.mark.skip(reason='flaky in CI')\n"
        "def test_apply_discount():\n    assert apply_discount({'total': 100}) == 100\n"
    )
    base_sha = commit_all(root, "base with a skipped test")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n"
    )
    commit_all(root, "add loyalty discount; un-skip test_apply_discount, no longer flaky")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert report.gaps == ()


# ---- 8a. Precision check: an unrelated pre-existing test weakened while a
# DIFFERENT production file changes elsewhere -> the unrelated test stays quiet ----


async def test_case_unrelated_test_weakened_elsewhere_in_pr_stays_quiet(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Precision proof for the companion-anchor correlation: a real
    production change exists *somewhere* in the PR (shipping.py), and a
    wholly unrelated pre-existing test (test_pricing.py, testing
    pricing.py, which never changes) is independently weakened in the
    very same PR. The anchor is per-file (via the companion's own
    expected_file_path), never "any production change in the PR
    unlocks any touched test file" -- so the unrelated weakening never
    fires."""

    full_name = "test/ti-unrelated-weakened"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    result = apply_discount({'total': 100})\n"
        "    assert result == 100\n"
        "    assert isinstance(result, int)\n"
    )
    (root / "shipping.py").write_text("def apply_shipping(order):\n    return order['total'] + 5\n")
    base_sha = commit_all(root, "base: pricing + its test + an unrelated shipping module")
    repository_id = await _make_repo(session_factory, full_name)

    # shipping.py changes (a real, unrelated production change) --
    # pricing.py itself never changes, only its test does.
    (root / "shipping.py").write_text(
        "def apply_shipping(order):\n    if order.get('express'):\n        return order['total'] + 15\n"
        "    return order['total'] + 5\n"
    )
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    result = apply_discount({'total': 100})\n"
        "    assert result == 100\n"
    )
    commit_all(root, "add express shipping; unrelated: simplify test_apply_discount")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    # No companion names test_pricing.py at all: pricing.py itself never
    # changed, so companions.py's own production-side lookup never runs
    # for it.
    assert not any(
        c.expected_file_path == "test_pricing.py" for c in change_report.expected_companions
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert not any(g.expectation.source_file_path == "test_pricing.py" for g in report.gaps)


# ---- 9. CONTRACT-kind (real cross-file caller) -> scope restriction holds ----


async def test_case_contract_kind_function_never_flagged(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A real caller exists (classify_candidate -> CONTRACT, not
    BEHAVIOR) -- NO_TEST_SURFACE_FOUND is deliberately BEHAVIOR-kind-only
    (see the audit's "Scope restriction" section), so even a genuinely
    untested contract function is never flagged here -- that gap space
    belongs to K's own stale-consumer mechanism, not this milestone."""

    full_name = "test/ti-contract-kind"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "caller.py").write_text(
        "from pricing import apply_discount\n\n\ndef checkout(order):\n    return apply_discount(order)\n"
    )
    base_sha = commit_all(root, "base with a real caller")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "add loyalty discount, no test, has a real caller")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    assert any(u.change_kind is ChangeKind.CONTRACT for u in change_report.change_units)
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert not any(g.expectation.reason_code is TestExpectationReasonCode.NO_TEST_SURFACE_FOUND for g in report.gaps)


# ---- 10. CONFIGURATION-kind file -> never flagged ----


async def test_case_configuration_kind_file_never_flagged(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-config-kind"
    root = _setup_base(tmp_path)
    (root / "settings.py").write_text("DISCOUNT_RATE = 0.1\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "settings.py").write_text("DISCOUNT_RATE = 0.2\n")
    commit_all(root, "bump discount rate")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert report.gaps == ()


# ---- 11. MIXED unit (behavior + infra in one connected component) -> never flagged ----


async def test_case_mixed_unit_never_flagged(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-mixed-kind"
    root = _setup_base(tmp_path)
    (root / "docker").mkdir()
    (root / "docker" / "entrypoint.py").write_text(
        "def entrypoint():\n    return run()\n\n\ndef run():\n    return True\n"
    )
    base_sha = commit_all(root, "base infra entrypoint")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "docker" / "entrypoint.py").write_text(
        "def entrypoint():\n    return run(retries=3)\n\n\ndef run(retries=1):\n    return retries > 0\n"
    )
    commit_all(root, "add retries to docker entrypoint")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert not any(g.expectation.reason_code is TestExpectationReasonCode.NO_TEST_SURFACE_FOUND for g in report.gaps)


# ---- 12. Coexistence with a real K stale consumer ----


async def test_case_real_contract_stale_consumer_coexistence(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-contract-coexist"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "caller.py").write_text(
        "from pricing import apply_discount\n\n\ndef checkout(order):\n    return apply_discount(order)\n"
    )
    (root / "shipping.py").write_text("def apply_shipping(order):\n    return order['total'] + 5\n")
    base_sha = commit_all(root, "base with a real caller and an untested helper")
    repository_id = await _make_repo(session_factory, full_name)

    # Breaking signature change (new required parameter) -- caller.py is
    # NOT updated, a real K stale consumer.
    (root / "pricing.py").write_text("def apply_discount(order, rate):\n    return order['total'] * rate\n")
    # A second, wholly-unrelated genuinely-untested behavior change.
    (root / "shipping.py").write_text(
        "def apply_shipping(order):\n    if order.get('express'):\n        return order['total'] + 15\n"
        "    return order['total'] + 5\n"
    )
    commit_all(root, "require an explicit rate for apply_discount; add express shipping")

    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    diff_files = diff_against_base(root, base_sha)
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        candidates: list[ReviewCandidate] = list(
            await ReviewCandidateGenerator().generate(
                session, repository_index_id=index.id, diff_files=diff_files, static_findings=[], max_candidates=40,
            )
        )
        change_report = await build_change_intelligence_report(session, candidates=candidates)
        contract_report = await build_contract_intelligence_report(
            session, candidates=candidates, change_units=change_report.change_units,
            base_sha=base_sha, local=True, root_path=root,
        )

    assert contract_report.stale_consumers  # a real K stale consumer exists

    combined_companions = change_report.expected_companions + contract_report.stale_consumers
    report = build_test_intelligence_report(
        change_units=change_report.change_units, expected_companions=combined_companions, diff_files=tuple(diff_files)
    )

    # M's own signal (the untested shipping.py BEHAVIOR change) fires
    # independently, alongside (never instead of) K's own stale consumer.
    assert any(
        g.expectation.reason_code is TestExpectationReasonCode.NO_TEST_SURFACE_FOUND
        and g.expectation.source_file_path == "shipping.py"
        for g in report.gaps
    )


# ---- 13. Coexistence with a real L intent gap ----


async def test_case_real_intent_gap_coexistence(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-intent-coexist"
    root = _setup_base(tmp_path)
    (root / "retry_worker.py").write_text("def schedule_retry(request):\n    return True\n")
    (root / "pricing.py").write_text(
        "from retry_worker import schedule_retry\n\n\ndef apply_discount(order):\n"
        "    schedule_retry(order)\n    return order['total']\n"
    )
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "from retry_worker import schedule_retry\n\n\ndef apply_discount(order):\n"
        "    if order.get('id') in _seen:\n        return None\n"
        "    schedule_retry(order)\n    return order['total']\n\n\n_seen = set()\n"
    )
    commit_all(root, "prevent duplicate retry discount processing")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    intent_report = build_intent_verification_report(
        title="Prevent duplicate retry discount processing",
        body=None,
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )
    test_report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert len(intent_report.gaps) == 1  # schedule_retry never itself updated
    assert any(
        g.expectation.reason_code is TestExpectationReasonCode.NO_TEST_SURFACE_FOUND
        and g.expectation.source_file_path == "pricing.py"
        for g in test_report.gaps
    )


# ---- 14. INFRASTRUCTURE-kind file -> never flagged ----


async def test_case_infrastructure_kind_file_never_flagged(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-infra-kind"
    root = _setup_base(tmp_path)
    (root / "docker").mkdir()
    (root / "docker" / "healthcheck.py").write_text("def healthcheck():\n    return True\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "docker" / "healthcheck.py").write_text("def healthcheck():\n    return {'ok': True}\n")
    commit_all(root, "richer healthcheck payload")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert report.gaps == ()


# ---- 15. A brand-new test file added alongside the new behavior -> no gap ----


async def test_case_new_test_file_added_suppresses_gap(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-new-test-added"
    root = _setup_base(tmp_path)
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount_loyalty():\n"
        "    assert apply_discount({'total': 100, 'loyalty_years': 6}) == 90\n"
    )
    commit_all(root, "add loyalty discount pricing rule, with a new test file")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    assert any(
        c.reason_code is CompanionReasonCode.TEST_NOT_UPDATED and c.status is CompanionStatus.OBSERVED
        for c in change_report.expected_companions
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert report.gaps == ()


# ---- 16. Intent-mapped behavior + relevant test updated -> fully supported, no gaps ----


async def test_case_intent_mapped_behavior_with_test_updated_no_gap(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Companion case to test_case_real_intent_gap_coexistence: same
    intent-mapped behavior change, but this time a real test is also
    added for the changed function -- both L's IntentCoverage and M's
    own signal report clean/SUPPORTED, proving the positive path is
    equally exercised, not just the gap path."""

    full_name = "test/ti-intent-test-updated"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order.get('id') in _seen:\n        return None\n"
        "    return order['total']\n\n\n_seen = set()\n"
    )
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount_duplicate():\n"
        "    apply_discount({'id': 1, 'total': 100})\n"
        "    assert apply_discount({'id': 1, 'total': 100}) is None\n"
    )
    commit_all(root, "prevent duplicate discount processing")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    intent_report = build_intent_verification_report(
        title="Prevent duplicate discount processing",
        body=None,
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )
    test_report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert intent_report.gaps == ()
    assert test_report.gaps == ()


# ---- 17. Docs-only change -> zero gaps, no crash ----


async def test_case_docs_only_change_no_gap_no_crash(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ti-docs-only"
    root = _setup_base(tmp_path)
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "README.md").write_text("# scratch repo\n\nNow with real documentation.\n")
    commit_all(root, "document the scratch repo")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert report.gaps == ()


# ---- 18. Two unrelated ChangeUnits, each with its own untested behavior ----


async def test_case_two_unrelated_change_units_each_get_their_own_gap(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Two wholly independent BEHAVIOR-kind ChangeUnits (no shared
    files, no graph connection) in one PR, each genuinely untested --
    proves gaps are attributed per-unit/per-file, never merged or
    conflated into one candidate."""

    full_name = "test/ti-two-units"
    root = _setup_base(tmp_path)
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    (root / "shipping.py").write_text(
        "def apply_shipping(order):\n    if order.get('express'):\n        return order['total'] + 15\n"
        "    return order['total'] + 5\n"
    )
    commit_all(root, "add loyalty discount and express shipping, both untested")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    flagged_files = {g.expectation.source_file_path for g in report.gaps}
    assert flagged_files == {"pricing.py", "shipping.py"}
    change_unit_ids = {g.change_unit_id for g in report.gaps}
    assert len(change_unit_ids) == 2  # two distinct, unmerged ChangeUnits


# ---- 19. Large fan-out boundedness (real corpus) ----


async def test_case_large_fanout_bounded_per_unit(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """One production function, independently exercised by many
    separate test files (a real, if unusual, pattern -- e.g. a unit
    test, an integration test, and several edge-case-focused test
    files all covering the same function) -- all weakened in the same
    PR. Each linked test file produces its own
    ``ExpectedCompanionChange`` sharing the *same* ``change_unit_id``
    (all traced back to the one changed ``apply_discount`` candidate),
    so ``derive_gaps``'s ``MAX_TEST_GAPS_PER_UNIT`` bound is exercised
    against real, non-fabricated data -- proving the system does not
    flood the reviewer with one candidate per linked test file."""

    full_name = "test/ti-large-fanout"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    test_file_count = 7
    for i in range(test_file_count):
        (root / f"test_pricing_case_{i}.py").write_text(
            "from pricing import apply_discount\n\n\ndef test_apply_discount_case():\n"
            "    result = apply_discount({'total': 100})\n"
            "    assert result == 100\n"
            "    assert isinstance(result, int)\n"
        )
    base_sha = commit_all(root, "base: one function, many independent test files")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    for i in range(test_file_count):
        (root / f"test_pricing_case_{i}.py").write_text(
            "from pricing import apply_discount\n\n\ndef test_apply_discount_case():\n"
            "    result = apply_discount({'total': 100})\n"
            "    assert result == 100\n"
        )
    commit_all(root, "add loyalty discount; weaken every one of the linked test files")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    linked_companions = [
        c for c in change_report.expected_companions if c.reason_code is CompanionReasonCode.TEST_NOT_UPDATED
    ]
    assert len(linked_companions) == test_file_count
    assert len({c.change_unit_id for c in linked_companions}) == 1  # all trace back to the same unit

    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    from patchfrog.test_intelligence.domain import MAX_TEST_GAPS_PER_UNIT

    assert test_file_count > MAX_TEST_GAPS_PER_UNIT
    assert len(report.gaps) == MAX_TEST_GAPS_PER_UNIT  # bounded, not one per linked test file
    assert all(
        g.expectation.reason_code is TestExpectationReasonCode.TEST_TOUCHED_BUT_WEAKENED for g in report.gaps
    )


# ---- 20. A related test file is DELETED in the same commit -> treated as no surface ----


async def test_case_deleted_related_test_treated_as_no_surface(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """J's own graph reflects the *current* (head) checkout -- once the
    related test file is deleted, ``likely_tests_for_file`` finds no
    edge at all, so no TEST_NOT_UPDATED companion of any status exists.
    From this milestone's perspective that is indistinguishable from
    "no test file was ever found" -- NO_TEST_SURFACE_FOUND correctly
    fires, rather than the deletion silently producing no signal at
    all."""

    full_name = "test/ti-deleted-test"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n"
    )
    base_sha = commit_all(root, "base with existing test")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    (root / "test_pricing.py").unlink()
    commit_all(root, "add loyalty discount; delete its test file entirely")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    assert not any(
        c.reason_code is CompanionReasonCode.TEST_NOT_UPDATED for c in change_report.expected_companions
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )

    assert len(report.gaps) == 1
    assert report.gaps[0].expectation.reason_code is TestExpectationReasonCode.NO_TEST_SURFACE_FOUND
    assert report.gaps[0].expectation.source_file_path == "pricing.py"


# ---- 21. Stale gap disappears on a new exact head (real incremental regression) ----


async def test_case_stale_gap_disappears_on_new_exact_head(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Acceptance criterion: "stale test gaps disappear on new exact
    head." Head A introduces an untested behavior change (a real gap).
    Head B advances the *same* branch with a real test added for it,
    and Test Intelligence is recomputed from scratch against the new
    exact head -- via the real index/diff path, never by hand-editing a
    report. The previous gap must not survive into the new report."""

    full_name = "test/ti-stale-gap"
    root = _setup_base(tmp_path)
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    # Head A: untested behavior change -> a real gap.
    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "add loyalty discount pricing rule")

    change_report_a, diff_files_a = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report_a = build_test_intelligence_report(
        change_units=change_report_a.change_units,
        expected_companions=change_report_a.expected_companions,
        diff_files=tuple(diff_files_a),
    )
    assert len(report_a.gaps) == 1
    assert report_a.gaps[0].expectation.reason_code is TestExpectationReasonCode.NO_TEST_SURFACE_FOUND

    # Head B: same branch advances with a real test added -- recompute
    # against the new exact head from scratch (fresh index, fresh diff,
    # fresh report; never carrying report_a forward).
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount_loyalty():\n"
        "    assert apply_discount({'total': 100, 'loyalty_years': 6}) == 90\n"
    )
    commit_all(root, "add the missing test for the loyalty discount rule")

    change_report_b, diff_files_b = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report_b = build_test_intelligence_report(
        change_units=change_report_b.change_units,
        expected_companions=change_report_b.expected_companions,
        diff_files=tuple(diff_files_b),
    )

    assert report_b.gaps == ()  # the stale gap from head A does not survive
    assert not any(
        "pricing" in e.source_file_path and e.reason_code is TestExpectationReasonCode.NO_TEST_SURFACE_FOUND
        for e in report_b.expectations
    )


# ==================================================================
# Supporting integration/structural tests (NOT counted as behavioral
# corpus scenarios -- see validation/test_intelligence/latest-summary.md
# section 8's explicit accounting).
# ==================================================================


# ---- Real end-to-end review_local pipeline: persisted through to ReviewRunModel ----


async def test_case_review_local_pipeline_persists_test_intelligence(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Proves the wiring, not just the package: a real
    :meth:`PullRequestReviewService.review_local` run, with a scripted
    :class:`FakeLLMProvider` (no live LLM), actually computes and
    persists Test Intelligence -- mirrors
    ``test_intent_verification_review_pipeline.py``'s own end-to-end
    proof pattern exactly."""

    import json

    from sqlalchemy import select

    from patchfrog.persistence.models.review import ReviewRunModel
    from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
    from patchfrog.review.service import PullRequestReviewService

    full_name = "test/ti-pipeline"
    root = _setup_base(tmp_path)
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    head_sha = commit_all(root, "add loyalty discount pricing rule")

    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    diff_files = diff_against_base(root, base_sha)

    provider = FakeLLMProvider(response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []})))
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)
    await service.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name,
        commit_sha=head_sha, diff_files=diff_files, base_sha=base_sha,
    )

    async with session_factory() as session:
        run = (
            await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id))
        ).scalars().one()

    assert run.test_expectation_count == 1
    assert run.test_gap_candidate_count == 1
    assert run.test_coverage_summary_rendered is True
    assert "no_test_surface_found" in run.test_reason_code_counts
    assert "Test impact:" in (run.change_story or "")


# ---- Telemetry/versioning round trip on a real corpus-built report ----


async def test_case_telemetry_and_versioning_real_report(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from patchfrog.test_intelligence.domain import TEST_INTELLIGENCE_VERSION
    from patchfrog.test_intelligence.telemetry import summarize_for_persistence

    full_name = "test/ti-telemetry"
    root = _setup_base(tmp_path)
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "add loyalty discount pricing rule")

    change_report, diff_files = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_test_intelligence_report(
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
        diff_files=tuple(diff_files),
    )
    assert report.version == TEST_INTELLIGENCE_VERSION

    summary = summarize_for_persistence(report)
    assert summary.test_gap_candidate_count == len(report.gaps) == 1
    assert summary.test_coverage_summary_rendered is True
    assert summary.test_coverage_summary_text is not None
    assert "no_test_surface_found" in summary.test_reason_code_counts_json


# ---- Zero repository-graph queries: structurally synchronous/session-free ----


def test_test_intelligence_never_imports_a_session_type() -> None:
    """Structural proof (mirrors
    tests.integration.test_intent_verification_corpus's own
    ``test_intent_verification_never_calls_a_provider``): this package
    needs no new repository-graph query and no new I/O -- see the audit,
    ``validation/test_intelligence/latest-summary.md`` section 1."""

    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "patchfrog" / "test_intelligence"
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert "AsyncSession" not in (node.module or "") and not any(
                    alias.name == "AsyncSession" for alias in node.names
                ), f"{path} imports AsyncSession -- Test Intelligence must stay session-free"
