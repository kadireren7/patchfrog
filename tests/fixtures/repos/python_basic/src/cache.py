from src.utils import normalize_key


class Cache:
    def __init__(self) -> None:
        self._store: dict[str, object] = {}

    def get(self, key: str) -> object:
        return self._store.get(normalize_key(key))

    def set(self, key: str, value: object) -> None:
        self._store[normalize_key(key)] = value
