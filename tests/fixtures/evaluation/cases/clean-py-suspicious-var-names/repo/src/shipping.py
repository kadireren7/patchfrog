def compute_shipping_cost(weight_kg: float, distance_km: float) -> float:
    tmp = weight_kg * 0.5
    hack = distance_km * 0.1
    foo = tmp + hack
    return round(foo, 2)


def apply_discount(total: float, pct_off: float) -> float:
    return total * (1 - pct_off / 100)
