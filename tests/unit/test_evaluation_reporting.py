"""Unit coverage for :mod:`patchfrog.evaluation.reporting` -- JSON/
Markdown report serialization. No I/O beyond a real tmp_path for the
write/read round trip; the report content itself is built from a
minimal, hand-constructed :class:`EvaluationRunResult`."""

from __future__ import annotations

import json
from pathlib import Path

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.evaluation.domain import (
    CaseResult,
    CaseStatus,
    Difficulty,
    EvaluationCase,
    EvaluationIdentity,
    EvaluationMode,
    EvaluationRunResult,
    ExpectedFinding,
    ExpectedFindingOutcome,
    ExpectedOutcome,
    FixtureInfo,
    IncrementalScenarioResult,
    Language,
    MatchOutcome,
    PredictedFinding,
    PredictionOutcome,
    PredictionSource,
)
from patchfrog.evaluation.reporting import build_report, read_json, render_markdown, write_json


def _identity() -> EvaluationIdentity:
    return EvaluationIdentity(
        evaluation_benchmark_version=1, evaluation_engine_version=1, review_engine_version=1,
        review_prompt_version=1, review_policy_version=1, incremental_review_engine_version=1,
        review_memory_version=1, reviewer_provider="fake-oracle", reviewer_model="oracle-v1",
        critic_enabled=True, static_toolchain_available=True, mode=EvaluationMode.FULL_PIPELINE,
    )


def _case() -> EvaluationCase:
    expected = ExpectedFinding(id="ef1", category=FindingCategory.CORRECTNESS, file="a.py", issue_family="fam", severity=Severity.HIGH)
    return EvaluationCase(
        id="c1", title="t", description="d", language=Language.PYTHON, fixture="c1", difficulty=Difficulty.EASY,
        expected=(expected,),
    )


def _case_result() -> CaseResult:
    pred = PredictedFinding(
        source=PredictionSource.AI, category=FindingCategory.CORRECTNESS, severity=Severity.HIGH, title="t",
        message="m", file_path="a.py", start_line=1, end_line=1, symbol_qualified_name="foo", evidence_text="x",
    )
    outcome = PredictionOutcome(prediction=pred, outcome=MatchOutcome.TRUE_POSITIVE, matched_expected_id="ef1", detail="")
    expected_outcome = ExpectedFindingOutcome(
        expected=_case().expected[0], outcome=ExpectedOutcome.FOUND, matched_prediction_index=0, detail="",
    )
    return CaseResult(
        case_id="c1", mode=EvaluationMode.FULL_PIPELINE, status=CaseStatus.COMPLETED_WITH_FINDINGS, duration_ms=12.5,
        predictions=(outcome,), expected_outcomes=(expected_outcome,),
    )


def _run_result(*, incremental: tuple[IncrementalScenarioResult, ...] = ()) -> EvaluationRunResult:
    return EvaluationRunResult(
        identity=_identity(), generated_at="2026-01-01T00:00:00Z", duration_ms=100.0,
        case_results=(_case_result(),), incremental_scenarios=incremental,
    )


def _fixture_info() -> dict[str, FixtureInfo]:
    return {"c1": FixtureInfo(valid_file_paths=frozenset({"a.py"}), file_line_counts={"a.py": 10})}


def test_build_report_is_json_native() -> None:
    report = build_report(_run_result(), cases_by_id={"c1": _case()}, fixture_info=_fixture_info())
    # Every StrEnum/dataclass must already be normalized -- a second
    # json.dumps must never raise.
    json.dumps(report)
    assert report["metrics"]["overall"]["confusion"]["true_positives"] == 1
    assert report["identity"]["mode"] == "full_pipeline"


def test_build_report_incremental_section_absent_when_no_scenarios() -> None:
    report = build_report(_run_result(), cases_by_id={"c1": _case()}, fixture_info=_fixture_info())
    assert report["incremental"] is None


def test_build_report_incremental_section_present_and_summarized() -> None:
    scenario = IncrementalScenarioResult(
        scenario_id="s1", description="d", passed=True, provider_calls_full=2, provider_calls_incremental=1,
        provider_calls_avoided=1, unsafe_carry_forward=False, detail="ok",
    )
    report = build_report(_run_result(incremental=(scenario,)), cases_by_id={"c1": _case()}, fixture_info=_fixture_info())
    inc = report["incremental"]
    assert inc["scenarios_total"] == 1
    assert inc["scenarios_passed"] == 1
    assert inc["total_provider_calls_avoided"] == 1
    assert inc["unsafe_carry_forward_count"] == 0


def test_write_json_then_read_json_round_trips(tmp_path: Path) -> None:
    report = build_report(_run_result(), cases_by_id={"c1": _case()}, fixture_info=_fixture_info())
    path = tmp_path / "nested" / "report.json"
    write_json(report, path)
    assert path.is_file()
    loaded = read_json(path)
    assert loaded == report


def test_render_markdown_contains_key_sections() -> None:
    report = build_report(_run_result(), cases_by_id={"c1": _case()}, fixture_info=_fixture_info())
    markdown = render_markdown(report)
    assert "# PatchFrog Quality Evaluation Report" in markdown
    assert "## Overall" in markdown
    assert "## Safety" in markdown
    assert "## Category breakdown" in markdown
    assert "Precision:" in markdown


def test_render_markdown_includes_incremental_scenario_lines() -> None:
    scenario = IncrementalScenarioResult(
        scenario_id="unsafe_one", description="d", passed=False, provider_calls_full=1, provider_calls_incremental=1,
        provider_calls_avoided=0, unsafe_carry_forward=True, detail="bad",
    )
    report = build_report(_run_result(incremental=(scenario,)), cases_by_id={"c1": _case()}, fixture_info=_fixture_info())
    markdown = render_markdown(report)
    assert "unsafe_one" in markdown
    assert "[FAIL]" in markdown
