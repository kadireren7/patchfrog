_ALLOWED_EXPRESSIONS = {
    "double": "x * 2",
    "square": "x ** 2",
}


def compute(op_name: str, x: int) -> int:
    # op_name is validated against a fixed, hardcoded allow-list of
    # expressions defined above in this module -- never derived from
    # request input. The eval'd string is always one of the two
    # constant expressions in _ALLOWED_EXPRESSIONS.
    expr = _ALLOWED_EXPRESSIONS[op_name]
    return eval(expr)


def list_operations() -> list[str]:
    return list(_ALLOWED_EXPRESSIONS)
