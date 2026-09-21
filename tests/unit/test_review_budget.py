from __future__ import annotations

import pytest

from patchfrog.review.budget import (
    BudgetExceeded,
    BudgetTerminationReason,
    CostBudget,
    ModelPricing,
    PricingCatalog,
    ReviewBudget,
)
from patchfrog.review.provider import ProviderIdentity, ProviderUsage

_IDENTITY = ProviderIdentity(provider="fake", model="cheap")


def _limits(**overrides: object) -> CostBudget:
    values: dict[str, object] = {
        "max_provider_calls": 4,
        "max_retry_attempts": 2,
        "max_input_tokens": 10_000,
        "max_output_tokens": 2_000,
        "max_estimated_cost_usd": None,
        "max_elapsed_seconds": 60.0,
    }
    values.update(overrides)
    return CostBudget(**values)  # type: ignore[arg-type]


async def test_fake_pricing_accounts_by_provider_and_model() -> None:
    budget = ReviewBudget(
        _limits(),
        pricing=PricingCatalog(
            {"fake/cheap": ModelPricing(input_usd_per_million_tokens=1.0, output_usd_per_million_tokens=2.0)}
        ),
    )
    reservation = await budget.reserve_call(
        _IDENTITY, estimated_input_tokens=1_000, estimated_output_tokens=200, is_retry=False
    )
    await budget.reconcile(reservation, ProviderUsage(input_tokens=800, output_tokens=100))

    snapshot = await budget.snapshot()
    assert snapshot.provider_calls == 1
    assert snapshot.input_tokens == 800
    assert snapshot.output_tokens == 100
    assert snapshot.estimated_cost_usd == pytest.approx(0.001)
    assert snapshot.by_model[0].provider == "fake"
    assert snapshot.by_model[0].model == "cheap"


async def test_cost_ceiling_stops_before_overspend() -> None:
    budget = ReviewBudget(
        _limits(max_estimated_cost_usd=0.0005),
        pricing=PricingCatalog(
            {"fake/cheap": ModelPricing(input_usd_per_million_tokens=1.0, output_usd_per_million_tokens=1.0)}
        ),
    )

    with pytest.raises(BudgetExceeded) as raised:
        await budget.reserve_call(
            _IDENTITY, estimated_input_tokens=500, estimated_output_tokens=100, is_retry=False
        )

    assert raised.value.reason is BudgetTerminationReason.ESTIMATED_COST
    snapshot = await budget.snapshot()
    assert snapshot.provider_calls == 0
    assert snapshot.termination_reason is BudgetTerminationReason.ESTIMATED_COST


async def test_monetary_ceiling_requires_operator_pricing() -> None:
    budget = ReviewBudget(_limits(max_estimated_cost_usd=1.0))

    with pytest.raises(BudgetExceeded) as raised:
        await budget.reserve_call(
            _IDENTITY, estimated_input_tokens=1, estimated_output_tokens=1, is_retry=False
        )

    assert raised.value.reason is BudgetTerminationReason.PRICING_UNAVAILABLE


async def test_review_wide_retry_budget_is_bounded() -> None:
    budget = ReviewBudget(_limits(max_retry_attempts=1))
    await budget.reserve_call(
        _IDENTITY, estimated_input_tokens=10, estimated_output_tokens=10, is_retry=True
    )
    with pytest.raises(BudgetExceeded) as raised:
        await budget.reserve_call(
            _IDENTITY, estimated_input_tokens=10, estimated_output_tokens=10, is_retry=True
        )
    assert raised.value.reason is BudgetTerminationReason.RETRIES


async def test_elapsed_ceiling_is_checked_before_call() -> None:
    ticks = iter((10.0, 12.1, 12.1))
    budget = ReviewBudget(_limits(max_elapsed_seconds=2.0), monotonic=lambda: next(ticks))
    with pytest.raises(BudgetExceeded) as raised:
        await budget.reserve_call(
            _IDENTITY, estimated_input_tokens=1, estimated_output_tokens=1, is_retry=False
        )
    assert raised.value.reason is BudgetTerminationReason.ELAPSED_TIME
