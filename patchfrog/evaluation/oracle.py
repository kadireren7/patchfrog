"""Deterministic "pipeline correctness" oracle for :class:`FakeLLMProvider`.

Builds a scripted response factory keyed on one :class:`EvaluationCase`'s
own committed ``ai_expected``/``either`` :class:`ExpectedFinding` rows, so
a corpus run under :class:`~patchfrog.review.providers.fake.FakeLLMProvider`
proves the *plumbing* -- candidate generation -> context -> prompt ->
validation -> critic -> confidence -> persistence -- correctly carries a
finding that matches ground truth through to an accepted finding, and
correctly produces zero findings for a case whose ground truth expects
none.

This is explicitly NOT an AI-quality benchmark (Phase 8 spec section 8):
the oracle already knows the answer, so a perfect score here proves
nothing about whether a real model would have found the bug. Every
report/CLI surface using this provider must label its results
"pipeline correctness benchmark", never "AI quality benchmark" -- see
:mod:`patchfrog.evaluation.reporting`.

Reads quoted evidence from the case's *committed* fixture tree
(``cases_root / case.id / "repo"``), not a materialized copy -- identical
bytes either way (see :func:`patchfrog.evaluation.fixtures.materialize_case_repo`),
and avoids a materialize-before-you-can-build-the-provider ordering
dependency.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from patchfrog.analysis.domain import Severity
from patchfrog.evaluation.domain import EvaluationCase, ExpectedFinding, GroundTruthSource
from patchfrog.review.provider import ProviderRequest
from patchfrog.review.providers.fake import ScriptedResponse

_ACCEPT_VERDICT = ScriptedResponse(
    raw_json=json.dumps(
        {
            "decision": "accept",
            "reasoning_summary": "oracle: matches committed ground truth",
            "downgraded_severity": None,
            "downgraded_confidence": None,
        }
    )
)
_NO_FINDINGS = ScriptedResponse(raw_json=json.dumps({"findings": []}))


def build_oracle_response_factory(
    case: EvaluationCase, *, cases_root: Path
) -> Callable[[ProviderRequest], ScriptedResponse]:
    repo_root = cases_root / case.id / "repo"
    ai_expected = [
        e
        for e in case.expected
        if e.ground_truth_source in (GroundTruthSource.AI_EXPECTED, GroundTruthSource.EITHER) and e.symbol
    ]

    def factory(request: ProviderRequest) -> ScriptedResponse:
        if request.schema_name == "critic_verdict":
            return _ACCEPT_VERDICT
        target = _review_target(request.user_prompt)
        if target is None:
            return _NO_FINDINGS
        matches = [e for e in ai_expected if _symbol_matches(e.symbol, target)]
        if not matches:
            return _NO_FINDINGS
        findings = [_oracle_finding(e, repo_root=repo_root) for e in matches]
        return ScriptedResponse(raw_json=json.dumps({"findings": findings}))

    return factory


def _review_target(user_prompt: str) -> str | None:
    for line in user_prompt.splitlines():
        if line.startswith("Review target: `"):
            return line.split("`")[1]
    return None


def _symbol_matches(expected_symbol: str | None, candidate_target: str) -> bool:
    """Mirrors :func:`patchfrog.evaluation.matcher._symbol_matches`'s
    tolerant qualified-name suffix rule -- kept as an independent, small
    inline check rather than importing a private matcher helper across
    modules."""

    if expected_symbol is None:
        return False
    if candidate_target == expected_symbol:
        return True
    return candidate_target.endswith(f".{expected_symbol}") or candidate_target.endswith(f"::{expected_symbol}")


def _oracle_finding(expected: ExpectedFinding, *, repo_root: Path) -> dict[str, Any]:
    line = expected.line if expected.line is not None else 1
    line_end = expected.line_end if expected.line_end is not None else line
    quoted = _quote_lines(repo_root / expected.file, line, line_end)
    severity = expected.severity or expected.severity_max or expected.severity_min or Severity.MEDIUM
    label = expected.issue_family or expected.id
    return {
        "title": f"[oracle] {label}",
        "message": expected.notes or f"ground-truth {expected.category.value} issue: {label}",
        "category": expected.category.value,
        "severity": severity.value,
        "confidence": "high",
        "file_path": expected.file,
        "start_line": line,
        "end_line": line_end,
        "evidence": [
            {"file_path": expected.file, "start_line": line, "end_line": line_end, "quoted_text": quoted}
        ],
        "reasoning_summary": "oracle-generated verbatim from committed ground truth",
        "suggested_fix": None,
    }


def _quote_lines(path: Path, start: int, end: int) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(errors="replace").splitlines()
    if start < 1 or end < start or end > len(lines):
        return ""
    return "\n".join(lines[start - 1 : end]).strip()
