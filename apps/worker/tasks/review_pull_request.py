"""Celery task: run the AI reviewer against one pull request.

Thin adapter only — resolves the installation access token, re-fetches
the PR's changed files from GitHub (diff hunks are not persisted by
ingestion; see :mod:`patchfrog.services.pull_request_ingestion`), resolves
the repository's *exact-commit* ``.patchfrog.yml`` review config (see
:mod:`patchfrog.review.config_resolution` -- the same function the CLI
uses, so the two paths can never diverge in what config a given
repository/commit resolves to), and delegates all real work to
:class:`patchfrog.review.service.PullRequestReviewService`. Phase 2's
repository index for the exact commit under review must already exist
(see :class:`patchfrog.review.service.StaleReviewIndexError`) -- the same
"analysis/context/review commit SHA == repository index commit SHA"
invariant Phases 3 and 4 established.

Never posts a GitHub comment, never applies a suggested fix, never
commits anything -- this task's only side effect is persisting rows via
:class:`~patchfrog.review.service.PullRequestReviewService`. Posting
review results to a PR is an explicit Phase 5 non-goal; see the Phase 5
PR description.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import structlog

from apps.worker.celery_app import celery_app
from patchfrog.config.settings import Settings, get_settings
from patchfrog.diff.parser import build_diff_file
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.github.client import GitHubClient
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.persistence.repositories import PullRequestRepository, RepositoryRepository
from patchfrog.review.config import MalformedReviewConfigError
from patchfrog.review.config_resolution import resolve_repository_review_config
from patchfrog.review.domain import ReviewRunSummary
from patchfrog.review.provider_factory import build_critic_provider, build_reviewer_provider
from patchfrog.review.service import PullRequestReviewService, persist_malformed_config_failure

logger = structlog.get_logger(__name__)


async def _review_pull_request(
    *,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
    pull_request_number: int,
    head_sha: str,
    settings: Settings,
) -> ReviewRunSummary:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            repository_row = await RepositoryRepository().upsert(
                session,
                github_repository_id=github_repository_id,
                owner=owner,
                name=name,
                full_name=full_name,
                installation_id=installation_id,
            )
            await session.commit()
            repository_id = repository_row.id

            pull_request_row = await PullRequestRepository().get_by_repository_and_number(
                session, repository_id=repository_id, github_pr_number=pull_request_number
            )
            pull_request_id: uuid.UUID | None = pull_request_row.id if pull_request_row is not None else None

        async with httpx.AsyncClient(timeout=settings.github_api_timeout_seconds) as http_client:
            token_provider = InstallationTokenProvider(
                http_client=http_client,
                app_id=settings.github_app_id,
                private_key=settings.github_private_key,
                api_base_url=settings.github_api_base_url,
            )
            token = await token_provider.get_token(installation_id)

            github_client = GitHubClient(
                http_client=http_client,
                token_provider=token_provider,
                api_base_url=settings.github_api_base_url,
                timeout_seconds=settings.github_api_timeout_seconds,
            )
            changed_files = await github_client.list_pull_request_files(
                installation_id=installation_id,
                ref=PullRequestRef(owner=owner, repository=name, number=pull_request_number),
            )

        diff_files = [build_diff_file(f.path, f.patch) for f in changed_files]

        clone_url = f"https://github.com/{full_name}.git"
        try:
            review_config = await resolve_repository_review_config(
                local=False,
                commit_sha=head_sha,
                repository_full_name=full_name,
                clone_url=clone_url,
                token=token,
            )
        except MalformedReviewConfigError as exc:
            await persist_malformed_config_failure(
                session_factory,
                repository_id=repository_id,
                commit_sha=head_sha,
                pull_request_id=pull_request_id,
                exc=exc,
            )
            raise

        reviewer_provider = build_reviewer_provider(review_config, settings=settings)
        critic_provider = build_critic_provider(review_config, settings=settings)

        service = PullRequestReviewService(
            session_factory=session_factory,
            reviewer_provider=reviewer_provider,
            critic_provider=critic_provider,
        )
        return await service.review_pull_request(
            repository_id=repository_id,
            clone_url=clone_url,
            commit_sha=head_sha,
            repository_full_name=full_name,
            token=token,
            diff_files=diff_files,
            pull_request_id=pull_request_id,
            config=review_config,
        )
    finally:
        await engine.dispose()


@celery_app.task(name="patchfrog.review_pull_request")  # type: ignore[untyped-decorator]
def review_pull_request_task(
    *,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
    pull_request_number: int,
    head_sha: str,
) -> str:
    summary = asyncio.run(
        _review_pull_request(
            github_repository_id=github_repository_id,
            owner=owner,
            name=name,
            full_name=full_name,
            installation_id=installation_id,
            pull_request_number=pull_request_number,
            head_sha=head_sha,
            settings=get_settings(),
        )
    )
    logger.info(
        "review_task_completed",
        repository=full_name,
        pull_request_number=pull_request_number,
        status=summary.status.value,
        accepted_count=summary.accepted_count,
        reused_existing_run=summary.reused_existing_run,
    )
    return (
        f"status={summary.status.value} accepted={summary.accepted_count} "
        f"rejected={summary.rejected_count} reviewed={summary.candidates_reviewed}"
    )
