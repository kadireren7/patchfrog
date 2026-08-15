"""Sandboxed subprocess execution for analyzer binaries.

Analyzer binaries process attacker-controlled source files — they are
untrusted subprocesses, exactly like the repository content they read.
Every analyzer adapter must launch its tool through :func:`run_sandboxed`
rather than calling ``subprocess``/``asyncio.create_subprocess_exec``
directly, so every invocation gets the same guarantees:

- an explicit argument array, never ``shell=True`` and never a
  caller-assembled command string
- an explicit working directory
- an *allowlisted* environment — not a denylist of known secret names,
  which is only ever as complete as the list of secrets someone thought
  to add. Only ``PATH``/``HOME``/``LANG``/``LC_ALL`` are ever passed
  through; ``DATABASE_URL``, ``GITHUB_*``, ``REDIS_URL``, and everything
  else PatchFrog's own process has in its environment are never visible
  to an analyzer subprocess, whether or not the caller thought to name it
- an explicit timeout, with the process group killed (not just the
  immediate child) if it's exceeded
- a genuine *streaming* cap on stdout/stderr capture — reading stops once
  the cap is hit rather than buffering unbounded output and truncating
  afterward, so a misbehaving or malicious tool can't exhaust memory. If
  the tool keeps writing after its output is abandoned, the OS pipe
  eventually applies backpressure and the process stalls until the
  overall timeout reaps it — memory stays bounded either way.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

#: Environment variables ever passed through to an analyzer subprocess.
#: Deliberately an allowlist, not a denylist of secret names to strip.
_ALLOWED_ENV_VARS = ("PATH", "HOME", "LANG", "LC_ALL")

MAX_CAPTURED_OUTPUT_BYTES = 5 * 1024 * 1024  # 5 MiB per stream
_READ_CHUNK_BYTES = 65536


class AnalyzerSubprocessError(RuntimeError):
    """The subprocess itself could not be started (missing binary, etc.)."""


@dataclass(frozen=True, slots=True)
class SandboxedProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


def sandboxed_env() -> dict[str, str]:
    """The allowlisted environment every analyzer subprocess receives."""

    env = {name: os.environ[name] for name in _ALLOWED_ENV_VARS if name in os.environ}
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    return env


async def run_sandboxed(
    args: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    extra_env: dict[str, str] | None = None,
) -> SandboxedProcessResult:
    """Run an analyzer binary under the sandbox guarantees described above.

    ``extra_env`` may add a small number of *non-secret* variables an
    analyzer genuinely needs (e.g. a tool-specific config path) on top of
    the allowlisted base environment — it is layered on, never replaces
    the allowlisting itself.
    """

    env = sandboxed_env()
    if extra_env:
        env.update(extra_env)

    start = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group, so a timeout kill reaches children too
        )
    except OSError as exc:
        raise AnalyzerSubprocessError(f"failed to start {args[0]!r}: {exc}") from exc

    timed_out = False
    stdout, stdout_truncated = b"", False
    stderr, stderr_truncated = b"", False
    try:
        assert process.stdout is not None
        assert process.stderr is not None
        (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.wait_for(
            asyncio.gather(
                _read_capped(process.stdout, MAX_CAPTURED_OUTPUT_BYTES),
                _read_capped(process.stderr, MAX_CAPTURED_OUTPUT_BYTES),
            ),
            timeout=timeout_seconds,
        )
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        await _kill_process_group(process)

    duration_ms = (time.monotonic() - start) * 1000
    exit_code = process.returncode if process.returncode is not None else -1

    return SandboxedProcessResult(
        exit_code=exit_code,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        duration_ms=duration_ms,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks), False
        total += len(chunk)
        if total > cap:
            overflow = total - cap
            chunks.append(chunk[: len(chunk) - overflow])
            return b"".join(chunks), True
        chunks.append(chunk)


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, 9)
    except ProcessLookupError:
        pass
    except OSError as exc:
        logger.warning("analyzer_kill_failed", pid=process.pid, error=str(exc))
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        logger.warning("analyzer_did_not_exit_after_kill", pid=process.pid)
