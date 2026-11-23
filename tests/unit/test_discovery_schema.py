"""JSON Schema of ApplicationModel (Milestone 2.5): the committed snapshot is
exactly what the generator produces from the typed model, every real
artifact validates, and the schema and the runtime loader
(``ApplicationModel.from_dict``) reject the same invalid documents (the loader
is the runtime validator; ``jsonschema`` is only a test dependency)."""

from __future__ import annotations

import copy
import importlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from patchfrog.app.discovery import run_discovery
from patchfrog.app.store import LocalRunStore
from patchfrog.scalability.project import DISCOVERY_SCHEMA_VERSION, ApplicationModel
from patchfrog.scalability.project.schema import (
    SCHEMA_ID,
    SUPPORTED_SCHEMA_VERSIONS,
    application_model_json_schema,
    application_model_json_schema_text,
)

# A test-only validator. It is installed with the base dependency `mcp`
# (which requires jsonschema>=4.20.0), so it must be present: a missing
# validator fails here instead of silently skipping the schema contract.
jsonschema = importlib.import_module("jsonschema")

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "docs" / "schemas" / f"application-model.v{DISCOVERY_SCHEMA_VERSION}.schema.json"
CORPORA = [ROOT / "tests" / "fixtures" / "scalability" / "discovery" / "cases", ROOT / "tests" / "fixtures" / "scalability" / "database" / "cases"]
CASES = sorted(p for corpus in CORPORA for p in corpus.iterdir() if (p / "repo").is_dir())


def validator() -> Any:
    schema = application_model_json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def discover(case: Path, tmp: Path) -> ApplicationModel:
    shutil.copytree(case / "repo", tmp / "repo")
    result = run_discovery(LocalRunStore(tmp / "store"), tmp / "repo", engine_version="0.1.0-beta")
    assert result.model is not None
    return result.model


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    case = ROOT / "tests" / "fixtures" / "scalability" / "database" / "cases" / "05_sessions_sync"
    return discover(case, tmp_path_factory.mktemp("sample")).to_dict()


def test_committed_snapshot_is_the_generated_schema() -> None:
    assert SNAPSHOT.read_text() == application_model_json_schema_text(), (
        "regenerate: python -m patchfrog scale schema > " + str(SNAPSHOT.relative_to(ROOT))
    )


def test_generation_is_deterministic_and_well_formed() -> None:
    assert application_model_json_schema_text() == application_model_json_schema_text()
    schema = application_model_json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$id"] == SCHEMA_ID and SUPPORTED_SCHEMA_VERSIONS == (1, 2)
    # Every dataclass is closed and lists every field as required (nullable, never omitted).
    for name, definition in schema["$defs"].items():  # type: ignore[attr-defined]
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False, name
            assert sorted(definition["required"]) == sorted(definition["properties"]), name


@pytest.mark.parametrize("case", CASES, ids=[f"{c.parent.parent.name}/{c.name}" for c in CASES])
def test_every_corpus_artifact_validates_and_round_trips(case: Path, tmp_path: Path) -> None:
    model = discover(case, tmp_path)
    data = json.loads(model.to_json_bytes())
    validator().validate(data)
    again = ApplicationModel.from_dict(data)
    assert again == model and again.to_json_bytes() == model.to_json_bytes()


def test_version_1_artifact_validates_and_loads(sample: dict[str, Any]) -> None:
    legacy = copy.deepcopy(sample)
    legacy["schema_version"] = 1
    del legacy["database"]
    validator().validate(legacy)
    assert ApplicationModel.from_dict(legacy).database.models == ()


def _set(path: tuple[object, ...], value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(doc: dict[str, Any]) -> None:
        target: Any = doc
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _delete(path: tuple[object, ...]) -> Callable[[dict[str, Any]], None]:
    def mutate(doc: dict[str, Any]) -> None:
        target: Any = doc
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


INVALID: dict[str, Callable[[dict[str, Any]], None]] = {
    "future schema version": _set(("schema_version",), DISCOVERY_SCHEMA_VERSION + 1),
    "version 2 without database": _delete(("database",)),
    "unknown enum value": _set(("routes", 0, "kind"), "graphql"),
    "null in a non-nullable field": _set(("routes", 0, "path"), None),
    "wrong type": _set(("coverage", "python_files"), "many"),
    "missing required field": _delete(("coverage", "complete")),
    "unknown top-level field": _set(("score",), 99),
    "unknown nested field": _set(("database", "objects", 0, "password"), "x"),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_schema_and_loader_both_reject_invalid_documents(name: str, sample: dict[str, Any]) -> None:
    doc = copy.deepcopy(sample)
    INVALID[name](doc)
    assert list(validator().iter_errors(doc)), f"schema accepted: {name}"
    with pytest.raises((KeyError, TypeError, ValueError)):
        ApplicationModel.from_dict(doc)
