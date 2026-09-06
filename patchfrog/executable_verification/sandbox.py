"""Isolation layer for Executable Verification -- wraps the existing,
unmodified :func:`patchfrog.analysis.subprocess_sandbox.run_sandboxed`
with a fixed, deterministic isolation prefix rather than reimplementing
subprocess execution a second time.

**Empirically verified** (not merely asserted -- see
``validation/executable_verification/latest-summary.md`` section 3) on
this environment: ``unshare --net --map-root-user`` genuinely isolates
network (even loopback is down by default; a raw socket connection to an
external host fails with "Network is unreachable"); ``unshare --pid
--fork --mount-proc`` genuinely isolates the process tree (the sandboxed
command becomes PID 1 inside its own namespace); ``prlimit
--nproc=N --as=BYTES --cpu=SECONDS`` genuinely constrains resources.

**Fails closed**: :func:`is_sandbox_available` does not stop at checking
whether ``unshare``/``prlimit`` are discoverable on ``PATH`` -- it also
runs a real, side-effect-free probe through this module's own exact
isolation prefix. Binary presence alone is not sufficient evidence: a
hardened host can have both installed while still refusing to create an
unprivileged user namespace (Ubuntu 24.04+'s default AppArmor
restriction on unprivileged ``CLONE_NEWUSER`` is a real, confirmed
example -- reproduced on GitHub Actions' own ``ubuntu-latest`` runner).
When the probe fails, no execution is ever attempted -- never a silent,
unisolated fallback. See the latest-summary's own discussion of what
this sandbox does and does not achieve (process/network/PID isolation,
not a full container -- no mount namespace, no chroot).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from patchfrog.analysis.subprocess_sandbox import SandboxedProcessResult, run_sandboxed
from patchfrog.executable_verification.domain import (
    MAX_SANDBOX_ADDRESS_SPACE_BYTES,
    MAX_SANDBOX_CPU_SECONDS,
    MAX_SANDBOX_PROCESSES,
    MAX_STDERR_EXCERPT_BYTES,
    MAX_STDOUT_EXCERPT_BYTES,
)

#: A cheap, side-effect-free command run through the exact isolation
#: prefix this module actually uses, to prove namespace creation itself
#: succeeds -- binary presence alone (``shutil.which``) is not enough. A
#: hardened host may have both ``unshare``/``prlimit`` installed while
#: still refusing to create an unprivileged user namespace (e.g. Ubuntu
#: 24.04+'s default AppArmor restriction on unprivileged ``CLONE_NEWUSER``,
#: confirmed to reproduce exactly this on GitHub Actions' own
#: ``ubuntu-latest`` runner: ``unshare: write failed
#: /proc/self/uid_map: Operation not permitted``).
_PROBE_TIMEOUT_SECONDS = 5.0


def _probe_isolation() -> bool:
    try:
        result = subprocess.run(
            ["unshare", "--pid", "--fork", "--mount-proc", "--net", "--map-root-user", "--", "true"],
            capture_output=True, timeout=_PROBE_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def is_sandbox_available() -> bool:
    """``unshare``/``prlimit`` must both be discoverable, and a real
    ``unshare`` invocation using this module's own isolation flags must
    actually succeed -- never assumed from binary presence alone. Mirrors
    :meth:`patchfrog.analysis.analyzers.base.Analyzer.discover`'s own
    "never raise, represent as unavailable" contract."""

    if shutil.which("unshare") is None or shutil.which("prlimit") is None:
        return False
    return _probe_isolation()


class VerificationSandbox:
    """Owns the fixed isolation-prefix argv construction and delegates
    actual process execution to the existing, unmodified
    ``run_sandboxed`` -- never a second, parallel subprocess-execution
    implementation."""

    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds

    def _wrap(self, args: list[str]) -> list[str]:
        return [
            "unshare", "--pid", "--fork", "--mount-proc", "--net", "--map-root-user", "--",
            "prlimit",
            f"--nproc={MAX_SANDBOX_PROCESSES}",
            f"--as={MAX_SANDBOX_ADDRESS_SPACE_BYTES}",
            f"--cpu={MAX_SANDBOX_CPU_SECONDS}",
            "--",
            *args,
        ]

    async def run(self, args: list[str], *, cwd: Path) -> SandboxedProcessResult:
        """Run ``args`` (a fixed, already-built argv -- never a
        caller-assembled shell string) inside the isolation prefix,
        bounded by this sandbox's own timeout."""

        return await run_sandboxed(self._wrap(args), cwd=cwd, timeout_seconds=self._timeout_seconds)


def bounded_excerpt(text: str, *, max_bytes: int) -> str:
    """Truncates ``text`` to at most ``max_bytes`` (UTF-8), never
    silently -- appends a marker so a reader can tell truncation
    happened."""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "\n... (truncated)"


def stdout_excerpt(text: str) -> str:
    return bounded_excerpt(text, max_bytes=MAX_STDOUT_EXCERPT_BYTES)


def stderr_excerpt(text: str) -> str:
    return bounded_excerpt(text, max_bytes=MAX_STDERR_EXCERPT_BYTES)
