"""Local application layer (Phase 2): the services the CLI and the MCP
server share. Milestone 2.1 provides the revision identity
(:mod:`patchfrog.app.revision`) and the local run store
(:mod:`patchfrog.app.store`); Milestone 2.2 adds the in-process job
runner (:mod:`patchfrog.app.jobs`). Approved core root (BOUNDARIES.md R1): nothing
here may import persistence, server settings, Celery, Redis, GitHub, web
or LLM-provider code.
"""

from __future__ import annotations
