"""Celery task: build a deterministic context bundle for a finding.

Thin adapter only — resolves the installation access token and the
target's exact repository/commit from the finding itself, then delegates
all real work to :class:`patchfrog.context.service.ContextService`. Kept
as its own task (not folded into ``patchfrog.analyze_repository``) so
context generation scales and fails independently — Phase 2's repository
index for the exact commit under analysis must already exist (see
:class:`patchfrog.context.service.StaleContextIndexError`).

Nothing here triggers automatically on a webhook yet — for Phase 4 this
is invoked explicitly, per the "controlled trigger" requirement (no
automatic PR comments, no automatic blocking, no LLM involvement).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import structlog

from apps.worker.celery_app import celery_app
from patchfrog.config.settings import Settings, get_settings
from patchfrog.context.domain import ContextBundle, ContextTargetType
from patchfrog.context.service import ContextService
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.persistence.database import create_engine, create_session_factory
from patchfrog.persistence.models.analysis import AnalysisRunModel, FindingModel
from patchfrog.persistence.repositories import RepositoryRepository

logger = structlog.get_logger(__name__)


async def _build_context(
    *,
    finding_id: uuid.UUID,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
    settings: Settings,
) -> ContextBundle:
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

            finding = await session.get(FindingModel, finding_id)
            if finding is None:
                raise ValueError(f"no finding with id {finding_id}")
            analysis_run = await session.get(AnalysisRunModel, finding.analysis_run_id)
            if analysis_run is None:
                raise ValueError(f"no analysis run with id {finding.analysis_run_id}")
            commit_sha = analysis_run.commit_sha

        async with httpx.AsyncClient(timeout=settings.github_api_timeout_seconds) as http_client:
            token_provider = InstallationTokenProvider(
                http_client=http_client,
                app_id=settings.github_app_id,
                private_key=settings.github_private_key,
                api_base_url=settings.github_api_base_url,
            )
            token = await token_provider.get_token(installation_id)

        service = ContextService(session_factory=session_factory)
        return await service.build_context(
            repository_id=repository_id,
            clone_url=f"https://github.com/{full_name}.git",
            commit_sha=commit_sha,
            repository_full_name=full_name,
            token=token,
            target_type=ContextTargetType.FINDING,
            file_path=finding.file_path,
            line=finding.start_line,
            symbol_id=finding.symbol_id,
            finding_id=finding.id,
            analysis_run_id=finding.analysis_run_id,
        )
    finally:
        await engine.dispose()


@celery_app.task(name="patchfrog.build_context")  # type: ignore[untyped-decorator]
def build_context_task(
    *,
    finding_id: str,
    github_repository_id: int,
    owner: str,
    name: str,
    full_name: str,
    installation_id: int,
) -> str:
    bundle = asyncio.run(
        _build_context(
            finding_id=uuid.UUID(finding_id),
            github_repository_id=github_repository_id,
            owner=owner,
            name=name,
            full_name=full_name,
            installation_id=installation_id,
            settings=get_settings(),
        )
    )
    logger.info(
        "context_task_completed",
        repository=full_name,
        finding_id=finding_id,
        selected_count=bundle.metrics.selected_count,
        total_tokens=bundle.total_tokens_estimate,
        reused_existing_bundle=bundle.reused_existing_bundle,
    )
    return f"selected={bundle.metrics.selected_count} tokens={bundle.total_tokens_estimate}"
