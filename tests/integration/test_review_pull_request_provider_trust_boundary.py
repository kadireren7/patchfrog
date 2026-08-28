"""Milestone C (operator-controlled provider runtime) end-to-end
regression coverage at the production Celery task boundary: a
repository's committed ``.patchfrog.yml`` trying to select
provider/model/critic model/timeout must never reach provider
construction at all -- ``_review_pull_request`` must fail with
``MalformedReviewConfigError`` (from repository config resolution,
which always runs in ``on_malformed="raise"`` mode for a real review
attempt) *before* ``build_reviewer_provider``/``build_critic_provider``
are ever called. This is the strongest form of the guarantee that a PR
changing ``.patchfrog.yml`` can never change which provider/model
actually runs: the repo-supplied selection never even gets as far as
provider construction.

Runs against a real SQLite *file* database (not ``:memory:``) because
``_review_pull_request`` creates its own engine directly from
``settings.database_url``, independent of any session fixture (same
pattern as ``test_review_pull_request_supersession.py``). GitHub network
calls are stubbed at the client-method boundary; the repository "clone"
is a real local git repository used as ``clone_url`` (git natively
supports a local path as a remote), so the repository config resolution
path exercised here is the real one, not a mock of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

import apps.worker.tasks.review_pull_request as worker_task_module
from apps.worker.tasks.review_pull_request import _review_pull_request
from patchfrog.config.settings import Settings
from patchfrog.domain.pull_request import ChangedFile, PullRequestMetadata
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.github.client import GitHubClient
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.persistence.models import Base
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.repository.snapshot import RepositorySnapshot, RepositorySnapshotProvider
from patchfrog.review.config import MalformedReviewConfigError
from tests.support.git_repo import commit_all, materialize_fixture_repo

_GITHUB_INSTALLATION_ID = 11223344
_GITHUB_REPOSITORY_ID = 445566


def _settings(database_url: str) -> Settings:
    base: dict[str, Any] = {
        "DATABASE_URL": database_url,
        "REDIS_URL": "redis://localhost:6379/0",
        "GITHUB_APP_ID": "1",
        "GITHUB_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        "GITHUB_WEBHOOK_SECRET": "x",
    }
    return Settings(**base)


async def _fake_get_token(self: InstallationTokenProvider, installation_id: int) -> str:
    return "fake-installation-token"


def _fake_get_pull_request_for(head_sha: str) -> Any:
    async def _fake(self: GitHubClient, *, installation_id: int, ref: Any) -> PullRequestMetadata:
        return PullRequestMetadata(
            number=ref.number, title="t", body=None, author="kadireren7", base_branch="main",
            head_branch="feature", base_sha="c" * 40, head_sha=head_sha,
            html_url="https://github.com/kadireren7/patchfrog-trust-boundary/pull/1",
            state="open", merged=False,
        )
    return _fake


async def _fake_list_pull_request_files(
    self: GitHubClient, *, installation_id: int, ref: Any
) -> list[ChangedFile]:
    return []


def _local_snapshot_acquire_for(root_path: Path) -> Any:
    """The worker task always builds a hardcoded `https://github.com/...`
    `clone_url` -- redirect the actual git fetch to the real local
    fixture repository instead, so this test still exercises the real
    config-resolution code path (`RepositorySnapshotProvider.acquire_local`,
    not a mock of config resolution itself) without a real network
    clone."""

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


async def test_repo_committed_provider_field_never_reaches_provider_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "trust_boundary.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    setup_engine = create_engine(database_url)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    setup_session_factory = create_session_factory(setup_engine)

    snapshot = materialize_fixture_repo(
        tmp_path / "repo", "ai_review_python", full_name="kadireren7/patchfrog-trust-boundary"
    )
    (snapshot.root_path / ".patchfrog.yml").write_text(
        "review:\n  provider: gemini\n  model: gemini-3.6-flash\n"
    )
    commit_sha = commit_all(snapshot.root_path, "attempt to select provider via .patchfrog.yml")

    # persist_malformed_config_failure (invoked by the task once
    # MalformedReviewConfigError is raised) itself requires a matching
    # repository index to exist -- same production invariant as a
    # successful review, unrelated to this test's actual concern -- so a
    # real index must exist for the assertions below to actually reach
    # the trust-boundary error rather than a StaleReviewIndexError.
    async with setup_session_factory() as session:
        repository_row = await RepositoryRepository().upsert(
            session, github_repository_id=_GITHUB_REPOSITORY_ID, owner="kadireren7",
            name="patchfrog-trust-boundary", full_name="kadireren7/patchfrog-trust-boundary",
            installation_id=_GITHUB_INSTALLATION_ID,
        )
        await session.commit()
        repository_id = repository_row.id
    await RepositoryIndexingService(session_factory=setup_session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path,
        repository_full_name="kadireren7/patchfrog-trust-boundary",
    )
    await setup_engine.dispose()

    monkeypatch.setattr(InstallationTokenProvider, "get_token", _fake_get_token)
    monkeypatch.setattr(GitHubClient, "get_pull_request", _fake_get_pull_request_for(commit_sha))
    monkeypatch.setattr(GitHubClient, "list_pull_request_files", _fake_list_pull_request_files)
    monkeypatch.setattr(
        RepositorySnapshotProvider, "acquire", _local_snapshot_acquire_for(snapshot.root_path)
    )

    def _must_not_be_called(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "build_reviewer_provider/build_critic_provider must never be called when "
            "repository config resolution fails -- a malicious .patchfrog.yml must never "
            "get as far as provider construction"
        )

    monkeypatch.setattr(worker_task_module, "build_reviewer_provider", _must_not_be_called)
    monkeypatch.setattr(worker_task_module, "build_critic_provider", _must_not_be_called)

    with pytest.raises(MalformedReviewConfigError, match="no longer repository-controlled"):
        await _review_pull_request(
            github_repository_id=_GITHUB_REPOSITORY_ID,
            owner="kadireren7",
            name="patchfrog-trust-boundary",
            full_name="kadireren7/patchfrog-trust-boundary",
            installation_id=_GITHUB_INSTALLATION_ID,
            pull_request_number=1,
            head_sha=commit_sha,
            settings=_settings(database_url),
        )


async def test_repo_config_without_operator_fields_reaches_provider_construction_with_operator_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control case: a repository config with only behavior fields (no
    operator-only fields) must resolve cleanly and reach provider
    construction using the *operator's* runtime config (default
    anthropic/claude-opus-5 here, since no PATCHFROG_REVIEW_* env is
    set) -- never anything derived from the repository."""

    db_path = tmp_path / "trust_boundary_control.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    setup_engine = create_async_engine(database_url)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    snapshot = materialize_fixture_repo(
        tmp_path / "repo", "ai_review_python", full_name="kadireren7/patchfrog-trust-boundary-control"
    )
    (snapshot.root_path / ".patchfrog.yml").write_text("review:\n  max_candidates: 3\n")
    commit_sha = commit_all(snapshot.root_path, "behavior-only config")

    monkeypatch.setattr(InstallationTokenProvider, "get_token", _fake_get_token)
    monkeypatch.setattr(GitHubClient, "get_pull_request", _fake_get_pull_request_for(commit_sha))
    monkeypatch.setattr(GitHubClient, "list_pull_request_files", _fake_list_pull_request_files)
    monkeypatch.setattr(
        RepositorySnapshotProvider, "acquire", _local_snapshot_acquire_for(snapshot.root_path)
    )

    class _StopAfterProviderResolution(Exception):
        pass

    captured: dict[str, Any] = {}

    def _capture_reviewer(runtime_config: Any, *, settings: Any) -> Any:
        captured["runtime_config"] = runtime_config
        raise _StopAfterProviderResolution()

    monkeypatch.setattr(worker_task_module, "build_reviewer_provider", _capture_reviewer)

    with pytest.raises(_StopAfterProviderResolution):
        await _review_pull_request(
            github_repository_id=_GITHUB_REPOSITORY_ID + 1,
            owner="kadireren7",
            name="patchfrog-trust-boundary-control",
            full_name="kadireren7/patchfrog-trust-boundary-control",
            installation_id=_GITHUB_INSTALLATION_ID,
            pull_request_number=1,
            head_sha=commit_sha,
            settings=_settings(database_url),
        )

    runtime_config = captured["runtime_config"]
    assert runtime_config.provider == "anthropic"
    assert runtime_config.model == "claude-opus-5"
