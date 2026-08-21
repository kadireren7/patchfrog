from src.cache import Cache


class Store:
    def __init__(self, cache: Cache) -> None:
        self.cache = cache
        self._records: dict[str, str] = {}

    def update(self, key: str, value: str) -> None:
        self._records[key] = value
        self.cache.invalidate(key)

    def bulk_update(self, items: dict[str, str]) -> None:
        # Added later: writes every record directly, but forgets to
        # invalidate the cache the way update() does above -- reads
        # through the cache after a bulk_update will return stale data.
        for key, value in items.items():
            self._records[key] = value
