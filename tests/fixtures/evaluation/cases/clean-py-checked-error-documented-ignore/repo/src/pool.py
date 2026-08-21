class ConnectionPool:
    def __init__(self) -> None:
        self._connections: list[object] = []

    def release_all(self) -> None:
        for conn in self._connections:
            closer = getattr(conn, "close", None)
            if closer is not None:
                # Return value intentionally ignored: close() returns a
                # boolean "was already closed" flag that release_all()'s
                # callers have no use for -- only that shutdown
                # completes without raising.
                closer()
        self._connections.clear()
