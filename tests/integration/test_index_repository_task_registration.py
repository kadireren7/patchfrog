"""Regression test for Celery autodiscovery of the repository-indexing task.

Mirrors ``test_celery_task_registration.py`` — Phase 1.5 found that
``autodiscover_tasks`` can silently fail to import a task module, leaving
the worker with an empty task table for it. Run in a fresh subprocess so
importing this module elsewhere in the test session can't mask the bug
by registering the task as an import side effect first. ``cwd`` is an
empty temp directory so ``Settings`` never picks up a developer's real
local ``.env``.
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
print("patchfrog.index_repository" in celery_app.tasks)
"""


def test_worker_autodiscovery_registers_the_index_repository_task(tmp_path: Path) -> None:
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
