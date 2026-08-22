"""Unit coverage for :mod:`patchfrog.evaluation.oracle` -- the scripted
FakeLLM "pipeline correctness" provider. Verified against a real
on-disk fixture tree (not the committed corpus) so quoted-evidence
extraction reads real file content, exactly like a real case run."""

from __future__ import annotations

import json
from pathlib import Path

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.evaluation.domain import (
    Difficulty,
    EvaluationCase,
    ExpectedFinding,
    GroundTruthSource,
    Language,
)
from patchfrog.evaluation.oracle import build_oracle_response_factory
from patchfrog.review.provider import ProviderRequest


def _write_repo(tmp_path: Path, case_id: str, file_name: str, content: str) -> Path:
    repo_root = tmp_path / case_id / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / file_name).write_text(content)
    return tmp_path


def _request(*, user_prompt: str, schema_name: str = "review_findings") -> ProviderRequest:
    return ProviderRequest(system_prompt="sys", user_prompt=user_prompt, json_schema={}, schema_name=schema_name, max_output_tokens=100)


def test_critic_schema_always_returns_accept(tmp_path: Path) -> None:
    cases_root = _write_repo(tmp_path, "c1", "a.py", "def foo():\n    return 1\n")
    case = EvaluationCase(id="c1", title="t", description="", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY)
    factory = build_oracle_response_factory(case, cases_root=cases_root)
    response = factory(_request(user_prompt="anything", schema_name="critic_verdict"))
    payload = json.loads(response.raw_json)
    assert payload["decision"] == "accept"


def test_no_findings_for_a_target_with_no_matching_expected(tmp_path: Path) -> None:
    cases_root = _write_repo(tmp_path, "c1", "a.py", "def foo():\n    return 1\n")
    case = EvaluationCase(id="c1", title="t", description="", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY)
    factory = build_oracle_response_factory(case, cases_root=cases_root)
    response = factory(_request(user_prompt="Review target: `foo` in `a.py`, lines 1-2."))
    assert json.loads(response.raw_json) == {"findings": []}


def test_oracle_echoes_matching_expected_finding_with_verbatim_evidence(tmp_path: Path) -> None:
    cases_root = _write_repo(tmp_path, "c1", "a.py", "def foo():\n    return a - b\n")
    expected = ExpectedFinding(
        id="ef1", category=FindingCategory.CORRECTNESS, file="a.py", issue_family="inverted", symbol="foo",
        severity=Severity.HIGH, line=2, ground_truth_source=GroundTruthSource.AI_EXPECTED,
    )
    case = EvaluationCase(
        id="c1", title="t", description="", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY,
        expected=(expected,),
    )
    factory = build_oracle_response_factory(case, cases_root=cases_root)
    response = factory(_request(user_prompt="Review target: `foo` in `a.py`, lines 1-2."))
    payload = json.loads(response.raw_json)
    assert len(payload["findings"]) == 1
    finding = payload["findings"][0]
    assert finding["category"] == "correctness"
    assert finding["severity"] == "high"
    assert finding["file_path"] == "a.py"
    assert finding["evidence"][0]["quoted_text"] == "return a - b"


def test_oracle_matches_qualified_target_against_bare_symbol(tmp_path: Path) -> None:
    cases_root = _write_repo(tmp_path, "c1", "a.py", "class Account:\n    def foo(self):\n        return 1\n")
    expected = ExpectedFinding(
        id="ef1", category=FindingCategory.CORRECTNESS, file="a.py", issue_family="fam", symbol="foo",
        line=3, ground_truth_source=GroundTruthSource.AI_EXPECTED,
    )
    case = EvaluationCase(
        id="c1", title="t", description="", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY,
        expected=(expected,),
    )
    factory = build_oracle_response_factory(case, cases_root=cases_root)
    response = factory(_request(user_prompt="Review target: `Account.foo` in `a.py`, lines 2-3."))
    payload = json.loads(response.raw_json)
    assert len(payload["findings"]) == 1


def test_oracle_ignores_static_expected_only_findings(tmp_path: Path) -> None:
    # A finding declared static_expected (not ai_expected/either) must
    # never be echoed by the AI oracle -- it's the static analyzer's job,
    # not the reviewer's, to catch it.
    cases_root = _write_repo(tmp_path, "c1", "a.py", "def foo():\n    return 1\n")
    expected = ExpectedFinding(
        id="ef1", category=FindingCategory.CORRECTNESS, file="a.py", issue_family="fam", symbol="foo",
        line=2, ground_truth_source=GroundTruthSource.STATIC_EXPECTED,
    )
    case = EvaluationCase(
        id="c1", title="t", description="", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY,
        expected=(expected,),
    )
    factory = build_oracle_response_factory(case, cases_root=cases_root)
    response = factory(_request(user_prompt="Review target: `foo` in `a.py`, lines 1-2."))
    assert json.loads(response.raw_json) == {"findings": []}


def test_oracle_echoes_security_quality_ground_truth_when_present(tmp_path: Path) -> None:
    cases_root = _write_repo(tmp_path, "c1", "a.py", "def foo():\n    return a - b\n")
    expected = ExpectedFinding(
        id="ef1", category=FindingCategory.SECURITY, file="a.py", issue_family="cred-exposure", symbol="foo",
        severity=Severity.HIGH, line=2, ground_truth_source=GroundTruthSource.AI_EXPECTED,
        expected_root_cause_concept="the secret reaches the response text without redaction",
        expected_impact_concept="an attacker who triggers this path receives the plaintext secret",
        acceptable_remediation_direction="remove the secret from the returned/logged text",
    )
    case = EvaluationCase(
        id="c1", title="t", description="", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY,
        expected=(expected,),
    )
    factory = build_oracle_response_factory(case, cases_root=cases_root)
    response = factory(_request(user_prompt="Review target: `foo` in `a.py`, lines 1-2."))
    finding = json.loads(response.raw_json)["findings"][0]
    assert finding["reasoning_summary"] == "the secret reaches the response text without redaction"
    assert finding["impact"] == "an attacker who triggers this path receives the plaintext secret"
    assert finding["suggested_fix"] == "remove the secret from the returned/logged text"


def test_oracle_falls_back_to_generic_reasoning_without_quality_ground_truth(tmp_path: Path) -> None:
    cases_root = _write_repo(tmp_path, "c1", "a.py", "def foo():\n    return a - b\n")
    expected = ExpectedFinding(
        id="ef1", category=FindingCategory.CORRECTNESS, file="a.py", issue_family="inverted", symbol="foo",
        severity=Severity.HIGH, line=2, ground_truth_source=GroundTruthSource.AI_EXPECTED,
    )
    case = EvaluationCase(
        id="c1", title="t", description="", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY,
        expected=(expected,),
    )
    factory = build_oracle_response_factory(case, cases_root=cases_root)
    response = factory(_request(user_prompt="Review target: `foo` in `a.py`, lines 1-2."))
    finding = json.loads(response.raw_json)["findings"][0]
    assert finding["reasoning_summary"] == "oracle-generated verbatim from committed ground truth"
    assert finding["impact"] is None
    assert finding["suggested_fix"] is None


def test_oracle_never_routes_by_bare_prompt_substring_without_target_line(tmp_path: Path) -> None:
    cases_root = _write_repo(tmp_path, "c1", "a.py", "def foo():\n    return 1\n")
    expected = ExpectedFinding(
        id="ef1", category=FindingCategory.CORRECTNESS, file="a.py", issue_family="fam", symbol="foo",
        line=2, ground_truth_source=GroundTruthSource.AI_EXPECTED,
    )
    case = EvaluationCase(
        id="c1", title="t", description="", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY,
        expected=(expected,),
    )
    factory = build_oracle_response_factory(case, cases_root=cases_root)
    # No "Review target: `...`" line at all -- must default to no findings.
    response = factory(_request(user_prompt="some unrelated prompt mentioning foo"))
    assert json.loads(response.raw_json) == {"findings": []}
