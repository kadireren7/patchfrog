"""JobRunner (Milestone 2.2): idempotency, lifecycle, cancellation, budgets
and failure classes over RepositoryRevision + LocalRunStore.

Time is a fake monotonic clock everywhere a budget matters: no test asserts
on real durations (TEST_STRATEGY.md)."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from patchfrog.app.jobs import (
    JOB_REPORT_ARTIFACT,
    CancellationToken,
    FailureClass,
    JobBudget,
    JobContext,
    JobError,
    JobReport,
    JobResult,
    JobRunner,
    JobSpec,
    config_fingerprint,
    derive_idempotency_key,
)
from patchfrog.app.revision import RevisionScope
from patchfrog.app.store import LocalRunStore, RunState

REV = "a" * 64


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("print('hello')\n")
    (root / "lib.py").write_text("X = 1\n")
    return root


@pytest.fixture
def store(tmp_path: Path) -> LocalRunStore:
    return LocalRunStore(tmp_path / "store")


def _spec(repo: Path, **overrides: object) -> JobSpec:
    values: dict[str, object] = {"kind": "analysis", "root": repo, "engine_version": "0.1.0-beta"}
    values.update(overrides)
    return JobSpec(**values)  # type: ignore[arg-type]


class Recorder:
    """A job that writes one artifact and counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, ctx: JobContext) -> None:
        self.calls += 1
        ctx.checkpoint()
        ctx.put_bytes("summary.json", json.dumps({"files": ctx.revision.file_count}).encode())


# --- idempotency key ---------------------------------------------------------------


def _key(**overrides: object) -> str:
    values: dict[str, object] = {
        "kind": "analysis",
        "revision_id": REV,
        "config": {"rules": ["PF-SCALE-001"], "depth": 4},
        "engine_version": "0.1.0",
        "ruleset_version": "1",
    }
    values.update(overrides)
    return derive_idempotency_key(**values)  # type: ignore[arg-type]


def test_idempotency_key_is_deterministic_and_order_independent() -> None:
    assert _key() == _key(config={"depth": 4, "rules": ["PF-SCALE-001"]})
    assert len(_key()) == 64 and int(_key(), 16) >= 0


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "verification"},
        {"revision_id": "b" * 64},
        {"config": {"rules": ["PF-SCALE-001"], "depth": 5}},
        {"config": {"rules": ["PF-SCALE-001"], "depth": 4, "extra": None}},
        {"engine_version": "0.1.1"},
        {"ruleset_version": "2"},
    ],
)
def test_every_key_input_changes_the_key(change: dict[str, object]) -> None:
    assert _key(**change) != _key()


@pytest.mark.parametrize(
    "config",
    [
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": {1, 2}},
        {"x": b"bytes"},
        {"x": {1: "int key"}},
        {"x": "y" * (70 * 1024)},
    ],
)
def test_non_json_or_oversized_config_is_rejected(config: dict[str, object], repo: Path) -> None:
    with pytest.raises(ValueError):
        config_fingerprint(config)
    with pytest.raises(ValueError):
        _spec(repo, config=config)


def test_spec_validates_kind_and_versions(repo: Path) -> None:
    for bad in ({"kind": "Bad Kind"}, {"engine_version": ""}, {"ruleset_version": "../x"}):
        with pytest.raises(ValueError):
            _spec(repo, **bad)
    with pytest.raises(ValueError):
        JobBudget(max_seconds=0)
    with pytest.raises(ValueError):
        JobBudget(max_seconds=float("inf"))
    with pytest.raises(ValueError):
        JobBudget(max_artifacts=0)


# --- lifecycle and reuse ---------------------------------------------------------------


def test_success_records_artifacts_report_and_metadata(repo: Path, store: LocalRunStore) -> None:
    job = Recorder()
    result = JobRunner(store).run(_spec(repo, config={"a": 1}), job)

    assert job.calls == 1 and not result.reused
    assert result.state is RunState.SUCCEEDED
    assert set(result.artifacts) == {"summary.json"}
    assert JOB_REPORT_ARTIFACT in result.record.artifacts
    assert json.loads(store.read_bytes(result.artifacts["summary.json"])) == {"files": 2}
    assert result.report.failure_class is None and result.report.partial_reasons == ()
    assert result.report.budget_usage.artifacts == 1
    assert result.record.revision_id == result.revision.revision_id
    assert result.record.metadata["config_fingerprint"] == config_fingerprint({"a": 1})
    assert result.record.metadata["engine_version"] == "0.1.0-beta"
    stored = JobReport.from_dict(json.loads(store.read_bytes(result.record.artifacts[JOB_REPORT_ARTIFACT])))
    assert stored == result.report


def test_repeat_request_reuses_the_succeeded_run_without_calling_the_job(repo: Path, store: LocalRunStore) -> None:
    runner = JobRunner(store)
    first_job, second_job = Recorder(), Recorder()
    first = runner.run(_spec(repo), first_job)
    second = runner.run(_spec(repo), second_job)

    assert second.reused and second_job.calls == 0
    assert second.record.run_id == first.record.run_id
    assert second.report == first.report
    assert len(store.list_runs()) == 1


def test_budget_change_reuses_but_content_config_or_version_change_recomputes(
    repo: Path, store: LocalRunStore
) -> None:
    runner = JobRunner(store)
    base = runner.run(_spec(repo), Recorder())
    assert runner.run(_spec(repo, budget=JobBudget(max_seconds=5)), Recorder()).reused

    for spec in (_spec(repo, config={"x": 1}), _spec(repo, engine_version="0.2.0"), _spec(repo, ruleset_version="2")):
        job = Recorder()
        result = runner.run(spec, job)
        assert not result.reused and job.calls == 1
        assert result.record.idempotency_key != base.record.idempotency_key

    (repo / "lib.py").write_text("X = 2\n")
    job = Recorder()
    changed = runner.run(_spec(repo), job)
    assert not changed.reused and job.calls == 1
    assert changed.revision.revision_id != base.revision.revision_id


def test_scope_change_changes_the_request(repo: Path, store: LocalRunStore) -> None:
    runner = JobRunner(store)
    runner.run(_spec(repo), Recorder())
    job = Recorder()
    narrowed = runner.run(_spec(repo, scope=RevisionScope(exclude_prefixes=("lib.py",))), job)
    assert not narrowed.reused and job.calls == 1 and narrowed.revision.file_count == 1


@pytest.mark.parametrize("outcome", ["partial", "failed", "cancelled"])
def test_non_succeeded_runs_are_never_reused(repo: Path, store: LocalRunStore, outcome: str) -> None:
    runner = JobRunner(store)

    def first(ctx: JobContext) -> None:
        if outcome == "partial":
            ctx.mark_partial("skipped broken.py: syntax error")
        elif outcome == "failed":
            raise JobError(FailureClass.UNSUPPORTED_PROJECT, "no FastAPI app found")
        else:
            token.cancel()
            ctx.checkpoint()

    token = CancellationToken()
    runner.run(_spec(repo), first, token=token)
    job = Recorder()
    again = runner.run(_spec(repo), job)
    assert not again.reused and job.calls == 1 and again.state is RunState.SUCCEEDED


def test_store_inside_the_analysed_root_does_not_change_identity(repo: Path) -> None:
    store = LocalRunStore(repo / ".patchfrog-cache")
    runner = JobRunner(store)
    first = runner.run(_spec(repo), Recorder())
    second = runner.run(_spec(repo), Recorder())  # the store now holds files under the root
    assert second.reused and second.revision.revision_id == first.revision.revision_id


def test_reused_record_without_job_report_is_recomputed(repo: Path, store: LocalRunStore) -> None:
    runner = JobRunner(store)
    first = runner.run(_spec(repo), Recorder())
    # A SUCCEEDED run for the same key written by something other than a JobRunner.
    store_only = LocalRunStore(store.root)
    (store.root / "index" / first.record.idempotency_key).unlink()
    handle = store_only.begin_run(
        idempotency_key=first.record.idempotency_key, revision_id=first.revision.revision_id, kind="analysis"
    )
    handle.finish(RunState.SUCCEEDED)
    job = Recorder()
    assert not runner.run(_spec(repo), job).reused and job.calls == 1


# --- partial, failure classes -----------------------------------------------------------


def test_mark_partial_finishes_partial_with_bounded_reasons(repo: Path, store: LocalRunStore) -> None:
    def job(ctx: JobContext) -> None:
        ctx.put_bytes("a.json", b"{}")
        for i in range(150):
            ctx.mark_partial(f"skipped file_{i}.py " + "x" * 500)

    result = JobRunner(store).run(_spec(repo), job)
    assert result.state is RunState.PARTIAL and result.report.failure_class is None
    assert len(result.report.partial_reasons) == 101
    assert result.report.partial_reasons[-1] == "further partial reasons omitted"
    assert all(len(r) <= 200 for r in result.report.partial_reasons)
    assert set(result.artifacts) == {"a.json"}  # partial results are kept and labelled partial


def test_typed_job_error_fails_with_its_class_and_no_artifacts(repo: Path, store: LocalRunStore) -> None:
    def job(ctx: JobContext) -> None:
        ctx.put_bytes("a.json", b"{}")
        raise JobError(FailureClass.TOOL_UNAVAILABLE, "pytest is not installed")

    result = JobRunner(store).run(_spec(repo), job)
    assert result.state is RunState.FAILED
    assert result.report.failure_class is FailureClass.TOOL_UNAVAILABLE
    assert result.record.reason == "tool_unavailable: pytest is not installed"
    assert result.artifacts == {}


def test_internal_error_records_only_the_exception_type(repo: Path, store: LocalRunStore) -> None:
    def job(ctx: JobContext) -> None:
        raise RuntimeError("secret=hunter2 in /home/user/.env")

    result = JobRunner(store).run(_spec(repo), job)
    assert result.state is RunState.FAILED
    assert result.report.failure_class is FailureClass.INTERNAL_ERROR
    assert result.record.reason == "internal_error: RuntimeError"
    raw = store.read_bytes(result.record.artifacts[JOB_REPORT_ARTIFACT]) + (
        store.root / "runs" / result.record.run_id / "run.json"
    ).read_bytes()
    assert b"hunter2" not in raw


def test_swallowing_exceptions_cannot_hide_a_stop(repo: Path, store: LocalRunStore) -> None:
    token = CancellationToken()
    token.cancel()

    def job(ctx: JobContext) -> None:
        try:
            ctx.checkpoint()
        except Exception:  # a job's broad handler must not swallow cancellation
            ctx.put_bytes("leak.json", b"{}")

    result = JobRunner(store).run(_spec(repo), job, token=token)
    assert result.state is RunState.CANCELLED and result.artifacts == {}


def test_swallowing_exceptions_cannot_hide_a_budget_stop(repo: Path, store: LocalRunStore) -> None:
    clock = FakeClock()

    def job(ctx: JobContext) -> None:
        clock.now += 10.0
        # If the stop were an Exception, this would swallow it and the job would "succeed".
        with contextlib.suppress(Exception):
            ctx.checkpoint()

    result = JobRunner(store, clock=clock).run(_spec(repo, budget=JobBudget(max_seconds=5.0)), job)
    assert result.state is RunState.PARTIAL
    assert result.report.failure_class is FailureClass.BUDGET_EXHAUSTED


def test_keyboard_interrupt_is_recorded_then_propagated(repo: Path, store: LocalRunStore) -> None:
    def job(ctx: JobContext) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        JobRunner(store).run(_spec(repo), job)
    (record,) = store.list_runs()
    assert record.state is RunState.FAILED and record.reason == "interrupted: KeyboardInterrupt"
    report = json.loads(store.read_bytes(record.artifacts[JOB_REPORT_ARTIFACT]))
    assert report["failure_class"] == "interrupted"


def test_reserved_duplicate_and_invalid_artifact_names_are_rejected(repo: Path, store: LocalRunStore) -> None:
    seen: list[str] = []

    def job(ctx: JobContext) -> None:
        for name in (JOB_REPORT_ARTIFACT, "../escape", "Upper"):
            try:
                ctx.put_bytes(name, b"x")
            except ValueError:
                seen.append(name)
        ctx.put_bytes("ok.json", b"x")
        try:
            ctx.put_bytes("ok.json", b"y")
        except ValueError:
            seen.append("duplicate")

    result = JobRunner(store).run(_spec(repo), job)
    assert seen == [JOB_REPORT_ARTIFACT, "../escape", "Upper", "duplicate"]
    assert result.state is RunState.SUCCEEDED and set(result.artifacts) == {"ok.json"}


def test_config_is_a_read_only_copy(repo: Path, store: LocalRunStore) -> None:
    config: dict[str, object] = {"depth": 4}

    def job(ctx: JobContext) -> None:
        assert ctx.config == {"depth": 4}
        with pytest.raises(TypeError):
            cast("dict[str, object]", ctx.config)["depth"] = 5

    assert JobRunner(store).run(_spec(repo, config=config), job).state is RunState.SUCCEEDED


# --- cancellation -------------------------------------------------------------------------


def test_cancel_before_start_never_calls_the_job(repo: Path, store: LocalRunStore) -> None:
    token = CancellationToken()
    token.cancel()
    job = Recorder()
    result = JobRunner(store).run(_spec(repo), job, token=token)
    assert job.calls == 0 and result.state is RunState.CANCELLED
    assert result.record.reason == "cancelled" and result.artifacts == {}


def test_cancel_after_the_last_checkpoint_is_still_cancelled(repo: Path, store: LocalRunStore) -> None:
    token = CancellationToken()

    def job(ctx: JobContext) -> None:
        ctx.put_bytes("result.json", b"{}")
        token.cancel()  # arrives after the job's own last checkpoint

    result = JobRunner(store).run(_spec(repo), job, token=token)
    assert result.state is RunState.CANCELLED and result.artifacts == {}
    assert store.find_succeeded(result.record.idempotency_key) is None


def test_cancel_from_another_thread_stops_at_the_next_checkpoint(repo: Path, store: LocalRunStore) -> None:
    token = CancellationToken()
    started = threading.Event()
    steps: list[int] = []

    def job(ctx: JobContext) -> None:
        started.set()
        for i in range(10_000):
            ctx.checkpoint()
            steps.append(i)
            if i == 0:
                assert worker_cancelled.wait(timeout=30)

    worker_cancelled = threading.Event()

    def canceller() -> None:
        assert started.wait(timeout=30)
        token.cancel()
        worker_cancelled.set()

    thread = threading.Thread(target=canceller)
    thread.start()
    result = JobRunner(store).run(_spec(repo), job, token=token)
    thread.join(timeout=30)
    assert result.state is RunState.CANCELLED
    assert steps == [0]  # exactly one unit of work after which the cancel was honoured


# --- budgets --------------------------------------------------------------------------------


def test_wall_time_budget_stops_cooperatively_as_partial(repo: Path, store: LocalRunStore) -> None:
    clock = FakeClock()
    done: list[int] = []

    def job(ctx: JobContext) -> None:
        for i in range(100):
            ctx.checkpoint()
            ctx.put_bytes(f"part-{i}.json", str(i).encode())
            done.append(i)
            clock.now += 1.0

    runner = JobRunner(store, clock=clock)
    result = runner.run(_spec(repo, budget=JobBudget(max_seconds=3.0)), job)
    assert done == [0, 1, 2]
    assert result.state is RunState.PARTIAL
    assert result.report.failure_class is FailureClass.BUDGET_EXHAUSTED
    assert result.record.reason == "budget_exhausted: max_seconds"
    assert "budget_exhausted: max_seconds" in result.report.partial_reasons
    assert set(result.artifacts) == {"part-0.json", "part-1.json", "part-2.json"}
    assert result.report.budget_usage.elapsed_seconds == 3.0
    assert result.report.budget_usage.checkpoints == 5  # start + 4 in the loop


def test_budget_spent_during_revision_capture_never_starts_the_job(repo: Path, store: LocalRunStore) -> None:
    clock = FakeClock()

    def slow_capture(*args: object, **kwargs: object) -> object:
        from patchfrog.app.revision import capture_revision

        revision = capture_revision(*args, **kwargs)  # type: ignore[arg-type]
        clock.now += 10.0
        return revision

    job = Recorder()
    runner = JobRunner(store, clock=clock, capture=slow_capture)  # type: ignore[arg-type]
    result = runner.run(_spec(repo, budget=JobBudget(max_seconds=5.0)), job)
    assert job.calls == 0 and result.state is RunState.PARTIAL
    assert result.report.failure_class is FailureClass.BUDGET_EXHAUSTED


def test_artifact_count_budget(repo: Path, store: LocalRunStore) -> None:
    def job(ctx: JobContext) -> None:
        for i in range(10):
            ctx.put_bytes(f"a{i}", b"x")

    result = JobRunner(store).run(_spec(repo, budget=JobBudget(max_artifacts=3)), job)
    assert result.state is RunState.PARTIAL and result.record.reason == "budget_exhausted: max_artifacts"
    assert set(result.artifacts) == {"a0", "a1", "a2"}


def test_artifact_bytes_budget_for_bytes_and_streams(repo: Path, store: LocalRunStore) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"y" * 60
        yield b"y" * 60

    def job(ctx: JobContext) -> None:
        ctx.put_bytes("small", b"x" * 50)
        ctx.put_stream("streamed", chunks())

    result = JobRunner(store).run(_spec(repo, budget=JobBudget(max_artifact_bytes=100)), job)
    assert result.state is RunState.PARTIAL and result.record.reason == "budget_exhausted: max_artifact_bytes"
    assert set(result.artifacts) == {"small"} and result.report.budget_usage.artifact_bytes == 50

    def big(ctx: JobContext) -> None:
        ctx.put_bytes("too-big", b"z" * 101)

    result = JobRunner(store).run(_spec(repo, config={"v": 2}, budget=JobBudget(max_artifact_bytes=100)), big)
    assert result.state is RunState.PARTIAL and result.artifacts == {}


def test_revision_budget_and_bad_root_raise_typed_errors_without_a_run(
    repo: Path, store: LocalRunStore, tmp_path: Path
) -> None:
    runner = JobRunner(store)
    with pytest.raises(JobError) as budget:
        runner.run(_spec(repo, scope=RevisionScope(max_files=1)), Recorder())
    assert budget.value.failure_class is FailureClass.BUDGET_EXHAUSTED
    with pytest.raises(JobError) as missing:
        runner.run(_spec(tmp_path / "does-not-exist"), Recorder())
    assert missing.value.failure_class is FailureClass.INPUT_ERROR
    assert store.list_runs() == []


# --- concurrency (threads; processes are in tests/integration/test_app_jobs_processes.py) ---


def test_concurrent_identical_requests_execute_once(repo: Path, store: LocalRunStore) -> None:
    calls: list[int] = []
    gate = threading.Event()
    lock = threading.Lock()

    def job(ctx: JobContext) -> None:
        with lock:
            calls.append(1)
        gate.wait(timeout=0.5)  # widen the race window; never a timing assertion
        ctx.put_bytes("result.json", b"{}")

    runner = JobRunner(store)
    results: list[JobResult] = []

    def worker() -> None:
        results.append(runner.run(_spec(repo), job))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(timeout=60)
    assert len(calls) == 1 and len(results) == 4
    run_ids = {r.record.run_id for r in results}
    assert len(run_ids) == 1
    assert sum(1 for r in results if not r.reused) == 1


def test_waiting_for_a_held_request_lock_is_bounded(repo: Path, store: LocalRunStore) -> None:
    runner = JobRunner(store)
    probe = runner.run(_spec(repo, config={"probe": True}), Recorder())  # learn the key for this content
    key = derive_idempotency_key(
        kind="analysis",
        revision_id=probe.revision.revision_id,
        config={},
        engine_version="0.1.0-beta",
        ruleset_version="none",
    )
    with store.request_lock(key, timeout_seconds=0) as held:
        assert held
        with pytest.raises(JobError) as busy:
            runner.run(_spec(repo, budget=JobBudget(max_seconds=0.2)), Recorder())
    assert busy.value.failure_class is FailureClass.BUDGET_EXHAUSTED and busy.value.retryable


def test_request_lock_rejects_bad_keys_and_uses_bounded_stripes(store: LocalRunStore) -> None:
    with pytest.raises(ValueError), store.request_lock("../x", timeout_seconds=0):
        pass
    for prefix in ("00", "ff", "00"):
        with store.request_lock(prefix + "0" * 62, timeout_seconds=0) as held:
            assert held
    assert sorted(os.listdir(store.root / "locks")) == ["00", "ff"]


# --- local-first --------------------------------------------------------------------------


def test_job_runner_works_in_an_empty_environment_without_server_modules(tmp_path: Path, repo: Path) -> None:
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from patchfrog.app.jobs import JobRunner, JobSpec\n"
        "from patchfrog.app.store import LocalRunStore\n"
        f"store = LocalRunStore(Path({str(tmp_path / 'store')!r}))\n"
        "def job(ctx):\n"
        "    ctx.checkpoint(); ctx.put_bytes('r.json', b'{}')\n"
        f"spec = JobSpec(kind='analysis', root=Path({str(repo)!r}), engine_version='0.1.0-beta')\n"
        "first = JobRunner(store).run(spec, job)\n"
        "second = JobRunner(store).run(spec, job)\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'sqlalchemy', 'celery', 'redis', 'anthropic', 'openai', 'fastapi', 'asyncpg', 'apps', 'pydantic'} "
        "or m.startswith(('google.genai', 'patchfrog.persistence', 'patchfrog.review', "
        "'patchfrog.config.settings', 'patchfrog.github')))\n"
        "print(json.dumps({'state': first.state.value, 'reused': second.reused, 'heavy': heavy}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"state": "succeeded", "reused": True, "heavy": []}
