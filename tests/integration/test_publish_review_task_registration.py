"""Regression test for Celery autodiscovery of the publish-review task.

Mirrors ``test_review_pull_request_task_registration.py`` exactly -- run
in a fresh subprocess so importing this module elsewhere in the test
session can't mask a registration bug by registering the task as an
import side effect first.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PROBE = """
import celery.signals as signals
from apps.worker.celery_app import celery_app

celery_app.finalize()
signals.import_modules.send(sender=celery_app)
print("patchfrog.publish_review" in celery_app.tasks)
"""


def test_worker_autodiscovery_registers_the_publish_review_task(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True", result.stderr


_ALL_SIX_PROBE = """
import celery.signals as signals
from apps.worker.celery_app import celery_app

celery_app.finalize()
signals.import_modules.send(sender=celery_app)
expected = {
    "patchfrog.process_pull_request_event",
    "patchfrog.index_repository",
    "patchfrog.analyze_repository",
    "patchfrog.build_context",
    "patchfrog.review_pull_request",
    "patchfrog.publish_review",
}
registered = set(celery_app.tasks)
print(expected.issubset(registered))
"""


def test_all_six_patchfrog_tasks_are_registered(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _ALL_SIX_PROBE],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True", result.stderr
