class TimeWindow:
    """[start, end] is inclusive on both ends, per this service's
    documented contract."""

    def __init__(self, start: int, end: int) -> None:
        self.start = start
        self.end = end

    def contains(self, timestamp: int) -> bool:
        return self.start <= timestamp <= self.end

    def duration(self) -> int:
        return self.end - self.start + 1
