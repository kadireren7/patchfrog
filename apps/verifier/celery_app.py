"""Celery application instance for the verifier process.

A **separate** Celery app instance from :mod:`apps.worker.celery_app` --
not merely a new task registered on the review worker's own app. Built
from :class:`patchfrog.config.verifier_settings.VerifierSettings`
(Redis URL only), never
:class:`patchfrog.config.settings.Settings` (which requires the GitHub
App private key, database URL, and webhook secret at construction time
-- fields this process must never hold). Shares the same Redis
broker/result backend as the review worker's own app (so
``AsyncResult`` lookups from either side work), but consumes only its
own dedicated queue (see
:data:`patchfrog.executable_verification.protocol.VERIFICATION_QUEUE_NAME`)
-- run as ``celery -A apps.verifier.celery_app worker -Q
patchfrog-verification``, never the review worker's own default queue.
"""

from __future__ import annotations

from celery import Celery

from patchfrog.config.verifier_settings import get_verifier_settings
from patchfrog.executable_verification.protocol import VERIFICATION_QUEUE_NAME

settings = get_verifier_settings()

verifier_celery_app = Celery("patchfrog-verifier", broker=settings.redis_url, backend=settings.redis_url)

verifier_celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Fixed, never repository- or environment-of-the-reviewed-code
    # -controlled -- the verifier only ever consumes this one queue.
    task_default_queue=VERIFICATION_QUEUE_NAME,
)

verifier_celery_app.autodiscover_tasks(["apps.verifier"])
