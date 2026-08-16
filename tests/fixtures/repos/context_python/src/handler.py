from src.cache import cache_insert


def process_request(store, key, value):
    cache_insert(store, key, value)
