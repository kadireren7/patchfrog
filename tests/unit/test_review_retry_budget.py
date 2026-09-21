from __future__ import annotations

import pytest

from patchfrog.review.provider import (
    ProviderInsufficientQuotaError,
    ProviderServerError,
)
from patchfrog.review.retry import call_with_retry


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
