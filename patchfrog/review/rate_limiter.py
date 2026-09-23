"""Process-wide provider request-rate limiter.

A free-tier provider quota (e.g. Gemini's observed
``GenerateRequestsPerMinutePerProjectPerModel`` ceiling of 5) is a hard
per-minute cap PatchFrog's own concurrency can blow through on its own,
with no external traffic at all: specialist-role fan-out
(:mod:`patchfrog.review.orchestration` runs reviewer roles concurrently
via ``asyncio.gather``), critic verification, and bounded retries can
each add another provider call within the same second. This module
makes exceeding a configured ceiling structurally impossible for any
call that goes through it: every call acquires a slot from a shared,
per ``(provider, model)`` sliding-window limiter *before* the network
request is made. Acquisition blocks (a single computed ``asyncio.sleep``,
never a poll loop) rather than raising -- a burst is smoothed into legal
spacing, never rejected outright.

**Scope (v1, honest boundary, not a silent gap)**: state lives in
process memory, keyed by ``(provider, model)`` -- Gemini's own quota
unit -- and is shared by every review run in one worker process for
that process's lifetime. It does not coordinate across multiple worker
processes or machines; a self-hosted deployment running a single
low-concurrency worker against one free-tier project (exactly the case
this exists to protect) gets correct enforcement. A distributed
(Redis-backed) limiter would be a deliberate, separate future addition
for a multi-process deployment, not something this module claims to do.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping

from patchfrog.review.provider import LLMProvider, ProviderIdentity, ProviderRequest, ProviderResult

_WINDOW_SECONDS = 60.0


class ProviderRateLimiter:
    """Sliding-window limiter: never lets more than ``requests_per_minute``
    calls through in any trailing 60-second window."""

    def __init__(
        self,
        *,
        requests_per_minute: int,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self._rpm = requests_per_minute
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._timestamps: deque[float] = deque()

    @property
    def requests_per_minute(self) -> int:
        return self._rpm

    async def acquire(self) -> None:
        """Block until a slot is available, then record the call.

        Runs the wait *inside* the lock -- deliberately, so concurrent
        acquirers are serialized into legal spacing rather than all
        waking at once and re-violating the window together (the exact
        failure mode a naive "check, release lock, then sleep" version
        would have).
        """

        async with self._lock:
            self._evict_expired()
            if len(self._timestamps) >= self._rpm:
                wait = _WINDOW_SECONDS - (self._monotonic() - self._timestamps[0])
                if wait > 0:
                    await self._sleep(wait)
                self._evict_expired()
            self._timestamps.append(self._monotonic())

    def _evict_expired(self) -> None:
        cutoff = self._monotonic() - _WINDOW_SECONDS
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()


class RateLimitedProvider:
    """Wraps any real :class:`LLMProvider` with a shared
    :class:`ProviderRateLimiter`, acquiring a slot before every
    ``generate_structured`` call. Reviewer, critic, retry, and fallback
    calls all resolve to calls on the same wrapped instance (see
    :mod:`patchfrog.review.service`/:mod:`patchfrog.review.orchestration`),
    so wrapping once at construction covers every call site without
    threading a limiter through the orchestration layer."""

    def __init__(self, inner: LLMProvider, limiter: ProviderRateLimiter) -> None:
        self._inner = inner
        self._limiter = limiter

    @property
    def identity(self) -> ProviderIdentity:
        return self._inner.identity

    async def generate_structured(self, request: ProviderRequest) -> ProviderResult:
        await self._limiter.acquire()
        return await self._inner.generate_structured(request)


class ProviderRateLimiterRegistry:
    """Process-wide store of one limiter per ``(provider, model)``."""

    def __init__(self) -> None:
        self._limiters: dict[tuple[str, str], ProviderRateLimiter] = {}

    def get(self, *, provider: str, model: str, requests_per_minute: int) -> ProviderRateLimiter:
        key = (provider, model)
        limiter = self._limiters.get(key)
        if limiter is None or limiter.requests_per_minute != requests_per_minute:
            limiter = ProviderRateLimiter(requests_per_minute=requests_per_minute)
            self._limiters[key] = limiter
        return limiter


_default_registry = ProviderRateLimiterRegistry()


def default_rate_limiter_registry() -> ProviderRateLimiterRegistry:
    """The process-wide registry real provider construction wires
    through (see :func:`patchfrog.routing.router._build_provider`).
    A test that needs isolation constructs its own
    :class:`ProviderRateLimiterRegistry` instead of using this one."""

    return _default_registry


def resolve_rate_limit_rpm(limits: Mapping[str, int], *, provider: str, model: str) -> int | None:
    """Look up a configured RPM ceiling for ``provider``/``model``.

    Mirrors :meth:`patchfrog.review.budget.PricingCatalog.key`'s
    ``"{provider}/{model}"`` convention: a model-specific entry wins,
    falling back to a provider-wide entry, falling back to ``None``
    (unlimited -- no wrapping applied) when neither is configured. Never
    a default ceiling of its own: an operator who sets nothing gets
    today's unthrottled behavior, unchanged.
    """

    specific = limits.get(f"{provider}/{model}")
    if specific is not None:
        return specific
    return limits.get(provider)


__all__ = [
    "ProviderRateLimiter",
    "ProviderRateLimiterRegistry",
    "RateLimitedProvider",
    "default_rate_limiter_registry",
    "resolve_rate_limit_rpm",
]
