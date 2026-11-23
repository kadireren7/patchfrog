"""Mutation proofs: the boundary rules detect realistic violations.

Every mutation is applied **in memory** to the real repository graph
(``build_graph(overrides=...)``) or to an in-memory copy of the baseline.
No production file is ever modified. The checks are the exact functions
the production boundary tests use (``tests/architecture/rules.py``).
"""

from __future__ import annotations

import copy

import pytest

from tests.architecture.import_graph import (
    REPO_ROOT,
    Baseline,
    ModuleGraph,
    build_graph,
    load_baseline,
    repository_graph,
    strongly_connected_components,
    unit_edges,
)
from tests.architecture.rules import CORE_ROOTS, check_apps_imports, check_core_purity, check_cycles


def _mutated(path: str, transform: str | None = None, *, append: str = "", replace: tuple[str, str] | None = None) -> ModuleGraph:
    source = (REPO_ROOT / path).read_text() if transform is None else transform
    if replace is not None:
        old, new = replace
        assert old in source, f"mutation anchor not found in {path}: {old!r}"
        source = source.replace(old, new)
    return build_graph(REPO_ROOT, overrides={path: source + append})


@pytest.fixture(scope="module")
def baseline() -> Baseline:
    return load_baseline()


def test_production_tree_is_clean_before_mutation(baseline: Baseline) -> None:
    graph = repository_graph()
    assert check_core_purity(graph, CORE_ROOTS) == []
    assert check_cycles(graph, baseline) == []
    assert check_apps_imports(graph, baseline) == []


# 1. Forbidden persistence import in patchfrog.config.core -------------------------


@pytest.mark.parametrize(
    ("snippet", "kind"),
    [
        ("\nimport patchfrog.persistence.database\n", "module"),
        ("\ndef _later() -> None:\n    from patchfrog.persistence import database  # noqa\n", "lazy"),
        ("\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import patchfrog.persistence.database\n", "type_checking"),
    ],
)
def test_m1_persistence_import_in_config_core_is_detected(snippet: str, kind: str) -> None:
    graph = _mutated("patchfrog/config/core.py", append=snippet)

    violations = check_core_purity(graph, ["patchfrog.config.core"])

    assert violations, "R1 must flag persistence reached from patchfrog.config.core"
    message = violations[0]
    assert "core root patchfrog.config.core reaches forbidden module patchfrog.persistence" in message
    assert "patchfrog/config/core.py:" in message and f"[{kind}]" in message


def test_m1b_transitive_forbidden_library_is_reported_with_its_chain() -> None:
    # A core module importing a helper that imports sqlalchemy: the chain
    # must show both hops so the failure is actionable.
    graph = build_graph(
        REPO_ROOT,
        overrides={
            "patchfrog/config/core.py": (REPO_ROOT / "patchfrog/config/core.py").read_text()
            + "\nimport patchfrog.config._mutation_helper\n",
            "patchfrog/config/_mutation_helper.py": "import sqlalchemy\n",
        },
    )

    violations = check_core_purity(graph, ["patchfrog.config.core"])

    assert any("forbidden library sqlalchemy" in v for v in violations), violations
    chain = next(v for v in violations if "sqlalchemy" in v)
    assert "patchfrog.config.core -> patchfrog.config._mutation_helper" in chain
    assert "patchfrog.config._mutation_helper -> sqlalchemy" in chain


def test_m1c_non_allowlisted_library_needs_an_adr() -> None:
    graph = _mutated("patchfrog/diff/__init__.py", append="\nimport numpy\n")
    violations = check_core_purity(graph, ["patchfrog.diff"])
    assert any("non-allowlisted (needs an ADR) library numpy" in v for v in violations), violations


# 2. New dependency cycles -----------------------------------------------------------


def test_m2_new_cycle_between_two_acyclic_packages_is_detected(baseline: Baseline) -> None:
    # change_risk -> diff exists today; diff -> change_risk closes a new cycle.
    graph = _mutated("patchfrog/diff/__init__.py", append="\nimport patchfrog.change_risk\n")

    violations = check_cycles(graph, baseline)

    assert any("new dependency cycle among ['patchfrog.change_risk', 'patchfrog.diff']" in v for v in violations), violations
    message = next(v for v in violations if "new dependency cycle" in v)
    assert "patchfrog/diff/__init__.py:" in message and "example cycle:" in message


def test_m2b_package_joining_the_historical_scc_is_detected(baseline: Baseline) -> None:
    # analysis -> parsing exists; parsing -> persistence pulls parsing into the SCC.
    graph = _mutated("patchfrog/parsing/detect.py", append="\nimport patchfrog.persistence.database\n")

    violations = check_cycles(graph, baseline)

    assert any("unit(s) ['patchfrog.parsing'] joined the historical SCC" in v for v in violations), violations


def test_m2c_new_edge_inside_the_historical_scc_is_detected(baseline: Baseline) -> None:
    graph = repository_graph()
    edges = unit_edges(graph)
    (scc,) = strongly_connected_components(edges)
    source, target = next((s, t) for s in sorted(scc) for t in sorted(scc) if s != t and (s, t) not in edges)
    source_path = next(path for module, path in graph.paths.items() if module == source)
    target_module = next(module for module in sorted(graph.paths) if module == target)

    mutated = _mutated(source_path, append=f"\nimport {target_module}\n")
    violations = check_cycles(mutated, baseline)

    assert any(f"new edge inside the historical SCC: {source} -> {target}" in v for v in violations), violations


# 3. Forbidden apps.* imports -------------------------------------------------------


def test_m3_module_level_apps_import_in_a_library_module_is_detected(baseline: Baseline) -> None:
    graph = _mutated("patchfrog/diff/__init__.py", append="\nimport apps.worker.celery_app\n")

    violations = check_apps_imports(graph, baseline)

    assert any(
        "R4: library module imports apps.worker.celery_app at module level" in v and "patchfrog/diff/__init__.py:" in v
        for v in violations
    ), violations


def test_m3b_new_function_local_apps_import_needs_review(baseline: Baseline) -> None:
    graph = _mutated(
        "patchfrog/diff/__init__.py",
        append="\ndef _enqueue() -> None:\n    from apps.worker.celery_app import celery_app  # noqa\n",
    )

    violations = check_apps_imports(graph, baseline)

    assert any("new function-local apps.* import needs architectural review" in v and "_enqueue()" in v for v in violations), violations


# 4. Obsolete baseline entries (shrink-only) ------------------------------------------


def test_m4_obsolete_baseline_edge_must_be_removed(baseline: Baseline) -> None:
    stale = copy.deepcopy(baseline)
    stale["historical_sccs"][0]["edges"]["patchfrog.diff -> patchfrog.review"] = "historical debt"

    violations = check_cycles(repository_graph(), stale)

    assert any(
        "R3: baseline edge patchfrog.diff -> patchfrog.review is no longer part of a dependency cycle" in v for v in violations
    ), violations


def test_m4b_removing_real_debt_forces_the_baseline_to_shrink(baseline: Baseline) -> None:
    # Remove config -> review (the only edge keeping patchfrog.config in the SCC).
    source = (REPO_ROOT / "patchfrog/config/settings.py").read_text()
    for line in (
        "from patchfrog.review.cost_policy import ReviewStrategy, parse_tier_budgets\n",
        "from patchfrog.review.critic_policy import CriticFailurePolicy\n",
    ):
        assert line in source
        source = source.replace(line, "")
    graph = build_graph(REPO_ROOT, overrides={"patchfrog/config/settings.py": source})

    violations = check_cycles(graph, baseline)

    assert any("R3: patchfrog.config is no longer in any dependency cycle" in v for v in violations), violations
    assert any("R3: baseline edge patchfrog.config -> patchfrog.review is no longer part" in v for v in violations)
    assert not any(v.startswith("R2") for v in violations), "removing debt must never look like growth"


def test_m4c_obsolete_apps_exception_must_be_removed(baseline: Baseline) -> None:
    graph = _mutated(
        "patchfrog/cli.py",
        replace=("    from apps.worker.tasks.run_review_pipeline import run_review_pipeline_task\n", ""),
    )

    violations = check_apps_imports(graph, baseline)

    assert any(
        "baseline exception patchfrog.cli:_ops_retry_async() -> apps.worker.tasks.run_review_pipeline no longer exists" in v
        for v in violations
    ), violations


def test_m4d_unjustified_baseline_entries_are_rejected(baseline: Baseline) -> None:
    todo = copy.deepcopy(baseline)
    first_edge = next(iter(todo["historical_sccs"][0]["edges"]))
    todo["historical_sccs"][0]["edges"][first_edge] = "TODO: justify this new dependency"
    todo["apps_lazy_import_exceptions"][0]["reason"] = ""

    assert any("has no reviewed justification" in v for v in check_cycles(repository_graph(), todo))
    assert any("has no reviewed justification" in v for v in check_apps_imports(repository_graph(), todo))
