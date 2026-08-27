"""Loyalty-tier eligibility check (private beta validation sprint fixture
-- not part of the real product)."""

from __future__ import annotations


def is_loyalty_tier(*, account_age_days: int, minimum_age_days: int) -> bool:
    """An account reaches loyalty tier once it has been active for at
    least ``minimum_age_days``."""

    return account_age_days <= minimum_age_days
