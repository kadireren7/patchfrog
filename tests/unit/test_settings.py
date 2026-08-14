from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from patchfrog.config.settings import Settings


def test_settings_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_private_key_requires_pem_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "not-a-pem-key")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_private_key_converts_escaped_newlines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "GITHUB_PRIVATE_KEY",
        "-----BEGIN RSA PRIVATE KEY-----\\nabc\\n-----END RSA PRIVATE KEY-----",
    )
    settings = Settings(_env_file=None)
    assert "\\n" not in settings.github_private_key
    assert "\n" in settings.github_private_key


def test_private_key_loaded_from_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_file = tmp_path / "key.pem"
    key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
    monkeypatch.delenv("GITHUB_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", str(key_file))

    settings = Settings(_env_file=None)

    assert "BEGIN RSA PRIVATE KEY" in settings.github_private_key


def test_private_key_path_and_inline_both_set_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.pem"
    key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", str(key_file))
    # GITHUB_PRIVATE_KEY is already set via the test-session default in conftest.

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_private_key_missing_both_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY_PATH", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_private_key_path_nonexistent_file_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GITHUB_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", str(tmp_path / "does-not-exist.pem"))

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_log_level_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "not-a-level")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_repr_never_leaks_secrets() -> None:
    settings = Settings(_env_file=None)
    text = repr(settings)
    assert settings.github_private_key not in text
    assert settings.github_webhook_secret not in text
