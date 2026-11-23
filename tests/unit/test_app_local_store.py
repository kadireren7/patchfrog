"""LocalRunStore (Milestone 2.1), in-process behaviour. Crash and
multi-process behaviour: tests/integration/test_app_store_processes.py."""

from __future__ import annotations

import hashlib
import json
import os
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchfrog.app.store import (
    ArtifactRef,
    ArtifactTooLargeError,
    CorruptArtifactError,
    CorruptRecordError,
    IncompatibleSchemaError,
    LocalRunStore,
    RunState,
    RunStateError,
    StoreError,
    UnsafeStorePathError,
)

KEY = hashlib.sha256(b"request").hexdigest()
REV = hashlib.sha256(b"revision").hexdigest()


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def store(tmp_path: Path) -> LocalRunStore:
    return LocalRunStore(tmp_path / "store", gc_grace_seconds=0)


def _object_files(store: LocalRunStore) -> list[Path]:
    return sorted(p for p in (store.root / "objects").rglob("*") if p.is_file())


# --- artifacts -----------------------------------------------------------------


def test_round_trip_and_content_addressed_dedup(store: LocalRunStore) -> None:
    ref = store.put_bytes(b"findings")
    inode = os.stat(store.root / "objects" / ref.sha256[:2] / ref.sha256).st_ino

    again = store.put_bytes(b"findings")
    streamed = store.put_stream(iter([b"find", b"ings"]))

    assert ref == again == streamed == ArtifactRef(hashlib.sha256(b"findings").hexdigest(), 8)
    assert store.read_bytes(ref) == b"findings"
    assert len(_object_files(store)) == 1
    assert os.stat(store.root / "objects" / ref.sha256[:2] / ref.sha256).st_ino == inode  # never rewritten
    assert list((store.root / "tmp").iterdir()) == []


def test_large_streamed_artifact_uses_bounded_memory(store: LocalRunStore) -> None:
    chunk = os.urandom(1 << 20)

    def chunks() -> Iterator[bytes]:
        for _ in range(64):  # 64 MiB
            yield chunk

    tracemalloc.start()
    ref = store.put_stream(chunks())
    ok = store.verify_artifact(ref)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert ref.size == 64 << 20 and ok
    assert peak < 8 << 20, f"peak traced allocation {peak} bytes for a 64 MiB artifact"


def test_artifact_size_limits_and_no_leftover_temp_files(tmp_path: Path) -> None:
    small = LocalRunStore(tmp_path / "s", max_artifact_bytes=10)
    with pytest.raises(ArtifactTooLargeError):
        small.put_bytes(b"x" * 11)
    with pytest.raises(ArtifactTooLargeError):
        small.put_stream(iter([b"x" * 6, b"x" * 6]))
    assert list((small.root / "tmp").iterdir()) == []
    assert _object_files(small) == []


def test_corrupted_artifact_is_detected_on_read_and_by_verify(store: LocalRunStore) -> None:
    ref = store.put_bytes(b"precious evidence")
    path = store.root / "objects" / ref.sha256[:2] / ref.sha256
    os.chmod(path, 0o600)
    path.write_bytes(b"precious evidencX")

    with pytest.raises(CorruptArtifactError):
        store.read_bytes(ref)
    assert store.verify_artifact(ref) is False
    assert store.verify().corrupt_objects == (ref.sha256,)


# --- run lifecycle ----------------------------------------------------------------


def test_run_lifecycle_and_idempotent_lookup(store: LocalRunStore) -> None:
    artifact = store.put_bytes(b"{}")
    handle = store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis", metadata={"engine": "0.1"})
    assert store.get_run(handle.run_id).state is RunState.RUNNING
    assert store.find_succeeded(KEY) is None

    final = handle.finish(RunState.SUCCEEDED, artifacts={"findings.json": artifact})

    assert final.state is RunState.SUCCEEDED and final.finished_at is not None
    assert store.get_run(handle.run_id) == final
    assert store.find_succeeded(KEY) == final
    with pytest.raises(RunStateError):
        handle.finish(RunState.FAILED)


def test_first_succeeded_run_wins_the_idempotency_key(store: LocalRunStore) -> None:
    first = store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis")
    second = store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis")
    first.finish(RunState.SUCCEEDED)
    second.finish(RunState.SUCCEEDED)

    found = store.find_succeeded(KEY)
    assert found is not None and found.run_id == first.run_id


@pytest.mark.parametrize("state", [RunState.PARTIAL, RunState.FAILED, RunState.CANCELLED])
def test_non_succeeded_runs_are_never_reused(store: LocalRunStore, state: RunState) -> None:
    store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis").finish(state, reason="x")
    assert store.find_succeeded(KEY) is None


def test_invalid_transitions_and_missing_artifacts_are_rejected(store: LocalRunStore) -> None:
    handle = store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis")
    for state in (RunState.RUNNING, RunState.INTERRUPTED):
        with pytest.raises(RunStateError):
            handle.finish(state)
    ghost = ArtifactRef("0" * 64, 3)
    with pytest.raises(StoreError, match="not in the store"):
        handle.finish(RunState.SUCCEEDED, artifacts={"findings.json": ghost})
    with pytest.raises(ValueError):
        handle.finish(RunState.SUCCEEDED, artifacts={"../escape": store.put_bytes(b"x")})
    assert store.get_run(handle.run_id).state is RunState.RUNNING  # nothing half-applied


def test_plain_string_states_are_normalized_and_unknown_ones_rejected(store: LocalRunStore) -> None:
    handle = store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis")
    with pytest.raises(RunStateError):
        handle.finish("exploded")  # type: ignore[arg-type]
    record = handle.finish("succeeded")  # type: ignore[arg-type]
    assert record.state is RunState.SUCCEEDED
    assert store.get_run(handle.run_id).state is RunState.SUCCEEDED


def test_context_manager_records_failure(store: LocalRunStore) -> None:
    with pytest.raises(KeyError), store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis") as handle:
        raise KeyError("boom")
    record = store.get_run(handle.run_id)
    assert record.state is RunState.FAILED and record.reason == "unhandled KeyError"


def test_find_succeeded_ignores_runs_whose_artifacts_disappeared(store: LocalRunStore) -> None:
    artifact = store.put_bytes(b"data")
    store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis").finish(
        RunState.SUCCEEDED, artifacts={"a.json": artifact}
    )
    os.unlink(store.root / "objects" / artifact.sha256[:2] / artifact.sha256)

    assert store.find_succeeded(KEY) is None
    assert store.verify().missing_artifacts[0][1] == "a.json"


def test_live_run_is_never_marked_interrupted(store: LocalRunStore) -> None:
    handle = store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis")
    report = store.recover()
    assert report.interrupted_runs == ()
    assert store.get_run(handle.run_id).state is RunState.RUNNING
    handle.finish(RunState.SUCCEEDED)


# --- records, schema, corruption ----------------------------------------------------


def _record_path(store: LocalRunStore, run_id: str) -> Path:
    return store.root / "runs" / run_id / "run.json"


def test_truncated_or_tampered_records_are_detected(store: LocalRunStore) -> None:
    a = store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis")
    a.finish(RunState.SUCCEEDED)
    b = store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis")
    b.finish(RunState.FAILED)

    raw = _record_path(store, a.run_id).read_bytes()
    _record_path(store, a.run_id).write_bytes(raw[: len(raw) // 2])
    envelope = json.loads(_record_path(store, b.run_id).read_bytes())
    envelope["record"]["state"] = "succeeded"  # checksum now wrong
    _record_path(store, b.run_id).write_text(json.dumps(envelope))

    for run_id in (a.run_id, b.run_id):
        with pytest.raises(CorruptRecordError):
            store.get_run(run_id)
    assert store.list_runs() == []
    assert set(store.verify().corrupt_records) == {a.run_id, b.run_id}
    assert store.find_succeeded(KEY) is None


def test_newer_record_and_store_schemas_are_refused(store: LocalRunStore, tmp_path: Path) -> None:
    handle = store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis")
    handle.finish(RunState.SUCCEEDED)
    envelope = json.loads(_record_path(store, handle.run_id).read_bytes())
    envelope["record"]["schema_version"] = 2
    body = json.dumps(envelope["record"], sort_keys=True, separators=(",", ":")).encode()
    envelope["sha256"] = hashlib.sha256(body).hexdigest()
    _record_path(store, handle.run_id).write_text(json.dumps(envelope))
    with pytest.raises(IncompatibleSchemaError):
        store.get_run(handle.run_id)

    future = tmp_path / "future"
    future.mkdir(mode=0o700)
    (future / "store.json").write_text('{"schema_version": 99}')
    with pytest.raises(IncompatibleSchemaError):
        LocalRunStore(future)


# --- path safety ----------------------------------------------------------------------


def test_identifiers_are_validated_against_path_traversal(store: LocalRunStore) -> None:
    for bad in ("../../etc", "a" * 31, "A" * 32):
        with pytest.raises(ValueError):
            store.get_run(bad)
    with pytest.raises(ValueError):
        ArtifactRef("../" + "0" * 61, 1)
    with pytest.raises(ValueError):
        store.begin_run(idempotency_key="not-hex", revision_id=REV, kind="analysis")
    with pytest.raises(ValueError):
        store.begin_run(idempotency_key=KEY, revision_id=REV, kind="../x")


def test_symlinked_or_shared_store_locations_are_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(UnsafeStorePathError):
        LocalRunStore(tmp_path / "link")

    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    with pytest.raises(UnsafeStorePathError):
        LocalRunStore(shared)

    store = LocalRunStore(tmp_path / "ok")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    ref = hashlib.sha256(b"payload").hexdigest()
    (store.root / "objects" / ref[:2]).symlink_to(outside)  # planted bucket symlink
    with pytest.raises(UnsafeStorePathError):
        store.put_bytes(b"payload")
    assert list(outside.iterdir()) == []


# --- retention ---------------------------------------------------------------------


def test_prune_keeps_newest_and_running_runs_and_collects_garbage(tmp_path: Path) -> None:
    clock = _Clock()
    store = LocalRunStore(tmp_path / "store", gc_grace_seconds=0, clock=clock)
    finished = []
    for i in range(5):
        artifact = store.put_bytes(f"artifact-{i}".encode())
        handle = store.begin_run(idempotency_key=hashlib.sha256(bytes([i])).hexdigest(), revision_id=REV, kind="analysis")
        finished.append(handle.finish(RunState.SUCCEEDED, artifacts={"a.json": artifact}))
        clock.now += timedelta(days=1)
    live = store.begin_run(idempotency_key=KEY, revision_id=REV, kind="analysis")

    report = store.prune(keep_last=2, older_than_seconds=36 * 3600)

    remaining = {r.run_id for r in store.list_runs()}
    assert remaining == {finished[3].run_id, finished[4].run_id, live.run_id}
    assert set(report.deleted_runs) == {r.run_id for r in finished[:3]}
    assert report.deleted_objects == 3 and len(_object_files(store)) == 2
    assert store.find_succeeded(finished[0].idempotency_key) is None
    assert store.verify().corrupt_records == () and store.verify().missing_artifacts == ()


def test_prune_by_size_never_deletes_live_data_and_reports_over_budget(tmp_path: Path) -> None:
    clock = _Clock()
    store = LocalRunStore(tmp_path / "store", gc_grace_seconds=0, clock=clock)
    for i in range(3):
        artifact = store.put_bytes(bytes([i]) * 1000)
        store.begin_run(idempotency_key=hashlib.sha256(bytes([i])).hexdigest(), revision_id=REV, kind="analysis").finish(
            RunState.SUCCEEDED, artifacts={"a.json": artifact}
        )
        clock.now += timedelta(minutes=1)

    report = store.prune(max_total_bytes=1500)
    assert report.remaining_bytes == 1000 and not report.over_budget and len(report.deleted_runs) == 2

    protected = store.prune(keep_last=1, max_total_bytes=10)
    assert protected.over_budget and protected.remaining_bytes == 1000


def test_gc_grace_protects_fresh_unreferenced_objects(tmp_path: Path) -> None:
    store = LocalRunStore(tmp_path / "store")  # default 24 h grace
    store.put_bytes(b"stored by a run that has not finished yet")
    assert store.prune(max_total_bytes=0).deleted_objects == 0
    assert len(_object_files(store)) == 1


def test_revision_and_store_work_in_an_empty_environment_without_server_modules(tmp_path: Path) -> None:
    import subprocess
    import sys

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("print('local')\n")
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from patchfrog.app.revision import capture_revision\n"
        "from patchfrog.app.store import LocalRunStore, RunState\n"
        f"rev = capture_revision(Path({str(repo)!r}))\n"
        f"store = LocalRunStore(Path({str(tmp_path / 'store')!r}))\n"
        "ref = store.put_bytes(json.dumps(rev.to_dict()).encode())\n"
        "handle = store.begin_run(idempotency_key=rev.revision_id, revision_id=rev.revision_id, kind='analysis')\n"
        "handle.finish(RunState.SUCCEEDED, artifacts={'revision.json': ref})\n"
        "found = store.find_succeeded(rev.revision_id)\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'sqlalchemy', 'celery', 'redis', 'anthropic', 'openai', 'fastapi', 'asyncpg', 'apps'} "
        "or m.startswith(('google.genai', 'patchfrog.persistence', 'patchfrog.review', 'patchfrog.config.settings', 'patchfrog.github')))\n"
        "print(json.dumps({'found': found is not None, 'files': rev.file_count, 'heavy': heavy}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"found": True, "files": 1, "heavy": []}
