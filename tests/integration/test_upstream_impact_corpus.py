"""M6 end-to-end corpus: contract diff -> classification -> consumers ->
blast radius, against real fixture repositories (real discovery, real
code graph -- no hand-built domain objects)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from patchfrog.upstream.workspace import RepositoryImpact, analyze_repository
from tests.support.upstream_cases import CASES_ROOT, UpstreamCase, all_case_dirs, load_case

CASE_DIRS = all_case_dirs()


def test_corpus_covers_the_required_cases() -> None:
    names = {p.name for p in CASE_DIRS}
    assert {
        "openai_sdk_method_rename", "stripe_parameter_rename", "github_endpoint_replacement",
        "required_parameter_added", "response_schema_change", "auth_requirement_changed",
        "generic_openapi_internal_change", "package_major_bump_insufficient_evidence",
        "unaffected_consumer_trap", "multi_repo_dependency_usage", "demo",
    } <= names


def _analyze(case: UpstreamCase) -> dict[str, RepositoryImpact]:
    return {
        name: analyze_repository(root, case.event, hints=case.hints, repository=name)[0]
        for name, root in case.repositories.items()
    }


@pytest.mark.parametrize("case_dir", CASE_DIRS, ids=lambda p: p.name)
def test_case_matches_expectations(case_dir: Path) -> None:
    case = load_case(case_dir)
    expect = case.expect
    assert case.event.classification.risk.value == expect["risk"]
    assert sorted({i.kind.value for i in case.event.consumer_affecting_items}) == sorted(
        set(expect["consumer_affecting_kinds"])
    )
    for reason in expect.get("reasons_include", []):
        assert reason in case.event.classification.reasons

    results = _analyze(case)
    for name, wanted in expect["repositories"].items():
        impact = results[name]
        assert impact.status.value == wanted["status"], (name, impact.reason)
        direct = sorted({c.location for i in impact.consumer_impacts for c in i.direct})
        potential = sorted({c.location for i in impact.consumer_impacts for c in i.potential})
        ruled_out = sorted({s.location for i in impact.consumer_impacts for s in i.ruled_out_sites})
        assert direct == sorted(wanted["direct"]), name
        assert potential == sorted(wanted["potential"]), name
        assert ruled_out == sorted(wanted["ruled_out"]), name
        transitive = sorted({n.key for r in impact.blast_radii for n in r.transitive})
        assert transitive == sorted(wanted["transitive"]), name
        tests = sorted({t.file_path for r in impact.blast_radii for t in r.related_tests})
        assert tests == sorted(wanted["related_tests"]), name
        unaffected = {loc for i in impact.consumer_impacts for loc in i.unaffected_locations}
        for location in wanted.get("unaffected_includes", []):
            assert location in unaffected, (name, location)
        if "migration" in wanted:
            from patchfrog.dependencies.discovery import discover_dependencies
            from patchfrog.migration.planner import plan_migration
            from patchfrog.upstream.workspace import adapters_for_target

            inventory = discover_dependencies(case.repositories[name], repository=name,
                                              adapters=adapters_for_target(case.event.target))
            plan = plan_migration(case.event, impact, inventory, hints=case.hints)
            assert plan.status.value == wanted["migration"]["plan_status"], name
            assert plan.residual_risk.value == wanted["migration"]["residual_risk"], name
            steps = sorted(f"{s.eligibility.value} {s.strategy.value} {s.target.location}" for s in plan.steps)
            assert steps == sorted(wanted["migration"]["steps"]), name
        if "files_without_call_graph" in wanted:
            assert sorted({f for r in impact.blast_radii for f in r.files_without_call_graph}) == sorted(
                wanted["files_without_call_graph"]
            )


def test_demo_blast_radius_numbers_and_edges() -> None:
    case = load_case(CASES_ROOT / "demo")
    impact = _analyze(case)["demo"]
    (radius,) = impact.blast_radii
    assert radius.summary() == {
        "direct_sites": 2, "direct": 2, "transitive": 3, "potential": 0, "related_tests": 2, "modules": 3, "files": 4,
    }
    depths = {n.key: (n.depth, n.confidence.value) for n in radius.transitive}
    assert depths["app/api/routes.py::reply_route"] == (1, "medium")
    assert depths["app/api/routes.py::bulk_reply"] == (2, "low")  # confidence decays per hop
    assert all(e.relation in ("uses_changed_contract", "called_by") for e in radius.edges)
    assert radius.graph_languages == ("python",)


def test_workspace_classifies_repositories() -> None:
    from patchfrog.upstream.workspace import analyze_workspace

    case = load_case(CASES_ROOT / "multi_repo_dependency_usage")
    workspace = analyze_workspace([(root, name) for name, root in case.repositories.items()], case.event,
                                  hints=case.hints)
    assert [r.repository for r in workspace.affected] == ["checkout-service"]
    assert [r.repository for r in workspace.uncertain] == ["billing-worker"]
    assert sorted(r.repository for r in workspace.unaffected) == ["crm-service", "docs-site"]
    reasons = {r.repository: r.reason for r in workspace.unaffected}
    assert reasons["docs-site"] == "dependency not used"
    assert "no usage reaches" in reasons["crm-service"]


def test_version_only_event_makes_every_consumer_uncertain_not_affected() -> None:
    from patchfrog.dependencies.domain import Ecosystem
    from patchfrog.upstream.domain import DependencyTarget
    from patchfrog.upstream.events import build_version_change
    from patchfrog.upstream.workspace import analyze_workspace

    case = load_case(CASES_ROOT / "multi_repo_dependency_usage")
    event = build_version_change(DependencyTarget(ecosystem=Ecosystem.PYPI, package_name="stripe"), "12.3.0", "13.0.0")
    workspace = analyze_workspace([(root, name) for name, root in case.repositories.items()], event)
    assert sorted(r.repository for r in workspace.uncertain) == ["billing-worker", "checkout-service", "crm-service"]
    assert [r.repository for r in workspace.unaffected] == ["docs-site"]
    assert not workspace.affected
    already = build_version_change(DependencyTarget(ecosystem=Ecosystem.PYPI, package_name="stripe"), "12.0.0", "12.3.0")
    result = analyze_workspace([(case.repositories["crm-service"], "crm-service")], already)
    assert result.unaffected[0].reason == "already on the target version"


# -- structural invariants ---------------------------------------------------------

_CORE = Path(__file__).resolve().parents[2] / "patchfrog" / "upstream"


def test_upstream_package_never_imports_a_provider() -> None:
    for path in _CORE.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("patchfrog.review", "patchfrog.routing", "anthropic", "openai",
                                                   "google")), f"{path.name} imports {node.module}"
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] in ("anthropic", "openai", "google") for a in node.names)


def _code_strings(tree: ast.AST) -> set[str]:
    """String literals and identifiers used in code (docstrings and
    comments excluded -- they may carry illustrative examples)."""

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found.add(node.value.lower())
        elif isinstance(node, ast.Name):
            found.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            found.add(node.attr.lower())
    return found


def test_core_contains_no_fixture_specific_strings() -> None:
    forbidden = ("acme", "paylane", "legacy-stats", "payment_method_types", "chat.create", "responses.create",
                 "orders.internal", "inventory.internal", "checkout.sessions", "stripe", "openai")
    for path in _CORE.glob("*.py"):
        for value in _code_strings(ast.parse(path.read_text())):
            for word in forbidden:
                assert word not in value, f"{path.name} special-cases fixture string {word!r}: {value!r}"
