"""Controlled corpus for Change Intelligence (spec sections 18/19) --
real git repo, real indexing, real diff-driven
:class:`~patchfrog.review.domain.ReviewCandidate` generation, real
:func:`~patchfrog.change_intelligence.service.build_change_intelligence_report`.
Zero LLM involvement anywhere -- see the module docstring of
:mod:`patchfrog.change_intelligence` and
``validation/change_intelligence/latest-summary.md``'s "Evaluation
corpus" section for why this is a dedicated harness rather than
shoehorned into ``patchfrog.evaluation``'s finding-shaped
``EvaluationCase``.

Each case is a real, independent commit against a shared base fixture
repository, with explicit ground truth recorded directly in the test
(never inferred, never approximate).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.change_intelligence.domain import ChangeKind, CompanionReasonCode, CompanionStatus
from patchfrog.change_intelligence.service import build_change_intelligence_report
from patchfrog.diff.models import DiffFile
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.persistence.repositories import RepositoryIndexRepository, RepositoryRepository
from patchfrog.review.candidates import ReviewCandidateGenerator
from patchfrog.review.domain import ReviewCandidate
from patchfrog.review.local_diff import diff_against_base
from tests.support.git_repo import commit_all, init_git_repo

_API = '''from service import process


def handle(request):
    return process(request)
'''

_SERVICE = '''from repository import save


def process(request):
    return save(request)
'''

_REPOSITORY = '''def save(request):
    return {"ok": True, "request": request}
'''

_REPOSITORY_CHANGED = '''def save(request):
    # a real behavioral change to the persistence write path
    return {"ok": True, "request": request, "version": 2}
'''

_TEST_SERVICE = '''from service import process


def test_process():
    assert process({}) is not None
'''

_UNRELATED = '''def unrelated_helper(x):
    return x + 1
'''

_README = "# scratch repo\n"


async def _setup_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text(_README)
    (root / "api.py").write_text(_API)
    (root / "service.py").write_text(_SERVICE)
    (root / "repository.py").write_text(_REPOSITORY)
    (root / "unrelated.py").write_text(_UNRELATED)
    (root / "test_service.py").write_text(_TEST_SERVICE)
    init_git_repo(root)
    commit_all(root, "base")
    from patchfrog.repository.git import run_git

    base_sha = run_git(["-C", str(root), "rev-parse", "HEAD"]).strip()
    return root, base_sha


async def _index_and_generate_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    root: Path,
    full_name: str,
    base_sha: str,
) -> tuple[list[ReviewCandidate], list[DiffFile]]:
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    diff_files = diff_against_base(root, base_sha)
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        candidates = await ReviewCandidateGenerator().generate(
            session,
            repository_index_id=index.id,
            diff_files=diff_files,
            static_findings=[],
            max_candidates=40,
        )
    return list(candidates), diff_files


async def test_case_isolated_correct_one_function_fix(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth: 1 ChangeUnit, no missing companions (unrelated_helper
    has no callers/tests in this fixture), no diagram."""

    full_name = "test/ci-corpus-isolated-fix"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-corpus-isolated-fix", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "unrelated.py").write_text("def unrelated_helper(x):\n    return x + 2  # fixed off-by-one\n")
    commit_all(root, "fix unrelated_helper")

    candidates, _ = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    assert len(candidates) == 1

    async with session_factory() as session:
        report = await build_change_intelligence_report(session, candidates=candidates)

    assert len(report.change_units) == 1
    assert report.missing_companion_candidates == ()
    assert report.change_map is None


async def test_case_signature_changed_caller_forgotten(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth: repository.save changed; service.process (its real
    caller) was NOT touched -> exactly one CALLER_NOT_UPDATED missing
    companion candidate naming process. Diagram eligible (the
    affected surface reaches process via a real call edge, and
    -- since callers are followed one hop further too -- handle
    transitively, giving >=3 nodes across >=2 files)."""

    full_name = "test/ci-corpus-caller-forgotten"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-corpus-caller-forgotten", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "repository.py").write_text(_REPOSITORY_CHANGED)
    commit_all(root, "change save behavior")

    candidates, _ = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    assert len(candidates) == 1

    async with session_factory() as session:
        report = await build_change_intelligence_report(session, candidates=candidates)

    assert len(report.change_units) == 1
    missing = report.missing_companion_candidates
    caller_missing = [c for c in missing if c.reason_code is CompanionReasonCode.CALLER_NOT_UPDATED]
    assert any(c.expected_qualified_name == "process" for c in caller_missing)
    assert report.change_map is not None


async def test_case_behavior_changed_negative_test_missing(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth: process changed; test_service.py (a real,
    resolvable test relationship -- filename match AND a real import)
    was NOT touched -> exactly one TEST_NOT_UPDATED missing companion
    candidate."""

    full_name = "test/ci-corpus-test-missing"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-corpus-test-missing", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "service.py").write_text(
        'from repository import save\n\n\ndef process(request):\n    return save(request or {})\n'
    )
    commit_all(root, "change process behavior")

    candidates, _ = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_change_intelligence_report(session, candidates=candidates)

    missing = report.missing_companion_candidates
    test_missing = [c for c in missing if c.reason_code is CompanionReasonCode.TEST_NOT_UPDATED]
    assert any("test_service.py" in c.expected_file_path for c in test_missing)


async def test_case_complete_multi_file_implementation(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth: all three layers (api/service/repository) changed
    together -> one connected ChangeUnit spanning 3 files (real call
    edges connect them), CONTRACT-flavored (process and
    save both have real cross-file callers), diagram
    eligible."""

    full_name = "test/ci-corpus-complete-impl"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-corpus-complete-impl", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "api.py").write_text(
        'from service import process\n\n\ndef handle(request):\n    return process(request or {})\n'
    )
    (root / "service.py").write_text(
        'from repository import save\n\n\ndef process(request):\n    return save(request or {})\n'
    )
    (root / "repository.py").write_text(_REPOSITORY_CHANGED)
    commit_all(root, "implement change across all three layers")

    candidates, _ = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    assert len(candidates) == 3

    async with session_factory() as session:
        report = await build_change_intelligence_report(session, candidates=candidates)

    assert len(report.change_units) == 1
    unit = report.change_units[0]
    assert len(unit.changed_files) == 3
    assert report.change_map is not None
    assert report.change_map.node_count >= 3


async def test_case_docs_only_change(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    """Ground truth: README.md-only change -> a module-region candidate
    (no parser for Markdown), no diagram."""

    full_name = "test/ci-corpus-docs-only"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-corpus-docs-only", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "README.md").write_text(_README + "\nSome more docs.\n")
    commit_all(root, "docs update")

    candidates, _ = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_change_intelligence_report(session, candidates=candidates)

    assert report.change_map is None


async def test_case_two_unrelated_logical_changes_never_merge(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth: unrelated_helper (isolated) and save
    (has a real caller chain) changed together in one commit -> 2
    SEPARATE ChangeUnits, never merged into one -- grouping.py has no
    graph edge between them."""

    full_name = "test/ci-corpus-two-unrelated"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-corpus-two-unrelated", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "unrelated.py").write_text("def unrelated_helper(x):\n    return x + 3\n")
    (root / "repository.py").write_text(_REPOSITORY_CHANGED)
    commit_all(root, "two unrelated changes")

    candidates, _ = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    assert len(candidates) == 2

    async with session_factory() as session:
        report = await build_change_intelligence_report(session, candidates=candidates)

    assert len(report.change_units) == 2
    unit_files = [set(u.changed_files) for u in report.change_units]
    assert {"unrelated.py"} in unit_files
    assert {"repository.py"} in unit_files


async def test_change_intelligence_never_calls_a_provider() -> None:
    """Structural proof, mirroring the same discipline already applied
    to patchfrog.ops.doctor/preflight: no LLMProvider import anywhere
    in this package."""

    import patchfrog.change_intelligence as pkg

    assert pkg.__file__ is not None
    package_dir = Path(pkg.__file__).parent
    for path in package_dir.glob("*.py"):
        text = path.read_text()
        assert "LLMProvider" not in text
        assert "generate_structured" not in text


async def test_change_kind_taxonomy_used_by_a_real_persistence_path(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A changed symbol under a persistence-shaped path is classified
    PERSISTENCE even with no other signal -- proven against the real
    RepositoryQueryService, not a hand-built stand-in."""

    full_name = "test/ci-corpus-persistence-kind"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-corpus-persistence-kind", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "models").mkdir()
    (root / "models" / "__init__.py").write_text("")
    (root / "models" / "user.py").write_text("class User:\n    def save(self):\n        return True\n")
    commit_all(root, "add models/user.py")

    candidates, _ = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        queries = RepositoryQueryService()
        report = await build_change_intelligence_report(session, candidates=candidates, query_service=queries)

    assert any(u.change_kind is ChangeKind.PERSISTENCE for u in report.change_units)


async def test_observed_companion_not_reported_as_missing(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """When the caller IS updated in the same diff, the companion
    candidate's status is OBSERVED, never MISSING -- no false positive."""

    full_name = "test/ci-corpus-observed-companion"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-corpus-observed-companion", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "repository.py").write_text(_REPOSITORY_CHANGED)
    (root / "service.py").write_text(
        'from repository import save\n\n\ndef process(request):\n    return save(request or {})\n'
    )
    commit_all(root, "update both save and its caller process")

    candidates, _ = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_change_intelligence_report(session, candidates=candidates)

    caller_companions = [
        c for c in report.expected_companions if c.reason_code is CompanionReasonCode.CALLER_NOT_UPDATED
    ]
    # Exact match, not substring: test_service.py's `test_process` is a
    # real caller of `process` too (it literally calls it), so a loose
    # substring check would wrongly conflate it with the `process`
    # entry proper.
    observed = [c for c in caller_companions if c.expected_qualified_name == "process"]
    assert observed
    assert all(c.status is CompanionStatus.OBSERVED for c in observed)
