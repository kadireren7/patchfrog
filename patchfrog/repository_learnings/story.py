"""Deterministic Repository Learning Story prefix -- at most one
bounded sentence, prepended to the existing Change/Contract/Intent/
Test/Historical Story text, never a separate publication block
(mirrors every other Intelligence package's own "prefix, not a block"
discipline). Only rendered when a real, current-PR application exists
-- never for every PR with any active learning anywhere in the
repository."""

from __future__ import annotations

from patchfrog.repository_learnings.domain import PotentialRepositoryLearningApplication


def build_repository_learning_story_prefix(
    applications: tuple[PotentialRepositoryLearningApplication, ...],
) -> str:
    if not applications:
        return ""

    primary = applications[0]
    label = primary.current_qualified_name or primary.current_file_path
    count = primary.learning.support_count
    return (
        f"Repository learning: {label!r} has repeatedly produced trusted regressions "
        f"across {count} independent reviews."
    )
