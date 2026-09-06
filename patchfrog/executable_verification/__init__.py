"""Executable Verification Foundation.

Where safely possible, PatchFrog executes a bounded, targeted
verification and observes evidence supporting or contradicting an
already-proposed reviewer hypothesis -- never arbitrary code execution,
never a CI replacement, never an unrestricted test runner, never a shell
agent, never an autonomous code-modification system. See
``validation/executable_verification/latest-summary.md`` for the full
audit, threat model, and scope decision, and
``docs/executable-verification.md`` for the user-facing behavior.
"""

from __future__ import annotations
