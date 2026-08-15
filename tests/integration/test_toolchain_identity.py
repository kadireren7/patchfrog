"""Regression coverage for analysis-run identity depending on the
*effective* analyzer toolchain, not just configuration intent.

Before this fix, canonical run reuse was keyed only on
``(repository_id, commit_sha, config_fingerprint)`` -- a successful run
for repository X + commit A + config C + ruff 0.16.3 was reusable as the
canonical result forever after, even once the effective analyzer
environment moved to ruff 0.17.x, because analyzer behavior/findings can
change without the repository or ``.patchfrog.yml`` changing at all. Each
test below reproduces the specific stale-reuse scenario the fix closes.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from patchfrog.analysis.analyzers.base import AnalyzerAvailability, AnalyzerDiscoveryResult
from patchfrog.analysis.analyzers.registry import AnalyzerRegistry
from patchfrog.analysis.analyzers.semgrep import SemgrepAnalyzer
from patchfrog.analysis.domain import (
    AnalysisContext,
    AnalysisRunResultStatus,
    AnalyzerCapabilities,
    AnalyzerExecutionStatus,
    AnalyzerResult,
)
from patchfrog.analysis.service import StaticAnalysisService
from patchfrog.domain.code import Language
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.analysis import AnalysisRunModel
from patchfrog.persistence.repositories import RepositoryRepository
from tests.support.git_repo import materialize_fixture_repo

_POSTGRES_URL = "postgresql+asyncpg://patchfrog:patchfrog@localhost:5432/patchfrog"


class _VersionedStubAnalyzer:
    """A minimal analyzer whose discovered version is controllable,
    simulating an analyzer binary being upgraded between two requests
    against the exact same repository/commit/config."""

    def __init__(self, *, version: str) -> None:
        self.version = version
        self.capabilities = AnalyzerCapabilities(
            name="stub", languages=frozenset({Language.PYTHON}), requires_compile_database=False,
            supports_file_scope=True, supports_repository_scope=True, supports_changed_files_only=True,
            supports_structured_output=True,
        )

    async def discover(self) -> AnalyzerDiscoveryResult:
        return AnalyzerDiscoveryResult(availability=AnalyzerAvailability.AVAILABLE, version=self.version)

    async def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        return AnalyzerResult(
            analyzer="stub", version=self.version, status=AnalyzerExecutionStatus.SUCCEEDED, duration_ms=0.0
        )


def _stub_registry(*, version: str) -> AnalyzerRegistry:
    registry = AnalyzerRegistry()
    registry.register("stub", _VersionedStubAnalyzer(version=version))
    return registry


async def _create_repository(session_factory: async_sessionmaker[AsyncSession], *, full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session,
            github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0],
            name=full_name.split("/")[-1],
            full_name=full_name,
            installation_id=0,
        )
        await session.commit()
        return row.id


async def _index(session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, root_path: Path, full_name: str) -> None:
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root_path, repository_full_name=full_name
    )


async def _succeeded_runs(session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID) -> list[AnalysisRunModel]:
    async with session_factory() as session:
        runs = (
            await session.execute(
                select(AnalysisRunModel).where(AnalysisRunModel.repository_id == repository_id)
            )
        ).scalars().all()
        return [r for r in runs if r.status.value == "succeeded"]


async def test_changing_only_analyzer_version_creates_a_distinct_canonical_run(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Requirements 1 + 2: reproduces the stale-reuse bug and verifies the
    fix -- an identical repo + commit + config against a different
    discovered analyzer version must never reuse a prior canonical run."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python")
    repository_id = await _create_repository(session_factory, full_name="test/toolchain-version")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/toolchain-version")
    (snapshot.root_path / ".patchfrog.yml").write_text("analysis:\n  enabled: [stub]\n")

    service_v1 = StaticAnalysisService(session_factory=session_factory, analyzer_registry=_stub_registry(version="1.0.0"))
    summary_v1 = await service_v1.analyze_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/toolchain-version"
    )
    assert summary_v1.status is AnalysisRunResultStatus.SUCCEEDED
    assert summary_v1.reused_existing_run is False

    service_v2 = StaticAnalysisService(session_factory=session_factory, analyzer_registry=_stub_registry(version="2.0.0"))
    summary_v2 = await service_v2.analyze_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/toolchain-version"
    )
    assert summary_v2.status is AnalysisRunResultStatus.SUCCEEDED
    assert summary_v2.reused_existing_run is False  # the core stale-reuse regression check

    succeeded = await _succeeded_runs(session_factory, repository_id=repository_id)
    assert len(succeeded) == 2
    assert len({r.toolchain_fingerprint for r in succeeded}) == 2  # distinct effective-toolchain identities
    assert len({r.config_fingerprint for r in succeeded}) == 1  # same configuration intent both times


async def test_identical_repo_commit_config_and_toolchain_reuses_safely(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Requirement 4: the flip side -- nothing about the effective
    toolchain changing must still reuse the canonical run rather than
    re-running analysis and duplicating data."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python")
    repository_id = await _create_repository(session_factory, full_name="test/toolchain-stable")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/toolchain-stable")
    (snapshot.root_path / ".patchfrog.yml").write_text("analysis:\n  enabled: [stub]\n")

    service_a = StaticAnalysisService(session_factory=session_factory, analyzer_registry=_stub_registry(version="1.0.0"))
    summary_a = await service_a.analyze_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/toolchain-stable"
    )
    assert summary_a.reused_existing_run is False

    # A fresh service instance (simulating a separate request) with the
    # exact same discovered analyzer version.
    service_b = StaticAnalysisService(session_factory=session_factory, analyzer_registry=_stub_registry(version="1.0.0"))
    summary_b = await service_b.analyze_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/toolchain-stable"
    )
    assert summary_b.reused_existing_run is True

    succeeded = await _succeeded_runs(session_factory, repository_id=repository_id)
    assert len(succeeded) == 1


async def test_changing_only_semgrep_ruleset_content_creates_a_distinct_canonical_run(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 3: PatchFrog's own bundled semgrep ruleset changing
    must invalidate a prior canonical run, exercised through the real
    SemgrepAnalyzer end-to-end (not a stub), the same way an actual
    ruleset edit between two deployments would."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python")
    repository_id = await _create_repository(session_factory, full_name="test/toolchain-ruleset")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/toolchain-ruleset")
    (snapshot.root_path / ".patchfrog.yml").write_text("analysis:\n  enabled: [semgrep]\n")

    rules_v1 = tmp_path / "rules_v1.yml"
    rules_v1.write_text("rules: []\n")
    rules_v2 = tmp_path / "rules_v2.yml"
    rules_v2.write_text("rules: []\n# a deliberately different, but still valid, empty ruleset\n")

    registry = AnalyzerRegistry()
    registry.register("semgrep", SemgrepAnalyzer())
    service = StaticAnalysisService(session_factory=session_factory, analyzer_registry=registry)

    monkeypatch.setattr("patchfrog.analysis.analyzers.semgrep.BUNDLED_RULES_PATH", rules_v1, raising=True)
    summary_v1 = await service.analyze_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/toolchain-ruleset"
    )
    assert summary_v1.reused_existing_run is False

    monkeypatch.setattr("patchfrog.analysis.analyzers.semgrep.BUNDLED_RULES_PATH", rules_v2, raising=True)
    summary_v2 = await service.analyze_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/toolchain-ruleset"
    )
    assert summary_v2.reused_existing_run is False

    succeeded = await _succeeded_runs(session_factory, repository_id=repository_id)
    assert len(succeeded) == 2
    assert len({r.toolchain_fingerprint for r in succeeded}) == 2


async def _postgres_available() -> AsyncEngine | None:
    engine = create_async_engine(_POSTGRES_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1 FROM analysis_runs LIMIT 1"))
    except (OperationalError, ProgrammingError):
        await engine.dispose()
        return None
    return engine


async def test_concurrent_identical_toolchain_identity_remains_race_safe_in_real_postgres(
    tmp_path: Path,
) -> None:
    """Requirement 5: two concurrent requests that land on the exact same
    effective toolchain identity (not just the same config) must still
    produce exactly one canonical succeeded run under real Postgres --
    the advisory lock is keyed by the combined identity now, not the old
    config-only one."""

    engine = await _postgres_available()
    if engine is None:
        pytest.skip("real PostgreSQL not reachable at localhost:5432 (docker compose up -d postgres)")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    full_name = f"toolchain-concurrency-test/{uuid.uuid4().hex[:8]}"
    repository_id: uuid.UUID | None = None

    try:
        repository_id = await _create_repository(session_factory, full_name=full_name)
        snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python", full_name=full_name)
        await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name=full_name)
        (snapshot.root_path / ".patchfrog.yml").write_text("analysis:\n  enabled: [stub]\n")

        service_a = StaticAnalysisService(session_factory=session_factory, analyzer_registry=_stub_registry(version="1.0.0"))
        service_b = StaticAnalysisService(session_factory=session_factory, analyzer_registry=_stub_registry(version="1.0.0"))

        results = await asyncio.gather(
            service_a.analyze_local_repository(
                repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
            ),
            service_b.analyze_local_repository(
                repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise AssertionError(f"concurrent analysis run raised unexpectedly: {result!r}")

        succeeded = await _succeeded_runs(session_factory, repository_id=repository_id)
        assert len(succeeded) == 1
    finally:
        if repository_id is not None:
            async with session_factory() as session:
                run_rows = (
                    await session.execute(
                        select(AnalysisRunModel).where(AnalysisRunModel.repository_id == repository_id)
                    )
                ).scalars().all()
                for row in run_rows:
                    await session.delete(row)
                await session.flush()
                await session.execute(text("DELETE FROM repository_indexes WHERE repository_id = :id"), {"id": str(repository_id)})
                await session.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": str(repository_id)})
                await session.commit()
        await engine.dispose()
