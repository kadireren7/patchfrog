import asyncio


class RequestCounter:
    def __init__(self) -> None:
        self.total = 0

    async def record(self) -> None:
        """Called concurrently by many in-flight request handlers."""
        current = self.total
        await asyncio.sleep(0)
        self.total = current + 1

    def snapshot(self) -> int:
        return self.total
