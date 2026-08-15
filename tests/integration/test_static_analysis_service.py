from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.analyzers.base import AnalyzerAvailability, AnalyzerDiscoveryResult
from patchfrog.analysis.analyzers.registry import AnalyzerRegistry
from patchfrog.analysis.domain import (
    AnalysisContext,
    AnalysisRunResultStatus,
    AnalyzerCapabilities,
    AnalyzerResult,
)
from patchfrog.analysis.service import StaleIndexError, StaticAnalysisService
from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.domain.code import Language
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.analysis import AnalysisRunModel, AnalysisRunStatus, FindingModel
from patchfrog.persistence.repositories import FindingRepository, RepositoryRepository
from tests.support.git_repo import commit_all, materialize_fixture_repo


async def _create_repository(
    session_factory: async_sessionmaker[AsyncSession], *, full_name: str
) -> uuid.UUID:
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


async def _index(
    session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, root_path: Path, full_name: str
) -> None:
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root_path, repository_full_name=full_name
    )


async def test_analyze_python_fixture_finds_and_dedups_bare_except(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python")
    repository_id = await _create_repository(session_factory, full_name="test/static-python")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/static-python")

    service = StaticAnalysisService(session_factory=session_factory)
    summary = await service.analyze_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/static-python"
    )

    assert summary.status is AnalysisRunResultStatus.SUCCEEDED
    assert summary.analyzers_succeeded >= 1
    assert summary.findings_count > 0
    assert summary.reused_existing_run is False

    async with session_factory() as session:
        run = (await session.execute(select(AnalysisRunModel).where(AnalysisRunModel.repository_id == repository_id))).scalar_one()
        findings = await FindingRepository().list_for_run(session, analysis_run_id=run.id)

    bare_except = next(f for f in findings if "bare" in f.rule_id.lower() or f.rule_id == "E722")
    # ruff (E722) and semgrep (patchfrog-python-bare-except) both flag this
    # construct -- must be deduplicated into one finding with both sources.
    from patchfrog.persistence.repositories import FindingSourceRepository

    async with session_factory() as session:
        sources = await FindingSourceRepository().list_for_finding(session, finding_id=bare_except.id)
    assert {s.analyzer for s in sources} == {"ruff", "semgrep"}


async def test_stale_index_protection_blocks_mismatched_commit(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python")
    repository_id = await _create_repository(session_factory, full_name="test/stale-index")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/stale-index")

    # Advance the repo one commit past what was indexed.
    (snapshot.root_path / "extra.py").write_text("def extra():\n    pass\n")
    commit_all(snapshot.root_path, "advance past indexed commit")

    service = StaticAnalysisService(session_factory=session_factory)
    with pytest.raises(StaleIndexError):
        await service.analyze_local_repository(
            repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/stale-index"
        )


async def test_no_index_at_all_raises_stale_index_error(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python")
    repository_id = await _create_repository(session_factory, full_name="test/no-index")

    service = StaticAnalysisService(session_factory=session_factory)
    with pytest.raises(StaleIndexError):
        await service.analyze_local_repository(
            repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/no-index"
        )


async def test_repeated_analysis_of_same_commit_reuses_existing_run(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python")
    repository_id = await _create_repository(session_factory, full_name="test/idempotent")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/idempotent")

    service = StaticAnalysisService(session_factory=session_factory)
    first = await service.analyze_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/idempotent"
    )
    second = await service.analyze_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/idempotent"
    )

    assert first.reused_existing_run is False
    assert second.reused_existing_run is True
    assert second.findings_count == first.findings_count

    async with session_factory() as session:
        runs = (
            await session.execute(select(AnalysisRunModel).where(AnalysisRunModel.repository_id == repository_id))
        ).scalars().all()
    # Only one run row was ever created -- no duplicate on retry.
    assert len(runs) == 1


async def test_changed_line_classification_marks_findings_correctly(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python")
    repository_id = await _create_repository(session_factory, full_name="test/changed-lines")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/changed-lines")

    # bad.py's bare `except:` is on line 8. Simulate a PR diff that added
    # exactly that line (and only that line).
    diff_file = DiffFile(
        path="bad.py",
        hunks=(
            DiffHunk(
                old_start=8, old_lines=1, new_start=8, new_lines=1, section_heading=None,
                lines=(DiffLine(line_type=DiffLineType.ADDITION, old_line_number=None, new_line_number=8, content="    except:"),),
            ),
        ),
    )

    service = StaticAnalysisService(session_factory=session_factory)
    summary = await service.analyze_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/changed-lines",
        diff_files=[diff_file],
    )
    assert summary.status is AnalysisRunResultStatus.SUCCEEDED

    async with session_factory() as session:
        run = (await session.execute(select(AnalysisRunModel).where(AnalysisRunModel.repository_id == repository_id))).scalar_one()
        findings = await FindingRepository().list_for_run(session, analysis_run_id=run.id)

    by_rule = {f.rule_id: f for f in findings}
    assert by_rule["E722"].is_on_changed_line is True
    # F401 (unused import `os`) is on line 1, not touched by the diff, but
    # still inside... actually it's module-level, so it's neither on a
    # changed line nor in a changed symbol; the bare-except's containing
    # function should be marked changed-symbol too.
    assert by_rule["F401"].is_on_changed_line is False


async def test_one_analyzer_crashing_produces_partial_not_total_failure(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python")
    repository_id = await _create_repository(session_factory, full_name="test/partial")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/partial")

    class _CrashingAnalyzer:
        capabilities = AnalyzerCapabilities(
            name="crasher", languages=frozenset({Language.PYTHON}), requires_compile_database=False,
            supports_file_scope=True, supports_repository_scope=True, supports_changed_files_only=True,
            supports_structured_output=True,
        )

        async def discover(self) -> AnalyzerDiscoveryResult:
            return AnalyzerDiscoveryResult(availability=AnalyzerAvailability.AVAILABLE, version="0.0.0")

        async def analyze(self, context: AnalysisContext) -> AnalyzerResult:
            raise RuntimeError("boom")

    registry = AnalyzerRegistry()
    registry.register("crasher", _CrashingAnalyzer())
    from patchfrog.analysis.analyzers.ruff import RuffAnalyzer

    registry.register("ruff", RuffAnalyzer())

    # A real .patchfrog.yml in the checkout root -- simpler and more
    # realistic than monkeypatching config loading.
    (snapshot.root_path / ".patchfrog.yml").write_text("analysis:\n  enabled: [crasher, ruff]\n")

    service = StaticAnalysisService(session_factory=session_factory, analyzer_registry=registry)
    summary = await service.analyze_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/partial"
    )

    assert summary.status is AnalysisRunResultStatus.PARTIAL
    assert summary.analyzers_failed == 1
    assert summary.analyzers_succeeded == 1


async def test_failed_run_does_not_leave_orphaned_findings(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A run that crashes mid-persistence must roll back cleanly -- no
    partial findings exposed under a run that never completed."""

    snapshot = materialize_fixture_repo(tmp_path / "repo", "static_python")
    repository_id = await _create_repository(session_factory, full_name="test/rollback")
    await _index(session_factory, repository_id=repository_id, root_path=snapshot.root_path, full_name="test/rollback")

    from patchfrog.persistence.repositories import FindingRepository as RealFindingRepository

    service = StaticAnalysisService(session_factory=session_factory)

    class _RaisingFindingRepository(RealFindingRepository):
        async def bulk_create(self, session: AsyncSession, models: Sequence[FindingModel]) -> None:
            raise RuntimeError("simulated persistence failure")

    service._finding_repo = _RaisingFindingRepository()

    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        await service.analyze_local_repository(
            repository_id=repository_id, root_path=snapshot.root_path, repository_full_name="test/rollback"
        )

    async with session_factory() as session:
        run = (await session.execute(select(AnalysisRunModel).where(AnalysisRunModel.repository_id == repository_id))).scalar_one()
        assert run.status is AnalysisRunStatus.FAILED
        leftover = await FindingRepository().list_for_run(session, analysis_run_id=run.id)
        assert leftover == []
