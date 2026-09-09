"""Verifier process -- Milestone S6, Production Execution Enablement.

A deliberately credential-minimal Celery app/worker, entirely separate
from :mod:`apps.worker`. Holds only a Redis credential (to consume its
own dedicated queue and share the Celery result backend) -- never the
GitHub App private key, never an LLM provider key, never
``DATABASE_URL``. See
``validation/production_execution/latest-summary.md`` for the full
trust-boundary audit and architecture decision, and
:mod:`patchfrog.executable_verification.protocol` for the request/result
wire contract this process implements.
"""

from __future__ import annotations
