"""Regression test for Celery task autodiscovery.

Phase 1.5 found that ``celery_app.autodiscover_tasks(["apps.worker.tasks"])``
made Celery search for a nonexistent ``apps.worker.tasks.tasks`` module
(Celery's default ``related_name="tasks"`` is appended to each package in
the list). The real running worker booted with an empty ``[tasks]``
section — it never imported ``process_pull_request.py``, so the
``@celery_app.task`` decorator never ran in that process.

This must be tested in a **fresh subprocess**: importing the task module
anywhere earlier in the test session (e.g. to test its pure helpers) would
register the task as a side effect and mask the bug, since the decorator
runs on first import regardless of how that import happened.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import celery.signals as signals
from apps.worker.celery_app import celery_app

celery_app.finalize()
signals.import_modules.send(sender=celery_app)
print("patchfrog.process_pull_request_event" in celery_app.tasks)
"""


def test_worker_autodiscovery_registers_the_pull_request_task() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True", result.stderr
