"""Unit coverage for :mod:`patchfrog.evaluation.fixtures` -- ground-truth
loading and the fail-fast validation the Phase 8 spec requires before any
benchmark run starts (missing files, bad line ranges, duplicate expected
findings, malformed forbidden rules, mismatched case ids)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from patchfrog.evaluation.fixtures import (
    BenchmarkValidationError,
    fixture_content_hash,
    load_all_cases,
    load_case,
    validate_and_raise,
    validate_case,
)


def _write_case(root: Path, case_id: str, *, case_yaml: dict[str, Any], files: dict[str, str]) -> Path:
    case_dir = root / case_id
    (case_dir / "repo").mkdir(parents=True, exist_ok=True)
    (case_dir / "case.yaml").write_text(yaml.safe_dump(case_yaml, sort_keys=False))
    for rel, content in files.items():
        path = case_dir / "repo" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return case_dir


def _minimal_case_yaml(case_id: str, **expected_overrides: object) -> dict[str, Any]:
    expected = {
        "id": "ef1", "category": "correctness", "file": "a.py", "symbol": "foo", "line": 2,
        **expected_overrides,
    }
    return {"case": {"id": case_id, "language": "python", "difficulty": "easy"}, "expected": [expected]}


def test_load_case_requires_directory_name_to_match_case_id(tmp_path: Path) -> None:
    case_dir = _write_case(tmp_path, "on-disk-name", case_yaml={"case": {"id": "different-id", "language": "python"}}, files={})
    with pytest.raises(BenchmarkValidationError, match="must match directory name"):
        load_case(case_dir)


def test_load_case_requires_case_yaml_present(tmp_path: Path) -> None:
    case_dir = tmp_path / "empty-case"
    case_dir.mkdir()
    with pytest.raises(BenchmarkValidationError, match=r"missing case\.yaml"):
        load_case(case_dir)


def test_validate_case_flags_missing_expected_file(tmp_path: Path) -> None:
    case_dir = _write_case(tmp_path, "c1", case_yaml=_minimal_case_yaml("c1"), files={})  # a.py never written
    case = load_case(case_dir)
    errors = validate_case(case, cases_root=tmp_path)
    assert any("file does not exist" in e for e in errors)


def test_validate_case_flags_line_out_of_range(tmp_path: Path) -> None:
    case_dir = _write_case(
        tmp_path, "c1", case_yaml=_minimal_case_yaml("c1", line=999), files={"a.py": "def foo():\n    return 1\n"}
    )
    case = load_case(case_dir)
    errors = validate_case(case, cases_root=tmp_path)
    assert any("out of range" in e for e in errors)


def test_validate_case_flags_line_end_before_line(tmp_path: Path) -> None:
    case_dir = _write_case(
        tmp_path, "c1", case_yaml=_minimal_case_yaml("c1", line=2, line_end=1),
        files={"a.py": "def foo():\n    return 1\n"},
    )
    case = load_case(case_dir)
    errors = validate_case(case, cases_root=tmp_path)
    assert any("line_end" in e for e in errors)


def test_validate_case_flags_symbol_not_found_in_file(tmp_path: Path) -> None:
    case_dir = _write_case(
        tmp_path, "c1", case_yaml=_minimal_case_yaml("c1", symbol="does_not_exist"),
        files={"a.py": "def foo():\n    return 1\n"},
    )
    case = load_case(case_dir)
    errors = validate_case(case, cases_root=tmp_path)
    assert any("not found" in e for e in errors)


def test_validate_case_flags_duplicate_expected_ids(tmp_path: Path) -> None:
    yaml_doc = {
        "case": {"id": "c1", "language": "python", "difficulty": "easy"},
        "expected": [
            {"id": "ef1", "category": "correctness", "file": "a.py", "symbol": "foo", "line": 1},
            {"id": "ef1", "category": "security", "file": "a.py", "symbol": "foo", "line": 2},
        ],
    }
    case_dir = _write_case(tmp_path, "c1", case_yaml=yaml_doc, files={"a.py": "def foo():\n    return 1\n"})
    case = load_case(case_dir)
    errors = validate_case(case, cases_root=tmp_path)
    assert any("duplicate expected finding id" in e for e in errors)


def test_validate_case_flags_duplicate_dedup_key(tmp_path: Path) -> None:
    # Same (file, symbol, category, line) under two different ids --
    # a real accidental duplicate a benchmark author is very likely to make.
    yaml_doc = {
        "case": {"id": "c1", "language": "python", "difficulty": "easy"},
        "expected": [
            {"id": "ef1", "category": "correctness", "file": "a.py", "symbol": "foo", "line": 2},
            {"id": "ef2", "category": "correctness", "file": "a.py", "symbol": "foo", "line": 2},
        ],
    }
    case_dir = _write_case(tmp_path, "c1", case_yaml=yaml_doc, files={"a.py": "def foo():\n    return 1\n"})
    case = load_case(case_dir)
    errors = validate_case(case, cases_root=tmp_path)
    assert any("duplicates another" in e for e in errors)


def test_validate_case_flags_forbidden_rule_without_reason(tmp_path: Path) -> None:
    yaml_doc = {
        "case": {"id": "c1", "language": "python", "difficulty": "easy"},
        "expected": [], "forbidden": [{"reason": "", "category": "style"}],
    }
    case_dir = _write_case(tmp_path, "c1", case_yaml=yaml_doc, files={"a.py": "def foo():\n    return 1\n"})
    case = load_case(case_dir)
    errors = validate_case(case, cases_root=tmp_path)
    assert any("missing a reason" in e for e in errors)


def test_validate_case_flags_forbidden_rule_without_category_or_family(tmp_path: Path) -> None:
    yaml_doc = {
        "case": {"id": "c1", "language": "python", "difficulty": "easy"},
        "expected": [], "forbidden": [{"reason": "some reason"}],
    }
    case_dir = _write_case(tmp_path, "c1", case_yaml=yaml_doc, files={"a.py": "def foo():\n    return 1\n"})
    case = load_case(case_dir)
    errors = validate_case(case, cases_root=tmp_path)
    assert any("category or issue_family" in e for e in errors)


def test_valid_case_has_no_errors(tmp_path: Path) -> None:
    case_dir = _write_case(tmp_path, "c1", case_yaml=_minimal_case_yaml("c1"), files={"a.py": "def foo():\n    return 1\n"})
    case = load_case(case_dir)
    assert validate_case(case, cases_root=tmp_path) == []


def test_load_all_cases_loads_every_case_directory(tmp_path: Path) -> None:
    _write_case(tmp_path, "c1", case_yaml=_minimal_case_yaml("c1"), files={"a.py": "def foo():\n    return 1\n"})
    _write_case(tmp_path, "c2", case_yaml=_minimal_case_yaml("c2"), files={"a.py": "def foo():\n    return 1\n"})
    cases = load_all_cases(tmp_path)
    assert {c.id for c in cases} == {"c1", "c2"}


def test_load_all_cases_raises_on_genuine_duplicate() -> None:
    from patchfrog.evaluation.domain import Difficulty, EvaluationCase, Language
    from patchfrog.evaluation.fixtures import BenchmarkValidationError as Err
    from patchfrog.evaluation.fixtures import _validate_no_duplicate_case_ids

    dup = [
        EvaluationCase(id="same", title="a", description="", language=Language.PYTHON, fixture="same", difficulty=Difficulty.EASY),
        EvaluationCase(id="same", title="b", description="", language=Language.PYTHON, fixture="same", difficulty=Difficulty.EASY),
    ]
    with pytest.raises(Err, match="duplicate case id"):
        _validate_no_duplicate_case_ids(dup)


def test_validate_and_raise_collects_all_case_errors_with_case_id_prefix(tmp_path: Path) -> None:
    case_dir = _write_case(tmp_path, "c1", case_yaml=_minimal_case_yaml("c1", line=999), files={})
    case = load_case(case_dir)
    with pytest.raises(BenchmarkValidationError, match=r"\[c1\]"):
        validate_and_raise([case], cases_root=tmp_path)


def test_fixture_content_hash_changes_when_source_changes(tmp_path: Path) -> None:
    case_dir = _write_case(tmp_path, "c1", case_yaml=_minimal_case_yaml("c1"), files={"a.py": "def foo():\n    return 1\n"})
    case = load_case(case_dir)
    hash1 = fixture_content_hash(case, cases_root=tmp_path)
    (case_dir / "repo" / "a.py").write_text("def foo():\n    return 2\n")
    hash2 = fixture_content_hash(case, cases_root=tmp_path)
    assert hash1 != hash2


def test_fixture_content_hash_stable_for_unchanged_content(tmp_path: Path) -> None:
    case_dir = _write_case(tmp_path, "c1", case_yaml=_minimal_case_yaml("c1"), files={"a.py": "def foo():\n    return 1\n"})
    case = load_case(case_dir)
    assert fixture_content_hash(case, cases_root=tmp_path) == fixture_content_hash(case, cases_root=tmp_path)
