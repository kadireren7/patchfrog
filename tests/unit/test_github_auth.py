from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import jwt
import pytest
import respx

from patchfrog.github.auth import InstallationTokenProvider, build_app_jwt
from patchfrog.github.errors import (
    GitHubAuthenticationError,
    GitHubResponseError,
    GitHubServerError,
)

API_BASE = "https://api.github.com"


def test_build_app_jwt_has_expected_claims(test_private_key: str) -> None:
    now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)

    token = build_app_jwt(app_id="123456", private_key=test_private_key, now=now)

    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["iss"] == "123456"
    assert decoded["iat"] < int(now.timestamp())
    assert decoded["exp"] > int(now.timestamp())


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@respx.mock
async def test_get_token_fetches_and_caches(
    http_client: httpx.AsyncClient, test_private_key: str
) -> None:
    route = respx.post(f"{API_BASE}/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(
            201,
            json={"token": "ghs_installationtoken", "expires_at": "2099-01-01T00:00:00Z"},
        )
    )

    provider = InstallationTokenProvider(
        http_client=http_client,
        app_id="123456",
        private_key=test_private_key,
        api_base_url=API_BASE,
    )

    token_one = await provider.get_token(42)
    token_two = await provider.get_token(42)

    assert token_one == "ghs_installationtoken"
    assert token_two == "ghs_installationtoken"
    assert route.call_count == 1  # second call served from cache


@respx.mock
async def test_get_token_authentication_failure(
    http_client: httpx.AsyncClient, test_private_key: str
) -> None:
    respx.post(f"{API_BASE}/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )

    provider = InstallationTokenProvider(
        http_client=http_client,
        app_id="123456",
        private_key=test_private_key,
        api_base_url=API_BASE,
    )

    with pytest.raises(GitHubAuthenticationError):
        await provider.get_token(42)


@respx.mock
async def test_get_token_server_error(
    http_client: httpx.AsyncClient, test_private_key: str
) -> None:
    respx.post(f"{API_BASE}/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(500, text="oops")
    )

    provider = InstallationTokenProvider(
        http_client=http_client,
        app_id="123456",
        private_key=test_private_key,
        api_base_url=API_BASE,
    )

    with pytest.raises(GitHubServerError):
        await provider.get_token(42)


async def test_get_token_wraps_malformed_private_key(http_client: httpx.AsyncClient) -> None:
    """A malformed GITHUB_PRIVATE_KEY must surface as GitHubAuthenticationError,
    not a raw jwt.exceptions.PyJWTError — this crashed the Celery worker in
    Phase 1.5 live testing when a placeholder key was configured.
    """

    provider = InstallationTokenProvider(
        http_client=http_client,
        app_id="123456",
        private_key="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----",
        api_base_url=API_BASE,
    )

    with pytest.raises(GitHubAuthenticationError):
        await provider.get_token(42)


@respx.mock
async def test_get_token_malformed_response(
    http_client: httpx.AsyncClient, test_private_key: str
) -> None:
    respx.post(f"{API_BASE}/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(201, json={"unexpected": "shape"})
    )

    provider = InstallationTokenProvider(
        http_client=http_client,
        app_id="123456",
        private_key=test_private_key,
        api_base_url=API_BASE,
    )

    with pytest.raises(GitHubResponseError):
        await provider.get_token(42)
