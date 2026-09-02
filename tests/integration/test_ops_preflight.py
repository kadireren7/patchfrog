"""Integration coverage for ``patchfrog ops preflight``
(:mod:`patchfrog.ops.preflight`) -- external beta readiness.

Real DB rows (repository/installation), real
:func:`patchfrog.ops.eligibility.check_eligibility` reuse -- never a
separate, potentially-diverging copy of eligibility logic. The one live
network call (resolving ``.patchfrog.yml`` from a repository's real
default branch) is either short-circuited by an injected fake
``GitHubClient`` that raises before ever reaching it, or the resolver
function itself is monkeypatched -- no real GitHub API call, no live
LLM call, anywhere in this file.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.config.settings import Settings
from patchfrog.github.errors import GitHubNotFoundError
from patchfrog.ops.doctor import DoctorStatus
from patchfrog.ops.preflight import PreflightOutcome, run_preflight
from patchfrog.persistence.models.installation import BetaState, InstallationStatus
from patchfrog.persistence.repositories import InstallationRepository, RepositoryRepository
from patchfrog.publishing.config import PublicationConfig


def _settings(*, test_private_key: str, **overrides: object) -> Settings:
    kwargs: dict[str, object] = {
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6379/0",
        "GITHUB_APP_ID": "123456",
        "GITHUB_WEBHOOK_SECRET": "a-real-webhook-secret",
        "GITHUB_PRIVATE_KEY": test_private_key,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)  # type: ignore[arg-type]


class _RaisingGitHubClient:
    """Structurally satisfies the one method preflight's live check
    calls -- always fails before any real HTTP request would occur."""

    async def get_default_branch_head_sha(self, *, installation_id: int, owner: str, repository: str) -> str:
        raise GitHubNotFoundError(f"no such repository: {owner}/{repository}")


async def _make_repository(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    full_name: str,
    github_installation_id: int,
    is_selected: bool = True,
    installation_status: InstallationStatus = InstallationStatus.ACTIVE,
    beta_state: BetaState = BetaState.ACTIVE,
    publication_allowed: bool = False,
) -> None:
    async with session_factory() as session:
        installation = await InstallationRepository().upsert(
            session,
            github_installation_id=github_installation_id,
            account_login=full_name.split("/")[0],
            account_type="Organization",
            default_beta_state=beta_state,
        )
        installation.status = installation_status
        installation.publication_allowed = publication_allowed
        repo = await RepositoryRepository().upsert(
            session,
            github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0],
            name=full_name.split("/")[-1],
            full_name=full_name,
            installation_id=github_installation_id,
        )
        repo.is_selected = is_selected
        await session.commit()


async def test_unknown_repository_is_blocked(
    session_factory: async_sessionmaker[AsyncSession], test_private_key: str
) -> None:
    settings = _settings(test_private_key=test_private_key)
    async with session_factory() as session:
        report = await run_preflight(session, settings=settings, repository_full_name="never/known-xyz")

    assert report.outcome is PreflightOutcome.BLOCKED
    assert report.checks[0].status is DoctorStatus.FAIL
    assert len(report.checks) == 1  # never proceeds to any further check


async def test_global_processing_disabled_is_blocked(
    session_factory: async_sessionmaker[AsyncSession], test_private_key: str
) -> None:
    full_name = f"test/preflight-global-off-{uuid.uuid4().hex[:8]}"
    await _make_repository(session_factory, full_name=full_name, github_installation_id=1)
    settings = _settings(test_private_key=test_private_key, GLOBAL_REVIEW_PROCESSING_ENABLED=False)

    async with session_factory() as session:
        report = await run_preflight(session, settings=settings, repository_full_name=full_name)

    assert report.outcome is PreflightOutcome.BLOCKED
    eligibility_check = next(c for c in report.checks if c.name == "eligibility")
    assert eligibility_check.status is DoctorStatus.FAIL
    assert "GLOBAL_REVIEW_PROCESSING_ENABLED" in eligibility_check.detail


async def test_repository_not_selected_is_blocked(
    session_factory: async_sessionmaker[AsyncSession], test_private_key: str
) -> None:
    full_name = f"test/preflight-not-selected-{uuid.uuid4().hex[:8]}"
    await _make_repository(session_factory, full_name=full_name, github_installation_id=2, is_selected=False)
    settings = _settings(test_private_key=test_private_key)

    async with session_factory() as session:
        report = await run_preflight(session, settings=settings, repository_full_name=full_name)

    assert report.outcome is PreflightOutcome.BLOCKED
    eligibility_check = next(c for c in report.checks if c.name == "eligibility")
    assert eligibility_check.status is DoctorStatus.FAIL


async def test_suspended_installation_is_blocked(
    session_factory: async_sessionmaker[AsyncSession], test_private_key: str
) -> None:
    full_name = f"test/preflight-suspended-{uuid.uuid4().hex[:8]}"
    await _make_repository(
        session_factory, full_name=full_name, github_installation_id=3, installation_status=InstallationStatus.SUSPENDED
    )
    settings = _settings(test_private_key=test_private_key)

    async with session_factory() as session:
        report = await run_preflight(session, settings=settings, repository_full_name=full_name)

    assert report.outcome is PreflightOutcome.BLOCKED


async def test_beta_pending_is_blocked(
    session_factory: async_sessionmaker[AsyncSession], test_private_key: str
) -> None:
    full_name = f"test/preflight-beta-pending-{uuid.uuid4().hex[:8]}"
    await _make_repository(session_factory, full_name=full_name, github_installation_id=4, beta_state=BetaState.PENDING)
    settings = _settings(test_private_key=test_private_key)

    async with session_factory() as session:
        report = await run_preflight(session, settings=settings, repository_full_name=full_name)

    assert report.outcome is PreflightOutcome.BLOCKED


async def test_eligible_but_all_publish_gates_closed_is_dry_run(
    session_factory: async_sessionmaker[AsyncSession], test_private_key: str
) -> None:
    full_name = f"test/preflight-dry-run-{uuid.uuid4().hex[:8]}"
    await _make_repository(session_factory, full_name=full_name, github_installation_id=5, publication_allowed=False)
    settings = _settings(test_private_key=test_private_key, GLOBAL_PUBLICATION_ENABLED=False)

    async with session_factory() as session:
        report = await run_preflight(
            session,
            settings=settings,
            repository_full_name=full_name,
            github_client=_RaisingGitHubClient(),  # type: ignore[arg-type]
        )

    assert report.outcome is PreflightOutcome.DRY_RUN
    assert next(c for c in report.checks if c.name == "publish_gate:global").status is DoctorStatus.WARN
    assert next(c for c in report.checks if c.name == "publish_gate:installation").status is DoctorStatus.WARN
    repo_gate = next(c for c in report.checks if c.name == "publish_gate:repository")
    assert repo_gate.status is DoctorStatus.WARN
    assert "could not resolve" in repo_gate.detail


async def test_unreachable_repository_config_never_assumed_open(
    session_factory: async_sessionmaker[AsyncSession], test_private_key: str
) -> None:
    """An unresolvable .patchfrog.yml must never be silently treated as
    publish.enabled=True -- fail closed, always DRY_RUN, never PUBLISH,
    even if the other two gates are wide open."""

    full_name = f"test/preflight-unreachable-{uuid.uuid4().hex[:8]}"
    await _make_repository(session_factory, full_name=full_name, github_installation_id=6, publication_allowed=True)
    settings = _settings(test_private_key=test_private_key, GLOBAL_PUBLICATION_ENABLED=True)

    async with session_factory() as session:
        report = await run_preflight(
            session,
            settings=settings,
            repository_full_name=full_name,
            github_client=_RaisingGitHubClient(),  # type: ignore[arg-type]
        )

    assert report.outcome is PreflightOutcome.DRY_RUN


async def test_all_gates_confirmed_open_is_publish(
    session_factory: async_sessionmaker[AsyncSession], test_private_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    full_name = f"test/preflight-publish-{uuid.uuid4().hex[:8]}"
    await _make_repository(session_factory, full_name=full_name, github_installation_id=7, publication_allowed=True)
    settings = _settings(test_private_key=test_private_key, GLOBAL_PUBLICATION_ENABLED=True)

    class _FakeClient:
        async def get_default_branch_head_sha(self, *, installation_id: int, owner: str, repository: str) -> str:
            return "a" * 40

    async def _fake_resolve(**kwargs: object) -> PublicationConfig:
        return PublicationConfig(enabled=True)

    async def _fake_get_token(self: object, installation_id: int) -> str:
        return "fake-token-not-real"

    monkeypatch.setattr("patchfrog.ops.preflight.resolve_repository_publication_config", _fake_resolve)
    monkeypatch.setattr("patchfrog.github.auth.InstallationTokenProvider.get_token", _fake_get_token)

    async with session_factory() as session:
        report = await run_preflight(
            session, settings=settings, repository_full_name=full_name, github_client=_FakeClient()  # type: ignore[arg-type]
        )

    assert report.outcome is PreflightOutcome.PUBLISH
    assert all(c.status is DoctorStatus.PASS for c in report.checks)


async def test_preflight_never_calls_a_provider_or_mutates_state(
    session_factory: async_sessionmaker[AsyncSession], test_private_key: str
) -> None:
    """No LLMProvider is imported anywhere in patchfrog.ops.preflight --
    structural proof, not just a behavioral assertion (see doctor's own
    analogous test for the same reasoning)."""

    import patchfrog.ops.preflight as preflight_module

    source = preflight_module.__file__
    assert source is not None
    text = Path(source).read_text()
    assert "LLMProvider" not in text
    assert "generate_structured" not in text

    full_name = f"test/preflight-no-mutation-{uuid.uuid4().hex[:8]}"
    await _make_repository(session_factory, full_name=full_name, github_installation_id=8)
    settings = _settings(test_private_key=test_private_key)

    async with session_factory() as session:
        before = await RepositoryRepository().get_by_github_id(session, github_repository_id=abs(hash(full_name)) % (2**62))
    assert before is not None
    before_updated_at = before.updated_at

    async with session_factory() as session:
        await run_preflight(
            session, settings=settings, repository_full_name=full_name, github_client=_RaisingGitHubClient()  # type: ignore[arg-type]
        )

    async with session_factory() as session:
        after = await RepositoryRepository().get_by_github_id(session, github_repository_id=abs(hash(full_name)) % (2**62))
    assert after is not None
    assert after.updated_at == before_updated_at
