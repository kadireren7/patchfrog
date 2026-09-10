from __future__ import annotations

from patchfrog.merge_readiness.domain import (
    MERGE_READINESS_VERSION,
    MergeReadinessDecision,
    MergeReadinessReasonCode,
)


def test_version_is_one() -> None:
    assert MERGE_READINESS_VERSION == 1


def test_decision_has_exactly_three_members_no_unknown() -> None:
    assert {d.value for d in MergeReadinessDecision} == {"ready", "blocked", "human_review_required"}
    assert not any(d.value == "unknown" for d in MergeReadinessDecision)


def test_reason_codes_are_bounded_typed_values() -> None:
    expected = {
        "no_unresolved_evidence",
        "unresolved_blocking_finding",
        "stale_review",
        "review_incomplete",
        "high_impact_inconclusive",
        "blocking_fix_not_verified",
    }
    assert {r.value for r in MergeReadinessReasonCode} == expected


def test_merge_readiness_never_imports_a_provider() -> None:
    # Merge Readiness is a deterministic synthesis over already-persisted
    # evidence -- it must never ask an LLM "should this PR merge."
    import ast
    from pathlib import Path

    package_dir = Path(__file__).parent.parent.parent / "patchfrog" / "merge_readiness"
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert "LLMProvider" not in (node.module or "") and not any(
                    alias.name == "LLMProvider" for alias in node.names
                ), f"{path} imports LLMProvider -- Merge Readiness must never call a provider"
