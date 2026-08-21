from src.validators import within_bounds


def apply_price(value: float, minimum: float, maximum: float) -> float:
    if not within_bounds(value, minimum, maximum):
        raise ValueError(f"price {value} out of bounds")
    return value


def clamp_quantity(quantity: int) -> int:
    return max(0, quantity)
