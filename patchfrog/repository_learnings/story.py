"""Deterministic Repository Learning Story prefix -- at most one
bounded sentence, prepended to the existing Change/Contract/Intent/
Test/Historical Story text, never a separate publication block
(mirrors every other Intelligence package's own "prefix, not a block"
discipline -- and, unlike every prior Intelligence package, this one
has no separate conditional summary block at all in v1: see this
package's own ``__init__.py`` docstring for why a second, standalone
``### Repository learning`` section would needlessly duplicate N's own
``### Historical context`` block for the exact same surface). Only
rendered when a real, current-PR application exists -- never for
every PR with any active learning anywhere in the repository.

**Never phrased as an invariant violation** -- an external-review
correction round flagged that "this surface recurred" is historical
-pattern evidence, not a requirement the current PR failed to meet.
The wording here says only that repeated, independent historical
evidence exists -- never that anything is "unsatisfied" or "missing.\""""

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
        f"Repository history: {label!r} has produced trusted findings across "
        f"{count} independent reviews."
    )
