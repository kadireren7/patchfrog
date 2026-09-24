"""M4.10 acceptance: the deterministic cost benchmark meets every target
without losing a finding. Fake provider, synthetic prices, no network."""

from __future__ import annotations

from patchfrog.evaluation.cost_benchmark import build_cost_benchmark_report, run_cost_benchmark

_TARGETS = {
    "comment_only": 0,
    "docs_only": 0,
    "tiny_code": 1,
    "normal_correctness_bug": 2,
    "medium_cross_module": 3,
    "auth_sensitive": 5,
    "schema_migration": 5,
    "public_api_change": 3,
    "test_only": 1,
    "exact_head_repeat": 0,
}
_EXPECTED_TIERS = {
    "comment_only": "no_ai",
    "docs_only": "no_ai",
    "tiny_code": "tiny",
    "normal_correctness_bug": "normal",
    "medium_cross_module": "elevated",
    "auth_sensitive": "high_risk",
    "schema_migration": "high_risk",
    "public_api_change": "elevated",
    "test_only": "tiny",
}


async def test_cost_benchmark_meets_every_m4_target_without_losing_findings() -> None:
    report = build_cost_benchmark_report(await run_cost_benchmark())
    by_id = {row["id"]: row for row in report["scenarios"]}

    assert set(by_id) == set(_TARGETS)
    for scenario_id, target in _TARGETS.items():
        after = by_id[scenario_id]["after"]
        assert after["provider_calls"] <= target, (scenario_id, after)
        if scenario_id in _EXPECTED_TIERS:
            assert after["risk_tier"] == _EXPECTED_TIERS[scenario_id], (scenario_id, after)
    assert report["all_targets_met"]
    assert report["all_findings_preserved"]

    # Exact-head repeat: zero new calls, served from the stored run.
    assert by_id["exact_head_repeat"]["after"]["cache_hit"] is True
    # Every extra (escalation) call carries a reason.
    assert by_id["auth_sensitive"]["after"]["escalation_reasons"] == ["security_sensitive_change"]
    assert by_id["tiny_code"]["after"]["escalation_reasons"] == []
    # Overall: strictly cheaper than the pre-M4 fan-out on this corpus.
    totals = report["totals"]
    assert totals["after"]["provider_calls"] < totals["before"]["provider_calls"]
    assert totals["after"]["estimated_input_tokens"] < totals["before"]["estimated_input_tokens"]
    assert totals["after"]["accepted_findings"] == totals["before"]["accepted_findings"]
    assert report["pricing"]["synthetic"] is True


async def test_cost_benchmark_is_deterministic() -> None:
    first = build_cost_benchmark_report(await run_cost_benchmark())
    second = build_cost_benchmark_report(await run_cost_benchmark())
    assert first == second
