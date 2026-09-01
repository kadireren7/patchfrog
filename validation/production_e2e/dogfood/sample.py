"""Throwaway fixture for PatchFrog Milestone H's production webhook E2E
dogfood PR.

Never imported by any real module and not part of any production code
path -- exists only to give PatchFrog's own AI reviewer one intentionally
reviewable correctness bug (and one adjacent clean function) to catch
during a real, live-webhook-driven review. Safe to delete once the
dogfood PR is closed.
"""


def apply_discount(price_cents: int, discount_percent: int) -> int:
    """Return the discounted price in cents, or the original price if the
    discount percentage is out of range."""
    if discount_percent < 0 or discount_percent > 100:
        return price_cents
    # Bug: subtracts the raw percent value instead of percent-of-price,
    # so a 10% discount on 20000 cents returns 19990 instead of 18000.
    return price_cents - discount_percent


def format_price_cents(cents: int) -> str:
    """Format an integer number of cents as a "$X.YY" string. No bug."""
    dollars, remainder = divmod(cents, 100)
    return f"${dollars}.{remainder:02d}"


def is_free(price_cents: int) -> bool:
    """Return True if the price is exactly zero. No bug -- a second,
    clean synchronize-event commit for Milestone H's E2E validation."""
    return price_cents == 0
