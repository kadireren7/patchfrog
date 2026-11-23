"""LocalRunStore across real processes (Milestone 2.1): SIGKILL recovery and
concurrent readers/writers. Each test starts at most a handful of short
subprocesses (bounded resource use)."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from patchfrog.app.store import ArtifactRef, LocalRunStore, RunState

KEY = hashlib.sha256(b"request").hexdigest()
REV = hashlib.sha256(b"revision").hexdigest()


def _spawn(code: str, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", code, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


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


_RUN_THEN_HANG = r"""
import sys, time
from pathlib import Path
from patchfrog.app.store import LocalRunStore
store = LocalRunStore(Path(sys.argv[1]))
handle = store.begin_run(idempotency_key=sys.argv[2], revision_id=sys.argv[3], kind="analysis")
print(handle.run_id, flush=True)
time.sleep(600)
"""


def test_sigkill_during_a_run_is_recovered_as_interrupted(tmp_path: Path) -> None:
    root = tmp_path / "store"
    LocalRunStore(root)
    child = _spawn(_RUN_THEN_HANG, str(root), KEY, REV)
    run_id = _wait_for_line(child)

    store = LocalRunStore(root)
    assert store.recover().interrupted_runs == ()  # owner alive: untouched
    child.send_signal(signal.SIGKILL)
    child.wait(timeout=30)

    report = store.recover()

    assert report.interrupted_runs == (run_id,)
    record = store.get_run(run_id)
    assert record.state is RunState.INTERRUPTED and record.reason
    assert store.find_succeeded(KEY) is None
    assert store.recover().interrupted_runs == ()  # idempotent


_WRITE_FOREVER = r"""
import sys
from pathlib import Path
from patchfrog.app.store import LocalRunStore
store = LocalRunStore(Path(sys.argv[1]), max_artifact_bytes=1 << 40)
chunk = b"x" * (1 << 20)
def chunks():
    print("writing", flush=True)
    while True:
        yield chunk
store.put_stream(chunks())
"""


def test_sigkill_during_a_write_never_leaves_a_partial_artifact(tmp_path: Path) -> None:
    root = tmp_path / "store"
    LocalRunStore(root)
    child = _spawn(_WRITE_FOREVER, str(root))
    assert _wait_for_line(child) == "writing"
    deadline = time.monotonic() + 30
    tmp_dir = root / "tmp"
    while time.monotonic() < deadline and not any(p.stat().st_size > (8 << 20) for p in tmp_dir.iterdir()):
        time.sleep(0.05)
    child.send_signal(signal.SIGKILL)
    child.wait(timeout=30)

    leftovers = list(tmp_dir.iterdir())
    assert leftovers, "the interrupted write should have left its temp file"
    assert not any(p.is_file() for p in (root / "objects").rglob("*")), "a partial artifact was published"

    store = LocalRunStore(root)
    assert store.recover().removed_tmp_files == 0  # younger than the temp grace period: kept
    for path in leftovers:
        os.utime(path, (time.time() - 3600, time.time() - 3600))
    assert store.recover().removed_tmp_files == len(leftovers)
    assert list(tmp_dir.iterdir()) == []
    assert store.verify().corrupt_objects == ()


_WRITER = r"""
import json, sys
from pathlib import Path
from patchfrog.app.store import LocalRunStore
store = LocalRunStore(Path(sys.argv[1]))
shared = bytes(range(256)) * 8192  # 2 MiB, identical in every writer
refs = []
for i in range(20):
    refs.append(store.put_bytes(shared).sha256)
    refs.append(store.put_stream(iter([f"{sys.argv[2]}-{i}".encode()] * 1000)).sha256)
handle = store.begin_run(idempotency_key=sys.argv[3], revision_id=sys.argv[4], kind="analysis")
handle.finish("succeeded", artifacts={})
print(json.dumps(sorted(set(refs))), flush=True)
"""

_READER = r"""
import sys, time
from pathlib import Path
from patchfrog.app.store import ArtifactRef, LocalRunStore
store = LocalRunStore(Path(sys.argv[1]))
ref = ArtifactRef(sys.argv[2], int(sys.argv[3]))
reads = 0
deadline = time.monotonic() + float(sys.argv[4])
while time.monotonic() < deadline:
    if store.has_artifact(ref):
        store.read_bytes(ref)  # raises on any torn or corrupt read
        reads += 1
    for record in store.list_runs():
        assert record.state.value in ("running", "succeeded")
print(reads, flush=True)
"""


def test_concurrent_writers_and_readers_never_observe_partial_data(tmp_path: Path) -> None:
    root = tmp_path / "store"
    LocalRunStore(root)
    shared = bytes(range(256)) * 8192
    shared_ref = ArtifactRef(hashlib.sha256(shared).hexdigest(), len(shared))

    readers = [_spawn(_READER, str(root), shared_ref.sha256, str(shared_ref.size), "6") for _ in range(2)]
    writers = [
        _spawn(_WRITER, str(root), f"w{n}", hashlib.sha256(f"key{n}".encode()).hexdigest(), REV) for n in range(4)
    ]

    outputs = []
    for proc in writers + readers:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        outputs.append(out.strip())

    written = {sha for line in outputs[:4] for sha in json.loads(line)}
    assert all(int(line) >= 0 for line in outputs[4:])
    store = LocalRunStore(root)
    stored = {p.name for p in (root / "objects").rglob("*") if p.is_file()}
    assert stored == written and len(stored) == 1 + 4 * 20  # the shared artifact deduplicated to one object
    report = store.verify()
    assert report.corrupt_objects == () and report.corrupt_records == ()
    assert len([r for r in store.list_runs() if r.state is RunState.SUCCEEDED]) == 4
    assert list((root / "tmp").iterdir()) == []
