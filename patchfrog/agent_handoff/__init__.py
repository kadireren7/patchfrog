"""Agent Handoff -- Milestone T.

Exposes PatchFrog's already-produced, already-persisted finding evidence
as a bounded, structured, exact-head-bound :class:`FindingHandoff` a coding
agent can consume through MCP (:mod:`patchfrog.mcp`). Deliberately never a
second review engine: nothing here re-derives, re-scores, or re-judges a
finding -- it only projects and redacts what already exists in
:mod:`patchfrog.persistence.models.review`.

See ``validation/agent_handoff/latest-summary.md`` for the full audit and
the reasoning behind exactly what evidence is (and, just as deliberately,
is not) included.
"""

from __future__ import annotations
