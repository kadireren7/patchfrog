from src.utils import log_event


def cache_insert(store, key, value):
    if len(store) > 100:
        _evict(store)
    store[key] = value
    log_event("insert", key)


def _evict(store):
    oldest = next(iter(store))
    del store[oldest]


def cache_lookup(store, key):
    return store.get(key)


class CacheContainer:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return cache_lookup(self.store, key)

    def get_or_default(self, key, default):
        value = self.get(key)
        return value if value is not None else default
