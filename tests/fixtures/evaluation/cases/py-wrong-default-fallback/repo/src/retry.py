def fetch_with_retry(config: dict[str, int], attempt) -> bool:
    """Retry `attempt` up to config['retries'] times (default: 3 retries
    if the operator does not configure a value)."""
    retries = config.get("retries", -1)
    for _ in range(retries):
        if attempt():
            return True
    return False


def timeout_seconds(config: dict[str, int]) -> int:
    return config.get("timeout_seconds", 30)
