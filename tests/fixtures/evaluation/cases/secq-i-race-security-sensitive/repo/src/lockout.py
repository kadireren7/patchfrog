import asyncio


class LoginAttemptTracker:
    def __init__(self) -> None:
        self._failed_attempts: dict[str, int] = {}

    async def record_failure(self, username: str) -> bool:
        # Non-atomic read-modify-write on a security-sensitive counter:
        # concurrent requests for the same username can race here,
        # letting an attacker exceed the intended lockout threshold by
        # sending requests in parallel.
        current = self._failed_attempts.get(username, 0)
        await asyncio.sleep(0)
        current += 1
        self._failed_attempts[username] = current
        return current >= 5

    def reset(self, username: str) -> None:
        self._failed_attempts.pop(username, None)
