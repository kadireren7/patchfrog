from __future__ import annotations

import pytest

from patchfrog.review.provider import (
    ProviderInsufficientQuotaError,
    ProviderRateLimitError,
    ProviderServerError,
)
from patchfrog.review.retry import MAX_RETRY_DELAY_SECONDS, call_with_retry


async def test_insufficient_quota_has_zero_retries() -> None:
    calls = 0

    async def attempt() -> None:
        nonlocal calls
        calls += 1
        raise ProviderInsufficientQuotaError("credit balance exhausted")

    with pytest.raises(ProviderInsufficientQuotaError):
        await call_with_retry(attempt, max_retries=5, base_delay=0)
    assert calls == 1


async def test_transient_server_error_uses_bounded_retry() -> None:
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        raise ProviderServerError("temporary")

    with pytest.raises(ProviderServerError):
        await call_with_retry(attempt, max_retries=2, base_delay=0)
    assert calls == 3


async def test_retry_after_hint_is_honored_over_exponential_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("patchfrog.review.retry.asyncio.sleep", fake_sleep)

    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderRateLimitError("rate limited", retry_after_seconds=12.5)
        return "ok"

    result, retries = await call_with_retry(attempt, max_retries=2, base_delay=1.0)
    assert result == "ok"
    assert retries == 1
    assert sleep_calls == [12.5]


async def test_retry_after_hint_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("patchfrog.review.retry.asyncio.sleep", fake_sleep)

    async def attempt() -> None:
        raise ProviderRateLimitError("rate limited", retry_after_seconds=10_000.0)

    with pytest.raises(ProviderRateLimitError):
        await call_with_retry(attempt, max_retries=1, base_delay=1.0)
    assert sleep_calls == [MAX_RETRY_DELAY_SECONDS]


async def test_missing_retry_after_falls_back_to_exponential_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("patchfrog.review.retry.asyncio.sleep", fake_sleep)

    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ProviderServerError("temporary")
        return "ok"

    result, retries = await call_with_retry(attempt, max_retries=2, base_delay=0.5)
    assert result == "ok"
    assert retries == 2
    assert sleep_calls == [0.5, 1.0]
