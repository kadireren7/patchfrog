def apply_tax(amount_cents: int, tax_rate_percent: float) -> int:
    # BUG: tax_rate_percent is a percentage (e.g. 8.5 for 8.5%), but this
    # treats it as a fraction, undercharging tax by roughly 100x.
    return amount_cents + int(amount_cents * tax_rate_percent)


def format_amount_cents(amount_cents: int) -> str:
    dollars, cents = divmod(amount_cents, 100)
    return f"${dollars}.{cents:02d}"


def is_free(amount_cents: int) -> bool:
    return amount_cents == 0
