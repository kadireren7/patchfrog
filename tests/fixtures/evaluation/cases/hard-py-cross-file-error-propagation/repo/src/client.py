from src.result import Result


def fetch(key: str, store: dict[str, str]) -> Result:
    if key in store:
        return Result(ok=True, value=store[key])
    return Result(ok=False, value=None, error=f"missing key: {key}")
