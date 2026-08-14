"""Test-session bootstrap.

Sets required environment variables *before* any application module is
imported by a test, so :func:`patchfrog.config.settings.get_settings`
never fails validation during collection. Values are test-only fixtures,
never real credentials.

The test RSA key is generated in-memory rather than committed to the repo
as a ``.pem`` file — even a throwaway test-only key is best kept out of
source control, and secret-scanning tooling can't tell the difference.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _generate_test_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


_TEST_PRIVATE_KEY_PEM = _generate_test_private_key_pem()

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("GITHUB_APP_ID", "123456")
os.environ.setdefault("GITHUB_PRIVATE_KEY", _TEST_PRIVATE_KEY_PEM)
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.fixture
def fixture_loader() -> Any:
    return load_fixture


@pytest.fixture
def test_private_key() -> str:
    return _TEST_PRIVATE_KEY_PEM
