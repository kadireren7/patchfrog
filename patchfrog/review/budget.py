"""Provider-neutral review budget and deterministic cost accounting."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from patchfrog.review.provider import ProviderIdentity, ProviderUsage


class BudgetTerminationReason(StrEnum):
    PROVIDER_CALLS = "max_provider_calls"
    RETRIES = "max_retry_attempts"
    INPUT_TOKENS = "max_input_tokens"
    OUTPUT_TOKENS = "max_output_tokens"
    ESTIMATED_COST = "max_estimated_cost"
    PRICING_UNAVAILABLE = "pricing_unavailable"
    ELAPSED_TIME = "max_elapsed_time"


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: BudgetTerminationReason) -> None:
        super().__init__(f"review budget exhausted: {reason.value}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_usd_per_million_tokens: float
    output_usd_per_million_tokens: float

    def estimate(self, *, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_usd_per_million_tokens
            + output_tokens * self.output_usd_per_million_tokens
        ) / 1_000_000


class PricingCatalog:
    """Immutable operator-supplied pricing; no vendor/model is hardcoded."""

    def __init__(self, prices: Mapping[str, ModelPricing] | None = None) -> None:
        self._prices = MappingProxyType(dict(prices or {}))

    @staticmethod
    def key(identity: ProviderIdentity) -> str:
        return f"{identity.provider}/{identity.model}"

    def get(self, identity: ProviderIdentity) -> ModelPricing | None:
        return self._prices.get(self.key(identity))

    @classmethod
    def from_config(cls, raw: Mapping[str, Mapping[str, float]]) -> PricingCatalog:
        prices: dict[str, ModelPricing] = {}
        for key, values in raw.items():
            input_rate = float(values.get("input_usd_per_million_tokens", 0.0))
            output_rate = float(values.get("output_usd_per_million_tokens", 0.0))
            if input_rate < 0 or output_rate < 0:
                raise ValueError(f"provider pricing rates must be non-negative for {key!r}")
            prices[key] = ModelPricing(input_rate, output_rate)
        return cls(prices)


@dataclass(frozen=True, slots=True)
class CostBudget:
    max_provider_calls: int
    max_retry_attempts: int
    max_input_tokens: int
    max_output_tokens: int
    max_estimated_cost_usd: float | None = None
    max_elapsed_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class CallReservation:
    identity: ProviderIdentity
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float


@dataclass(frozen=True, slots=True)
class ProviderCostMetric:
    provider: str
    model: str
    call_count: int
    retry_count: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


@dataclass(frozen=True, slots=True)
class ReviewBudgetSnapshot:
    provider_calls: int
    retry_attempts: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    elapsed_seconds: float
    termination_reason: BudgetTerminationReason | None
    by_model: tuple[ProviderCostMetric, ...]


@dataclass(slots=True)
class _MutableMetric:
    provider: str
    model: str
    call_count: int = 0
    retry_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


class ReviewBudget:
    """Atomic ledger shared by every reviewer, critic, retry, and fallback."""

    def __init__(
        self,
        limits: CostBudget,
        *,
        pricing: PricingCatalog | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._pricing = pricing or PricingCatalog()
        self._monotonic = monotonic
        self._started_at = self._now()
        self._lock = asyncio.Lock()
        self._provider_calls = 0
        self._retry_attempts = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._estimated_cost_usd = 0.0
        self._termination_reason: BudgetTerminationReason | None = None
        self._by_model: dict[str, _MutableMetric] = {}

    def _now(self) -> float:
        return float(self._monotonic())

    def _cost(self, identity: ProviderIdentity, input_tokens: int, output_tokens: int) -> float:
        price = self._pricing.get(identity)
        return price.estimate(input_tokens=input_tokens, output_tokens=output_tokens) if price else 0.0

    def _deny(self, reason: BudgetTerminationReason) -> None:
        if self._termination_reason is None:
            self._termination_reason = reason
        raise BudgetExceeded(reason)

    async def reserve_call(
        self,
        identity: ProviderIdentity,
        *,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        is_retry: bool,
    ) -> CallReservation:
        async with self._lock:
            if self._termination_reason is not None:
                raise BudgetExceeded(self._termination_reason)
            elapsed = self._now() - self._started_at
            if self.limits.max_elapsed_seconds is not None and elapsed >= self.limits.max_elapsed_seconds:
                self._deny(BudgetTerminationReason.ELAPSED_TIME)
            if self._provider_calls + 1 > self.limits.max_provider_calls:
                self._deny(BudgetTerminationReason.PROVIDER_CALLS)
            if is_retry and self._retry_attempts + 1 > self.limits.max_retry_attempts:
                self._deny(BudgetTerminationReason.RETRIES)
            if self._input_tokens + estimated_input_tokens > self.limits.max_input_tokens:
                self._deny(BudgetTerminationReason.INPUT_TOKENS)
            if self._output_tokens + estimated_output_tokens > self.limits.max_output_tokens:
                self._deny(BudgetTerminationReason.OUTPUT_TOKENS)
            price = self._pricing.get(identity)
            if self.limits.max_estimated_cost_usd is not None and price is None:
                self._deny(BudgetTerminationReason.PRICING_UNAVAILABLE)
            estimated_cost = self._cost(identity, estimated_input_tokens, estimated_output_tokens)
            if (
                self.limits.max_estimated_cost_usd is not None
                and self._estimated_cost_usd + estimated_cost > self.limits.max_estimated_cost_usd
            ):
                self._deny(BudgetTerminationReason.ESTIMATED_COST)

            self._provider_calls += 1
            self._retry_attempts += int(is_retry)
            self._input_tokens += estimated_input_tokens
            self._output_tokens += estimated_output_tokens
            self._estimated_cost_usd += estimated_cost
            key = self._pricing.key(identity)
            metric = self._by_model.setdefault(
                key, _MutableMetric(provider=identity.provider, model=identity.model)
            )
            metric.call_count += 1
            metric.retry_count += int(is_retry)
            metric.input_tokens += estimated_input_tokens
            metric.output_tokens += estimated_output_tokens
            metric.estimated_cost_usd += estimated_cost
            return CallReservation(identity, estimated_input_tokens, estimated_output_tokens, estimated_cost)

    async def terminate(self, reason: BudgetTerminationReason) -> None:
        """Record an external ceiling (for example an asyncio wall-clock timeout)."""

        async with self._lock:
            if self._termination_reason is None:
                self._termination_reason = reason

    async def reconcile(self, reservation: CallReservation, usage: ProviderUsage) -> None:
        """Replace a successful call's conservative reservation with actual usage."""

        async with self._lock:
            actual_cost = self._cost(reservation.identity, usage.input_tokens, usage.output_tokens)
            input_delta = usage.input_tokens - reservation.estimated_input_tokens
            output_delta = usage.output_tokens - reservation.estimated_output_tokens
            cost_delta = actual_cost - reservation.estimated_cost_usd
            self._input_tokens = max(0, self._input_tokens + input_delta)
            self._output_tokens = max(0, self._output_tokens + output_delta)
            self._estimated_cost_usd = max(0.0, self._estimated_cost_usd + cost_delta)
            metric = self._by_model[self._pricing.key(reservation.identity)]
            metric.input_tokens = max(0, metric.input_tokens + input_delta)
            metric.output_tokens = max(0, metric.output_tokens + output_delta)
            metric.estimated_cost_usd = max(0.0, metric.estimated_cost_usd + cost_delta)
            if self._termination_reason is None:
                if self._input_tokens >= self.limits.max_input_tokens:
                    self._termination_reason = BudgetTerminationReason.INPUT_TOKENS
                elif self._output_tokens >= self.limits.max_output_tokens:
                    self._termination_reason = BudgetTerminationReason.OUTPUT_TOKENS
                elif (
                    self.limits.max_estimated_cost_usd is not None
                    and self._estimated_cost_usd >= self.limits.max_estimated_cost_usd
                ):
                    self._termination_reason = BudgetTerminationReason.ESTIMATED_COST

    async def snapshot(self) -> ReviewBudgetSnapshot:
        async with self._lock:
            metrics = tuple(
                ProviderCostMetric(
                    provider=m.provider,
                    model=m.model,
                    call_count=m.call_count,
                    retry_count=m.retry_count,
                    input_tokens=m.input_tokens,
                    output_tokens=m.output_tokens,
                    estimated_cost_usd=m.estimated_cost_usd,
                )
                for _, m in sorted(self._by_model.items())
            )
            return ReviewBudgetSnapshot(
                provider_calls=self._provider_calls,
                retry_attempts=self._retry_attempts,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                estimated_cost_usd=self._estimated_cost_usd,
                elapsed_seconds=max(0.0, self._now() - self._started_at),
                termination_reason=self._termination_reason,
                by_model=metrics,
            )


__all__ = [
    "BudgetExceeded",
    "BudgetTerminationReason",
    "CallReservation",
    "CostBudget",
    "ModelPricing",
    "PricingCatalog",
    "ProviderCostMetric",
    "ReviewBudget",
    "ReviewBudgetSnapshot",
]
