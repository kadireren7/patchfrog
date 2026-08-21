import threading


class SharedCache:
    """Read/written from multiple worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, int] = {}

    def increment(self, key: str) -> int:
        with self._lock:
            self._store[key] = self._store.get(key, 0) + 1
            return self._store[key]

    def bulk_reset(self, keys: list[str]) -> None:
        for key in keys:
            self._store[key] = 0
