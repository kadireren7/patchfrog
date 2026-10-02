"""M8.3: deterministic targeted-test selection from real M6 BlastRadius
evidence -- never a filename-similarity guess."""

from __future__ import annotations

from patchfrog.dependencies.domain import DetectionConfidence
from patchfrog.migration.domain import (
    AutoFixEligibility,
    EditOperation,
    MigrationPlan,
    MigrationStatus,
    MigrationStep,
    MigrationStrategy,
    MigrationTarget,
    ResidualRisk,
)
from patchfrog.migration_verification.domain import MAX_TARGETED_TESTS_PER_PLAN, TestSelectionClass
from patchfrog.migration_verification.test_selection import select_targeted_tests
from patchfrog.upstream.blast_radius import BlastNode, BlastRadius, RelatedTest
from patchfrog.upstream.consumers import ImpactKind


def _automatic_plan() -> MigrationPlan:
    target = MigrationTarget("repo", "app/chat.py", "generate_reply", 3, "chat.create", "sdk_call", "python")
    step = MigrationStep(
        "s1", target, MigrationStrategy.RENAME_SYMBOL, AutoFixEligibility.AUTO_SAFE, (), (), "old", "new", "new",
        (), "", operation=EditOperation.of("rename_symbol"),
    )
    return MigrationPlan("fp", "repo", None, (), (step,), ResidualRisk.LOW, MigrationStatus.PLANNED)


def _radius(related_tests: tuple[RelatedTest, ...], *, with_direct_node: bool = True) -> BlastRadius:
    nodes = (
        (BlastNode("app/chat.py", "generate_reply", ImpactKind.DIRECT, DetectionConfidence.HIGH, 0, False, ()),)
        if with_direct_node else ()
    )
    return BlastRadius(
        repository="repo", dependency_key="acme-ai:pypi", nodes=nodes, edges=(), direct_site_keys=(),
        related_tests=related_tests, modules=(), direct_callees=(), files_without_call_graph=(), graph_languages=(),
    )


def test_direct_high_confidence_test_is_classified_direct() -> None:
    radius = _radius((RelatedTest("tests/test_chat.py", "app/chat.py::generate_reply",
                                  "test consumes the changed contract directly", DetectionConfidence.HIGH),))
    plan = _automatic_plan()
    selections = select_targeted_tests(plan, (radius,))
    assert len(selections) == 1
    assert selections[0].test_path == "tests/test_chat.py"
    assert selections[0].classification is TestSelectionClass.DIRECT


def test_medium_confidence_caller_test_is_transitive() -> None:
    radius = _radius((RelatedTest("tests/test_workers.py", "app/chat.py::generate_reply",
                                  "test calls an affected symbol", DetectionConfidence.MEDIUM),))
    selections = select_targeted_tests(_automatic_plan(), (radius,))
    assert selections[0].classification is TestSelectionClass.TRANSITIVE


def test_low_confidence_evidence_is_fallback_never_invented() -> None:
    radius = _radius((RelatedTest("tests/test_module.py", "app/chat.py",
                                  "tests app/chat.py (filename convention)", DetectionConfidence.LOW),))
    selections = select_targeted_tests(_automatic_plan(), (radius,))
    assert selections[0].classification is TestSelectionClass.FALLBACK


def test_no_related_tests_at_all_yields_no_selections() -> None:
    selections = select_targeted_tests(_automatic_plan(), (_radius(()),))
    assert selections == ()


def test_no_automatic_steps_yields_no_selections_even_with_evidence() -> None:
    target = MigrationTarget("repo", "app/x.py", None, None, "x", "sdk_call", "python")
    step = MigrationStep("s1", target, MigrationStrategy.MANUAL_REVIEW, AutoFixEligibility.HUMAN_REQUIRED, (), (),
                         "", "", "", (), "")
    plan = MigrationPlan("fp", "repo", None, (), (step,), ResidualRisk.HIGH, MigrationStatus.HUMAN_REQUIRED)
    radius = _radius((RelatedTest("tests/test_x.py", "app/x.py", "test consumes the changed contract directly",
                                  DetectionConfidence.HIGH),))
    assert select_targeted_tests(plan, (radius,)) == ()


def test_selection_is_bounded() -> None:
    related = tuple(
        RelatedTest(f"tests/test_{i}.py", "app/chat.py::generate_reply", "test calls an affected symbol",
                   DetectionConfidence.MEDIUM)
        for i in range(MAX_TARGETED_TESTS_PER_PLAN + 5)
    )
    selections = select_targeted_tests(_automatic_plan(), (_radius(related),))
    assert len(selections) == MAX_TARGETED_TESTS_PER_PLAN
