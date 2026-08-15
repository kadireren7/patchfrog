from __future__ import annotations

from pathlib import Path

import pytest

from patchfrog.analysis.analyzers.ruff import classify_rule_code, parse_ruff_output
from patchfrog.analysis.domain import Confidence, FindingCategory, Severity

FIXTURES = Path(__file__).parent.parent / "fixtures" / "analyzer_output"


def test_parses_stored_ruff_json_fixture() -> None:
    stdout = (FIXTURES / "ruff_output.json").read_text()

    findings = parse_ruff_output(stdout, checkout_path=Path("/repo"))

    assert len(findings) == 3
    unused_import = findings[0]
    assert unused_import.rule_id == "F401"
    assert unused_import.file_path == "bad.py"
    assert unused_import.span.start_line == 1
    assert unused_import.span.start_column == 8
    assert unused_import.source_analyzer == "ruff"
    assert unused_import.raw_metadata["url"].startswith("https://")

    bare_except = findings[1]
    assert bare_except.rule_id == "E722"
    assert bare_except.category is FindingCategory.CORRECTNESS  # not STYLE -- matters for dedup


def test_empty_output_returns_no_findings() -> None:
    assert parse_ruff_output("", checkout_path=Path("/repo")) == []
    assert parse_ruff_output("[]", checkout_path=Path("/repo")) == []


def test_malformed_json_raises_for_caller_to_handle() -> None:
    import json

    with pytest.raises(json.JSONDecodeError):
        parse_ruff_output("{not valid json", checkout_path=Path("/repo"))


@pytest.mark.parametrize(
    ("code", "expected_category", "expected_severity"),
    [
        ("F401", FindingCategory.CORRECTNESS, Severity.MEDIUM),
        ("F821", FindingCategory.CORRECTNESS, Severity.HIGH),
        ("E999", FindingCategory.CORRECTNESS, Severity.HIGH),  # syntax error family
        ("E722", FindingCategory.CORRECTNESS, Severity.MEDIUM),  # bare except
        ("E501", FindingCategory.STYLE, Severity.INFO),
        ("S110", FindingCategory.SECURITY, Severity.HIGH),
        ("B006", FindingCategory.CORRECTNESS, Severity.MEDIUM),
        ("SIM105", FindingCategory.MAINTAINABILITY, Severity.LOW),
        ("PERF401", FindingCategory.PERFORMANCE, Severity.LOW),
        ("ZZZ999", FindingCategory.UNKNOWN, Severity.MEDIUM),  # unmapped fallback
    ],
)
def test_classify_rule_code_mapping(
    code: str, expected_category: FindingCategory, expected_severity: Severity
) -> None:
    category, severity, _confidence = classify_rule_code(code)
    assert category is expected_category
    assert severity is expected_severity


def test_classify_rule_code_confidence_is_never_fabricated_high_for_unknown() -> None:
    _category, _severity, confidence = classify_rule_code("ZZZ999")
    assert confidence is Confidence.MEDIUM
