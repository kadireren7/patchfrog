"""Shared bounded-retry helper for provider calls.

Extracted from :mod:`patchfrog.review.service` so
:mod:`patchfrog.review.orchestration` (which now makes multiple
concurrent specialist-agent calls per candidate, not just one) can reuse
the identical retry policy without duplicating it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from patchfrog.review.provider import ProviderTransientError


async def call_with_retry[T](
    coro_factory: Callable[[], Awaitable[T]], *, max_retries: int, base_delay: float = 0.5
) -> tuple[T, int]:
    """Bounded retry, transient failures only. ``ProviderFatalError`` and
    :class:`~patchfrog.review.validation.ResponseSchemaError` propagate
    immediately -- retrying a schema-invalid or auth/400 response would
    just reproduce the identical failure. A fatal error therefore never
    consumes any of the caller's retry allowance.

    Returns ``(result, retries_used)`` -- the Quality + Cost Guard
    (:mod:`patchfrog.review.effort`) needs the actual retry count
    consumed by each candidate for cost/audit accounting, not just the
    final outcome.
    """

    attempt = 0
    while True:
        try:
            return await coro_factory(), attempt
        except ProviderTransientError:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(base_delay * (2**attempt))
            attempt += 1
