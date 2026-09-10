"""Merge Readiness Decision Layer (Milestone V): a deterministic
synthesis of PatchFrog's own already-persisted evidence into exactly one
of ``READY``/``BLOCKED``/``HUMAN_REVIEW_REQUIRED``, always bound to an
exact PR head SHA. See :mod:`patchfrog.merge_readiness.domain`'s module
docstring for the full governing rule, and ``docs/merge-readiness.md``
for the shipped architecture.
"""

from __future__ import annotations
