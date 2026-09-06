"""Unit tests for patchfrog.executable_verification.sandbox's
availability check -- specifically that binary presence alone is never
sufficient evidence the sandbox actually works. A real CI regression
(GitHub Actions' ubuntu-latest runner) proved that unshare/prlimit can
both be on PATH while unshare --map-root-user still fails at runtime
(Ubuntu 24.04+'s default AppArmor restriction on unprivileged
CLONE_NEWUSER: "unshare: write failed /proc/self/uid_map: Operation not
permitted"). is_sandbox_available must fail closed in exactly that
case, never fall through to an unisolated or misclassified execution."""

from __future__ import annotations

import shutil
import subprocess

import pytest

import patchfrog.executable_verification.sandbox as sandbox_module
from patchfrog.executable_verification.sandbox import (
    _probe_isolation,
    bounded_excerpt,
    is_sandbox_available,
)


def test_is_sandbox_available_false_when_unshare_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "unshare" else "/usr/bin/prlimit")
    assert is_sandbox_available() is False


def test_is_sandbox_available_false_when_prlimit_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "prlimit" else "/usr/bin/unshare")
    assert is_sandbox_available() is False


def test_is_sandbox_available_false_when_probe_fails_despite_binaries_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact CI regression: both binaries discoverable, but the real
    unshare invocation itself fails (e.g. AppArmor-restricted
    unprivileged user namespace creation)."""

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
        raise subprocess.TimeoutExpired(cmd="unshare", timeout=5.0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _probe_isolation() is False


def test_probe_isolation_returns_false_on_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("unshare not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _probe_isolation() is False


def test_probe_isolation_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _probe_isolation() is True


def test_bounded_excerpt_untruncated_when_within_limit() -> None:
    assert bounded_excerpt("short text", max_bytes=1024) == "short text"


def test_bounded_excerpt_truncates_with_marker() -> None:
    text = "x" * 100
    result = bounded_excerpt(text, max_bytes=10)
    assert len(result.encode("utf-8")) < len(text.encode("utf-8"))
    assert result.endswith("\n... (truncated)")
