"""Explicit policy for typed critic failures."""

from enum import StrEnum


class CriticFailurePolicy(StrEnum):
    """What to do when a selected critic cannot return a valid verdict.

    ``FAIL_OPEN`` preserves PatchFrog's historical behavior: deterministic
    validation and confidence aggregation may still accept the reviewer
    proposal. ``HOLD_FOR_REVIEW`` suppresses the proposal until a critic can
    verify it; this is safer for mandatory/high-risk review environments.
    Unexpected programming errors always propagate under either policy.
    """

    FAIL_OPEN = "fail_open"
    HOLD_FOR_REVIEW = "hold_for_review"


__all__ = ["CriticFailurePolicy"]
