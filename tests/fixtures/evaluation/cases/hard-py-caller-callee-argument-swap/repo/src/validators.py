def within_bounds(minimum: float, maximum: float, value: float) -> bool:
    """Returns whether value falls within [minimum, maximum].

    Argument order is (minimum, maximum, value) -- callers must not
    transpose the first two arguments with the third.
    """
    return minimum <= value <= maximum
