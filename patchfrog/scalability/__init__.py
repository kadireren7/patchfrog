"""PatchFrog scalability engine (Phase 2+). Approved core root
(BOUNDARIES.md R1): parse-only analysis of untrusted repository content.
Nothing here imports persistence, server settings, Celery, Redis, GitHub,
web frameworks or LLM-provider code, and nothing here imports or executes
the analysed code. :mod:`patchfrog.scalability.project` holds application
discovery (Milestone 2.3)."""

from __future__ import annotations
