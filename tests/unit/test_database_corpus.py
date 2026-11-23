"""SQLAlchemy / SQLModel discovery evaluation corpus (Milestone 2.4).

Each case under ``tests/fixtures/scalability/database/cases/<case>/`` has a
``repo/`` tree and an ``expected.json`` written by hand from SQLAlchemy's and
SQLModel's documented semantics, independently of the engine's output. Every
case runs end to end through :func:`patchfrog.app.discovery.run_discovery`.

Precision and recall are measured only for the supported patterns: models
(table, provenance, flavor, base, parents, inheritance, primary key,
``complete``), columns, relationships, engines/factories/sessions, their
allowlisted options, session providers and route -> session links.
Unresolved entries (kind, file, line), bases and overall ``complete`` are
checked exactly. A perfect score is agreement with these fixtures only, not
evidence of accuracy on real repositories. ``python -m
tests.unit.test_database_corpus`` prints the table.
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
from tests.unit.test_discovery_corpus import Score, score

CASES = Path(__file__).resolve().parents[1] / "fixtures" / "scalability" / "database" / "cases"
CASE_NAMES = sorted(p.name for p in CASES.iterdir() if (p / "expected.json").is_file())
SCORED = ("models", "columns", "relationships", "objects", "options", "providers", "route_sessions")

Row = tuple[object, ...]


def _freeze(value: object) -> object:
    return tuple(_freeze(v) for v in value) if isinstance(value, list | tuple) else value


def actual_rows(model: ApplicationModel) -> dict[str, Counter[Row]]:
    db = model.database
    routes = {r.route_id: f"{','.join(r.methods)} {r.path}" for r in model.routes}
    rows: dict[str, Counter[Row]] = {
        "models": Counter(
            (m.model_id, m.table_name, m.table_name_provenance.value, m.flavor.value, m.base_id, tuple(m.parents),
             m.inheritance.value, tuple(m.primary_key), m.primary_key_provenance.value, m.complete)
            for m in db.models
        ),
        "columns": Counter(
            (m.model_id, c.name, c.column_name, c.declaration.value, c.provenance.value, c.primary_key, tuple(c.foreign_keys), c.declared_in)
            for m in db.models
            for c in m.columns
        ),
        "relationships": Counter(
            (m.model_id, r.name, r.target_model, r.resolution.value, r.lazy, r.lazy_provenance.value, r.collection,
             r.uselist, r.back_populates, r.backref, r.secondary)
            for m in db.models
            for r in m.relationships
        ),
        "objects": Counter((o.object_id, o.kind.value, o.bound_to, o.url_source.value, o.url_scheme) for o in db.objects),
        "options": Counter((o.object_id, opt.name, opt.value) for o in db.objects for opt in o.options),
        "providers": Counter(
            (p.symbol, p.shape.value, p.session_kind.value, p.provenance.value, p.source) for p in db.session_providers
        ),
        "route_sessions": Counter((routes[s.route_id], s.provider, s.parameter, tuple(s.chain)) for s in db.route_sessions),
    }
    return rows


def expected_rows(expected: dict[str, Any]) -> dict[str, Counter[Row]]:
    models: Counter[Row] = Counter(
        (m["model_id"], m["table_name"], m["table_name_provenance"], m["flavor"], m["base_id"], tuple(m["parents"]),
         m["inheritance"], tuple(m["primary_key"]), m["primary_key_provenance"], m["complete"])
        for m in expected["models"]
    )
    rows: dict[str, Counter[Row]] = {"models": models}
    for key in SCORED[1:]:
        rows[key] = Counter(_freeze(row) for row in expected[key])  # type: ignore[misc]
    return rows


@dataclass(frozen=True)
class CaseResult:
    name: str
    model: ApplicationModel
    scores: dict[str, Score]
    mismatches: tuple[str, ...]


def evaluate_case(name: str, workdir: Path) -> CaseResult:
    expected: dict[str, Any] = json.loads((CASES / name / "expected.json").read_text())
    repo = workdir / "repo"
    shutil.copytree(CASES / name / "repo", repo)
    result = run_discovery(LocalRunStore(workdir / "store"), repo, engine_version="0.1.0-beta")
    assert result.job.state is RunState.SUCCEEDED, result.job.record.reason
    model = result.model
    assert model is not None
    mismatches: list[str] = []
    actual, wanted = actual_rows(model), expected_rows(expected)
    scores = {key: score(wanted[key], actual[key]) for key in SCORED}
    for key in SCORED:
        if wanted[key] != actual[key]:
            mismatches.append(
                f"{key}: missing {sorted(wanted[key] - actual[key], key=str)} extra {sorted(actual[key] - wanted[key], key=str)}"
            )
    bases = sorted([b.base_id, b.kind.value] for b in model.database.bases)
    if bases != sorted(expected["bases"]):
        mismatches.append(f"bases: expected {sorted(expected['bases'])} got {bases}")
    unresolved = Counter((u.kind.value, u.location.path, u.location.line) for u in model.database.unresolved)
    if unresolved != Counter(tuple(u) for u in expected["unresolved"]):
        mismatches.append(f"unresolved: expected {sorted(expected['unresolved'])} got {sorted(unresolved.elements())}")
    if model.coverage.complete != expected["complete"]:
        mismatches.append(f"complete: expected {expected['complete']} got {model.coverage.complete} {model.coverage.incomplete_reasons}")
    return CaseResult(name, model, scores, tuple(mismatches))


def test_corpus_shape() -> None:
    expectations = [json.loads((CASES / n / "expected.json").read_text()) for n in CASE_NAMES]
    assert len(CASE_NAMES) >= 12
    assert any("negative control" in e["patterns"] for e in expectations)
    assert sum(1 for e in expectations if e["unresolved"]) >= 4
    assert sum(len(e["models"]) for e in expectations) >= 20  # 20 models across the cases
    assert sum(len(e["providers"]) for e in expectations) >= 5


@pytest.mark.parametrize("name", CASE_NAMES)
def test_case_matches_its_independent_expectation(name: str, tmp_path: Path) -> None:
    result = evaluate_case(name, tmp_path)
    assert result.mismatches == (), "\n".join(result.mismatches)


def test_corpus_precision_and_recall(tmp_path: Path) -> None:
    totals = dict.fromkeys(SCORED, Score(0, 0, 0))
    for name in CASE_NAMES:
        result = evaluate_case(name, tmp_path / name)
        for key in SCORED:
            totals[key] = totals[key] + result.scores[key]
    for key, total in totals.items():
        # Fixture agreement only (see the module docstring).
        assert (total.precision, total.recall) == (1.0, 1.0), (key, total)


def main() -> int:
    totals = dict.fromkeys(SCORED, Score(0, 0, 0))
    with tempfile.TemporaryDirectory() as tmp:
        for name in CASE_NAMES:
            r = evaluate_case(name, Path(tmp) / name)
            for key in SCORED:
                totals[key] = totals[key] + r.scores[key]
            counts = " ".join(f"{k}={r.scores[k].true_positive}/{r.scores[k].false_positive}/{r.scores[k].false_negative}" for k in SCORED)
            print(f"{name:28} {'ok' if not r.mismatches else 'MISMATCH'}  unresolved={len(r.model.database.unresolved)}  {counts}")
            for line in r.mismatches:
                print(f"    {line}")
    for key, total in totals.items():
        print(f"{key:15} tp={total.true_positive:3} fp={total.false_positive} fn={total.false_negative}  P={total.precision:.3f} R={total.recall:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
