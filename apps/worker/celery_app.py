"""Celery application instance.

Imported both by the worker process (``celery -A apps.worker.celery_app worker``)
and by the API process, which uses it purely as a task producer
(``task.delay(...)``) without ever running a worker loop itself.
"""

from __future__ import annotations

from celery import Celery

from patchfrog.config.settings import get_settings

settings = get_settings()

celery_app = Celery("patchfrog", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
)

celery_app.autodiscover_tasks(["apps.worker"])
