"""HTTP fetching for watchers: a small protocol, typed errors, a safe real
implementation and an in-memory fake (M11.2/M11.4).

No watcher adapter talks to the network directly; they receive a
:class:`Fetcher`. Tests use :class:`FakeFetcher`; the hosted service uses
:class:`HttpxFetcher`. Neither ever logs a header value.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from patchfrog.watchers.net import Resolver, UnsafeUrlError, validate_public_url

DEFAULT_MAX_BYTES = 5_000_000
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 3
USER_AGENT = "patchfrog-upstream-watcher/1 (+https://github.com/kadireren7/patchfrog)"


class WatcherError(Exception):
    #: Whether retrying later can help.
    retryable: bool = False


class RateLimitedError(WatcherError):
    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class TransientFetchError(WatcherError):
    retryable = True


class PermanentFetchError(WatcherError):
    retryable = False


class UnsafeSourceError(PermanentFetchError):
    pass


@dataclass(frozen=True, slots=True)
class FetchResponse:
    status: int
    body: bytes = b""
    etag: str | None = None
    #: Seconds, from ``Retry-After`` / rate-limit reset headers, if given.
    retry_after_seconds: float | None = None
    rate_limit_remaining: int | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class Fetcher(Protocol):
    async def get(
        self, url: str, *, headers: Mapping[str, str] | None = None, max_bytes: int = DEFAULT_MAX_BYTES
    ) -> FetchResponse: ...


def raise_for_response(response: FetchResponse, *, what: str) -> None:
    """Maps an HTTP status to a typed error. 2xx and 304 return normally."""

    status = response.status
    if 200 <= status < 300 or status == 304:
        return
    if status == 429 or (status == 403 and response.rate_limit_remaining == 0):
        raise RateLimitedError(f"{what}: rate limited (HTTP {status})", retry_after_seconds=response.retry_after_seconds)
    if status in (408, 425) or 500 <= status < 600:
        raise TransientFetchError(f"{what}: upstream error (HTTP {status})")
    raise PermanentFetchError(f"{what}: HTTP {status}")


def backoff_delay_seconds(
    attempt: int,
    *,
    base: float = 30.0,
    cap: float = 3600.0,
    retry_after: float | None = None,
    jitter: Callable[[], float] = random.random,
) -> float:
    """Exponential backoff with full jitter on the upper half; a server's
    ``Retry-After`` is a floor. ``attempt`` starts at 1. Pure given ``jitter``."""

    if attempt < 1:
        raise ValueError("attempt starts at 1")
    ceiling = min(cap, base * float(2 ** (attempt - 1)))
    delay = ceiling / 2 + (ceiling / 2) * jitter()
    if retry_after is not None:
        delay = max(delay, min(retry_after, cap))
    return min(delay, cap)


class HttpxFetcher:
    """Real fetcher. Every request (and every redirect hop) is validated as a
    public URL; responses are size-capped; no cookies; ``auth_headers`` are
    only ever sent to the exact host they were registered for."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        auth_headers: Mapping[str, Mapping[str, str]] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        allow_http: bool = False,
        resolver: Resolver | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._auth = {host.lower(): dict(h) for host, h in (auth_headers or {}).items()}
        self._allow_http = allow_http
        self._resolver = resolver

    async def get(
        self, url: str, *, headers: Mapping[str, str] | None = None, max_bytes: int = DEFAULT_MAX_BYTES
    ) -> FetchResponse:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                await validate_public_url(current, allow_http=self._allow_http, resolver=self._resolver)
            except UnsafeUrlError as exc:
                raise UnsafeSourceError(f"refusing to fetch: {exc}") from exc
            host = (urlsplit(current).hostname or "").lower()
            request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, application/yaml, */*"}
            request_headers.update(headers or {})
            request_headers.update(self._auth.get(host, {}))
            try:
                async with self._client.stream("GET", current, headers=request_headers) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise PermanentFetchError("redirect without a location")
                        current = urljoin(current, location)
                        continue
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise PermanentFetchError(f"response larger than {max_bytes} bytes")
                        chunks.append(chunk)
                    return FetchResponse(
                        status=response.status_code, body=b"".join(chunks), etag=response.headers.get("etag"),
                        retry_after_seconds=_retry_after(response.headers),
                        rate_limit_remaining=_int_header(response.headers.get("x-ratelimit-remaining")),
                    )
            except httpx.TimeoutException as exc:
                raise TransientFetchError("request timed out") from exc
            except httpx.TransportError as exc:
                raise TransientFetchError(f"transport error: {exc.__class__.__name__}") from exc
        raise PermanentFetchError("too many redirects")

    async def aclose(self) -> None:
        await self._client.aclose()


def _int_header(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None
    return None


@dataclass
class FakeFetcher:
    """Deterministic in-memory fetcher: ``routes`` maps a URL to a response
    or an exception to raise. Records every call (URL and header *names*)."""

    routes: dict[str, FetchResponse | Exception] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    header_names: list[tuple[str, ...]] = field(default_factory=list)

    async def get(
        self, url: str, *, headers: Mapping[str, str] | None = None, max_bytes: int = DEFAULT_MAX_BYTES
    ) -> FetchResponse:
        self.calls.append(url)
        self.header_names.append(tuple(sorted(headers or {})))
        outcome = self.routes.get(url)
        if outcome is None:
            return FetchResponse(status=404)
        if isinstance(outcome, Exception):
            raise outcome
        if len(outcome.body) > max_bytes:
            raise PermanentFetchError(f"response larger than {max_bytes} bytes")
        return outcome


__all__ = [
    "DEFAULT_MAX_BYTES",
    "FakeFetcher",
    "FetchResponse",
    "Fetcher",
    "HttpxFetcher",
    "PermanentFetchError",
    "RateLimitedError",
    "TransientFetchError",
    "UnsafeSourceError",
    "WatcherError",
    "backoff_delay_seconds",
    "raise_for_response",
]
