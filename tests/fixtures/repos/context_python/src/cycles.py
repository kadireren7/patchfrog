def self_recursive(n):
    if n <= 0:
        return 0
    return self_recursive(n - 1)


def two_cycle_a(x):
    return two_cycle_b(x)


def two_cycle_b(x):
    return two_cycle_a(x)


def three_cycle_a(x):
    return three_cycle_b(x)


def three_cycle_b(x):
    return three_cycle_c(x)


def three_cycle_c(x):
    return three_cycle_a(x)


def diamond_top(x):
    diamond_left(x)
    diamond_right(x)


def diamond_left(x):
    diamond_bottom(x)


def diamond_right(x):
    diamond_bottom(x)


def diamond_bottom(x):
    return x
