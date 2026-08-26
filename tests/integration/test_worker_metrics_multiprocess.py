"""Regression test for the worker's multiprocess Prometheus metrics
setup (`patchfrog.ops.metrics.start_worker_metrics_server`).

Found via dogfooding the local Docker stack: the API's own `/metrics`
stayed at `0` for every review counter even moments after a real review
had started in the worker container, because every counter is
incremented from worker-side task code and `prometheus_client`'s default
registry is per-process memory, never shared across processes (let
alone containers). The fix is `prometheus_client`'s documented
multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`) plus an aggregating HTTP
server bound in the worker's own container.

A second, narrower bug surfaced while fixing the first: every
`Counter`/`Histogram` in `patchfrog.ops.metrics` opens a file inside
`PROMETHEUS_MULTIPROC_DIR` the moment it's *constructed* -- at import
time -- which happens before `start_worker_metrics_server` (wired to
Celery's `worker_init` signal, which only fires after imports are
already done) ever runs. The directory must exist, already clean,
before the process starts at all -- this test sets it up the same way
`docker/Dockerfile`'s worker `CMD` does, as a prerequisite to importing
the module, not from within it.

Must run in a fresh subprocess: `patchfrog.ops.metrics` is already
imported (without multiprocess mode) by the time any test in this suite
runs, and `Counter`/`Histogram` objects bind their storage backend once,
at construction -- re-importing the already-cached module in this
process would not retroactively switch it into multiprocess mode.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
from pathlib import Path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def test_multiprocess_metrics_server_aggregates_a_real_increment(tmp_path: Path) -> None:
    multiproc_dir = tmp_path / "prometheus_multiproc"
    multiproc_dir.mkdir()
    port = _free_port()

    script = textwrap.dedent(f"""
        import time
        import urllib.request

        from patchfrog.ops import metrics

        metrics.reviews_started_total.inc()
        started = metrics.start_worker_metrics_server({port})
        assert started is True

        deadline = time.monotonic() + 5
        body = b""
        while time.monotonic() < deadline:
            try:
                body = urllib.request.urlopen("http://127.0.0.1:{port}/metrics", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)

        assert b"patchfrog_reviews_started_total 1.0" in body, body
        print("OK")
    """)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={"PROMETHEUS_MULTIPROC_DIR": str(multiproc_dir), "PATH": "/usr/bin:/bin"},
        cwd=Path(__file__).resolve().parents[2],
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout


def test_importing_metrics_before_the_multiproc_dir_exists_fails_loudly(tmp_path: Path) -> None:
    """Pins the exact ordering bug found while building the fix above:
    if the directory doesn't exist yet when the module's Counter/Histogram
    objects are constructed, import itself must fail (not silently
    degrade) -- this is why the directory creation lives in the
    Dockerfile's shell CMD, ahead of the python process, not in any
    Python code that could run too late."""

    missing_dir = tmp_path / "does-not-exist"

    result = subprocess.run(
        [sys.executable, "-c", "from patchfrog.ops import metrics"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={"PROMETHEUS_MULTIPROC_DIR": str(missing_dir), "PATH": "/usr/bin:/bin"},
        cwd=Path(__file__).resolve().parents[2],
    )

    assert result.returncode != 0
    assert "FileNotFoundError" in result.stderr


def test_metrics_server_is_a_noop_without_the_env_var() -> None:
    from patchfrog.ops.metrics import start_worker_metrics_server

    assert start_worker_metrics_server(_free_port()) is False
