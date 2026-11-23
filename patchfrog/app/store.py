"""LocalRunStore: crash-safe, content-addressed local storage for immutable,
revision-bound analysis artifacts (Milestone 2.1, ADR-002,
docs/architecture/LOCAL_STORE.md).

Layout (everything below ``root``, created ``0o700``)::

    store.json                      store schema marker
    lock                            exclusive lock for recover()/prune()
    objects/<aa>/<sha256>           immutable artifact bytes (content address)
    runs/<run_id>/run.json          run record, checksummed
    runs/<run_id>/lock              held (flock) by the live run owner
    index/<idempotency_key>         run_id of the first SUCCEEDED run for a key
    tmp/                            in-flight writes (flock-held while writing)
    locks/<aa>                      request-lock stripes (Milestone 2.2), never deleted

Identities are separate on purpose:
* **artifact identity**: ``ArtifactRef.sha256``, the hash of the bytes;
  identical content is stored once (dedup);
* **execution identity**: ``RunRecord.run_id``, unique per execution;
* **request identity**: ``RunRecord.idempotency_key``, supplied by the
  caller (deterministic over revision + configuration); used to find an
  earlier SUCCEEDED run.

Guarantees (POSIX, local filesystem; see LOCAL_STORE.md "Durability"):
* Publication is atomic: data is written to ``tmp/``, ``fsync``ed, then
  ``os.replace``d into place, and the parent directory is ``fsync``ed. A
  reader sees either nothing or the complete file, never a partial one.
* Every read is verified: artifact bytes against their sha256, records
  against their embedded checksum and schema version.
* Liveness uses ``flock``: a run owner holds an exclusive lock on its run
  for the run's lifetime. The kernel releases it when the process dies,
  even on SIGKILL, so :meth:`LocalRunStore.recover` can tell an
  interrupted run from a live one without PIDs (no PID-reuse problem).
  ``flock`` is unreliable on network filesystems, so the store is for
  local disks only.
* Nothing ever turns a partial write into a completed artifact or record.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType

STORE_SCHEMA_VERSION = 1
RUN_RECORD_SCHEMA_VERSION = 1
DEFAULT_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
#: Unreferenced objects younger than this are never garbage-collected (an
#: in-flight run may have stored them but not yet recorded them).
DEFAULT_GC_GRACE_SECONDS = 24 * 3600
#: Abandoned temp files younger than this are left alone.
TMP_GRACE_SECONDS = 60.0

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_KIND = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CHUNK_BYTES = 1 << 20


class StoreError(RuntimeError):
    """Base class for store failures."""


class UnsafeStorePathError(StoreError):
    """The store root (or something inside it) is not safe to use."""


class CorruptArtifactError(StoreError):
    """Stored bytes do not match their content address."""


class CorruptRecordError(StoreError):
    """A run record is unreadable, truncated, or fails its checksum."""


class IncompatibleSchemaError(StoreError):
    """The store or a record was written by a newer, unknown schema."""


class ArtifactTooLargeError(StoreError):
    """An artifact exceeds the configured ``max_artifact_bytes``."""


class RunNotFoundError(StoreError):
    """No run with this id exists in the store (Milestone 2.5; still a StoreError)."""


class RunStateError(StoreError):
    """An invalid run lifecycle transition was requested."""


class RunState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATES = frozenset(RunState) - {RunState.RUNNING}
#: States an owner may finish with (INTERRUPTED is set only by recover()).
FINISH_STATES = TERMINAL_STATES - {RunState.INTERRUPTED}


@dataclass(frozen=True, order=True)
class ArtifactRef:
    sha256: str
    size: int

    def __post_init__(self) -> None:
        if not _HEX64.match(self.sha256) or self.size < 0:
            raise ValueError("invalid artifact reference")


@dataclass(frozen=True)
class RunRecord:
    schema_version: int
    run_id: str
    idempotency_key: str
    revision_id: str
    kind: str
    state: RunState
    created_at: str
    finished_at: str | None = None
    reason: str | None = None
    artifacts: Mapping[str, ArtifactRef] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "revision_id": self.revision_id,
            "kind": self.kind,
            "state": self.state.value,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "reason": self.reason,
            "artifacts": {name: {"sha256": r.sha256, "size": r.size} for name, r in sorted(self.artifacts.items())},
            "metadata": dict(sorted(self.metadata.items())),
        }

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> RunRecord:
        version = data.get("schema_version")
        if not isinstance(version, int):
            raise CorruptRecordError("run record without a schema version")
        if version > RUN_RECORD_SCHEMA_VERSION:
            raise IncompatibleSchemaError(f"run record schema {version} is newer than supported {RUN_RECORD_SCHEMA_VERSION}")
        try:
            raw_artifacts = data["artifacts"]
            raw_metadata = data["metadata"]
            assert isinstance(raw_artifacts, dict) and isinstance(raw_metadata, dict)
            return RunRecord(
                schema_version=version,
                run_id=str(data["run_id"]),
                idempotency_key=str(data["idempotency_key"]),
                revision_id=str(data["revision_id"]),
                kind=str(data["kind"]),
                state=RunState(str(data["state"])),
                created_at=str(data["created_at"]),
                finished_at=None if data["finished_at"] is None else str(data["finished_at"]),
                reason=None if data["reason"] is None else str(data["reason"]),
                artifacts={str(n): ArtifactRef(str(a["sha256"]), int(a["size"])) for n, a in raw_artifacts.items()},
                metadata={str(k): str(v) for k, v in raw_metadata.items()},
            )
        except (KeyError, ValueError, TypeError, AssertionError) as exc:
            raise CorruptRecordError(f"malformed run record: {exc!r}") from exc


@dataclass(frozen=True)
class RecoveryReport:
    interrupted_runs: tuple[str, ...]
    removed_tmp_files: int
    corrupt_records: tuple[str, ...]


@dataclass(frozen=True)
class PruneReport:
    deleted_runs: tuple[str, ...]
    deleted_objects: int
    freed_bytes: int
    remaining_bytes: int
    #: True when ``max_total_bytes`` could not be met without deleting live
    #: (running or protected) data, which prune never does.
    over_budget: bool


@dataclass(frozen=True)
class VerifyReport:
    objects_checked: int
    corrupt_objects: tuple[str, ...]
    corrupt_records: tuple[str, ...]
    missing_artifacts: tuple[tuple[str, str], ...]  # (run_id, artifact name)


def _canonical(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _utc(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_private_dir(path: Path) -> None:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise UnsafeStorePathError(f"{path.name!r} in the store must be a real directory, not a symlink or file")
    if st.st_uid != os.getuid():
        raise UnsafeStorePathError(f"{path.name!r} in the store is owned by another user")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafeStorePathError(f"{path.name!r} in the store is group- or world-writable")


class RunHandle:
    """Owner side of one RUNNING run. Holds the run's flock until finished.

    Use as a context manager: leaving the block without calling
    :meth:`finish` records FAILED (with the exception type, if any)."""

    def __init__(self, store: LocalRunStore, record: RunRecord, lock_fd: int) -> None:
        self._store = store
        self._record = record
        self._lock_fd: int | None = lock_fd

    @property
    def record(self) -> RunRecord:
        return self._record

    @property
    def run_id(self) -> str:
        return self._record.run_id

    def finish(
        self, state: RunState, *, artifacts: Mapping[str, ArtifactRef] | None = None, reason: str | None = None
    ) -> RunRecord:
        if self._lock_fd is None:
            raise RunStateError(f"run {self.run_id} is already finished")
        try:
            state = RunState(state)  # normalize: a plain str equal to a member is accepted, never stored raw
        except ValueError as exc:
            raise RunStateError(f"unknown run state {state!r}") from exc
        if state not in FINISH_STATES:
            raise RunStateError(f"cannot finish a run as {state.value!r}")
        final = self._store._finish(self._record, state, dict(artifacts or {}), reason)
        self._release()
        self._record = final
        return final

    def _release(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing the descriptor drops the flock
            self._lock_fd = None

    def __enter__(self) -> RunHandle:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._lock_fd is not None:
            reason = "run handle closed without finish" if exc_type is None else f"unhandled {exc_type.__name__}"
            self.finish(RunState.FAILED, reason=reason)


class LocalRunStore:
    """See the module docstring. One instance per process is fine; any
    number of processes may share one store directory."""

    def __init__(
        self,
        root: Path,
        *,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        gc_grace_seconds: float = DEFAULT_GC_GRACE_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        self._max_artifact_bytes = max_artifact_bytes
        self._gc_grace_seconds = gc_grace_seconds
        self._clock = clock
        root = Path(os.path.abspath(root))
        if root.is_symlink():
            raise UnsafeStorePathError("the store root must not be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_private_dir(root)
        self.root = root
        for name in ("objects", "runs", "index", "tmp", "locks"):
            sub = root / name
            with contextlib.suppress(FileExistsError):
                sub.mkdir(mode=0o700)
            _check_private_dir(sub)
        marker = root / "store.json"
        if marker.exists():
            try:
                version = json.loads(self._read_file(marker)).get("schema_version")
            except (ValueError, AttributeError) as exc:
                raise CorruptRecordError("store.json is unreadable") from exc
            if not isinstance(version, int) or version > STORE_SCHEMA_VERSION:
                raise IncompatibleSchemaError(f"store schema {version!r} is not supported (max {STORE_SCHEMA_VERSION})")
        else:
            self._atomic_write(marker, _canonical({"schema_version": STORE_SCHEMA_VERSION}))

    # --- low-level file helpers ---------------------------------------------------

    def _read_file(self, path: Path, max_bytes: int | None = None) -> bytes:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafeStorePathError(f"{path.name!r} is not a regular file")
            if max_bytes is not None and st.st_size > max_bytes:
                raise ArtifactTooLargeError(f"{st.st_size} bytes exceed the read limit of {max_bytes}")
            chunks = []
            while chunk := os.read(fd, _CHUNK_BYTES):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _tmp_file(self) -> Iterator[tuple[int, Path]]:
        """An exclusively created, flock-held temp file. Unlinked on error."""

        path = self.root / "tmp" / f"{uuid.uuid4().hex}.tmp"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield fd, path
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            raise
        finally:
            os.close(fd)

    def _atomic_write(self, final: Path, data: bytes) -> None:
        with self._tmp_file() as (fd, tmp):
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            os.replace(tmp, final)
        _fsync_dir(final.parent)

    # --- artifacts ------------------------------------------------------------------

    def _object_path(self, sha256: str) -> Path:
        if not _HEX64.match(sha256):
            raise ValueError("invalid artifact address")
        return self.root / "objects" / sha256[:2] / sha256

    def _object_dir(self, sha256: str) -> Path:
        directory = self._object_path(sha256).parent
        with contextlib.suppress(FileExistsError):
            directory.mkdir(mode=0o700)
        _check_private_dir(directory)
        return directory

    def has_artifact(self, ref: ArtifactRef) -> bool:
        try:
            st = os.lstat(self._object_path(ref.sha256))
        except FileNotFoundError:
            return False
        return stat.S_ISREG(st.st_mode) and st.st_size == ref.size

    def put_bytes(self, data: bytes) -> ArtifactRef:
        if len(data) > self._max_artifact_bytes:
            raise ArtifactTooLargeError(f"{len(data)} bytes exceed max_artifact_bytes={self._max_artifact_bytes}")
        ref = ArtifactRef(hashlib.sha256(data).hexdigest(), len(data))
        if self.has_artifact(ref):
            return ref  # dedup: identical content is already stored
        self._object_dir(ref.sha256)
        final = self._object_path(ref.sha256)
        with self._tmp_file() as (fd, tmp):
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fchmod(fd, 0o400)
            os.fsync(fd)
            os.replace(tmp, final)
        _fsync_dir(final.parent)
        return ref

    def put_stream(self, chunks: Iterable[bytes]) -> ArtifactRef:
        """Store a stream without holding it in memory (bounded by one chunk)."""

        digest = hashlib.sha256()
        size = 0
        with self._tmp_file() as (fd, tmp):
            for chunk in chunks:
                size += len(chunk)
                if size > self._max_artifact_bytes:
                    raise ArtifactTooLargeError(f"stream exceeds max_artifact_bytes={self._max_artifact_bytes}")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
            ref = ArtifactRef(digest.hexdigest(), size)
            if self.has_artifact(ref):
                os.unlink(tmp)
                return ref
            self._object_dir(ref.sha256)
            final = self._object_path(ref.sha256)
            os.fchmod(fd, 0o400)
            os.fsync(fd)
            os.replace(tmp, final)
        _fsync_dir(final.parent)
        return ref

    def read_bytes(self, ref: ArtifactRef, *, max_bytes: int | None = None) -> bytes:
        limit = self._max_artifact_bytes if max_bytes is None else max_bytes
        try:
            data = self._read_file(self._object_path(ref.sha256), max_bytes=limit)
        except FileNotFoundError as exc:
            raise StoreError(f"artifact {ref.sha256} is not in the store") from exc
        if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise CorruptArtifactError(f"artifact {ref.sha256} is corrupt")
        return data

    def verify_artifact(self, ref: ArtifactRef) -> bool:
        """Streaming check (bounded memory) that the stored bytes match ``ref``."""

        try:
            fd = os.open(self._object_path(ref.sha256), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := os.read(fd, _CHUNK_BYTES):
                size += len(chunk)
                digest.update(chunk)
        finally:
            os.close(fd)
        return size == ref.size and digest.hexdigest() == ref.sha256

    # --- runs -------------------------------------------------------------------------

    def _run_dir(self, run_id: str) -> Path:
        if not _RUN_ID.match(run_id):
            raise ValueError("invalid run id")
        return self.root / "runs" / run_id

    def _write_record(self, record: RunRecord) -> None:
        body = record.to_dict()
        envelope = {"record": body, "sha256": hashlib.sha256(_canonical(body)).hexdigest()}
        self._atomic_write(self._run_dir(record.run_id) / "run.json", _canonical(envelope))

    def get_run(self, run_id: str) -> RunRecord:
        path = self._run_dir(run_id) / "run.json"
        try:
            raw = self._read_file(path, max_bytes=8 * 1024 * 1024)
        except FileNotFoundError as exc:
            raise RunNotFoundError(f"run {run_id} does not exist") from exc
        try:
            envelope = json.loads(raw)
            body = envelope["record"]
            checksum = envelope["sha256"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CorruptRecordError(f"run {run_id}: unreadable record") from exc
        if not isinstance(body, dict) or hashlib.sha256(_canonical(body)).hexdigest() != checksum:
            raise CorruptRecordError(f"run {run_id}: record checksum mismatch")
        record = RunRecord.from_dict(body)
        if record.run_id != run_id:
            raise CorruptRecordError(f"run {run_id}: record names another run")
        return record

    def begin_run(
        self, *, idempotency_key: str, revision_id: str, kind: str, metadata: Mapping[str, str] | None = None
    ) -> RunHandle:
        if not _HEX64.match(idempotency_key) or not _HEX64.match(revision_id):
            raise ValueError("idempotency_key and revision_id must be lowercase sha256 hex")
        if not _KIND.match(kind):
            raise ValueError("invalid run kind")
        meta = dict(metadata or {})
        if len(_canonical(meta)) > 64 * 1024:
            raise ValueError("run metadata is limited to 64 KiB")
        run_id = uuid.uuid4().hex
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(mode=0o700)
        lock_fd = os.open(run_dir / "lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = RunRecord(
            schema_version=RUN_RECORD_SCHEMA_VERSION,
            run_id=run_id,
            idempotency_key=idempotency_key,
            revision_id=revision_id,
            kind=kind,
            state=RunState.RUNNING,
            created_at=_utc(self._clock()),
            metadata=meta,
        )
        try:
            self._write_record(record)
        except BaseException:
            os.close(lock_fd)
            raise
        return RunHandle(self, record, lock_fd)

    def _finish(
        self, record: RunRecord, state: RunState, artifacts: dict[str, ArtifactRef], reason: str | None
    ) -> RunRecord:
        for name, ref in artifacts.items():
            if not _NAME.match(name):
                raise ValueError(f"invalid artifact name {name!r}")
            if not self.has_artifact(ref):
                raise StoreError(f"artifact {name!r} ({ref.sha256}) is not in the store")
        current = self.get_run(record.run_id)
        if current.state is not RunState.RUNNING:
            raise RunStateError(f"run {record.run_id} is {current.state.value}, not running")
        final = replace(current, state=state, finished_at=_utc(self._clock()), reason=reason, artifacts=artifacts)
        self._write_record(final)
        if state is RunState.SUCCEEDED:
            self._publish_index(final)
        return final

    def _publish_index(self, record: RunRecord) -> None:
        index = self.root / "index" / record.idempotency_key
        with self._tmp_file() as (fd, tmp):
            os.write(fd, record.run_id.encode("ascii"))
            os.fsync(fd)
            try:
                os.link(tmp, index)  # first SUCCEEDED run wins; never replaced
            except FileExistsError:
                pass
            except OSError as exc:
                if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EXDEV):
                    raise
                os.replace(tmp, index)  # no hard links: last writer wins (documented)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        _fsync_dir(index.parent)

    def find_succeeded(self, idempotency_key: str) -> RunRecord | None:
        """The SUCCEEDED run for this request, if one exists and its
        artifacts are all present (otherwise ``None``: recompute)."""

        if not _HEX64.match(idempotency_key):
            raise ValueError("invalid idempotency key")
        try:
            run_id = self._read_file(self.root / "index" / idempotency_key, max_bytes=64).decode("ascii").strip()
            record = self.get_run(run_id)
        except (FileNotFoundError, StoreError, ValueError, UnicodeDecodeError):
            return None
        if record.state is not RunState.SUCCEEDED or record.idempotency_key != idempotency_key:
            return None
        if not all(self.has_artifact(ref) for ref in record.artifacts.values()):
            return None
        return record

    def _run_ids(self) -> list[str]:
        ids = []
        with os.scandir(self.root / "runs") as entries:
            for entry in entries:
                if _RUN_ID.match(entry.name) and entry.is_dir(follow_symlinks=False):
                    ids.append(entry.name)
        return sorted(ids)

    def list_runs(self) -> list[RunRecord]:
        """Readable records, oldest first (corrupt ones are reported by verify())."""

        records = []
        for run_id in self._run_ids():
            with contextlib.suppress(StoreError):
                records.append(self.get_run(run_id))
        return sorted(records, key=lambda r: (r.created_at, r.run_id))

    # --- recovery, retention, verification ----------------------------------------

    @contextlib.contextmanager
    def _store_lock(self) -> Iterator[None]:
        fd = os.open(self.root / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def request_lock(self, idempotency_key: str, *, timeout_seconds: float, poll_seconds: float = 0.05) -> Iterator[bool]:
        """Serialize work on one request (idempotency key) across threads and
        processes. Yields ``True`` when the lock is held, ``False`` when it
        could not be taken within ``timeout_seconds`` (the caller decides).

        Keys map onto 256 fixed lock files (first two hex digits). Lock files
        are never deleted: unlinking a lock file someone may hold would let
        two owners in. The stripe bound keeps their number fixed; unrelated
        keys that share a stripe only wait for each other. ``flock`` is per
        open file description, so two threads of one process also exclude
        each other, and the kernel drops the lock when its owner dies."""

        if not _HEX64.match(idempotency_key):
            raise ValueError("invalid idempotency key")
        if timeout_seconds < 0 or poll_seconds <= 0:
            raise ValueError("timeout_seconds must be >= 0 and poll_seconds > 0")
        fd = os.open(self.root / "locks" / idempotency_key[:2], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        yield False
                        return
                    time.sleep(min(poll_seconds, remaining))
            yield True
        finally:
            os.close(fd)

    def recover(self) -> RecoveryReport:
        """Mark runs whose owner died as INTERRUPTED and delete abandoned
        temp files. Safe to call at any time, from any process."""

        interrupted: list[str] = []
        corrupt: list[str] = []
        removed = 0
        with self._store_lock():
            for run_id in self._run_ids():
                try:
                    record = self.get_run(run_id)
                except StoreError:
                    corrupt.append(run_id)
                    continue
                if record.state is not RunState.RUNNING:
                    continue
                try:
                    fd = os.open(self._run_dir(run_id) / "lock", os.O_RDWR | os.O_NOFOLLOW)
                except FileNotFoundError:
                    fd = os.open(self._run_dir(run_id) / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue  # owner alive
                    current = self.get_run(run_id)  # re-read under the lock
                    if current.state is RunState.RUNNING:
                        self._write_record(
                            replace(
                                current,
                                state=RunState.INTERRUPTED,
                                finished_at=_utc(self._clock()),
                                reason="owner process exited without finishing",
                            )
                        )
                        interrupted.append(run_id)
                finally:
                    os.close(fd)
            now = time.time()
            with os.scandir(self.root / "tmp") as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if now - entry.stat(follow_symlinks=False).st_mtime < TMP_GRACE_SECONDS:
                        continue
                    try:
                        fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW)
                    except FileNotFoundError:
                        continue
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        os.close(fd)
                        continue  # a live writer holds it
                    try:
                        os.unlink(entry.path)
                        removed += 1
                    finally:
                        os.close(fd)
        return RecoveryReport(tuple(interrupted), removed, tuple(corrupt))

    def _objects(self) -> Iterator[tuple[str, os.stat_result]]:
        with os.scandir(self.root / "objects") as buckets:
            for bucket in sorted(buckets, key=lambda e: e.name):
                if not bucket.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(bucket.path) as files:
                    for item in sorted(files, key=lambda e: e.name):
                        if _HEX64.match(item.name) and item.is_file(follow_symlinks=False):
                            yield item.name, item.stat(follow_symlinks=False)

    def total_bytes(self) -> int:
        return sum(st.st_size for _sha, st in self._objects())

    def _delete_run(self, record: RunRecord) -> None:
        index = self.root / "index" / record.idempotency_key
        with contextlib.suppress(FileNotFoundError, StoreError, UnicodeDecodeError):
            if self._read_file(index, max_bytes=64).decode("ascii").strip() == record.run_id:
                os.unlink(index)
        run_dir = self._run_dir(record.run_id)
        for name in ("run.json", "lock"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(run_dir / name)
        with contextlib.suppress(OSError):
            os.rmdir(run_dir)

    def prune(
        self,
        *,
        keep_last: int | None = None,
        older_than_seconds: float | None = None,
        max_total_bytes: int | None = None,
    ) -> PruneReport:
        """Delete finished runs by policy, then unreferenced objects.

        Never deletes a RUNNING run, one of the ``keep_last`` newest finished
        runs, or an unreferenced object younger than the GC grace period."""

        with self._store_lock():
            now = self._clock()
            records = self.list_runs()
            finished = [r for r in records if r.state in TERMINAL_STATES]
            newest_first = sorted(finished, key=lambda r: (r.created_at, r.run_id), reverse=True)
            protected = {r.run_id for r in newest_first[: keep_last or 0]}

            def age(record: RunRecord) -> float:
                created = datetime.strptime(record.created_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
                return (now - created).total_seconds()

            doomed = [
                r for r in newest_first
                if r.run_id not in protected and (older_than_seconds is None or age(r) > older_than_seconds)
                and (keep_last is not None or older_than_seconds is not None)
            ]
            for record in doomed:
                self._delete_run(record)
            deleted = [r.run_id for r in doomed]
            freed, removed_objects = self._collect_garbage()

            over_budget = False
            if max_total_bytes is not None:
                remaining = [r for r in newest_first if r.run_id not in set(deleted) and r.run_id not in protected]
                while self.total_bytes() > max_total_bytes and remaining:
                    victim = remaining.pop()  # oldest unprotected finished run
                    self._delete_run(victim)
                    deleted.append(victim.run_id)
                    more_freed, more_removed = self._collect_garbage()
                    freed += more_freed
                    removed_objects += more_removed
                over_budget = self.total_bytes() > max_total_bytes
            return PruneReport(tuple(deleted), removed_objects, freed, self.total_bytes(), over_budget)

    def _collect_garbage(self) -> tuple[int, int]:
        referenced = {ref.sha256 for record in self.list_runs() for ref in record.artifacts.values()}
        now = time.time()
        freed = removed = 0
        for sha, st in list(self._objects()):
            if sha in referenced or now - st.st_mtime < self._gc_grace_seconds:
                continue
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._object_path(sha))
                freed += st.st_size
                removed += 1
        return freed, removed

    def verify(self) -> VerifyReport:
        """Full scan: every object re-hashed (streaming), every record
        checked, every referenced artifact present. Reports, never repairs."""

        corrupt_objects = []
        checked = 0
        for sha, st in self._objects():
            checked += 1
            if not self.verify_artifact(ArtifactRef(sha, st.st_size)):
                corrupt_objects.append(sha)
        corrupt_records = []
        missing = []
        for run_id in self._run_ids():
            try:
                record = self.get_run(run_id)
            except StoreError:
                corrupt_records.append(run_id)
                continue
            for name, ref in sorted(record.artifacts.items()):
                if not self.has_artifact(ref):
                    missing.append((run_id, name))
        return VerifyReport(checked, tuple(corrupt_objects), tuple(corrupt_records), tuple(missing))


__all__ = [
    "ArtifactRef",
    "ArtifactTooLargeError",
    "CorruptArtifactError",
    "CorruptRecordError",
    "IncompatibleSchemaError",
    "LocalRunStore",
    "PruneReport",
    "RecoveryReport",
    "RunHandle",
    "RunRecord",
    "RunState",
    "RunStateError",
    "StoreError",
    "UnsafeStorePathError",
    "VerifyReport",
]
