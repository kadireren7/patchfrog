"""Beta-tier discount eligibility check for the usage dashboard (private
beta validation sprint fixture -- not part of the real product)."""

from __future__ import annotations


def is_eligible_for_discount(*, account_age_days: int, minimum_age_days: int) -> bool:
    """An account qualifies for the loyalty discount once it has been
    active for at least ``minimum_age_days``."""

    return account_age_days <= minimum_age_days
