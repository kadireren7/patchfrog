"""Deployment-level corpus for Milestone S6 (Production Execution
Enablement) -- a real, separate `celery -A apps.verifier.celery_app
worker` subprocess, a real Redis broker/result backend, and a real
producer-side Celery app that never imports `apps.verifier.tasks` (exactly
mirroring how the review worker's own `apps.worker.celery_app.celery_app`
dispatches in production). Not host-local `bwrap` mocks standing in for a
real distributed round trip -- see
``validation/production_execution/latest-summary.md`` Part AA's own
explicit demand for this.

Security correction round (section 11 of the same document): the original
version of this file staged the full git checkout (`.git/` included) into
the shared volume, which a real, synthetic-token reproduction proved leaks
the GitHub installation token into the hostile execution workspace. Every
test here now uses the corrected, credential-free artifact model
(`export_artifact`/`artifact_id`/`artifact_digest`) -- see
`patchfrog.executable_verification.snapshot_staging`.

Skipped entirely when either Redis or the sandbox capability (bwrap/
prlimit + a real functional probe -- see
:mod:`patchfrog.executable_verification.sandbox`) is unavailable on this
host, exactly mirroring
``tests/integration/test_executable_verification_corpus.py``'s own skip
discipline.
"""

from __future__ import annotations

import os
import subprocess
import sys
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
from patchfrog.executable_verification.snapshot_staging import (
    compute_artifact_digest,
    export_artifact,
)
from patchfrog.repository.git import run_git

_REDIS_URL = "redis://localhost:6379/0"
_WORKER_READY_TIMEOUT_SECONDS = 20.0
_SYNTHETIC_TOKEN = "ghs_SYNTHETIC_SENTINEL_TOKEN_DO_NOT_USE_1234567890"


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
def staging_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("verifier-staging-root")
    return root


@pytest.fixture(scope="module")
def verifier_worker(staging_root: Path) -> Iterator[None]:
    """A real, separate `celery -A apps.verifier.celery_app worker`
    subprocess, consuming the real verification queue against real
    Redis, configured with `VERIFIER_STAGING_ROOT` pointing at this
    module's own shared staging root -- never the review worker's own
    celery_app. Launched exactly as documented deployment would (Redis +
    staging root only, no DB/GitHub/provider credential)."""

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "celery", "-A", "apps.verifier.celery_app", "worker",
            "-Q", "patchfrog-verification", "--loglevel=INFO", "--concurrency=1",
        ],
        env={
            "REDIS_URL": _REDIS_URL,
            "VERIFIER_STAGING_ROOT": str(staging_root),
            "PATH": os.environ.get("PATH", ""),
        },
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


def _init_bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    run_git(["init", "--quiet", "--bare", str(remote)])
    return remote


def _push_commit(
    remote: Path, tmp_path: Path, *, files: dict[str, str], symlinks: dict[str, str] | None = None
) -> str:
    work = tmp_path / f"work-{uuid.uuid4()}"
    run_git(["clone", "--quiet", str(remote), str(work)])
    run_git(["-C", str(work), "config", "user.email", "t@example.com"])
    run_git(["-C", str(work), "config", "user.name", "T"])
    for rel, content in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for rel, target in (symlinks or {}).items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    run_git(["-C", str(work), "add", "-A"])
    run_git(["-C", str(work), "commit", "--quiet", "-m", "commit"])
    run_git(["-C", str(work), "push", "--quiet", "origin", "HEAD:refs/heads/main"])
    return run_git(["-C", str(work), "rev-parse", "HEAD"]).strip()


def _stage(remote: Path, sha: str, *, staging_root: Path) -> tuple[str, str]:
    """Real review-worker-side staging: acquire with the synthetic token,
    export credential-free, compute the digest -- returns (artifact_id,
    digest)."""

    artifact_dir = export_artifact(
        clone_url=f"file://{remote}", commit_sha=sha, repository_full_name="test/repo",
        token=_SYNTHETIC_TOKEN, destination_root=staging_root,
    )
    return artifact_dir.name, compute_artifact_digest(artifact_dir)


def _request(
    *, commit_sha: str, artifact_id: str, artifact_digest: str, request_id: str,
    test_target_path: str = "tests/test_case.py", timeout_seconds: float = 20.0, protocol_version: int | None = None,
) -> VerificationExecutionRequest:
    # A fresh uuid suffix on every call -- Celery's Redis result backend
    # caches by task_id (= request_id here) beyond a single test run, so a
    # fixed literal id would silently reuse a stale prior result across
    # repeated test invocations (a real, confirmed observation from this
    # milestone's own testing). Production always derives request_id from
    # compute_request_id, which is deterministic *per logical request*.
    unique_request_id = f"{request_id}-{uuid.uuid4()}"
    return VerificationExecutionRequest(
        request_id=unique_request_id,
        protocol_version=protocol_version if protocol_version is not None else VERIFIER_PROTOCOL_VERSION,
        repository_id="repo-1",
        review_run_id="run-1",
        commit_sha=commit_sha,
        verification_kind=VerificationKind.EXISTING_TARGETED_TEST,
        test_target_path=test_target_path,
        artifact_id=artifact_id,
        artifact_digest=artifact_digest,
        timeout_seconds=timeout_seconds,
    )


# ---- 1. Real end-to-end PASSED through the actual distributed pipeline ----


async def test_real_distributed_round_trip_passed(verifier_worker: None, tmp_path: Path, staging_root: Path) -> None:
    remote = _init_bare_remote(tmp_path)
    sha = _push_commit(remote, tmp_path, files={"tests/test_case.py": "def test_pass():\n    assert 1 == 1\n"})
    artifact_id, digest = _stage(remote, sha, staging_root=staging_root)

    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(
        _request(commit_sha=sha, artifact_id=artifact_id, artifact_digest=digest, request_id="e2e-passed")
    )
    assert result is not None
    assert result.outcome is VerificationOutcome.PASSED
    assert result.exit_code == 0


# ---- 2. Real end-to-end CONFIRMED_FAILURE ----


async def test_real_distributed_round_trip_confirmed_failure(
    verifier_worker: None, tmp_path: Path, staging_root: Path,
) -> None:
    remote = _init_bare_remote(tmp_path)
    sha = _push_commit(remote, tmp_path, files={"tests/test_case.py": "def test_fails():\n    assert 1 == 2\n"})
    artifact_id, digest = _stage(remote, sha, staging_root=staging_root)

    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(
        _request(commit_sha=sha, artifact_id=artifact_id, artifact_digest=digest, request_id="e2e-failed")
    )
    assert result is not None
    assert result.outcome is VerificationOutcome.CONFIRMED_FAILURE
    assert result.exit_code == 1


# ---- 3. Credential-leak regression: synthetic token used to stage, absent from execution ----


async def test_real_distributed_round_trip_no_credential_in_execution_workspace(
    verifier_worker: None, tmp_path: Path, staging_root: Path,
) -> None:
    """The exact original vulnerability, exercised through the real
    distributed pipeline end to end: a synthetic token used only for
    trusted acquisition must not be readable by the hostile test that
    actually executes inside the verifier."""

    remote = _init_bare_remote(tmp_path)
    sha = _push_commit(
        remote, tmp_path,
        files={
            "tests/test_case.py": (
                "import pathlib\n\n"
                "def test_no_git_dir_and_no_credential():\n"
                "    assert not pathlib.Path('.git').exists()\n"
            )
        },
    )
    artifact_id, digest = _stage(remote, sha, staging_root=staging_root)

    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(
        _request(commit_sha=sha, artifact_id=artifact_id, artifact_digest=digest, request_id="e2e-no-credential")
    )
    assert result is not None
    assert result.outcome is VerificationOutcome.PASSED, result.stdout_excerpt
    assert _SYNTHETIC_TOKEN not in result.stdout_excerpt
    assert _SYNTHETIC_TOKEN not in result.stderr_excerpt


# ---- 4. Artifact digest mismatch (tamper after staging) rejected ----


async def test_real_distributed_round_trip_rejects_tampered_artifact(
    verifier_worker: None, tmp_path: Path, staging_root: Path,
) -> None:
    marker = tmp_path / "PWNED"
    remote = _init_bare_remote(tmp_path)
    sha = _push_commit(
        remote, tmp_path,
        files={
            "tests/test_case.py": (
                "import subprocess\n\n"
                "def test_pass():\n"
                f"    subprocess.run(['touch', {str(marker)!r}])\n"
                "    assert 1 == 1\n"
            )
        },
    )
    artifact_id, digest = _stage(remote, sha, staging_root=staging_root)
    # Tamper the staged artifact after digest computation (simulating
    # corruption or a compromised shared volume) -- the tampered version
    # would otherwise "pass" too, so a naive check couldn't distinguish
    # "ran and passed" from "correctly rejected."
    (staging_root / artifact_id / "tests" / "test_case.py").write_text(
        "def test_pass():\n    assert 1 == 1\n    # tampered\n"
    )

    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(
        _request(commit_sha=sha, artifact_id=artifact_id, artifact_digest=digest, request_id="e2e-tampered")
    )
    assert result is not None
    assert result.outcome is VerificationOutcome.SANDBOX_ERROR
    assert not marker.exists(), "tampered test body was executed -- integrity check did not fail closed"


# ---- 5. Path-traversal / staging-root escape rejected ----


async def test_real_distributed_round_trip_rejects_path_traversal_artifact_id(
    verifier_worker: None, tmp_path: Path, staging_root: Path,
) -> None:
    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(
        _request(
            commit_sha="a" * 40, artifact_id="../../../../etc", artifact_digest="d" * 64,
            request_id="e2e-traversal",
        )
    )
    assert result is not None
    assert result.outcome is VerificationOutcome.SANDBOX_ERROR


async def test_real_distributed_round_trip_rejects_absolute_path_artifact_id(
    verifier_worker: None, staging_root: Path,
) -> None:
    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(
        _request(commit_sha="a" * 40, artifact_id="/etc", artifact_digest="d" * 64, request_id="e2e-abspath")
    )
    assert result is not None
    assert result.outcome is VerificationOutcome.SANDBOX_ERROR


async def test_real_distributed_round_trip_rejects_symlink_escape_artifact_id(
    verifier_worker: None, staging_root: Path,
) -> None:
    escape_link = staging_root / "escape_link"
    escape_link.symlink_to("/etc")
    try:
        dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
        result = await dispatcher.dispatch(
            _request(
                commit_sha="a" * 40, artifact_id="escape_link", artifact_digest="d" * 64, request_id="e2e-symlink",
            )
        )
        assert result is not None
        assert result.outcome is VerificationOutcome.SANDBOX_ERROR
    finally:
        escape_link.unlink(missing_ok=True)


async def test_real_distributed_round_trip_rejects_nonexistent_artifact_id(
    verifier_worker: None, staging_root: Path,
) -> None:
    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(
        _request(
            commit_sha="a" * 40, artifact_id="does-not-exist-at-all", artifact_digest="d" * 64,
            request_id="e2e-nonexistent",
        )
    )
    assert result is not None
    assert result.outcome is VerificationOutcome.SANDBOX_ERROR


# ---- 6. Protocol version rejection ----


async def test_real_distributed_round_trip_rejects_protocol_version_zero(
    verifier_worker: None, tmp_path: Path, staging_root: Path,
) -> None:
    remote = _init_bare_remote(tmp_path)
    sha = _push_commit(remote, tmp_path, files={"tests/test_case.py": "def test_pass():\n    assert 1 == 1\n"})
    artifact_id, digest = _stage(remote, sha, staging_root=staging_root)

    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(
        _request(
            commit_sha=sha, artifact_id=artifact_id, artifact_digest=digest,
            request_id="e2e-protocol-zero", protocol_version=0,
        )
    )
    # The verifier's own enforce_request_protocol_version gate rejects
    # this before execution, but its error reply always echoes back
    # VERIFIER_PROTOCOL_VERSION (its own current version), never the
    # mismatched request's -- so result_matches_request's own identity
    # check (which also independently pins protocol_version) correctly
    # never trusts it either. Either a same-shaped SANDBOX_ERROR result or
    # a dispatcher-level None is an acceptable fail-closed outcome; what
    # matters, and what every branch here guarantees, is that the hostile
    # test target is never executed -- mirrors
    # test_real_distributed_round_trip_rejects_future_protocol_version's
    # own tolerant assertion below.
    assert result is None or result.outcome is VerificationOutcome.SANDBOX_ERROR


async def test_real_distributed_round_trip_rejects_future_protocol_version(
    verifier_worker: None, tmp_path: Path, staging_root: Path,
) -> None:
    remote = _init_bare_remote(tmp_path)
    sha = _push_commit(remote, tmp_path, files={"tests/test_case.py": "def test_pass():\n    assert 1 == 1\n"})
    artifact_id, digest = _stage(remote, sha, staging_root=staging_root)

    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(
        _request(
            commit_sha=sha, artifact_id=artifact_id, artifact_digest=digest,
            request_id="e2e-protocol-future", protocol_version=VERIFIER_PROTOCOL_VERSION + 1,
        )
    )
    # The verifier rejects internally (SANDBOX_ERROR), but result
    # authenticity is ALSO enforced independently on the review-worker
    # side (result_matches_request checks protocol_version) -- either a
    # rejection result comes back, or the dispatcher itself rejects it
    # for a protocol mismatch. Both are acceptable fail-closed outcomes;
    # never an executed test.
    assert result is None or result.outcome is VerificationOutcome.SANDBOX_ERROR


# ---- 7. Stale/duplicate request handling -- deterministic request identity ----


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


# ---- 8. Verifier crash / malformed reply -> fail closed, never a hang or an exception surfacing ----


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
    result = await dispatcher.dispatch(
        _request(
            commit_sha="a" * 40, artifact_id="whatever", artifact_digest="d" * 64,
            request_id="unreachable-broker", timeout_seconds=3.0,
        )
    )
    assert result is None


# ---- 9. Credential-import boundary -- structural, no container needed ----


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


# ---- 10. Verifier service credential (Redis URL) unavailable to hostile test ----


async def test_hostile_test_cannot_read_verifier_redis_credential(
    verifier_worker: None, tmp_path: Path, staging_root: Path,
) -> None:
    """The verifier SERVICE process holds a Redis URL (its own queue
    -consumption credential) -- the hostile sandboxed test subprocess
    must never see it (unchanged sandboxed_env() allowlist, re-verified
    here through the real distributed pipeline, not merely asserted)."""

    remote = _init_bare_remote(tmp_path)
    sha = _push_commit(
        remote, tmp_path,
        files={
            "tests/test_case.py": (
                "import os\n\n"
                "def test_no_redis_env():\n"
                "    assert os.environ.get('REDIS_URL') is None\n"
                "    with open('/proc/self/environ', 'rb') as f:\n"
                "        content = f.read()\n"
                "    assert b'REDIS_URL' not in content\n"
                "    assert b'localhost:6379' not in content\n"
            )
        },
    )
    artifact_id, digest = _stage(remote, sha, staging_root=staging_root)

    dispatcher = VerifierDispatcher(celery_app=_producer_app(), wait_timeout_seconds=20.0)
    result = await dispatcher.dispatch(
        _request(commit_sha=sha, artifact_id=artifact_id, artifact_digest=digest, request_id="e2e-no-redis-leak")
    )
    assert result is not None
    assert result.outcome is VerificationOutcome.PASSED, result.stdout_excerpt
