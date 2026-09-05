"""Trajectory Intelligence Foundation -- deterministic detection of
current-PR-lineage evolution signals (repeated same-surface churn) used
only to deepen existing review mechanisms, never published as a
standalone finding.

See ``docs/trajectory-intelligence.md`` and
``validation/trajectory_intelligence/latest-summary.md`` for the full
design narrative. Reuses Phase 7's own already-persisted, already
-ancestry-verified ``review_generations`` lineage directly -- zero new
git operations, zero new GitHub API calls, zero new history crawler.
"""

from __future__ import annotations
