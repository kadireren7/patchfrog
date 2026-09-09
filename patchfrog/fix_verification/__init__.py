"""Fix Verification -- Milestone T (T3).

PatchFrog independently checks whether an attempted fix, identified by an
exact new commit SHA, actually resolves a specific, already-handed-off
finding. Deterministic-first (Part X of the milestone spec): file-level
change detection, static-analyzer re-check, and Executable Verification
re-run are all tried before ever falling back to one bounded LLM call.

Never a "the agent says it fixed it" self-declaration, never an automatic
full PR re-review, never a new specialist agent -- see
``validation/agent_handoff/latest-summary.md`` section 2.3 for the full
design rationale, and the module docstring of
:mod:`patchfrog.fix_verification.service` for the algorithm itself.
"""

from __future__ import annotations
