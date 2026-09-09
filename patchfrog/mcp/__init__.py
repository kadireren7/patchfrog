"""PatchFrog MCP server -- Milestone T (T2).

Exposes :mod:`patchfrog.agent_handoff` and :mod:`patchfrog.fix_verification`
to an MCP-capable coding agent over stdio only (see
``validation/agent_handoff/latest-summary.md`` section 2.2 for why stdio,
not HTTP/SSE, in v1). **Read-mostly**: every tool either reads already-
persisted state or writes exactly one narrow, new ``fix_attempts`` row --
never a file write, ``git commit``/``push``, GitHub write, or arbitrary
shell (Part L/AL/AM of the milestone spec; see
``tests/unit/test_mcp_module_boundaries.py``).

Launch via ``python -m patchfrog.cli mcp serve`` -- see
:mod:`patchfrog.mcp.server`.
"""

from __future__ import annotations
