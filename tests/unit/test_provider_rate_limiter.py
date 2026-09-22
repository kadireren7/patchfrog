from __future__ import annotations

import pytest

from patchfrog.review.provider import (
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from patchfrog.review.rate_limiter import (
    ProviderRateLimiter,
    ProviderRateLimiterRegistry,
    RateLimitedProvider,
    resolve_rate_limit_rpm,
)


class _FakeClock:
    """Deterministic, manually-advanced monotonic clock -- no real time
    ever passes in these tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _SleepRecorder:
    """Records every requested delay instead of actually sleeping, and
    advances the fake clock by that amount -- proves the limiter issues
    exactly one computed sleep per throttled acquire, never a poll loop."""

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


async def test_first_n_calls_within_capacity_never_wait() -> None:
    clock = _FakeClock()
    sleeper = _SleepRecorder(clock)
    limiter = ProviderRateLimiter(requests_per_minute=5, monotonic=clock, sleep=sleeper)

    for _ in range(5):
        await limiter.acquire()

    assert sleeper.calls == []


async def test_sixth_call_waits_for_the_oldest_slot_to_expire() -> None:
    clock = _FakeClock()
    sleeper = _SleepRecorder(clock)
    limiter = ProviderRateLimiter(requests_per_minute=5, monotonic=clock, sleep=sleeper)

    for _ in range(5):
        await limiter.acquire()
    await limiter.acquire()

    assert sleeper.calls == [60.0]


async def test_never_busy_loops_at_most_one_sleep_per_acquire() -> None:
    clock = _FakeClock()
    sleeper = _SleepRecorder(clock)
    limiter = ProviderRateLimiter(requests_per_minute=2, monotonic=clock, sleep=sleeper)

    sleeps_per_acquire: list[int] = []
    for _ in range(6):
        before = len(sleeper.calls)
        await limiter.acquire()
        sleeps_per_acquire.append(len(sleeper.calls) - before)

    # A polling implementation would call sleep an unbounded number of
    # times per acquire (a wait loop). This limiter computes the exact
    # wait once and sleeps exactly that long -- never more than one
    # sleep call per acquire.
    assert all(count <= 1 for count in sleeps_per_acquire)
    assert sum(sleeps_per_acquire) >= 1  # capacity 2 with 6 calls must throttle at least once


async def test_spacing_calls_across_the_window_never_waits() -> None:
    clock = _FakeClock()
    sleeper = _SleepRecorder(clock)
    limiter = ProviderRateLimiter(requests_per_minute=5, monotonic=clock, sleep=sleeper)

    for _ in range(5):
        await limiter.acquire()
    clock.advance(60.0)
    await limiter.acquire()

    assert sleeper.calls == []


def test_requests_per_minute_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ProviderRateLimiter(requests_per_minute=0)


async def test_registry_shares_one_limiter_per_provider_model() -> None:
    registry = ProviderRateLimiterRegistry()
    first = registry.get(provider="gemini", model="gemini-3.6-flash", requests_per_minute=5)
    second = registry.get(provider="gemini", model="gemini-3.6-flash", requests_per_minute=5)
    other_model = registry.get(provider="gemini", model="gemini-2.5-flash", requests_per_minute=5)

    assert first is second
    assert first is not other_model


def test_resolve_rate_limit_rpm_prefers_model_specific_entry() -> None:
    limits = {"gemini": 10, "gemini/gemini-3.6-flash": 5}
    assert resolve_rate_limit_rpm(limits, provider="gemini", model="gemini-3.6-flash") == 5
    assert resolve_rate_limit_rpm(limits, provider="gemini", model="some-other-model") == 10
    assert resolve_rate_limit_rpm(limits, provider="openai", model="gpt-x") is None


async def test_rate_limited_provider_acquires_before_delegating() -> None:
    clock = _FakeClock()
    sleeper = _SleepRecorder(clock)
    limiter = ProviderRateLimiter(requests_per_minute=1, monotonic=clock, sleep=sleeper)

    order: list[str] = []

    class _RecordingInner:
        identity = ProviderIdentity(provider="gemini", model="gemini-3.6-flash")

        async def generate_structured(self, request: ProviderRequest) -> ProviderResult:
            order.append("inner_call")
            return ProviderResult(raw_json="{}", usage=ProviderUsage(), latency_ms=1.0)

    wrapped = RateLimitedProvider(_RecordingInner(), limiter)
    request = ProviderRequest(
        system_prompt="s", user_prompt="u", json_schema={}, schema_name="x", max_output_tokens=10
    )

    await wrapped.generate_structured(request)
    await wrapped.generate_structured(request)

    assert order == ["inner_call", "inner_call"]
    assert sleeper.calls == [60.0]
    assert wrapped.identity == ProviderIdentity(provider="gemini", model="gemini-3.6-flash")
