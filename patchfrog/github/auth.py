"""GitHub App authentication: JWT construction and installation tokens.

Encapsulates the two-step GitHub App auth flow so the rest of PatchFrog
never constructs a JWT or exchanges it for an installation token directly::

    GitHub App JWT -> installation access token -> GitHub API requests

Nothing in this module ever logs a private key, JWT, or installation
token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import jwt

from patchfrog.github.errors import (
    GitHubAuthenticationError,
    GitHubResponseError,
    GitHubServerError,
    GitHubTimeoutError,
)

_JWT_CLOCK_SKEW = timedelta(seconds=60)
_JWT_TTL = timedelta(minutes=9)
# Refresh installation tokens a little before GitHub actually expires them.
_TOKEN_REFRESH_MARGIN = timedelta(minutes=2)


def build_app_jwt(*, app_id: str, private_key: str, now: datetime | None = None) -> str:
    """Build a short-lived JWT authenticating as the GitHub App itself."""

    issued_at = now or datetime.now(UTC)
    payload = {
        "iat": int((issued_at - _JWT_CLOCK_SKEW).timestamp()),
        "exp": int((issued_at + _JWT_TTL).timestamp()),
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


@dataclass(frozen=True, slots=True)
class _CachedToken:
    token: str
    expires_at: datetime


class InstallationTokenProvider:
    """Exchanges the GitHub App JWT for per-installation access tokens.

    Tokens are cached in-memory per installation until shortly before
    GitHub expires them.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        app_id: str,
        private_key: str,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self._http_client = http_client
        self._app_id = app_id
        self._private_key = private_key
        self._api_base_url = api_base_url.rstrip("/")
        self._cache: dict[int, _CachedToken] = {}

    async def get_token(self, installation_id: int) -> str:
        """Return a valid installation access token, refreshing if necessary."""

        cached = self._cache.get(installation_id)
        now = datetime.now(UTC)
        if cached is not None and cached.expires_at - _TOKEN_REFRESH_MARGIN > now:
            return cached.token

        try:
            app_jwt = build_app_jwt(app_id=self._app_id, private_key=self._private_key, now=now)
        except jwt.exceptions.PyJWTError as exc:
            raise GitHubAuthenticationError(
                "Failed to sign GitHub App JWT — check GITHUB_PRIVATE_KEY is a valid PEM key"
            ) from exc
        url = f"{self._api_base_url}/app/installations/{installation_id}/access_tokens"

        try:
            response = await self._http_client.post(
                url,
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                },
            )
        except httpx.TimeoutException as exc:
            raise GitHubTimeoutError(
                f"Timed out exchanging App JWT for installation token (installation={installation_id})"
            ) from exc
        except httpx.HTTPError as exc:
            raise GitHubTimeoutError(
                f"Network error exchanging App JWT for installation token (installation={installation_id})"
            ) from exc

        if response.status_code in (401, 403, 404):
            raise GitHubAuthenticationError(
                f"GitHub rejected installation token request for installation={installation_id} "
                f"(status={response.status_code})"
            )
        if response.status_code >= 500:
            raise GitHubServerError(
                f"GitHub server error exchanging installation token (status={response.status_code})"
            )
        if response.status_code != 201:
            raise GitHubResponseError(
                f"Unexpected status exchanging installation token: {response.status_code}"
            )

        try:
            data = response.json()
            token = data["token"]
            expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubResponseError(
                "Malformed response exchanging installation token"
            ) from exc

        if not isinstance(token, str):
            raise GitHubResponseError("Malformed response exchanging installation token")

        self._cache[installation_id] = _CachedToken(token=token, expires_at=expires_at)
        return token
