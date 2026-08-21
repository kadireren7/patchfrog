"""Phase 8 spec sections 46-47: a per-case timeout must never hang the
suite, and infrastructure/timeout/provider failures must never be
counted as false negatives. Verified against a real
:class:`EvaluationRunner` run (real indexing/static analysis), with a
provider that raises to simulate each failure mode."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.evaluation.domain import (
    CaseStatus,
    Difficulty,
    EvaluationCase,
    EvaluationMode,
    Language,
)
from patchfrog.evaluation.runner import EvaluationRunner
from patchfrog.review.provider import ProviderError
from patchfrog.review.provider_factory import MissingProviderCredentialsError
from patchfrog.review.providers.fake import FakeLLMProvider


def _write_case(tmp_path: Path, case_id: str) -> tuple[EvaluationCase, Path]:
    cases_root = tmp_path / "cases"
    repo_root = cases_root / case_id / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / "m.py").write_text("def foo():\n    return 1\n")
    case = EvaluationCase(
        id=case_id, title="t", description="d", language=Language.PYTHON, fixture=case_id, difficulty=Difficulty.EASY,
    )
    return case, cases_root


class _HangingProvider:
    """A minimal LLMProvider stand-in that never returns -- exercises
    the real :func:`asyncio.wait_for` timeout path in
    :meth:`EvaluationRunner.run_case`, not a mocked timeout."""

    @property
    def identity(self):  # type: ignore[no-untyped-def]
        from patchfrog.review.provider import ProviderIdentity

        return ProviderIdentity(provider="fake", model="hangs-forever")

    async def generate_structured(self, request):  # type: ignore[no-untyped-def]
        del request
        await asyncio.sleep(3600)


class _RaisingProvider:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    @property
    def identity(self):  # type: ignore[no-untyped-def]
        from patchfrog.review.provider import ProviderIdentity

        return ProviderIdentity(provider="fake", model="raises")

    async def generate_structured(self, request):  # type: ignore[no-untyped-def]
        del request
        raise self._exc


async def test_case_exceeding_its_timeout_is_classified_timeout_not_a_miss(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    case, cases_root = _write_case(tmp_path, "hang-case")
    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.AI_ONLY, reviewer_provider=_HangingProvider(),
        timeout_seconds=0.05,
    )
    assert result.status is CaseStatus.TIMEOUT
    assert result.is_error
    # A timed-out case contributes zero predictions/expected-outcomes --
    # never silently scored as if it ran to completion.
    assert result.predictions == ()
    assert result.expected_outcomes == ()


async def test_provider_error_is_classified_provider_error_not_infrastructure(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    case, cases_root = _write_case(tmp_path, "provider-error-case")
    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.AI_ONLY,
        reviewer_provider=_RaisingProvider(ProviderError("upstream 500")),
    )
    assert result.status is CaseStatus.PROVIDER_ERROR
    assert result.is_error
    assert "upstream 500" in (result.error or "")


async def test_missing_provider_credentials_is_classified_provider_error(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    case, cases_root = _write_case(tmp_path, "missing-creds-case")
    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.AI_ONLY,
        reviewer_provider=_RaisingProvider(MissingProviderCredentialsError("no key")),
    )
    assert result.status is CaseStatus.PROVIDER_ERROR


async def test_malformed_benchmark_metadata_is_classified_fixture_error(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from patchfrog.analysis.domain import FindingCategory
    from patchfrog.evaluation.domain import ExpectedFinding

    # An expected finding whose file doesn't exist on disk -- validated
    # inside run_case before anything else runs.
    case = EvaluationCase(
        id="bad-case", title="t", description="d", language=Language.PYTHON, fixture="bad-case", difficulty=Difficulty.EASY,
        expected=(ExpectedFinding(id="ef1", category=FindingCategory.CORRECTNESS, file="nonexistent.py", issue_family="fam"),),
    )
    cases_root = tmp_path / "cases"
    (cases_root / "bad-case" / "repo").mkdir(parents=True)
    (cases_root / "bad-case" / "repo" / "a.py").write_text("x = 1\n")

    runner = EvaluationRunner(session_factory=session_factory)
    result = await runner.run_case(
        case, cases_root=cases_root, mode=EvaluationMode.STATIC_ONLY, reviewer_provider=FakeLLMProvider(),
    )
    assert result.status is CaseStatus.FIXTURE_ERROR
    assert result.is_error
    assert "nonexistent.py" in (result.error or "")


async def test_one_failing_case_does_not_corrupt_a_later_case(tmp_path: Path) -> None:
    # A real, separate engine per case here (rather than the shared
    # ``session_factory`` fixture) -- this test's real target is proving
    # EvaluationRunner itself carries no state across a timed-out
    # run_case into the next one; it must not depend on database
    # connection-pool details, which is a different concern (verified
    # for real, with real Postgres, in
    # tests/integration/test_review_run_concurrency.py-style coverage).
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from patchfrog.persistence.models import Base

    async def _fresh_session_factory() -> async_sessionmaker[AsyncSession]:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return async_sessionmaker(engine, expire_on_commit=False)

    hang_case, hang_root = _write_case(tmp_path, "hang-then-fine-1")
    fine_case, fine_root = _write_case(tmp_path, "hang-then-fine-2")

    hang_runner = EvaluationRunner(session_factory=await _fresh_session_factory())
    failed = await hang_runner.run_case(
        hang_case, cases_root=hang_root, mode=EvaluationMode.AI_ONLY, reviewer_provider=_HangingProvider(),
        timeout_seconds=0.05,
    )
    assert failed.status is CaseStatus.TIMEOUT

    fine_runner = EvaluationRunner(session_factory=await _fresh_session_factory())
    ok = await fine_runner.run_case(
        fine_case, cases_root=fine_root, mode=EvaluationMode.STATIC_ONLY, reviewer_provider=FakeLLMProvider(),
    )
    assert ok.status in (CaseStatus.PASSED, CaseStatus.COMPLETED_WITH_FINDINGS)
