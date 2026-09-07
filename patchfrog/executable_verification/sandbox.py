"""Isolation layer for Executable Verification -- wraps the existing,
unmodified :func:`patchfrog.analysis.subprocess_sandbox.run_sandboxed`
with a fixed, deterministic isolation prefix rather than reimplementing
subprocess execution a second time.

Repository-controlled test code is treated as hostile. Network/PID
namespace isolation alone (this module's first version) is not
sufficient to claim the host home directory, worker application state,
Docker socket, or arbitrary host paths are unavailable to it -- a
malicious ``pytest`` target can execute arbitrary Python and attempt
absolute-path reads. A real escape was **empirically confirmed** against
that first version before this correction (see
``validation/executable_verification/latest-summary.md`` section 3):
the real ``$HOME``, an arbitrary file outside the disposable workspace,
and the real Docker socket were all readable/visible.

This version adds real filesystem confinement via `bubblewrap
<https://github.com/containers/bubblewrap>`_ (``bwrap``) -- the same
purpose-built, widely-audited unprivileged-sandboxing primitive Flatpak
uses, rather than hand-rolled ``pivot_root``/mount-namespace logic. A
fresh mount namespace exposes **only**: a read-only bind of ``/usr``
(and the symlinked ``/bin``/``/lib``/``/lib64``/``/sbin`` on a
usr-merged host) -- everything the Python/pytest runtime needs to
execute -- plus the disposable verification workspace itself
(read-write) and a private ``tmpfs`` at ``/tmp`` (which also hosts a
throwaway, sandbox-owned ``HOME`` -- never the real worker home). No
other host path is bound. Network remains disabled; PID/resource
isolation are unchanged from the first version.

**Empirically verified** (not merely asserted -- see the latest-summary
for the full transcript): with this sandbox, the exact same malicious
test that previously read the real ``$HOME``, the outside-workspace
sentinel, and the Docker socket now sees none of them (``FileNotFound``/
not visible); a symlink inside the workspace pointing at ``$HOME`` or an
outside path resolves to nothing inside the sandbox; ``/proc/self/environ``
shows only the sandbox's own minimal, disposable environment; network
remains unreachable; ``prlimit`` resource limits (process count, address
space, CPU time) still genuinely enforce inside the new namespaces
(verified: a memory bomb over the ``--as`` limit raises ``MemoryError``,
a fork bomb past the ``--nproc`` limit raises ``OSError``) when applied
*inside* the ``bwrap`` sandbox rather than around it -- applying
``prlimit`` outside ``bwrap`` counts against the real calling UID's
*entire host* process count, not the sandbox's own, and was confirmed to
break ``bwrap``'s own internal setup on a busy desktop.

**Fails closed, and the functional probe matches these real guarantees**:
:func:`is_sandbox_available` does not stop at checking whether
``bwrap``/``prlimit`` are discoverable on ``PATH`` -- binary presence
alone is not sufficient evidence the sandbox actually works. It runs a
real, side-effect-free probe that exercises the same categories of
operation the real sandbox depends on (fresh mount namespace, read-only
bind, private ``tmpfs``, fresh ``/proc``/``/dev``) -- never merely the
simpler PID/network unshare this module's first version checked. A host
that cannot establish full filesystem confinement is treated exactly
like one missing the binaries entirely: unavailable, no execution ever
attempted, no silent fallback to weaker isolation.

**Confirmed host/container-dependent, in both directions**: Ubuntu
24.04+'s default AppArmor restriction on unprivileged ``CLONE_NEWUSER``
blocks this on GitHub Actions' own ``ubuntu-latest`` runner. Separately,
and importantly, PatchFrog's own worker Docker image (a *default*,
non-privileged container, matching how it actually ships) was also
confirmed unable to create the required namespaces at all -- this is a
pre-existing limitation of the deployment shape, not a regression
introduced by this correction (the first, simpler PID/network-only
mechanism this module previously shipped never worked inside that
container either). See the latest-summary for exactly what additional
container-level grants would be required and why none of them are made
here (`docs/executable-verification.md`'s "Production readiness"
section).
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

from patchfrog.analysis.subprocess_sandbox import SandboxedProcessResult, run_sandboxed
from patchfrog.executable_verification.domain import (
    MAX_SANDBOX_ADDRESS_SPACE_BYTES,
    MAX_SANDBOX_CPU_SECONDS,
    MAX_SANDBOX_PROCESSES,
    MAX_STDERR_EXCERPT_BYTES,
    MAX_STDOUT_EXCERPT_BYTES,
)

_PROBE_TIMEOUT_SECONDS = 5.0

#: The disposable, sandbox-owned HOME -- created fresh inside the
#: sandbox's own private tmpfs via --dir, never a real host path. Vanishes
#: automatically when the sandboxed process tree exits; there is nothing
#: for a caller to create or clean up on the host side.
_SANDBOX_HOME = "/tmp/home"
_SANDBOX_CACHE_HOME = f"{_SANDBOX_HOME}/.cache"
_SANDBOX_PYCACHE = f"{_SANDBOX_CACHE_HOME}/pycache"

#: Symlinks a usr-merged host (Debian/Ubuntu, including this module's own
#: real deployment target -- the python:3.12-slim-based worker image)
#: points at /usr/<name>. Recreated inside the sandbox rather than bound
#: as directories, since binding a path that is really a symlink on the
#: host would bind the symlink file itself, not its target.
_USR_MERGE_LINKS = ("bin", "sbin", "lib", "lib64")


@functools.lru_cache(maxsize=1)
def _runtime_bind_args() -> tuple[str, ...]:
    """Read-only bind-mount arguments giving the sandboxed process
    exactly the Python/pytest runtime it needs -- computed once (the
    running interpreter's own location never changes within a process)
    and shared by the real sandbox and its own functional probe, so the
    probe can never drift from what execution actually depends on.

    /usr covers the production worker image's own runtime in full (its
    system Python, with pytest installed as a base dependency, lives
    under /usr/local -- itself nested inside /usr, so no separate bind is
    needed there). A local/CLI dev environment commonly runs PatchFrog
    itself from a venv outside /usr (e.g. under a developer's home
    directory) -- that venv's own root is bound too, read-only, so
    verification keeps working there without exposing anything else from
    the real host home.
    """

    args: list[str] = ["--ro-bind", "/usr", "/usr"]
    for name in _USR_MERGE_LINKS:
        path = f"/{name}"
        if not os.path.islink(path):
            continue
        target = os.readlink(path)
        args += ["--symlink", target, path]

    interpreter_root = os.path.realpath(sys.prefix)
    if not (interpreter_root == "/usr" or interpreter_root.startswith("/usr/")):
        args += ["--ro-bind", interpreter_root, interpreter_root]

    return tuple(args)


def _probe_argv() -> list[str]:
    return [
        "bwrap",
        "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
        *_runtime_bind_args(),
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
        "--chdir", "/tmp",
        "--", "/usr/bin/true",
    ]


def _probe_isolation() -> bool:
    """A real, side-effect-free invocation through the exact isolation
    shape :class:`VerificationSandbox` uses for real verification (fresh
    mount namespace, read-only runtime bind, private tmpfs, fresh
    proc/dev) -- not merely the cheaper PID/network unshare this module's
    first version checked. Binary presence never substitutes for this."""

    try:
        result = subprocess.run(_probe_argv(), capture_output=True, timeout=_PROBE_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def is_sandbox_available() -> bool:
    """``bwrap``/``prlimit`` must both be discoverable, and a real
    isolation probe exercising the same mount-namespace/bind/tmpfs
    operations real verification depends on must actually succeed --
    never assumed from binary presence alone. Mirrors
    :meth:`patchfrog.analysis.analyzers.base.Analyzer.discover`'s own
    "never raise, represent as unavailable" contract."""

    if shutil.which("bwrap") is None or shutil.which("prlimit") is None:
        return False
    return _probe_isolation()


class VerificationSandbox:
    """Owns the fixed isolation-prefix argv construction and delegates
    actual process execution to the existing, unmodified
    ``run_sandboxed`` -- never a second, parallel subprocess-execution
    implementation."""

    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds

    def _wrap(self, args: list[str], *, workspace: Path) -> list[str]:
        workspace_str = str(workspace)
        return [
            "bwrap",
            "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
            "--setenv", "HOME", _SANDBOX_HOME,
            "--setenv", "TMPDIR", "/tmp",
            "--setenv", "XDG_CACHE_HOME", _SANDBOX_CACHE_HOME,
            "--setenv", "PYTHONPYCACHEPREFIX", _SANDBOX_PYCACHE,
            "--setenv", "LANG", "C.UTF-8",
            "--setenv", "LC_ALL", "C.UTF-8",
            *_runtime_bind_args(),
            "--tmpfs", "/tmp",
            "--dir", _SANDBOX_HOME,
            "--bind", workspace_str, workspace_str,
            "--proc", "/proc",
            "--dev", "/dev",
            "--chdir", workspace_str,
            "--",
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
        bounded by this sandbox's own timeout. ``cwd`` is both the outer
        process's working directory and the one host path bound
        read-write inside the sandbox -- the disposable verification
        workspace, and nothing else from the host filesystem."""

        return await run_sandboxed(self._wrap(args, workspace=cwd), cwd=cwd, timeout_seconds=self._timeout_seconds)


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
