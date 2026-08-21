class Cart:
    def __init__(self) -> None:
        self._items: dict[str, float] = {}
        self._cached_total: float | None = None

    def add_item(self, name: str, price: float) -> None:
        self._items[name] = price
        self._cached_total = None

    def remove_item(self, name: str) -> None:
        self._items.pop(name, None)

    def total(self) -> float:
        if self._cached_total is None:
            self._cached_total = sum(self._items.values())
        return self._cached_total
