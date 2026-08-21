def sum_of_squares(n: int) -> int:
    squares = (i * i for i in range(n))
    # squares is consumed exactly once here by design -- this helper
    # exists solely to feed sum(), so there is no bug in "reusing" a
    # generator, because it is never reused.
    return sum(squares)
