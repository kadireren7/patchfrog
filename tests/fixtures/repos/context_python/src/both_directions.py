def both_caller_root(x):
    return both_caller_mid(x)


def both_caller_mid(x):
    return both_target(x)


def both_target(x):
    return both_callee_mid(x)


def both_callee_mid(x):
    return both_callee_leaf(x)


def both_callee_leaf(x):
    return x
