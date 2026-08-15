from __future__ import annotations

import json
from pathlib import Path

import pytest

from patchfrog.analysis.analyzers.semgrep import BUNDLED_RULES_PATH, parse_semgrep_output
from patchfrog.analysis.domain import Confidence, FindingCategory, Severity

FIXTURES = Path(__file__).parent.parent / "fixtures" / "analyzer_output"


def test_bundled_rules_file_exists_and_is_valid_yaml() -> None:
    import yaml

    assert BUNDLED_RULES_PATH.is_file()
    parsed = yaml.safe_load(BUNDLED_RULES_PATH.read_text())
    assert "rules" in parsed
    assert len(parsed["rules"]) > 0


def test_parses_stored_semgrep_json_fixture() -> None:
    stdout = (FIXTURES / "semgrep_output.json").read_text()

    findings = parse_semgrep_output(stdout, checkout_path=Path("/repo"))

    assert len(findings) == 2
    eval_finding = findings[0]
    assert eval_finding.rule_id == "patchfrog-python-eval-usage"  # config-path prefix stripped
    assert eval_finding.file_path == "bad.py"
    assert eval_finding.category is FindingCategory.SECURITY
    assert eval_finding.severity is Severity.HIGH
    assert eval_finding.confidence is Confidence.MEDIUM
    assert eval_finding.raw_metadata["cwe"] == "CWE-95"

    bare_except = findings[1]
    assert bare_except.category is FindingCategory.CORRECTNESS
    assert bare_except.severity is Severity.MEDIUM
    assert bare_except.confidence is Confidence.HIGH


def test_empty_output_returns_no_findings() -> None:
    assert parse_semgrep_output("", checkout_path=Path("/repo")) == []


def test_unrecognized_category_falls_back_to_unknown() -> None:
    payload = {
        "results": [
            {
                "check_id": "x.some-rule",
                "path": "/repo/a.py",
                "start": {"line": 1, "col": 1},
                "end": {"line": 1, "col": 5},
                "extra": {"message": "m", "metadata": {"category": "not-a-real-category"}, "severity": "INFO"},
            }
        ]
    }
    findings = parse_semgrep_output(json.dumps(payload), checkout_path=Path("/repo"))
    assert findings[0].category is FindingCategory.UNKNOWN


def test_malformed_json_raises_for_caller_to_handle() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_semgrep_output("{not valid", checkout_path=Path("/repo"))
