import time


class RateLimiter:
    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: list[float] = []

    def allow(self) -> bool:
        # time.monotonic() is deliberately re-read on every call rather
        # than cached: the whole point of a rate limiter is measuring
        # real elapsed time between calls, so caching "now" would make
        # every window check use a stale timestamp and defeat the
        # limiter entirely.
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < self.window_seconds]
        if len(self._calls) >= self.max_calls:
            return False
        self._calls.append(now)
        return True
