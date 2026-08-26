"""Celery application instance.

Imported both by the worker process (``celery -A apps.worker.celery_app worker``)
and by the API process, which uses it purely as a task producer
(``task.delay(...)``) without ever running a worker loop itself.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_init, worker_process_shutdown

from patchfrog.config.settings import get_settings
from patchfrog.ops.metrics import mark_worker_process_dead, start_worker_metrics_server

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


# Aggregating /metrics endpoint for this worker container -- see
# patchfrog.ops.metrics's module docstring for why the API process's own
# /metrics can never see these. Both are no-ops (only the API process
# imports this module too, as a task producer -- it must never start a
# second HTTP server on the same port) unless PROMETHEUS_MULTIPROC_DIR is
# set, which only the worker service does (see docker-compose.yml).
@worker_init.connect  # type: ignore[untyped-decorator]
def _start_metrics_server(**_kwargs: object) -> None:
    start_worker_metrics_server(settings.worker_metrics_port)


@worker_process_shutdown.connect  # type: ignore[untyped-decorator]
def _cleanup_metrics_on_child_exit(pid: int | None = None, **_kwargs: object) -> None:
    if pid is not None:
        mark_worker_process_dead(pid)
