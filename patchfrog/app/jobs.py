"""JobRunner: the in-process execution layer over RepositoryRevision and
LocalRunStore (Milestone 2.2, ADR-010, docs/architecture/LOCAL_STORE.md §3,
docs/architecture/SCALABILITY.md §2-4).

One call, :meth:`JobRunner.run`, does the whole request:

1. capture the analysed snapshot (:func:`capture_revision`; the store is
   excluded when it lies inside the root);
2. derive the request identity (:func:`derive_idempotency_key`);
3. take the store's request lock, so identical requests run once;
4. return the stored SUCCEEDED run for that key without calling the job, or
5. begin a run, call the job function with a :class:`JobContext`, and
   finish the run with exactly one terminal state and a ``job-report``
   artifact.

Deliberately not here (later milestones, only when needed): a queue, thread
or process pools, asyncio wrappers, retries, server-mode dispatch. Celery
or a hosted service would wrap the *same* job function (ADR-010).

Cancellation and the wall-time budget are cooperative: the job calls
:meth:`JobContext.checkpoint` between units of work. Revision capture
itself is bounded by its file and byte budgets, not by the deadline; the
deadline is checked right after it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from patchfrog.app.revision import (
    RepositoryRevision,
    RevisionBudgetExceeded,
    RevisionCaptureError,
    RevisionEntry,
    RevisionScope,
    capture_revision,
    read_revision_file,
)
from patchfrog.app.store import (
    ArtifactRef,
    LocalRunStore,
    RunHandle,
    RunRecord,
    RunState,
    StoreError,
)

#: Version of the key derivation and of the ``job-report`` layout. Part of
#: every idempotency key, so changing what a key means can never reuse an
#: old run.
JOB_PROTOCOL_VERSION = 1
JOB_REPORT_ARTIFACT = "job-report"
MAX_CONFIG_BYTES = 64 * 1024
MAX_PARTIAL_REASONS = 100
MAX_REASON_CHARS = 200

_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_KIND = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ARTIFACT_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class FailureClass(StrEnum):
    """Closed set of failure classes (SCALABILITY.md §2). Wire enum."""

    INPUT_ERROR = "input_error"
    UNSUPPORTED_PROJECT = "unsupported_project"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TOOL_UNAVAILABLE = "tool_unavailable"
    EXECUTION_FAILED = "execution_failed"
    ENVIRONMENT_UNSTABLE = "environment_unstable"
    INTERRUPTED = "interrupted"
    INTERNAL_ERROR = "internal_error"


class JobError(Exception):
    """A typed failure. Raised by job code to fail its run with a class,
    and by :meth:`JobRunner.run` when no run can be started (for example
    the revision cannot be captured). The message is recorded, so it must
    not contain secrets or source text."""

    def __init__(
        self, failure_class: FailureClass, message: str, *, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.failure_class = FailureClass(failure_class)
        self.retryable = retryable


class _JobStop(BaseException):
    """Unwinds the job from a checkpoint. A ``BaseException`` so that a job's
    own ``except Exception`` cannot swallow a cancellation or a budget stop."""

    def __init__(self, state: RunState, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


class CancellationToken:
    """Thread-safe, one-way cancellation flag. Cancelling never interrupts
    the job directly; it takes effect at the job's next checkpoint."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class JobBudget:
    """Explicit limits for one request. Budgets only stop work; they are
    not part of the idempotency key (anything that shapes a *complete*
    result, such as a findings cap, belongs in the job config instead)."""

    max_seconds: float = 600.0
    max_artifacts: int = 64
    max_artifact_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        if not (math.isfinite(self.max_seconds) and self.max_seconds > 0):
            raise ValueError("max_seconds must be a positive finite number")
        if self.max_artifacts < 1 or self.max_artifact_bytes < 1:
            raise ValueError("max_artifacts and max_artifact_bytes must be positive")


@dataclass(frozen=True)
class JobSpec:
    """What to run. ``config`` is JSON data (str keys; str, int, finite
    float, bool, None, lists and dicts) and is part of the request identity."""

    kind: str
    root: Path
    engine_version: str
    config: Mapping[str, object] = field(default_factory=dict)
    ruleset_version: str = "none"
    scope: RevisionScope = field(default_factory=RevisionScope)
    budget: JobBudget = field(default_factory=JobBudget)

    def __post_init__(self) -> None:
        if not _KIND.match(self.kind):
            raise ValueError("invalid job kind")
        for label, value in (
            ("engine_version", self.engine_version),
            ("ruleset_version", self.ruleset_version),
        ):
            if not _VERSION.match(value):
                raise ValueError(f"invalid {label}")
        config_fingerprint(self.config)  # validate early


@dataclass(frozen=True)
class BudgetUsage:
    elapsed_seconds: float
    checkpoints: int
    artifacts: int
    artifact_bytes: int


@dataclass(frozen=True)
class JobReport:
    """Content of the ``job-report`` artifact written on every terminal state."""

    job_protocol_version: int
    kind: str
    state: RunState
    failure_class: FailureClass | None
    reason: str | None
    partial_reasons: tuple[str, ...]
    budget_usage: BudgetUsage
    config_fingerprint: str
    engine_version: str
    ruleset_version: str

    def to_dict(self) -> dict[str, object]:
        return {
            "job_protocol_version": self.job_protocol_version,
            "kind": self.kind,
            "state": self.state.value,
            "failure_class": None if self.failure_class is None else self.failure_class.value,
            "reason": self.reason,
            "partial_reasons": list(self.partial_reasons),
            "budget_usage": {
                "elapsed_seconds": self.budget_usage.elapsed_seconds,
                "checkpoints": self.budget_usage.checkpoints,
                "artifacts": self.budget_usage.artifacts,
                "artifact_bytes": self.budget_usage.artifact_bytes,
            },
            "config_fingerprint": self.config_fingerprint,
            "engine_version": self.engine_version,
            "ruleset_version": self.ruleset_version,
        }

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> JobReport:
        try:
            usage = data["budget_usage"]
            reasons = data["partial_reasons"]
            failure = data["failure_class"]
            reason = data["reason"]
            if not isinstance(usage, Mapping) or not isinstance(reasons, list):
                raise TypeError("malformed job report")
            if reason is not None and not isinstance(reason, str):
                raise TypeError("malformed job report")
            return JobReport(
                job_protocol_version=_as_int(data["job_protocol_version"]),
                kind=str(data["kind"]),
                state=RunState(str(data["state"])),
                failure_class=None if failure is None else FailureClass(str(failure)),
                reason=reason,
                partial_reasons=tuple(str(r) for r in reasons),
                budget_usage=BudgetUsage(
                    elapsed_seconds=float(_as_number(usage["elapsed_seconds"])),
                    checkpoints=_as_int(usage["checkpoints"]),
                    artifacts=_as_int(usage["artifacts"]),
                    artifact_bytes=_as_int(usage["artifact_bytes"]),
                ),
                config_fingerprint=str(data["config_fingerprint"]),
                engine_version=str(data["engine_version"]),
                ruleset_version=str(data["ruleset_version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError("job report is malformed") from exc


@dataclass(frozen=True)
class JobResult:
    """Outcome of :meth:`JobRunner.run`. ``reused`` is True when an earlier
    SUCCEEDED run was returned and the job function was not called."""

    record: RunRecord
    revision: RepositoryRevision
    report: JobReport
    reused: bool

    @property
    def state(self) -> RunState:
        return self.record.state

    @property
    def artifacts(self) -> Mapping[str, ArtifactRef]:
        """The job's own artifacts (the ``job-report`` excluded)."""

        return {k: v for k, v in self.record.artifacts.items() if k != JOB_REPORT_ARTIFACT}


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected an integer")
    return value


def _as_number(value: object) -> float | int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("expected a number")
    return value


def _check_json(value: object, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("config is nested too deeply")
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("config floats must be finite")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _check_json(item, depth + 1)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("config keys must be strings")
            _check_json(item, depth + 1)
        return
    raise ValueError(f"config value of type {type(value).__name__} is not JSON data")


def _canonical(data: object) -> bytes:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def config_fingerprint(config: Mapping[str, object]) -> str:
    """sha256 of the canonical JSON form (key order never matters)."""

    _check_json(config)
    encoded = _canonical(config)
    if len(encoded) > MAX_CONFIG_BYTES:
        raise ValueError(f"config is limited to {MAX_CONFIG_BYTES} bytes")
    return hashlib.sha256(encoded).hexdigest()


def derive_idempotency_key(
    *,
    kind: str,
    revision_id: str,
    config: Mapping[str, object],
    engine_version: str,
    ruleset_version: str,
) -> str:
    """The request identity. The scope is already part of ``revision_id``
    (via its scope fingerprint); budgets are intentionally excluded."""

    return hashlib.sha256(
        _canonical(
            {
                "job_protocol_version": JOB_PROTOCOL_VERSION,
                "kind": kind,
                "revision_id": revision_id,
                "config_fingerprint": config_fingerprint(config),
                "engine_version": engine_version,
                "ruleset_version": ruleset_version,
            }
        )
    ).hexdigest()


def _bounded(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 1] + "…"


class JobContext:
    """Handed to the job function. Not thread-safe: one job, one thread."""

    def __init__(
        self,
        *,
        store: LocalRunStore,
        root: Path,
        revision: RepositoryRevision,
        config: Mapping[str, object],
        budget: JobBudget,
        token: CancellationToken,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        self._store = store
        self._root = root
        self._entries: dict[str, RevisionEntry] | None = None
        self._budget = budget
        self._token = token
        self._deadline = deadline
        self._clock = clock
        self.revision = revision
        self.config: Mapping[str, object] = MappingProxyType(json.loads(_canonical(config)))
        self.checkpoints = 0
        self.artifact_bytes = 0
        self.artifacts: dict[str, ArtifactRef] = {}
        self.partial_reasons: list[str] = []

    def read_file(self, path: str, *, max_bytes: int) -> bytes:
        """The bytes of an in-revision file, verified against the revision
        (size and sha256; symlinks never followed). Raises ``KeyError`` for
        a path outside the revision and
        :class:`~patchfrog.app.revision.RevisionEntryChanged` when the file
        no longer matches, so a job never analyses content it can't attribute
        to ``self.revision``."""

        if self._entries is None:
            self._entries = {entry.path: entry for entry in self.revision.entries}
        return read_revision_file(self._root, self._entries[path], max_bytes=max_bytes)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def checkpoint(self) -> None:
        """Stop here if the run was cancelled or its wall-time budget is spent."""

        self.checkpoints += 1
        if self._token.cancelled:
            raise _JobStop(RunState.CANCELLED, "cancelled")
        if self._clock() >= self._deadline:
            raise _JobStop(RunState.PARTIAL, "budget_exhausted: max_seconds")

    def mark_partial(self, reason: str) -> None:
        """Record that the result is incomplete (for example a skipped file);
        the run then finishes PARTIAL, never SUCCEEDED."""

        if len(self.partial_reasons) < MAX_PARTIAL_REASONS:
            self.partial_reasons.append(_bounded(reason))
        elif len(self.partial_reasons) == MAX_PARTIAL_REASONS:
            self.partial_reasons.append("further partial reasons omitted")

    def _admit(self, name: str) -> None:
        if not _ARTIFACT_NAME.match(name) or name == JOB_REPORT_ARTIFACT:
            raise ValueError(f"invalid or reserved artifact name {name!r}")
        if name in self.artifacts:
            raise ValueError(f"artifact {name!r} was already written")
        if len(self.artifacts) >= self._budget.max_artifacts:
            raise _JobStop(RunState.PARTIAL, "budget_exhausted: max_artifacts")

    def _record(self, name: str, ref: ArtifactRef) -> ArtifactRef:
        if self.artifact_bytes + ref.size > self._budget.max_artifact_bytes:
            # The object may already be in the store; unreferenced, it is
            # garbage-collected by prune() after its grace period.
            raise _JobStop(RunState.PARTIAL, "budget_exhausted: max_artifact_bytes")
        self.artifact_bytes += ref.size
        self.artifacts[name] = ref
        return ref

    def put_bytes(self, name: str, data: bytes) -> ArtifactRef:
        self._admit(name)
        if self.artifact_bytes + len(data) > self._budget.max_artifact_bytes:
            raise _JobStop(RunState.PARTIAL, "budget_exhausted: max_artifact_bytes")
        return self._record(name, self._store.put_bytes(data))

    def put_stream(self, name: str, chunks: Iterable[bytes]) -> ArtifactRef:
        self._admit(name)
        return self._record(name, self._store.put_stream(chunks))

    def usage(self, elapsed: float) -> BudgetUsage:
        return BudgetUsage(
            elapsed_seconds=round(elapsed, 3),
            checkpoints=self.checkpoints,
            artifacts=len(self.artifacts),
            artifact_bytes=self.artifact_bytes,
        )


JobFunction = Callable[[JobContext], None]


class JobRunner:
    """Runs jobs in the calling thread against one :class:`LocalRunStore`.

    Construction calls :meth:`LocalRunStore.recover`, so runs left RUNNING by
    a dead process become INTERRUPTED before any new work starts."""

    def __init__(
        self,
        store: LocalRunStore,
        *,
        recover_on_start: bool = True,
        clock: Callable[[], float] = time.monotonic,
        capture: Callable[..., RepositoryRevision] = capture_revision,
    ) -> None:
        self._store = store
        self._clock = clock
        self._capture = capture
        if recover_on_start:
            store.recover()

    def run(
        self, spec: JobSpec, job: JobFunction, *, token: CancellationToken | None = None
    ) -> JobResult:
        token = token or CancellationToken()
        started = self._clock()
        deadline = started + spec.budget.max_seconds
        revision = self._capture_revision(spec)
        key = derive_idempotency_key(
            kind=spec.kind,
            revision_id=revision.revision_id,
            config=spec.config,
            engine_version=spec.engine_version,
            ruleset_version=spec.ruleset_version,
        )
        wait = max(0.0, deadline - self._clock())
        with self._store.request_lock(key, timeout_seconds=wait) as held:
            if not held:
                raise JobError(
                    FailureClass.BUDGET_EXHAUSTED,
                    "an identical request is still running; max_seconds elapsed while waiting",
                    retryable=True,
                )
            earlier = self._store.find_succeeded(key)
            if earlier is not None:
                report = self._read_report(earlier)
                if report is not None:
                    return JobResult(record=earlier, revision=revision, report=report, reused=True)
            return self._execute(spec, job, token, revision, key, started, deadline)

    def _capture_revision(self, spec: JobSpec) -> RepositoryRevision:
        try:
            return self._capture(spec.root, spec.scope, exclude_paths=(self._store.root,))
        except RevisionBudgetExceeded as exc:
            raise JobError(FailureClass.BUDGET_EXHAUSTED, f"revision capture: {exc}") from exc
        except RevisionCaptureError as exc:
            raise JobError(FailureClass.INPUT_ERROR, f"revision capture: {exc}") from exc

    def _read_report(self, record: RunRecord) -> JobReport | None:
        ref = record.artifacts.get(JOB_REPORT_ARTIFACT)
        if ref is None:
            return None  # not written by a JobRunner: recompute rather than guess
        try:
            return JobReport.from_dict(
                json.loads(self._store.read_bytes(ref, max_bytes=1024 * 1024))
            )
        except (StoreError, ValueError):
            return None

    def _execute(
        self,
        spec: JobSpec,
        job: JobFunction,
        token: CancellationToken,
        revision: RepositoryRevision,
        key: str,
        started: float,
        deadline: float,
    ) -> JobResult:
        fingerprint = config_fingerprint(spec.config)
        handle = self._store.begin_run(
            idempotency_key=key,
            revision_id=revision.revision_id,
            kind=spec.kind,
            metadata={
                "job_protocol_version": str(JOB_PROTOCOL_VERSION),
                "config_fingerprint": fingerprint,
                "engine_version": spec.engine_version,
                "ruleset_version": spec.ruleset_version,
            },
        )
        ctx = JobContext(
            store=self._store,
            root=spec.root,
            revision=revision,
            config=spec.config,
            budget=spec.budget,
            token=token,
            deadline=deadline,
            clock=self._clock,
        )
        state: RunState
        failure: FailureClass | None = None
        reason: str | None = None
        keep_artifacts = True
        with handle:
            try:
                ctx.checkpoint()  # honour a cancellation or a spent budget before any work
                job(ctx)
                if token.cancelled:
                    raise _JobStop(RunState.CANCELLED, "cancelled")
                state = RunState.PARTIAL if ctx.partial_reasons else RunState.SUCCEEDED
            except _JobStop as stop:
                state, reason = stop.state, stop.reason
                if state is RunState.PARTIAL:
                    ctx.mark_partial(stop.reason)
                    failure = FailureClass.BUDGET_EXHAUSTED
                else:
                    keep_artifacts = False  # a cancelled run never publishes its results
            except JobError as exc:
                state, failure, reason = (
                    RunState.FAILED,
                    exc.failure_class,
                    _bounded(f"{exc.failure_class}: {exc}"),
                )
                keep_artifacts = False
            except Exception as exc:
                # Internal errors record only the type: messages can carry
                # source text or environment values.
                state, failure = RunState.FAILED, FailureClass.INTERNAL_ERROR
                reason = f"internal_error: {type(exc).__name__}"
                keep_artifacts = False
            except BaseException as exc:  # KeyboardInterrupt, SystemExit: record, then propagate
                self._finish(
                    handle,
                    ctx,
                    spec,
                    fingerprint,
                    started,
                    RunState.FAILED,
                    FailureClass.INTERRUPTED,
                    f"interrupted: {type(exc).__name__}",
                    False,
                )
                raise
            record, report = self._finish(
                handle, ctx, spec, fingerprint, started, state, failure, reason, keep_artifacts
            )
        return JobResult(record=record, revision=revision, report=report, reused=False)

    def _finish(
        self,
        handle: RunHandle,
        ctx: JobContext,
        spec: JobSpec,
        fingerprint: str,
        started: float,
        state: RunState,
        failure: FailureClass | None,
        reason: str | None,
        keep_artifacts: bool,
    ) -> tuple[RunRecord, JobReport]:
        report = JobReport(
            job_protocol_version=JOB_PROTOCOL_VERSION,
            kind=spec.kind,
            state=state,
            failure_class=failure,
            reason=reason,
            partial_reasons=tuple(ctx.partial_reasons),
            budget_usage=ctx.usage(self._clock() - started),
            config_fingerprint=fingerprint,
            engine_version=spec.engine_version,
            ruleset_version=spec.ruleset_version,
        )
        artifacts = dict(ctx.artifacts) if keep_artifacts else {}
        artifacts[JOB_REPORT_ARTIFACT] = self._store.put_bytes(_canonical(report.to_dict()))
        record = handle.finish(state, artifacts=artifacts, reason=reason)
        return record, report
