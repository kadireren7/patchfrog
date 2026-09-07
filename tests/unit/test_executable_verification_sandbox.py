"""Unit tests for patchfrog.executable_verification.sandbox's
availability check and runtime-bind computation -- specifically that
binary presence alone is never sufficient evidence the sandbox actually
works. Two real regressions motivate this file:

1. A CI regression (GitHub Actions' ubuntu-latest runner) proved that
   unshare/prlimit can both be on PATH while ``unshare --map-root-user``
   still fails at runtime (Ubuntu 24.04+'s default AppArmor restriction
   on unprivileged CLONE_NEWUSER).
2. A real filesystem-escape regression (found via a security correction
   round, not CI) proved that PID/network isolation alone does not
   confine the filesystem at all -- a malicious pytest target could read
   the real $HOME, an arbitrary file outside the disposable workspace,
   and the real Docker socket. Fixed by routing execution through
   bubblewrap (bwrap) with an explicit, minimal read-only runtime bind
   plus a private tmpfs -- see sandbox.py's own module docstring and
   validation/executable_verification/latest-summary.md for the full
   empirical transcript (including inside a real, non-privileged worker
   Docker container, not just this dev host).

is_sandbox_available must fail closed in both failure classes, never
fall through to an unisolated or misclassified execution."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

import patchfrog.executable_verification.sandbox as sandbox_module
from patchfrog.executable_verification.sandbox import (
    _probe_isolation,
    _runtime_bind_args,
    bounded_excerpt,
    is_sandbox_available,
)


def test_is_sandbox_available_false_when_bwrap_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "bwrap" else "/usr/bin/prlimit")
    assert is_sandbox_available() is False


def test_is_sandbox_available_false_when_prlimit_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "prlimit" else "/usr/bin/bwrap")
    assert is_sandbox_available() is False


def test_is_sandbox_available_false_when_probe_fails_despite_binaries_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact CI regression: both binaries discoverable, but the real
    bwrap invocation itself fails (e.g. AppArmor-restricted unprivileged
    user namespace creation, or a non-privileged Docker container's
    default seccomp profile blocking the mount operations bwrap needs --
    both empirically confirmed cases)."""

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(sandbox_module, "_probe_isolation", lambda: False)
    assert is_sandbox_available() is False


def test_is_sandbox_available_true_when_binaries_present_and_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(sandbox_module, "_probe_isolation", lambda: True)
    assert is_sandbox_available() is True


def test_probe_isolation_returns_false_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=1)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _probe_isolation() is False


def test_probe_isolation_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd="bwrap", timeout=5.0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _probe_isolation() is False


def test_probe_isolation_returns_false_on_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("bwrap not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _probe_isolation() is False


def test_probe_isolation_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _probe_isolation() is True


def test_probe_argv_uses_the_same_runtime_binds_as_real_execution() -> None:
    """The probe must never drift from what real execution actually
    depends on -- both are built from the same cached helper."""

    probe_argv = sandbox_module._probe_argv()
    for bind_arg in _runtime_bind_args():
        assert bind_arg in probe_argv


def test_runtime_bind_args_always_binds_usr() -> None:
    args = _runtime_bind_args()
    assert "--ro-bind" in args
    idx = args.index("--ro-bind")
    assert args[idx + 1] == "/usr"
    assert args[idx + 2] == "/usr"


def test_runtime_bind_args_recreates_usrmerge_symlinks_not_directory_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    """/bin, /lib, /lib64, /sbin are symlinks into /usr on a usr-merged
    host (the actual worker image's own base, and this dev host) --
    binding the symlink file itself (rather than recreating it) would
    silently break the dynamic linker inside the sandbox, empirically
    confirmed during this correction (execvp failed with "No such file
    or directory" until the symlinks were recreated)."""

    sandbox_module._runtime_bind_args.cache_clear()
    monkeypatch.setattr(os.path, "islink", lambda path: path in ("/bin", "/lib"))
    monkeypatch.setattr(os, "readlink", lambda path: {"/bin": "usr/bin", "/lib": "usr/lib"}[path])
    try:
        args = _runtime_bind_args()
    finally:
        sandbox_module._runtime_bind_args.cache_clear()

    assert "--symlink" in args
    assert "usr/bin" in args
    assert "/bin" in args
    # /sbin and /lib64 are not symlinks on this (mocked) host, so they
    # must not appear in the bind args at all.
    assert "/sbin" not in args
    assert "/lib64" not in args


def test_runtime_bind_args_binds_interpreter_root_when_outside_usr(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox_module._runtime_bind_args.cache_clear()
    monkeypatch.setattr(os.path, "islink", lambda _path: False)
    monkeypatch.setattr(sys, "prefix", "/home/dev/project/.venv")
    monkeypatch.setattr(os.path, "realpath", lambda path: path)
    try:
        args = _runtime_bind_args()
    finally:
        sandbox_module._runtime_bind_args.cache_clear()

    assert "/home/dev/project/.venv" in args


def test_runtime_bind_args_does_not_double_bind_interpreter_already_under_usr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_module._runtime_bind_args.cache_clear()
    monkeypatch.setattr(os.path, "islink", lambda _path: False)
    monkeypatch.setattr(sys, "prefix", "/usr/local")
    monkeypatch.setattr(os.path, "realpath", lambda path: path)
    try:
        args = _runtime_bind_args()
    finally:
        sandbox_module._runtime_bind_args.cache_clear()

    # /usr is already bound once (for the runtime); /usr/local must not
    # produce a second, redundant bind entry.
    assert args.count("/usr/local") == 0


def test_bounded_excerpt_untruncated_when_within_limit() -> None:
    assert bounded_excerpt("short text", max_bytes=1024) == "short text"


def test_bounded_excerpt_truncates_with_marker() -> None:
    text = "x" * 100
    result = bounded_excerpt(text, max_bytes=10)
    assert len(result.encode("utf-8")) < len(text.encode("utf-8"))
    assert result.endswith("\n... (truncated)")


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not available on this host")
def test_real_probe_matches_is_sandbox_available_on_this_host() -> None:
    """No mocking -- proves the real probe and is_sandbox_available agree
    on this actual host, whatever the answer is."""

    assert is_sandbox_available() == (_probe_isolation() if shutil.which("prlimit") else False)


def test_runtime_bind_args_is_cached() -> None:
    assert _runtime_bind_args() is _runtime_bind_args()


def test_probe_command_targets_true_binary_absolute_path() -> None:
    argv = sandbox_module._probe_argv()
    assert argv[0] == "bwrap"
    assert argv[-1] == "/usr/bin/true"
    assert os.path.isabs(argv[-1])
