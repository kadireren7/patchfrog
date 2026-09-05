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

**No separate ``### Repository learning`` publication block in v1**
(no ``summary.py`` module) -- an external-review correction round
found that this package's only implemented pattern kind,
``REPEATED_SAME_SURFACE_REGRESSION``, only ever enriches an existing
Milestone N candidate on the exact same surface, so a second top-level
section would render immediately next to N's own ``### Historical
context`` block about the very same surface -- saying, in effect, the
same thing twice. This package's user-facing footprint in v1 is
therefore limited to: a bounded addendum folded into the shared Change
Story text (:mod:`patchfrog.repository_learnings.story`), bounded
per-candidate ``<repository_learning>`` prompt evidence
(:mod:`patchfrog.repository_learnings.evidence`), and count-only
telemetry (:mod:`patchfrog.repository_learnings.telemetry`).
"""

from __future__ import annotations
