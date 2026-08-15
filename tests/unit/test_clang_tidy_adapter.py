from __future__ import annotations

from pathlib import Path

import pytest

from patchfrog.analysis.analyzers.clang_tidy import (
    classify_check_name,
    find_compilation_database,
    parse_clang_tidy_output,
)
from patchfrog.analysis.domain import FindingCategory, Severity

FIXTURES = Path(__file__).parent.parent / "fixtures" / "analyzer_output"


def test_parses_stored_clang_tidy_text_fixture() -> None:
    stdout = (FIXTURES / "clang_tidy_output.txt").read_text()

    findings = parse_clang_tidy_output(stdout, checkout_path=Path("/repo"))

    # The "note:" line and the trailing summary line are not diagnostics
    # and must not produce findings.
    assert len(findings) == 3

    use_after_move = findings[0]
    assert use_after_move.rule_id == "bugprone-use-after-move"
    assert use_after_move.file_path == "src/cache.cpp"
    assert use_after_move.span.start_line == 15
    assert use_after_move.severity is Severity.MEDIUM
    assert use_after_move.category is FindingCategory.MEMORY_SAFETY

    error_finding = findings[2]
    assert error_finding.severity is Severity.HIGH


def test_empty_output_returns_no_findings() -> None:
    assert parse_clang_tidy_output("", checkout_path=Path("/repo")) == []


def test_note_only_lines_produce_no_findings() -> None:
    text = "/repo/a.cpp:1:1: note: just context, not a diagnostic\n"
    assert parse_clang_tidy_output(text, checkout_path=Path("/repo")) == []


@pytest.mark.parametrize(
    ("check_name", "expected_category"),
    [
        ("bugprone-use-after-move", FindingCategory.MEMORY_SAFETY),
        ("bugprone-string-constructor", FindingCategory.CORRECTNESS),
        ("clang-analyzer-core.NullDereference", FindingCategory.MEMORY_SAFETY),
        ("clang-analyzer-security.FloatLoopCounter", FindingCategory.SECURITY),
        ("cert-err34-c", FindingCategory.SECURITY),
        ("performance-move-const-arg", FindingCategory.PERFORMANCE),
        ("modernize-use-nullptr", FindingCategory.MAINTAINABILITY),
        ("totally-unmapped-check", FindingCategory.UNKNOWN),
    ],
)
def test_classify_check_name_mapping(check_name: str, expected_category: FindingCategory) -> None:
    assert classify_check_name(check_name) is expected_category


def test_find_compilation_database_at_repo_root(tmp_path: Path) -> None:
    (tmp_path / "compile_commands.json").write_text("[]")

    result = find_compilation_database(tmp_path)

    assert result == tmp_path


def test_find_compilation_database_in_build_subdir(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "compile_commands.json").write_text("[]")

    result = find_compilation_database(tmp_path)

    assert result == build_dir


def test_find_compilation_database_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_compilation_database(tmp_path) is None


def test_find_compilation_database_never_generates_one(tmp_path: Path) -> None:
    """Regression guard: this must be a pure filesystem check — no
    subprocess execution of any kind (e.g. running cmake/configure)."""

    import subprocess
    from unittest.mock import patch

    with patch.object(subprocess, "run") as mock_run:
        find_compilation_database(tmp_path)
        mock_run.assert_not_called()
