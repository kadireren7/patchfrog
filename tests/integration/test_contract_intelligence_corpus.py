"""Controlled corpus for Contract & Blast Radius Intelligence (spec
sections 16/18) -- real git repository, real indexing, real diff-driven
:class:`~patchfrog.review.domain.ReviewCandidate` generation, real
:func:`~patchfrog.contract_intelligence.service.build_contract_intelligence_report`
against a real *base commit* fetched via local ``git show``. Zero LLM
involvement anywhere.

Each case is a real, independent commit against a shared base fixture
repository, with explicit ground truth recorded directly in the test.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.change_intelligence.domain import CompanionReasonCode, CompanionStatus
from patchfrog.contract_intelligence.domain import BreakingCharacteristic
from patchfrog.contract_intelligence.service import build_contract_intelligence_report
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.repositories import RepositoryIndexRepository, RepositoryRepository
from patchfrog.review.candidates import ReviewCandidateGenerator
from patchfrog.review.domain import ReviewCandidate
from patchfrog.review.local_diff import diff_against_base
from tests.support.git_repo import commit_all, init_git_repo

_SERVICE = '''from repository import save


def process(request):
    return save(request)
'''

_CALLER = '''from service import process


def handle(request):
    return process(request)
'''

_REPOSITORY = '''def save(request):
    return {"ok": True}
'''

_UNRELATED = '''def unrelated_helper(x):
    return x + 1
'''

_README = "# scratch repo\n"


async def _setup_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text(_README)
    (root / "service.py").write_text(_SERVICE)
    (root / "caller.py").write_text(_CALLER)
    (root / "repository.py").write_text(_REPOSITORY)
    (root / "unrelated.py").write_text(_UNRELATED)
    init_git_repo(root)
    base_sha = commit_all(root, "base")
    return root, base_sha


async def _index_and_generate_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    root: Path,
    full_name: str,
    base_sha: str,
) -> list[ReviewCandidate]:
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
    return list(candidates)


async def test_case_required_argument_added_caller_forgotten(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth: `save` gains a required `retries` parameter;
    `process` (its real, unchanged caller) is NOT updated -> a
    REQUIRED_PARAMETER_ADDED breaking delta and one MISSING
    CONTRACT_CONSUMER_NOT_UPDATED candidate naming `process`."""

    full_name = "test/ci-required-arg-forgotten"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-required-arg-forgotten", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "repository.py").write_text('def save(request, retries):\n    return {"ok": True, "retries": retries}\n')
    commit_all(root, "add required retries parameter")

    candidates = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_contract_intelligence_report(
            session, candidates=candidates, base_sha=base_sha, local=True, root_path=root
        )

    assert len(report.deltas) == 1
    delta = report.deltas[0]
    assert delta.qualified_name == "save"
    assert BreakingCharacteristic.REQUIRED_PARAMETER_ADDED in delta.characteristics
    assert delta.is_potentially_breaking

    missing = [
        c for c in report.stale_consumers
        if c.status is CompanionStatus.MISSING and c.reason_code is CompanionReasonCode.CONTRACT_CONSUMER_NOT_UPDATED
    ]
    assert any(c.expected_qualified_name == "process" for c in missing)
    assert report.contract_story != ""


async def test_case_required_argument_added_all_callers_updated(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth: `save` gains a required parameter AND `process` (its
    only caller) is updated in the same diff -> the companion candidate
    is OBSERVED, never MISSING (no false positive)."""

    full_name = "test/ci-required-arg-updated"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-required-arg-updated", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "repository.py").write_text('def save(request, retries):\n    return {"ok": True, "retries": retries}\n')
    (root / "service.py").write_text('from repository import save\n\n\ndef process(request):\n    return save(request, retries=3)\n')
    commit_all(root, "add retries parameter and update caller")

    candidates = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_contract_intelligence_report(
            session, candidates=candidates, base_sha=base_sha, local=True, root_path=root
        )

    assert len(report.deltas) == 1
    process_companions = [c for c in report.stale_consumers if c.expected_qualified_name == "process"]
    assert process_companions
    assert all(c.status is CompanionStatus.OBSERVED for c in process_companions)


async def test_case_optional_parameter_with_default_no_false_positive(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth (negative/false-positive case, spec section 18):
    `save` gains an *optional* parameter with a default, caller left
    unchanged -> a delta exists (OPTIONAL_PARAMETER_ADDED) but it is
    never `is_potentially_breaking`, and zero stale-consumer candidates
    are produced."""

    full_name = "test/ci-optional-arg-no-fp"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-optional-arg-no-fp", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "repository.py").write_text('def save(request, retries=3):\n    return {"ok": True, "retries": retries}\n')
    commit_all(root, "add optional retries parameter with default")

    candidates = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_contract_intelligence_report(
            session, candidates=candidates, base_sha=base_sha, local=True, root_path=root
        )

    assert len(report.deltas) == 1
    delta = report.deltas[0]
    assert delta.characteristics == (BreakingCharacteristic.OPTIONAL_PARAMETER_ADDED,)
    assert not delta.is_potentially_breaking
    assert report.stale_consumers == ()


async def test_case_parameter_removed_stale_caller(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth: `save`'s only parameter is removed while `process`
    (its unchanged caller) still passes it -> PARAMETER_REMOVED breaking
    delta and a MISSING stale-consumer candidate naming `process`."""

    full_name = "test/ci-param-removed"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-param-removed", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "repository.py").write_text('def save():\n    return {"ok": True}\n')
    commit_all(root, "remove request parameter")

    candidates = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_contract_intelligence_report(
            session, candidates=candidates, base_sha=base_sha, local=True, root_path=root
        )

    assert len(report.deltas) == 1
    delta = report.deltas[0]
    assert BreakingCharacteristic.PARAMETER_REMOVED in delta.characteristics
    missing = [c for c in report.stale_consumers if c.status is CompanionStatus.MISSING]
    assert any(c.expected_qualified_name == "process" for c in missing)


async def test_case_internal_helper_with_no_consumer_produces_no_descriptor(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth (negative case): `unrelated_helper` has zero real
    callers -> it never becomes a ContractDescriptor at all (spec
    section 4's boundary gate), so no delta/stale-consumer either even
    though its signature genuinely changed."""

    full_name = "test/ci-internal-helper"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-internal-helper", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "unrelated.py").write_text("def unrelated_helper(x, y):\n    return x + y\n")
    commit_all(root, "change unrelated_helper signature")

    candidates = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_contract_intelligence_report(
            session, candidates=candidates, base_sha=base_sha, local=True, root_path=root
        )

    assert report.descriptors == ()
    assert report.deltas == ()
    assert report.stale_consumers == ()


async def test_case_docs_only_change_produces_empty_report(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/ci-docs-only"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-docs-only", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "README.md").write_text(_README + "\nmore docs\n")
    commit_all(root, "docs update")

    candidates = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_contract_intelligence_report(
            session, candidates=candidates, base_sha=base_sha, local=True, root_path=root
        )

    assert report.deltas == ()
    assert report.contract_story == ""


async def test_case_no_base_sha_is_a_no_op(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """`base_sha=None` (e.g. a review path that hasn't wired base-commit
    info) always produces the empty report -- zero I/O, never a crash."""

    full_name = "test/ci-no-base-sha"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-no-base-sha", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "repository.py").write_text('def save(request, retries):\n    return {"ok": True, "retries": retries}\n')
    commit_all(root, "add required retries parameter")

    candidates = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_contract_intelligence_report(session, candidates=candidates, base_sha=None)

    assert report.descriptors == ()
    assert report.deltas == ()
    assert report.stale_consumers == ()


async def test_case_return_became_optional_all_consumers_updated(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth (spec section 16 case 6): `save`'s return annotation
    widens to `Optional[dict]` AND its only caller `process` is updated
    in the same diff -> a real RETURN_BECAME_OPTIONAL breaking delta,
    but the companion candidate is OBSERVED, never MISSING."""

    full_name = "test/ci-return-optional-updated"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-return-optional-updated", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "repository.py").write_text(
        "def save(request) -> dict | None:\n    return {\"ok\": True} if request else None\n"
    )
    (root / "service.py").write_text(
        'from repository import save\n\n\ndef process(request):\n    result = save(request)\n'
        '    return result if result is not None else {}\n'
    )
    commit_all(root, "widen return type and update caller")

    candidates = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_contract_intelligence_report(
            session, candidates=candidates, base_sha=base_sha, local=True, root_path=root
        )

    assert len(report.deltas) == 1
    delta = report.deltas[0]
    assert BreakingCharacteristic.RETURN_BECAME_OPTIONAL in delta.characteristics
    process_companions = [c for c in report.stale_consumers if c.expected_qualified_name == "process"]
    assert process_companions
    assert all(c.status is CompanionStatus.OBSERVED for c in process_companions)


async def test_case_two_unrelated_contract_changes_never_conflated(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Ground truth: `save` (has a real caller) and `unrelated_helper`
    (no caller) both change signature in the same commit -> exactly one
    real delta (`save`), the unrelated helper produces no descriptor at
    all -- never a fabricated combined story about both."""

    full_name = "test/ci-two-unrelated"
    root, base_sha = await _setup_repo(tmp_path)
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="ci-two-unrelated", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo.id

    (root / "repository.py").write_text('def save(request, retries):\n    return {"ok": True, "retries": retries}\n')
    (root / "unrelated.py").write_text("def unrelated_helper(x, y):\n    return x + y\n")
    commit_all(root, "two unrelated signature changes")

    candidates = await _index_and_generate_candidates(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_contract_intelligence_report(
            session, candidates=candidates, base_sha=base_sha, local=True, root_path=root
        )

    assert len(report.deltas) == 1
    assert report.deltas[0].qualified_name == "save"
    assert all(d.qualified_name != "unrelated_helper" for d in report.deltas)


async def test_contract_intelligence_never_calls_a_provider() -> None:
    """Structural proof, mirroring the same discipline already applied
    to patchfrog.change_intelligence: no LLMProvider import anywhere in
    this package."""

    import patchfrog.contract_intelligence as pkg

    assert pkg.__file__ is not None
    package_dir = Path(pkg.__file__).parent
    for path in package_dir.glob("*.py"):
        text = path.read_text()
        assert "LLMProvider" not in text
        assert "generate_structured" not in text
