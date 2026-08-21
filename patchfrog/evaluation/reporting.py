"""JSON and Markdown report generation.

Evaluation results are file artifacts by default -- no database
persistence (see :mod:`patchfrog.evaluation.queries` for why). The JSON
report is the canonical machine-readable artifact (mandatory,
consumable without a database, usable by CI later); Markdown is a
rendering of the same data for PR artifacts / human review.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from patchfrog.evaluation.domain import EvaluationCase, EvaluationRunResult, FixtureInfo
from patchfrog.evaluation.metrics import compute_metrics


def build_report(
    result: EvaluationRunResult,
    *,
    cases_by_id: Mapping[str, EvaluationCase],
    fixture_info: Mapping[str, FixtureInfo],
) -> dict[str, Any]:
    computed_metrics = compute_metrics(result.case_results, cases_by_id=cases_by_id, fixture_info=fixture_info)
    report: dict[str, Any] = {
        "identity": asdict(result.identity),
        "generated_at": result.generated_at,
        "duration_ms": result.duration_ms,
        "metrics": asdict(computed_metrics),
        "incremental": _build_incremental_section(result),
        "case_results": [asdict(r) for r in result.case_results],
    }
    # Round-trip through JSON once: normalizes StrEnum members (already
    # plain str subclasses) and catches any non-JSON-native value early,
    # at report-build time, never at write time.
    return cast("dict[str, Any]", json.loads(json.dumps(report, default=str)))


def _build_incremental_section(result: EvaluationRunResult) -> dict[str, Any] | None:
    scenarios = result.incremental_scenarios
    if not scenarios:
        return None
    return {
        "scenarios": [asdict(s) for s in scenarios],
        "scenarios_total": len(scenarios),
        "scenarios_passed": sum(1 for s in scenarios if s.passed),
        "total_provider_calls_avoided": sum(s.provider_calls_avoided for s in scenarios),
        "unsafe_carry_forward_count": sum(1 for s in scenarios if s.unsafe_carry_forward),
    }


def write_json(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text()))


def render_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    overall = m["overall"]
    clean = m["clean"]
    severity = m["severity"]
    hallucination = m["hallucination"]
    efficiency = m["efficiency"]
    identity = report["identity"]

    lines: list[str] = []
    lines.append("# PatchFrog Quality Evaluation Report")
    lines.append("")
    lines.append(f"Generated: {report['generated_at']}  ")
    lines.append(f"Mode: `{identity['mode']}`  ")
    lines.append(
        f"Provider/model: `{identity['reviewer_provider']}` / `{identity['reviewer_model']}`  "
    )
    lines.append(f"Critic enabled: `{identity['critic_enabled']}`  ")
    lines.append(f"Duration: {report['duration_ms']:.0f} ms")
    lines.append("")

    lines.append("## Overall")
    lines.append("")
    lines.append(f"- Cases: {overall['cases']} (errors: {overall['error_cases']})")
    lines.append(f"- True positives: {overall['confusion']['true_positives']}")
    lines.append(f"- False positives: {overall['confusion']['false_positives']}")
    lines.append(f"- Missed (false negatives): {overall['confusion']['missed']}")
    lines.append(
        f"- Precision: {overall['scores']['precision']:.3f}  Recall: {overall['scores']['recall']:.3f}  "
        f"F1: {overall['scores']['f1']:.3f}"
    )
    lines.append(f"- False positives / case: {overall['false_positives_per_case']:.3f}")
    lines.append(f"- False positives / 100 candidates: {overall['false_positives_per_100_candidates']:.2f}")
    lines.append("")

    lines.append("## Safety")
    lines.append("")
    lines.append(f"- Clean-case pass rate: {clean['pass_rate']:.3f} ({clean['clean_cases_passed']}/{clean['clean_cases']})")
    lines.append(f"- Average findings on clean cases: {clean['average_findings_on_clean_cases']:.3f}")
    if clean["worst_case_id"]:
        lines.append(f"- Worst clean case: `{clean['worst_case_id']}` ({clean['worst_case_finding_count']} findings)")
    lines.append(f"- Duplicate rate: {overall['duplicate_rate']:.3f}")
    lines.append(
        f"- Unsupported (hallucination) rate: before validation "
        f"{hallucination['unsupported_rate_before_validation']:.3f} "
        f"({hallucination['unsupported_before_validation']}/{hallucination['proposals_before_validation']}), "
        f"after validation {hallucination['unsupported_rate_after_validation']:.3f} "
        f"({hallucination['unsupported_after_validation']}/{hallucination['accepted_findings']})"
    )
    lines.append(
        f"- Severity: exact match {severity['exact_match_rate']:.3f}, within one level "
        f"{severity['within_one_level_rate']:.3f}, overstated {severity['overstatement_rate']:.3f}, "
        f"understated {severity['understatement_rate']:.3f} (n={severity['matched_pairs']})"
    )
    lines.append("")

    lines.append("## Category breakdown")
    lines.append("")
    lines.append("| category | support | TP | FP | missed | precision | recall | F1 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for c in m["by_category"]:
        lines.append(
            f"| {c['category']} | {c['support']} | {c['confusion']['true_positives']} | "
            f"{c['confusion']['false_positives']} | {c['confusion']['missed']} | "
            f"{c['scores']['precision']:.3f} | {c['scores']['recall']:.3f} | {c['scores']['f1']:.3f} |"
        )
    lines.append("")

    lines.append("## Difficulty breakdown")
    lines.append("")
    lines.append("| difficulty | support | precision | recall | F1 |")
    lines.append("|---|---:|---:|---:|---:|")
    for d in m["by_difficulty"]:
        lines.append(
            f"| {d['difficulty']} | {d['support']} | {d['scores']['precision']:.3f} | "
            f"{d['scores']['recall']:.3f} | {d['scores']['f1']:.3f} |"
        )
    lines.append("")

    if m["static_ai_overlap"] is not None:
        overlap = m["static_ai_overlap"]
        lines.append("## Static / AI overlap")
        lines.append("")
        lines.append(
            f"- Static-only: {overlap['static_only']}  AI-only: {overlap['ai_only']}  "
            f"Both: {overlap['both']}  Missed by both: {overlap['missed_by_both']}"
        )
        lines.append("")

    lines.append("## Efficiency")
    lines.append("")
    lines.append(
        f"- Candidates: generated {efficiency['candidates_generated']}, reviewed {efficiency['candidates_reviewed']}, "
        f"skipped {efficiency['candidates_skipped']}"
    )
    lines.append(
        f"- Provider calls: {efficiency['provider_calls']}  "
        f"({efficiency['provider_calls_per_tp']:.2f} / true positive)"
    )
    lines.append(
        f"- Tokens per true positive: input {efficiency['input_tokens_per_tp']:.1f}, "
        f"output {efficiency['output_tokens_per_tp']:.1f}"
    )
    lines.append("")

    incremental = report.get("incremental")
    if incremental is not None:
        lines.append("## Incremental review memory")
        lines.append("")
        lines.append(
            f"- Scenarios passed: {incremental['scenarios_passed']}/{incremental['scenarios_total']}"
        )
        lines.append(f"- Total provider calls avoided: {incremental['total_provider_calls_avoided']}")
        lines.append(f"- Unsafe carry-forward count: {incremental['unsafe_carry_forward_count']} (target: 0)")
        lines.append("")
        for s in incremental["scenarios"]:
            mark = "OK" if s["passed"] and not s["unsafe_carry_forward"] else "FAIL"
            lines.append(f"  - [{mark}] `{s['scenario_id']}` -- {s['description']}: {s['detail']}")
        lines.append("")

    worst_fp = m.get("worst_false_positive_cases") or []
    if worst_fp:
        lines.append("## Worst false-positive cases")
        lines.append("")
        for case_id, count in worst_fp:
            lines.append(f"- `{case_id}`: {count} false positive(s)")
        lines.append("")

    missed = m.get("missed_expected") or []
    if missed:
        lines.append("## Missed expected findings")
        lines.append("")
        for case_id, expected_id in missed:
            lines.append(f"- `{case_id}` / `{expected_id}`")
        lines.append("")

    return "\n".join(lines)
