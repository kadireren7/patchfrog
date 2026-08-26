"""Unit coverage for the pure parts of :mod:`patchfrog.ops.eligibility`
-- resource limits. Quota/installation/repository checks are covered in
tests/integration/test_ops_eligibility_db.py (they need a real session)."""

from __future__ import annotations

from typing import Any

from patchfrog.config.settings import Settings
from patchfrog.ops.eligibility import check_resource_limits


def _settings(**overrides: object) -> Settings:
    base: dict[str, Any] = {
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6379/0",
        "GITHUB_APP_ID": "1",
        "GITHUB_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        "GITHUB_WEBHOOK_SECRET": "x",
    }
    base.update(overrides)
    return Settings(**base)


def test_within_limits_passes() -> None:
    ok, detail = check_resource_limits(settings=_settings(), changed_files_count=10, diff_bytes=1000)
    assert ok is True
    assert detail == ""


def test_too_many_changed_files_fails() -> None:
    settings = _settings(MAX_CHANGED_FILES=5)
    ok, detail = check_resource_limits(settings=settings, changed_files_count=6, diff_bytes=100)
    assert ok is False
    assert "changed files" in detail


def test_diff_too_large_fails() -> None:
    settings = _settings(MAX_DIFF_BYTES=100)
    ok, detail = check_resource_limits(settings=settings, changed_files_count=1, diff_bytes=101)
    assert ok is False
    assert "diff bytes" in detail


def test_exactly_at_limit_passes() -> None:
    settings = _settings(MAX_CHANGED_FILES=5, MAX_DIFF_BYTES=100)
    ok, _detail = check_resource_limits(settings=settings, changed_files_count=5, diff_bytes=100)
    assert ok is True
