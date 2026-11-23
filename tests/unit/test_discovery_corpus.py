"""Discovery evaluation corpus (Milestone 2.3).

Each case under ``tests/fixtures/scalability/discovery/cases/<case>/`` has a
``repo/`` tree and an ``expected.json`` written by hand from FastAPI's
semantics (independently of the engine's output). Every case runs end to end
through :func:`patchfrog.app.discovery.run_discovery` (JobRunner, store,
revision-verified reads).

Scored: routes as (app, methods, path, endpoint, sync/async) and effective
dependencies as (app, route, source, resolution, target, parameter), with
precision and recall. Checked exactly: unresolved references (kind, file,
line), limitations (code, file), framework evidence and ``complete``.

The corpus measures agreement with *these fixtures only*. A perfect score
is not evidence of accuracy on real repositories (that is what checkpoints
G1/G2 measure). Run ``python -m tests.unit.test_discovery_corpus`` for the
table.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from patchfrog.app.discovery import run_discovery
from patchfrog.app.store import LocalRunStore, RunState
from patchfrog.scalability.project.domain import ApplicationModel

CASES = Path(__file__).resolve().parents[1] / "fixtures" / "scalability" / "discovery" / "cases"
CASE_NAMES = sorted(p.name for p in CASES.iterdir() if (p / "expected.json").is_file())

Tuple = tuple[object, ...]


@dataclass(frozen=True)
class Score:
    true_positive: int
    false_positive: int
    false_negative: int

    @property
    def precision(self) -> float:
        found = self.true_positive + self.false_positive
        return 1.0 if found == 0 else self.true_positive / found

    @property
    def recall(self) -> float:
        expected = self.true_positive + self.false_negative
        return 1.0 if expected == 0 else self.true_positive / expected

    def __add__(self, other: Score) -> Score:
        return Score(
            self.true_positive + other.true_positive,
            self.false_positive + other.false_positive,
            self.false_negative + other.false_negative,
        )


def score(expected: Counter[Tuple], actual: Counter[Tuple]) -> Score:
    tp = sum((expected & actual).values())
    return Score(tp, sum((actual - expected).values()), sum((expected - actual).values()))


@dataclass(frozen=True)
class CaseResult:
    name: str
    model: ApplicationModel
    expected: dict[str, Any]
    routes: Score
    dependencies: Score
    mismatches: tuple[str, ...]


def _route_key(app: object, methods: object, path: object) -> str:
    assert isinstance(methods, list | tuple)
    return f"{','.join(str(m) for m in methods)} {path}"


def evaluate_case(name: str, workdir: Path) -> CaseResult:
    expected: dict[str, Any] = json.loads((CASES / name / "expected.json").read_text())
    repo = workdir / "repo"
    shutil.copytree(CASES / name / "repo", repo)
    result = run_discovery(LocalRunStore(workdir / "store"), repo, engine_version="0.1.0-beta")
    assert result.job.state in (RunState.SUCCEEDED, RunState.PARTIAL), result.job.record.reason
    model = result.model
    assert model is not None
    mismatches: list[str] = []

    exp_routes: Counter[Tuple] = Counter(
        (r["app"], tuple(r["methods"]), r["path"], r["endpoint"], r["kind"]) for r in expected["routes"]
    )
    act_routes: Counter[Tuple] = Counter(
        (r.app_id, r.methods, r.path, r.endpoint.symbol, r.endpoint.kind.value) for r in model.routes
    )
    route_score = score(exp_routes, act_routes)
    if exp_routes != act_routes:
        mismatches.append(f"routes: missing {sorted(exp_routes - act_routes, key=str)} extra {sorted(act_routes - exp_routes, key=str)}")

    exp_deps: Counter[Tuple] = Counter(tuple(d) for d in expected["dependencies"])
    act_deps: Counter[Tuple] = Counter(
        (r.app_id, _route_key(r.app_id, r.methods, r.path), d.source.value, d.resolution.value, d.target, d.parameter)
        for r in model.routes
        for d in r.dependencies
    )
    dep_score = score(exp_deps, act_deps)
    if exp_deps != act_deps:
        mismatches.append(f"dependencies: missing {sorted(exp_deps - act_deps, key=str)} extra {sorted(act_deps - exp_deps, key=str)}")

    if expected["dependency_functions"] is not None:
        exp_functions = sorted(json.dumps(f, sort_keys=True) for f in expected["dependency_functions"])
        act_functions = sorted(
            json.dumps(
                {
                    "symbol": f.symbol,
                    "kind": f.kind.value,
                    "is_generator": f.is_generator,
                    "uses": [[u.source.value, u.resolution.value, u.target, u.parameter] for u in f.uses],
                },
                sort_keys=True,
            )
            for f in model.dependency_functions
        )
        if exp_functions != act_functions:
            mismatches.append(f"dependency functions: expected {exp_functions} got {act_functions}")

    exp_unresolved = Counter((u["kind"], u["path"], u["line"]) for u in expected["unresolved"])
    act_unresolved = Counter((u.kind.value, u.location.path, u.location.line) for u in model.unresolved)
    if exp_unresolved != act_unresolved:
        mismatches.append(
            f"unresolved: missing {sorted(exp_unresolved - act_unresolved)} extra {sorted(act_unresolved - exp_unresolved)}"
        )

    exp_limitations = Counter((x["code"], x["path"]) for x in expected["limitations"])
    act_limitations = Counter((x.code.value, x.location.path if x.location else None) for x in model.limitations)
    if exp_limitations != act_limitations:
        mismatches.append(f"limitations: expected {sorted(exp_limitations.elements())} got {sorted(act_limitations.elements(), key=str)}")

    actual_frameworks = {f.package: f for f in model.frameworks}
    expected_frameworks: dict[str, dict[str, object]] = expected["frameworks"]
    if set(actual_frameworks) != set(expected_frameworks):
        mismatches.append(f"framework packages: expected {sorted(expected_frameworks)} got {sorted(actual_frameworks)}")
    for package, exp in expected_frameworks.items():
        act = actual_frameworks.get(package)
        if act is None:
            continue
        got: dict[str, object] = {
            "status": act.version_status.value,
            "resolved_version": act.resolved_version,
            "declared_specs": [d.spec for d in act.declarations],
        }
        if "importing_files" in exp:
            got["importing_files"] = act.importing_files
        if got != exp:
            mismatches.append(f"framework {package}: expected {exp} got {got}")

    if model.coverage.complete != expected["complete"]:
        mismatches.append(f"complete: expected {expected['complete']} got {model.coverage.complete} ({model.coverage.incomplete_reasons})")
    return CaseResult(name, model, expected, route_score, dep_score, tuple(mismatches))


def test_corpus_has_enough_positive_and_negative_cases() -> None:
    expectations = [json.loads((CASES / n / "expected.json").read_text()) for n in CASE_NAMES]
    assert len(CASE_NAMES) >= 12
    assert sum(1 for e in expectations if not e["routes"]) >= 1  # a pure negative control
    assert sum(1 for e in expectations if "negative control" in e["patterns"]) >= 2
    assert sum(1 for e in expectations if e["unresolved"]) >= 3  # uncertainty is exercised
    assert sum(len(e["routes"]) for e in expectations) >= 35


@pytest.mark.parametrize("name", CASE_NAMES)
def test_case_matches_its_independent_expectation(name: str, tmp_path: Path) -> None:
    result = evaluate_case(name, tmp_path)
    assert result.mismatches == (), "\n".join(result.mismatches)


def test_corpus_precision_and_recall(tmp_path: Path) -> None:
    routes = dependencies = Score(0, 0, 0)
    for name in CASE_NAMES:
        result = evaluate_case(name, tmp_path / name)
        routes, dependencies = routes + result.routes, dependencies + result.dependencies
    # Fixture agreement only (see the module docstring); the roadmap gate is 100% here.
    assert (routes.precision, routes.recall) == (1.0, 1.0), routes
    assert (dependencies.precision, dependencies.recall) == (1.0, 1.0), dependencies


def main() -> int:
    total_routes = total_deps = Score(0, 0, 0)
    print(f"{'case':28} {'routes tp/fp/fn':>16} {'P':>5} {'R':>5} {'deps tp/fp/fn':>14} {'unresolved':>10} {'complete':>8}  ok")
    with tempfile.TemporaryDirectory() as tmp:
        for name in CASE_NAMES:
            r = evaluate_case(name, Path(tmp) / name)
            total_routes, total_deps = total_routes + r.routes, total_deps + r.dependencies
            print(
                f"{name:28} {r.routes.true_positive:>6}/{r.routes.false_positive}/{r.routes.false_negative:<6} "
                f"{r.routes.precision:5.2f} {r.routes.recall:5.2f} "
                f"{r.dependencies.true_positive:>6}/{r.dependencies.false_positive}/{r.dependencies.false_negative:<5} "
                f"{len(r.model.unresolved):>10} {r.model.coverage.complete!s:>8}  {'yes' if not r.mismatches else 'NO'}"
            )
            for line in r.mismatches:
                print(f"    {line}")
    print(
        f"routes: P={total_routes.precision:.3f} R={total_routes.recall:.3f} ({total_routes}); "
        f"dependencies: P={total_deps.precision:.3f} R={total_deps.recall:.3f} ({total_deps})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
