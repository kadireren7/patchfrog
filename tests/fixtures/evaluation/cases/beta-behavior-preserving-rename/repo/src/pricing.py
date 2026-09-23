def discounted(price: int, discount: int) -> int:
    discounted_price = price - discount
    return max(0, discounted_price)
