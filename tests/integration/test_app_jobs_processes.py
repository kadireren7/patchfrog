"""JobRunner across real processes (Milestone 2.2): a SIGKILLed job is
recovered as INTERRUPTED and never reused, and identical requests from
several processes execute the job once. At most four short subprocesses
per test (bounded resource use)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from patchfrog.app.jobs import JobContext, JobRunner, JobSpec
from patchfrog.app.store import LocalRunStore, RunState

_HANG_IN_JOB = r"""
import sys, time
from pathlib import Path
from patchfrog.app.jobs import JobRunner, JobSpec
from patchfrog.app.store import LocalRunStore
def job(ctx):
    ctx.put_bytes("half.json", b"{}")
    print("in-job", flush=True)
    time.sleep(600)
JobRunner(LocalRunStore(Path(sys.argv[1]))).run(
    JobSpec(kind="analysis", root=Path(sys.argv[2]), engine_version="0.1.0-beta"), job
)
"""

_COUNTING_JOB = r"""
import json, os, sys, time
from pathlib import Path
from patchfrog.app.jobs import JobRunner, JobSpec
from patchfrog.app.store import LocalRunStore
calls = Path(sys.argv[3])
def job(ctx):
    fd = os.open(calls / f"{os.getpid()}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    time.sleep(0.3)  # widen the race window; nothing asserts on time
    ctx.put_bytes("result.json", b"{}")
start = Path(sys.argv[4])
while not start.exists():
    time.sleep(0.01)
result = JobRunner(LocalRunStore(Path(sys.argv[1]))).run(
    JobSpec(kind="analysis", root=Path(sys.argv[2]), engine_version="0.1.0-beta"), job
)
print(json.dumps({"run_id": result.record.run_id, "reused": result.reused, "state": result.state.value}))
"""


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("print('hello')\n")
    return root


def _wait_for_line(proc: subprocess.Popen[str], timeout: float = 60.0) -> str:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line:
            return line.strip()
        if proc.poll() is not None:
            raise AssertionError(f"child exited early: {proc.stderr.read() if proc.stderr else ''}")
    raise AssertionError("child did not report readiness")


def test_sigkill_during_a_job_is_interrupted_and_the_request_recomputes(tmp_path: Path) -> None:
    repo, store_root = _repo(tmp_path), tmp_path / "store"
    LocalRunStore(store_root)
    child = subprocess.Popen(
        [sys.executable, "-c", _HANG_IN_JOB, str(store_root), str(repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_for_line(child) == "in-job"
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()

    store = LocalRunStore(store_root)
    (killed,) = store.list_runs()
    assert killed.state is RunState.RUNNING  # nobody has recovered it yet

    calls: list[int] = []

    def job(ctx: JobContext) -> None:
        calls.append(1)
        ctx.put_bytes("result.json", b"{}")

    result = JobRunner(store).run(JobSpec(kind="analysis", root=repo, engine_version="0.1.0-beta"), job)

    assert store.get_run(killed.run_id).state is RunState.INTERRUPTED
    assert not result.reused and calls == [1] and result.state is RunState.SUCCEEDED
    assert result.record.idempotency_key == killed.idempotency_key


def test_identical_requests_from_several_processes_execute_once(tmp_path: Path) -> None:
    repo, store_root = _repo(tmp_path), tmp_path / "store"
    calls, start = tmp_path / "calls", tmp_path / "start"
    calls.mkdir()
    LocalRunStore(store_root)
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _COUNTING_JOB, str(store_root), str(repo), str(calls), str(start)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    try:
        start.touch()
        outputs = []
        for child in children:
            out, err = child.communicate(timeout=120)
            assert child.returncode == 0, err
            outputs.append(json.loads(out.strip().splitlines()[-1]))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()

    assert len(os.listdir(calls)) == 1  # the job function ran in exactly one process
    assert len({o["run_id"] for o in outputs}) == 1
    assert sorted(o["reused"] for o in outputs) == [False, True, True]
    assert {o["state"] for o in outputs} == {"succeeded"}
    assert len(LocalRunStore(store_root).list_runs()) == 1
