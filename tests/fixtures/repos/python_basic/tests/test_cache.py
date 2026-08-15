from src.cache import Cache


def test_get_set() -> None:
    cache = Cache()
    cache.set("A", 1)
    assert cache.get("a") == 1
