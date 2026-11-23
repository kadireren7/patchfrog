"""Architecture boundary enforcement -- Milestone 1.2 (ADR-012).

Rules R1-R4 from ``docs/architecture/BOUNDARIES.md`` against the real
repository. Static AST analysis only (``tests/architecture/import_graph.py``);
nothing under analysis is imported. R5 (no import-time server side effects
from ``patchfrog.cli``) lives in ``tests/unit/test_cli_local_startup.py``.

When a test here fails, its message names the exact import (file:line,
function, kind) and what to do. The historical cycle baseline
(``dependency_baseline.json``) may only shrink without review; see
BOUNDARIES.md section 6 for the safe update procedure.
"""

from __future__ import annotations

from tests.architecture.import_graph import load_baseline, repository_graph
from tests.architecture.rules import CORE_ROOTS, check_apps_imports, check_core_purity, check_cycles


def _report(violations: list[str]) -> str:
    return f"{len(violations)} architecture violation(s):\n  " + "\n  ".join(violations)


def test_r1_core_import_closures_stay_pure() -> None:
    violations = check_core_purity(repository_graph(), CORE_ROOTS)
    assert violations == [], _report(violations)


def test_r2_r3_no_new_cycles_and_baseline_is_current() -> None:
    violations = check_cycles(repository_graph(), load_baseline())
    assert violations == [], _report(violations)


def test_r4_library_modules_do_not_import_apps_outside_reviewed_exceptions() -> None:
    violations = check_apps_imports(repository_graph(), load_baseline())
    assert violations == [], _report(violations)


def test_baseline_document_is_well_formed() -> None:
    baseline = load_baseline()

    assert baseline["schema_version"] == 1
    assert baseline["edge_kinds"] == ["lazy", "module", "type_checking"]
    for component in baseline["historical_sccs"]:
        assert component["members"] == sorted(set(component["members"])), "members must be sorted and unique"
        endpoints: set[str] = set()
        for key in component["edges"]:
            source, _, target = key.partition(" -> ")
            assert source in component["members"] and target in component["members"], key
            endpoints.update((source, target))
        assert endpoints == set(component["members"]), "every member must appear in an intra-SCC edge"
        assert list(component["edges"]) == sorted(component["edges"]), "edges must be sorted"
    keys = [(e["importer"], e["function"], e["target"]) for e in baseline["apps_lazy_import_exceptions"]]
    assert keys == sorted(set(keys)), "exceptions must be sorted and unique"
