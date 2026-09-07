"""Deployment-level corpus for Milestone S6 (Production Execution
Enablement) -- a real, separate `celery -A apps.verifier.celery_app
worker` subprocess, a real Redis broker/result backend, and a real
producer-side Celery app that never imports `apps.verifier.tasks` (exactly
mirroring how the review worker's own `apps.worker.celery_app.celery_app`
dispatches in production). Not host-local `bwrap` mocks standing in for a
real distributed round trip -- see
``validation/production_execution/latest-summary.md`` Part AA's own
explicit demand for this.

Skipped entirely when either Redis or the sandbox capability (bwrap/
prlimit + a real functional probe -- see
:mod:`patchfrog.executable_verification.sandbox`) is unavailable on this
host, exactly mirroring
``tests/integration/test_executable_verification_corpus.py``'s own skip
discipline.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import redis
from celery import Celery

from patchfrog.executable_verification.dispatch import VerifierDispatcher
from patchfrog.executable_verification.domain import VerificationKind, VerificationOutcome
from patchfrog.executable_verification.protocol import (
    VERIFIER_PROTOCOL_VERSION,
    VerificationExecutionRequest,
    compute_request_id,
)
from patchfrog.executable_verification.sandbox import is_sandbox_available
from patchfrog.repository.git import run_git

_REDIS_URL = "redis://localhost:6379/0"
_WORKER_READY_TIMEOUT_SECONDS = 20.0


def _redis_available() -> bool:
    try:
        return bool(redis.Redis.from_url(_REDIS_URL, socket_connect_timeout=2).ping())
    except redis.RedisError:
        return False
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not (is_sandbox_available() and _redis_available()),
    reason="bwrap-based sandbox and/or a reachable Redis broker are not both available on this host",
)


@pytest.fixture(scope="module")
def verifier_worker() -> Iterator[None]:
    """A real, separate `celery -A apps.verifier.celery_app worker`
    subprocess, consuming the real verification queue against real
    Redis. Never the review worker's own celery_app -- this is the
    verifier's own, deliberately credential-minimal process, launched
    exactly as documented deployment would (`REDIS_URL` only)."""

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "celery", "-A", "apps.verifier.celery_app", "worker",
            "-Q", "patchfrog-verification", "--loglevel=INFO", "--concurrency=1",
        ],
        env={"REDIS_URL": _REDIS_URL, "PATH": __import__("os").environ.get("PATH", "")},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + _WORKER_READY_TIMEOUT_SECONDS
        ready = False
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            if "ready." in line:
                ready = True
                break
        if not ready:
            proc.terminate()
            raise RuntimeError("verifier worker subprocess did not become ready in time")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _producer_app() -> Celery:
    """A separate Celery app instance used purely as a task producer --
    never imports apps.verifier.tasks, exactly mirroring how the review
    worker's own apps.worker.celery_app dispatches in production."""

    return Celery("patchfrog-test-producer", broker=_REDIS_URL, backend=_REDIS_URL)


def _init_repo(tmp_path: Path, *, test_file_content: str) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(["-C", str(repo), "init", "--quiet"])
    run_git(["-C", str(repo), "config", "user.email", "test@example.com"])
    run_git(["-C", str(repo), "config", "user.name", "Test"])
    (repo / "tests").mkdir()
    (repo / "tests" / "test_case.py").write_text(test_file_content)
    run_git(["-C", str(repo), "add", "."])
    run_git(["-C", str(repo), "commit", "--quiet", "-m", "initial"])
    commit_sha = run_git(["-C", str(repo), "rev-parse", "HEAD"]).strip()
    return repo, commit_sha


def _request(*, commit_sha: str, snapshot_path: Path, request_id: str, timeout_seconds: float = 20.0) -> VerificationExecutionRequest:
    # A fresh uuid suffix on every call -- Celery's Redis result backend
    # caches by task_id (= request_id here) beyond a single test run, so a
    # fixed literal id would silently reuse a stale prior result across
    # repeated test invocations (a real, confirmed observation -- not
    # merely theoretical -- while writing this file). Production always
    # derives request_id from patchfrog.executable_verification.protocol.compute_request_id,
    # which is deterministic *per logical request* (repository/run/commit/
    # candidate/target) -- exactly the idempotency this uniqueness
    # requirement doesn't undermine: a genuinely new review run always
    # gets a genuinely new id there too.
    unique_request_id = f"{request_id}-{uuid.uuid4()}"
    return VerificationExecutionRequest(
        request_id=unique_request_id,
        protocol_version=VERIFIER_PROTOCOL_VERSION,
        repository_id="repo-1",
        review_run_id="run-1",
        commit_sha=commit_sha,
        verification_kind=VerificationKind.EXISTING_TARGETED_TEST,
        test_target_path="tests/test_case.py",
        snapshot_path=str(snapshot_path),
        timeout_seconds=timeout_seconds,
    )


# ---- 1. Real end-to-end PASSED through the actual distributed pipeline ----


async def test_real_distributed_round_trip_passed(verifier_worker: None, tmp_path: Path) -> None:
    repo, sha = _init_repo(tmp_path, test_file_content="def test_pass():\n    assert 1 == 1\n")
    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(_request(commit_sha=sha, snapshot_path=repo, request_id="e2e-passed"))
    assert result is not None
    assert result.outcome is VerificationOutcome.PASSED
    assert result.exit_code == 0


# ---- 2. Real end-to-end CONFIRMED_FAILURE ----


async def test_real_distributed_round_trip_confirmed_failure(verifier_worker: None, tmp_path: Path) -> None:
    repo, sha = _init_repo(tmp_path, test_file_content="def test_fails():\n    assert 1 == 2\n")
    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(_request(commit_sha=sha, snapshot_path=repo, request_id="e2e-failed"))
    assert result is not None
    assert result.outcome is VerificationOutcome.CONFIRMED_FAILURE
    assert result.exit_code == 1


# ---- 3. Tampered snapshot rejected through the real pipeline, test body never runs ----


async def test_real_distributed_round_trip_rejects_tampered_snapshot(verifier_worker: None, tmp_path: Path) -> None:
    marker = tmp_path / "PWNED"
    repo, sha = _init_repo(
        tmp_path,
        test_file_content=(
            "import subprocess\n\n"
            "def test_pass():\n"
            f"    subprocess.run(['touch', {str(marker)!r}])\n"
            "    assert 1 == 1\n"
        ),
    )
    # Tamper the staged snapshot after commit (simulating corruption or a
    # race between staging and verification) -- the tampered version
    # would otherwise "pass" too, so a naive check couldn't distinguish
    # "ran and passed" from "correctly rejected."
    (repo / "tests" / "test_case.py").write_text("def test_pass():\n    assert 1 == 1\n    # tampered\n")

    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(_request(commit_sha=sha, snapshot_path=repo, request_id="e2e-tampered"))
    assert result is not None
    assert result.outcome is VerificationOutcome.SANDBOX_ERROR
    assert not marker.exists(), "tampered test body was executed -- integrity check did not fail closed"


# ---- 4. Stale/duplicate request handling -- deterministic request identity ----


def test_request_id_deterministic_for_retry_of_same_logical_request() -> None:
    kwargs = {
        "repository_id": "repo-1", "review_run_id": "run-1", "commit_sha": "a" * 40,
        "file_path": "src/x.py", "qualified_name": "foo",
        "verification_kind": VerificationKind.EXISTING_TARGETED_TEST, "test_target_path": "tests/test_x.py",
    }
    first = compute_request_id(**kwargs)  # type: ignore[arg-type]
    retry = compute_request_id(**kwargs)  # type: ignore[arg-type]
    assert first == retry


def test_request_id_differs_for_a_stale_superseded_head() -> None:
    """A new PR head must never reuse an older head's request identity --
    stale verification evidence must never be mistaken for the current
    one."""

    base = {
        "repository_id": "repo-1", "review_run_id": "run-1",
        "file_path": "src/x.py", "qualified_name": "foo",
        "verification_kind": VerificationKind.EXISTING_TARGETED_TEST, "test_target_path": "tests/test_x.py",
    }
    old_head = compute_request_id(commit_sha="a" * 40, **base)  # type: ignore[arg-type]
    new_head = compute_request_id(commit_sha="b" * 40, **base)  # type: ignore[arg-type]
    assert old_head != new_head


# ---- 5. Verifier crash / malformed reply -> fail closed, never a hang or an exception surfacing ----


async def test_dispatch_returns_none_on_unreachable_verifier() -> None:
    """The broker itself is unreachable (not merely "no worker consuming
    the queue" -- this test file's own `verifier_worker` fixture is
    module-scoped and stays alive across other tests in this module, so
    "no consumer" cannot be reliably arranged here; an unreachable broker
    is the more representative real-world "verifier deployment is down"
    scenario anyway). The review worker must never hang indefinitely or
    raise; it must get None back promptly and treat it as SANDBOX_ERROR."""

    unreachable_app = Celery(
        "patchfrog-test-producer-unreachable",
        broker="redis://localhost:1/0", backend="redis://localhost:1/0",
    )
    dispatcher = VerifierDispatcher(celery_app=unreachable_app, wait_timeout_seconds=3.0)
    repo = Path(tempfile.mkdtemp())
    try:
        result = await dispatcher.dispatch(
            _request(commit_sha="a" * 40, snapshot_path=repo, request_id="unreachable-broker", timeout_seconds=3.0)
        )
        assert result is None
    finally:
        shutil.rmtree(repo, ignore_errors=True)


# ---- 6. Credential-import boundary -- structural, no container needed ----


def test_verifier_process_never_imports_the_credential_settings_class() -> None:
    """patchfrog.config.settings.Settings requires DATABASE_URL,
    GITHUB_APP_ID, GITHUB_PRIVATE_KEY(_PATH), and GITHUB_WEBHOOK_SECRET at
    construction time -- the verifier must never import it, structurally,
    not just "happen not to call get_settings()". Mirrors
    test_executable_verification_corpus.py's own
    test_case_never_imports_a_provider structural-AST pattern."""

    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "apps" / "verifier"
    forbidden_modules = {"patchfrog.config.settings"}
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                raise AssertionError(f"{path} imports {node.module} -- the verifier must never hold these credentials")


def test_verifier_process_never_imports_github_or_provider_modules() -> None:
    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "apps" / "verifier"
    forbidden_prefixes = ("patchfrog.github", "patchfrog.review.providers")
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and any(node.module == p or node.module.startswith(p + ".") for p in forbidden_prefixes)
            ):
                raise AssertionError(f"{path} imports {node.module} -- the verifier must never hold these credentials")
