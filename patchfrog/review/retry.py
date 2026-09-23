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

#: Upper bound on any single retry delay, including a provider-reported
#: ``retry_after_seconds`` hint -- a provider that reports (or a bug that
#: computes) an absurdly long delay must never stall a review run for
#: that long; :class:`~patchfrog.review.budget.ReviewBudget`'s own
#: ``max_elapsed_seconds`` ceiling is the real backstop, but capping the
#: delay here keeps one retry from single-handedly consuming most of it.
MAX_RETRY_DELAY_SECONDS = 65.0


async def call_with_retry[T](
    coro_factory: Callable[[], Awaitable[T]], *, max_retries: int, base_delay: float = 0.5
) -> tuple[T, int]:
    """Bounded retry, transient failures only. ``ProviderFatalError`` and
    :class:`~patchfrog.review.validation.ResponseSchemaError` propagate
    immediately -- retrying a schema-invalid or auth/400 response would
    just reproduce the identical failure. A fatal error therefore never
    consumes any of the caller's retry allowance.

    A transient failure that carries a provider-reported
    ``retry_after_seconds`` (see :class:`~patchfrog.review.provider.ProviderError`;
    Gemini's ``RetryInfo.retryDelay``, Anthropic/OpenAI's ``Retry-After``
    header) waits exactly that long instead of guessing via exponential
    backoff -- the provider is the authority on its own rate-limit
    window, so honoring it is strictly more accurate than blind doubling.
    Falls back to ``base_delay * 2**attempt`` when a transient failure
    didn't report one. Either way the delay is capped at
    :data:`MAX_RETRY_DELAY_SECONDS`.

    Returns ``(result, retries_used)`` -- the Quality + Cost Guard
    (:mod:`patchfrog.review.effort`) needs the actual retry count
    consumed by each candidate for cost/audit accounting, not just the
    final outcome.
    """

    attempt = 0
    while True:
        try:
            return await coro_factory(), attempt
        except ProviderTransientError as exc:
            if attempt >= max_retries:
                raise
            delay = exc.retry_after_seconds
            if delay is None:
                delay = base_delay * (2**attempt)
            await asyncio.sleep(min(max(delay, 0.0), MAX_RETRY_DELAY_SECONDS))
            attempt += 1
