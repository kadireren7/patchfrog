"""M4.8: operator-configurable per-risk-tier provider-call budgets."""

from __future__ import annotations

import pytest

from patchfrog.change_risk import ChangeRiskPolicy, ChangeRiskTier
from patchfrog.config.settings import Settings
from patchfrog.review.config import ReviewModelIdentity
from patchfrog.review.cost_policy import (
    DEFAULT_TIER_PROVIDER_CALL_BUDGETS,
    ReviewCostPolicy,
    ReviewStrategy,
    parse_tier_budgets,
)


def test_default_budgets_match_the_m4_targets() -> None:
    assert dict(DEFAULT_TIER_PROVIDER_CALL_BUDGETS) == {
        ChangeRiskTier.NO_AI: 0,
        ChangeRiskTier.TINY: 2,
        ChangeRiskTier.NORMAL: 2,
        ChangeRiskTier.ELEVATED: 3,
        ChangeRiskTier.HIGH_RISK: 5,
    }
    policy = ReviewCostPolicy()
    # Review-phase (reviewer + escalation) ceilings: TINY is exactly one
    # reviewer call; the rest of each total is the critic's reserve.
    assert [policy.review_call_budget(t) for t in ChangeRiskTier] == [0, 1, 1, 2, 3]


def test_strict_operator_budget_still_allows_one_reviewer_call() -> None:
    policy = ReviewCostPolicy(tier_provider_call_budgets=parse_tier_budgets({"tiny": 1}))
    assert policy.provider_call_budget(ChangeRiskTier.TINY) == 1
    assert policy.review_call_budget(ChangeRiskTier.TINY) == 1


def test_operator_overrides_merge_over_defaults() -> None:
    budgets = parse_tier_budgets({"normal": 3})
    assert budgets[ChangeRiskTier.NORMAL] == 3
    assert budgets[ChangeRiskTier.TINY] == 2


@pytest.mark.parametrize("raw", [{"huge": 1}, {"tiny": -1}, {"no_ai": 1}])
def test_invalid_budget_overrides_fail_closed(raw: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        parse_tier_budgets(raw)


def test_fingerprint_is_stable_and_changes_with_every_policy_dimension() -> None:
    base = ReviewCostPolicy()
    assert base.fingerprint() == ReviewCostPolicy().fingerprint()
    variants = [
        ReviewCostPolicy(strategy=ReviewStrategy.SPECIALIST_FANOUT),
        ReviewCostPolicy(tier_provider_call_budgets=parse_tier_budgets({"tiny": 3})),
        ReviewCostPolicy(risk_policy=ChangeRiskPolicy(tiny_max_semantic_lines=5)),
        ReviewCostPolicy(single_pass_max_input_tokens=1000),
    ]
    fingerprints = {v.fingerprint() for v in variants} | {base.fingerprint()}
    assert len(fingerprints) == len(variants) + 1


def test_cost_policy_is_part_of_review_run_identity_only_when_present() -> None:
    legacy = ReviewModelIdentity(reviewer_provider="fake", reviewer_model="m", critic_provider=None, critic_model=None)
    with_policy = legacy.model_copy(update={"cost_policy_fingerprint": ReviewCostPolicy().fingerprint()})
    other_policy = legacy.model_copy(
        update={"cost_policy_fingerprint": ReviewCostPolicy(single_pass_max_input_tokens=1).fingerprint()}
    )
    assert len({legacy.fingerprint(), with_policy.fingerprint(), other_policy.fingerprint()}) == 3


def _settings() -> Settings:
    # Required settings come from tests/conftest.py's test-only env.
    return Settings()


def test_from_settings_defaults_to_cost_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("PATCHFROG_REVIEW_STRATEGY", "PATCHFROG_RISK_TIER_MAX_PROVIDER_CALLS"):
        monkeypatch.delenv(key, raising=False)
    policy = ReviewCostPolicy.from_settings(_settings())
    assert policy.strategy is ReviewStrategy.COST_AWARE
    assert policy.provider_call_budget(ChangeRiskTier.NORMAL) == 2


def test_from_settings_reads_operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATCHFROG_REVIEW_STRATEGY", "specialist_fanout")
    monkeypatch.setenv("PATCHFROG_RISK_TIER_MAX_PROVIDER_CALLS", '{"high_risk": 4}')
    monkeypatch.setenv("PATCHFROG_REVIEW_EXCLUDE_GENERATED_AND_VENDOR", "false")
    policy = ReviewCostPolicy.from_settings(_settings())
    assert policy.strategy is ReviewStrategy.SPECIALIST_FANOUT
    assert policy.provider_call_budget(ChangeRiskTier.HIGH_RISK) == 4
    assert policy.risk_policy.exclude_generated_and_vendor is False


def test_invalid_operator_env_is_rejected_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATCHFROG_RISK_TIER_MAX_PROVIDER_CALLS", '{"no_ai": 2}')
    with pytest.raises(ValueError, match="no_ai tier budget must be 0"):
        _settings()
