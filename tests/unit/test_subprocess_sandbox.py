"""Regression tests for the analyzer subprocess security sandbox.

These are the guarantees every analyzer adapter depends on
(``patchfrog.analysis.subprocess_sandbox``): secrets never reach analyzer
subprocesses, arguments are never shell-interpreted, timeouts actually
kill the process (group), and captured output is genuinely bounded.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from patchfrog.analysis.subprocess_sandbox import (
    MAX_CAPTURED_OUTPUT_BYTES,
    AnalyzerSubprocessError,
    run_sandboxed,
    sandboxed_env,
)

_SECRET_ENV_VARS = {
    "DATABASE_URL": "postgresql+asyncpg://patchfrog:patchfrog@postgres:5432/patchfrog",
    "REDIS_URL": "redis://redis:6379/0",
    "GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----",
    "GITHUB_PRIVATE_KEY_PATH": "/run/secrets/github-app-key.pem",
    "GITHUB_WEBHOOK_SECRET": "super-secret-webhook",
    "GITHUB_APP_ID": "4598355",
}


def test_sandboxed_env_never_contains_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _SECRET_ENV_VARS.items():
        monkeypatch.setenv(name, value)

    env = sandboxed_env()

    for name in _SECRET_ENV_VARS:
        assert name not in env
    for value in _SECRET_ENV_VARS.values():
        assert value not in env.values()


def test_sandboxed_env_only_contains_allowlisted_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _SECRET_ENV_VARS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SOME_RANDOM_CI_VAR", "whatever")

    env = sandboxed_env()

    assert set(env).issubset({"PATH", "HOME", "LANG", "LC_ALL"})


async def test_extra_env_is_layered_on_not_a_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://leak-if-broken")

    result = await run_sandboxed(
        [sys.executable, "-c", "import os; print(os.environ.get('DATABASE_URL', '<absent>'))"],
        cwd=Path.cwd(),
        timeout_seconds=10,
        extra_env={"SOME_TOOL_CONFIG": "1"},
    )

    assert result.stdout.strip() == "<absent>"


async def test_arguments_are_never_shell_interpreted(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    injection_attempt = f"; touch {marker}; echo pwned"

    result = await run_sandboxed(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", injection_attempt],
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert result.stdout.strip() == injection_attempt
    assert not marker.exists()


async def test_cwd_is_respected() -> None:
    result = await run_sandboxed(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=Path("/tmp"),
        timeout_seconds=10,
    )

    assert result.stdout.strip() == os.path.realpath("/tmp")


async def test_timeout_kills_a_hanging_process() -> None:
    result = await run_sandboxed(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=Path.cwd(),
        timeout_seconds=0.2,
    )

    assert result.timed_out is True


async def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    """A timed-out parent must not leave an orphaned child still running --
    ``start_new_session=True`` + killing the process group, not just the
    immediate pid, is what makes that true."""

    marker = tmp_path / "child-still-ran"
    parent_script = tmp_path / "parent.py"
    child_script = tmp_path / "child.py"
    child_script.write_text(f"import time\ntime.sleep(2)\nopen({str(marker)!r}, 'w').close()\n")
    parent_script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
        "time.sleep(60)\n"
    )

    result = await run_sandboxed(
        [sys.executable, str(parent_script)], cwd=tmp_path, timeout_seconds=0.3
    )
    assert result.timed_out is True

    import asyncio

    await asyncio.sleep(3)
    assert not marker.exists()


async def test_captured_stdout_is_bounded_not_buffered_unbounded() -> None:
    start = time.monotonic()
    result = await run_sandboxed(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('a' * (10 * 1024 * 1024))",
        ],
        cwd=Path.cwd(),
        timeout_seconds=30,
    )
    elapsed = time.monotonic() - start

    assert result.stdout_truncated is True
    assert len(result.stdout.encode()) <= MAX_CAPTURED_OUTPUT_BYTES
    # A capped stream must not cost the caller anywhere near the full
    # timeout budget: hitting the cap on stdout leaves stderr genuinely
    # unable to reach EOF on its own (the process, no longer being
    # drained, can never exit naturally) -- this must resolve via the
    # short grace-period + kill path, not by waiting out `timeout_seconds`.
    assert elapsed < 15.0


async def test_many_fast_successful_runs_are_never_misclassified_as_timed_out() -> None:
    """Regression for a race in the streams-drained-early-exit path: a
    process's stdout/stderr pipes can hit real EOF a handful of
    microseconds *before* the OS reaps it and process.wait() resolves.
    Reacting to that gap as if the process were stuck falsely marked
    plenty of fast, entirely successful runs as timed out."""

    for _ in range(20):
        result = await run_sandboxed(
            [sys.executable, "-c", "print('ok')"], cwd=Path.cwd(), timeout_seconds=10
        )
        assert result.timed_out is False
        assert result.exit_code == 0
        assert result.stdout.strip() == "ok"


async def test_missing_binary_raises_analyzer_subprocess_error() -> None:
    with pytest.raises(AnalyzerSubprocessError):
        await run_sandboxed(
            ["/no/such/binary/patchfrog-does-not-exist"], cwd=Path.cwd(), timeout_seconds=5
        )


async def test_nonzero_exit_is_reported_not_raised() -> None:
    result = await run_sandboxed(
        [sys.executable, "-c", "import sys; sys.exit(3)"], cwd=Path.cwd(), timeout_seconds=10
    )

    assert result.exit_code == 3
    assert result.timed_out is False
