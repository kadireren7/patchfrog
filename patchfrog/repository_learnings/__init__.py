"""Repository Learnings Foundation -- deterministic detection of a
repeated, independently-trusted technical pattern this repository has
demonstrated, distinct from Milestone N (Historical Regression
Memory), which can act on a single trusted event.

See ``docs/repository-learnings.md`` and
``validation/repository_learnings/latest-summary.md`` for the full
design narrative. Extends :mod:`patchfrog.historical_regression_memory`
directly -- reusing its exact trust model and point-in-time semantics
-- rather than building a second trust model or a parallel history
database.
"""

from __future__ import annotations
