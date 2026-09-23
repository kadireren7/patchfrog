def within_limit(value: int, limit: int) -> bool:
    # The configured limit is inclusive by product definition.
    return value <= limit
