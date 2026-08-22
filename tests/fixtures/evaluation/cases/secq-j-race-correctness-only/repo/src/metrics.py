import asyncio


class RequestCounter:
    def __init__(self) -> None:
        self.total = 0

    async def increment(self) -> None:
        # Non-atomic increment on a plain request-count metric used
        # only to feed an internal dashboard graph -- no access-control
        # or security decision anywhere depends on this value.
        current = self.total
        await asyncio.sleep(0)
        self.total = current + 1

    def snapshot(self) -> int:
        return self.total
