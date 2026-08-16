from src.cache import cache_insert, cache_lookup


def test_cache_insert():
    store = {}
    cache_insert(store, "a", 1)
    assert cache_lookup(store, "a") == 1
