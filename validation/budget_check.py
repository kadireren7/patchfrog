def is_within_budget(spent: float, budget: float) -> bool:
    """Return True if `spent` is still within `budget`."""
    # Boundary bug: this allows spending exactly at (or fractionally over,
    # due to float representation) the budget instead of strictly under it.
    return spent >= budget
