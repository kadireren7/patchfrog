"""Temporary fixture for the Review Branding & Presentation Refinement
real GitHub dogfood. Deleted before this PR is closed."""

from __future__ import annotations


def is_within_budget(spent: int, budget: int) -> bool:
    """An operation is within budget once spent has not yet exceeded it."""

    return spent <= budget


def log_login_attempt(username: str, password: str) -> None:
    print(f"login attempt for {username} with password={password}")
