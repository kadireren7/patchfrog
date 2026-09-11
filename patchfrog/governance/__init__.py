"""Milestone Z: deterministic, generic policy evaluation primitives.
See ``validation/org_learning_governance/latest-summary.md`` section 5.
Cloud owns policy *definition, assignment, UI, and audit* -- this
package owns *evaluation* only, and is never given repository-controlled
input to decide anything (see ``domain.py``'s own module docstring).
"""
