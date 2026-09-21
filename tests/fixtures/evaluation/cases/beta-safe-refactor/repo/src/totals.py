def subtotal(price: int, quantity: int) -> int:
    return price * quantity


def invoice_total(price: int, quantity: int, shipping: int) -> int:
    return subtotal(price, quantity) + shipping
