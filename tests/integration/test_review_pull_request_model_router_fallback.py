"""Milestone Z14 regression coverage: ``_review_pull_request`` (the real
production Celery task body, not a reimplementation of it) must route
provider selection through :class:`~patchfrog.routing.router.ModelRouter`
rather than the older, single-provider ``provider_factory`` path -- see
``apps/worker/tasks/review_pull_request.py``'s own inline comment and
``docs/governance-policy.md``'s "Model Router integration (Z14)" section.

Each test stops the task immediately after routing (monkeypatching
``ModelRouter.route`` to call straight through to the real
implementation, capture its result, then raise) -- routing happens
before repository-index resolution, so no repository index fixture is
needed to reach it. This
exercises the *real* wiring: ``Settings`` -> ``ModelRouter`` inside the
production task, not a standalone ``ModelRouter`` unit test (that
coverage already exists in ``tests/unit/test_router_policy.py`` and is
unchanged by this milestone).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apps.worker.tasks.review_pull_request import _review_pull_request
from patchfrog.config.settings import Settings
from patchfrog.domain.pull_request import ChangedFile, PullRequestMetadata
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.github.client import GitHubClient
from patchfrog.persistence.database import create_engine
from patchfrog.persistence.models import Base
from patchfrog.repository.snapshot import RepositorySnapshot, RepositorySnapshotProvider
from patchfrog.routing.router import (
    ModelRouter,
    NoProviderConfiguredError,
    ProviderNotAllowedByPolicyError,
)
from tests.support.git_repo import materialize_fixture_repo

_GITHUB_INSTALLATION_ID = 99887766
_GITHUB_REPOSITORY_ID = 665544


def _settings(database_url: str, **overrides: object) -> Settings:
    base: dict[str, Any] = {
        "DATABASE_URL": database_url,
        "REDIS_URL": "redis://localhost:6379/0",
        "GITHUB_APP_ID": "1",
        "GITHUB_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        "GITHUB_WEBHOOK_SECRET": "x",
        # Never real secret-shaped values -- these are throwaway strings
        # a fake `has_credentials(...)` check treats as "present", never
        # sent anywhere (routing stops the task before any provider call).
    }
    base.update(overrides)
    return Settings(**base)


async def _fake_get_token(self: InstallationTokenProvider, installation_id: int) -> str:
    return "fake-installation-token"


def _fake_get_pull_request_for(head_sha: str) -> Any:
    async def _fake(self: GitHubClient, *, installation_id: int, ref: Any) -> PullRequestMetadata:
        return PullRequestMetadata(
            number=ref.number, title="t", body=None, author="kadireren7", base_branch="main",
            head_branch="feature", base_sha="c" * 40, head_sha=head_sha,
            html_url="https://github.com/kadireren7/patchfrog-router-fallback/pull/1",
            state="open", merged=False,
        )
    return _fake


async def _fake_list_pull_request_files(
    self: GitHubClient, *, installation_id: int, ref: Any
) -> list[ChangedFile]:
    return []


def _local_snapshot_acquire_for(root_path: Path) -> Any:
    def _acquire(
        self: RepositorySnapshotProvider,
        *,
        clone_url: str,
        commit_sha: str,
        repository_full_name: str,
        token: str | None = None,
        also_fetch: list[str] | None = None,
    ) -> RepositorySnapshot:
        return self.acquire_local(root_path=root_path, repository_full_name=repository_full_name)

    return _acquire


class _StoppedAfterRouting(Exception):
    pass


async def _prepare_repo(tmp_path: Path, database_url: str, full_name: str) -> str:
    engine = create_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    snapshot = materialize_fixture_repo(tmp_path / "repo", "ai_review_python", full_name=full_name)
    return snapshot.commit_sha


async def _run_until_routing(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings, full_name: str
) -> dict[str, Any]:
    commit_sha = await _prepare_repo(tmp_path, settings.database_url, full_name)
    snapshot_root = tmp_path / "repo"

    monkeypatch.setattr(InstallationTokenProvider, "get_token", _fake_get_token)
    monkeypatch.setattr(GitHubClient, "get_pull_request", _fake_get_pull_request_for(commit_sha))
    monkeypatch.setattr(GitHubClient, "list_pull_request_files", _fake_list_pull_request_files)
    monkeypatch.setattr(RepositorySnapshotProvider, "acquire", _local_snapshot_acquire_for(snapshot_root))

    captured: dict[str, Any] = {}
    _real_route = ModelRouter.route  # save before patching -- avoids self-recursion

    def _capture_and_stop_after_real_routing(
        self: ModelRouter, *, runtime_config: Any, critic_enabled: bool
    ) -> Any:
        # Calls the *real* ModelRouter.route (not a stand-in) so this
        # exercises actual provider-selection/fallback/policy logic --
        # only the task's continuation past routing is short-circuited,
        # so no repository index fixture is needed to reach this point.
        plan = _real_route(self, runtime_config=runtime_config, critic_enabled=critic_enabled)
        captured["route_plan"] = plan
        raise _StoppedAfterRouting()

    monkeypatch.setattr(ModelRouter, "route", _capture_and_stop_after_real_routing)

    with pytest.raises(_StoppedAfterRouting):
        await _review_pull_request(
            github_repository_id=_GITHUB_REPOSITORY_ID,
            owner="kadireren7",
            name=full_name.split("/")[-1],
            full_name=full_name,
            installation_id=_GITHUB_INSTALLATION_ID,
            pull_request_number=1,
            head_sha=commit_sha,
            settings=settings,
        )

    return captured


async def test_anthropic_missing_openai_available_review_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preferred provider (anthropic, the default) has no credential;
    the operator has explicitly configured openai as the one-hop
    config-time fallback (PATCHFROG_ROUTER_FALLBACK_PROVIDER) -- routing
    must succeed using openai, never raise
    MissingProviderCredentialsError."""

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"
    settings = _settings(
        database_url,
        OPENAI_API_KEY="test-openai-key-not-real",
        PATCHFROG_ROUTER_FALLBACK_PROVIDER="openai",
    )
    captured = await _run_until_routing(
        tmp_path=tmp_path, monkeypatch=monkeypatch, settings=settings,
        full_name="kadireren7/patchfrog-router-fallback-openai",
    )
    route_plan = captured["route_plan"]
    assert route_plan is not None
    assert route_plan.reviewer_provider_family == "openai"


async def test_anthropic_missing_gemini_available_router_selects_allowed_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same shape, gemini instead of openai, with an explicit
    allowed_providers restriction that still includes gemini -- the
    router must select the allowed, credentialed provider rather than
    failing."""

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"
    settings = _settings(
        database_url,
        GEMINI_API_KEY="test-gemini-key-not-real",
        PATCHFROG_ROUTER_FALLBACK_PROVIDER="gemini",
        PATCHFROG_ALLOWED_PROVIDERS="gemini",
    )
    captured = await _run_until_routing(
        tmp_path=tmp_path, monkeypatch=monkeypatch, settings=settings,
        full_name="kadireren7/patchfrog-router-fallback-gemini",
    )
    route_plan = captured["route_plan"]
    assert route_plan is not None
    assert route_plan.reviewer_provider_family == "gemini"


async def test_policy_allowed_providers_excludes_credentialed_but_disallowed_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """anthropic has a credential and is the preferred provider, but
    Settings.allowed_providers (threaded from Cloud/operator policy)
    excludes it -- routing must never select it, and with no allowed
    provider left credentialed, must raise
    ProviderNotAllowedByPolicyError, not silently fall back to the
    disallowed one."""

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"
    settings = _settings(
        database_url,
        ANTHROPIC_API_KEY="test-anthropic-key-not-real",
        PATCHFROG_ALLOWED_PROVIDERS="gemini,openai",
    )
    with pytest.raises(ProviderNotAllowedByPolicyError):
        await _run_until_routing(
            tmp_path=tmp_path, monkeypatch=monkeypatch, settings=settings,
            full_name="kadireren7/patchfrog-router-policy-block",
        )


async def test_no_allowed_provider_has_credentials_fails_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No provider has any credential configured at all -- routing must
    fail clearly (NoProviderConfiguredError, a MissingProviderCredentialsError
    subclass), never silently proceed or crash on something unrelated."""

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"
    settings = _settings(database_url)
    with pytest.raises(NoProviderConfiguredError):
        await _run_until_routing(
            tmp_path=tmp_path, monkeypatch=monkeypatch, settings=settings,
            full_name="kadireren7/patchfrog-router-no-credentials",
        )
