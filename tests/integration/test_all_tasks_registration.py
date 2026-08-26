"""Regression test for full Celery task autodiscovery (public beta
readiness). ``apps/worker/tasks/__init__.py`` forgot to import
``run_review_pipeline_task`` when it was first added -- caught by
counting registered tasks (8 instead of the expected 9), not by any
single task's own registration test. This test asserts the exact set so
a future missing import fails loudly instead of silently.

Run in a fresh subprocess for the same reason as
``test_celery_task_registration.py``: importing a task module anywhere
earlier in the test session would register it as a side effect and mask
a missing import in ``apps/worker/tasks/__init__.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_EXPECTED_TASKS = {
    "patchfrog.process_pull_request_event",
    "patchfrog.sync_installation_event",
    "patchfrog.sync_installation_repositories_event",
    "patchfrog.run_review_pipeline",
    "patchfrog.index_repository",
    "patchfrog.build_context",
    "patchfrog.analyze_repository",
    "patchfrog.review_pull_request",
    "patchfrog.publish_review",
}

_PROBE = """
import celery.signals as signals
from apps.worker.celery_app import celery_app

celery_app.finalize()
signals.import_modules.send(sender=celery_app)
registered = {name for name in celery_app.tasks if name.startswith("patchfrog.")}
for name in sorted(registered):
    print(name)
"""


def test_all_nine_patchfrog_tasks_are_registered(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    registered = set(result.stdout.strip().splitlines())
    assert registered == _EXPECTED_TASKS, (
        f"missing={_EXPECTED_TASKS - registered} unexpected={registered - _EXPECTED_TASKS}"
    )
