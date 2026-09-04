"""Controlled corpus for Intent Verification (spec sections 29/30) --
real git repository, real indexing, real diff-driven
:class:`~patchfrog.review.domain.ReviewCandidate` generation, real
:func:`~patchfrog.change_intelligence.service.build_change_intelligence_report`
for real `ChangeUnit`s, then real
:func:`~patchfrog.intent_verification.service.build_intent_verification_report`.
Zero LLM involvement anywhere.

Each case is a real, independent commit against a shared base fixture
repository, with explicit ground truth recorded directly in the test.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.change_intelligence.domain import ChangeIntelligenceReport
from patchfrog.change_intelligence.service import build_change_intelligence_report
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.intent_verification.domain import IntentCoverageStatus, IntentGapReasonCode
from patchfrog.intent_verification.service import build_intent_verification_report
from patchfrog.persistence.repositories import RepositoryIndexRepository, RepositoryRepository
from patchfrog.review.candidates import ReviewCandidateGenerator
from patchfrog.review.domain import ReviewCandidate
from patchfrog.review.local_diff import diff_against_base
from tests.support.git_repo import commit_all, init_git_repo

_SERVICE = '''from repository import save


def process_payment(request):
    return save(request)
'''

_CALLER = '''from service import process_payment


def handle_webhook(request):
    return process_payment(request)
'''

_RETRY_WORKER = '''from service import process_payment


def run_retry(request):
    return process_payment(request)
'''

_REPOSITORY = '''def save(request):
    return {"ok": True}
'''

_README = "# scratch repo\n"


async def _setup_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text(_README)
    (root / "service.py").write_text(_SERVICE)
    (root / "caller.py").write_text(_CALLER)
    (root / "repository.py").write_text(_REPOSITORY)
    init_git_repo(root)
    base_sha = commit_all(root, "base")
    return root, base_sha


async def _index_generate_and_group(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    root: Path,
    full_name: str,
    base_sha: str,
) -> ChangeIntelligenceReport:
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    diff_files = diff_against_base(root, base_sha)
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        candidates: list[ReviewCandidate] = list(
            await ReviewCandidateGenerator().generate(
                session, repository_index_id=index.id, diff_files=diff_files, static_findings=[], max_candidates=40,
            )
        )
        change_report = await build_change_intelligence_report(session, candidates=candidates)
    return change_report


async def _make_repo(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=0,
        )
        await session.commit()
        return repo.id


async def test_case_complete_implementation_no_gap(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 1: explicit intent + complete implementation
    -> no gap. All real, graph-linked consumers were updated too."""

    full_name = "test/iv-complete-impl"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "idempotent": True}\n')
    (root / "service.py").write_text(
        'from repository import save\n\n\ndef process_payment(request):\n    if request.get("id") in _seen:\n'
        '        return None\n    return save(request)\n\n\n_seen = set()\n'
    )
    (root / "caller.py").write_text(
        'from service import process_payment\n\n\ndef handle_webhook(request):\n'
        '    # idempotent retries are now safe end to end\n    return process_payment(request)\n'
    )
    commit_all(root, "prevent duplicate payment processing across full path")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="Prevent duplicate payment processing during webhook retries",
        body=None,
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )

    assert len(report.claims) == 1
    assert len(report.coverage) == 1
    assert report.coverage[0].status is IntentCoverageStatus.SUPPORTED
    assert report.gaps == ()


async def test_case_one_real_affected_path_forgotten(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 2/8: explicit intent + one real affected path
    forgotten (the retry worker, a real caller of process_payment that
    isn't itself touched) -> a real PotentialIntentGap."""

    full_name = "test/iv-path-forgotten"
    root, _ = await _setup_repo(tmp_path)
    (root / "retry_worker.py").write_text(_RETRY_WORKER)
    base_sha = commit_all(root, "add retry worker")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "idempotent": True}\n')
    (root / "service.py").write_text(
        'from repository import save\n\n\ndef process_payment(request):\n    if request.get("id") in _seen:\n'
        '        return None\n    return save(request)\n\n\n_seen = set()\n'
    )
    commit_all(root, "prevent duplicate retry payment processing")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="Prevent duplicate retry payment processing",
        body=None,
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )

    assert len(report.claims) == 1
    assert report.coverage[0].status is IntentCoverageStatus.PARTIAL_EVIDENCE
    assert len(report.gaps) >= 1
    assert any("run_retry" in (g.expected_surface.qualified_name or "") for g in report.gaps)
    assert all(g.reason_code is IntentGapReasonCode.EXPECTED_SURFACE_UNCHANGED for g in report.gaps)


async def test_case_vague_title_skipped(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 4: vague title -> Intent Verification
    skipped entirely."""

    full_name = "test/iv-vague-title"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "v": 2}\n')
    commit_all(root, "fix stuff")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="fix stuff", body=None, change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )

    assert report.claims == ()
    assert report.coverage == ()
    assert report.gaps == ()


async def test_case_docs_only_pr_with_explicit_intent_no_code_gap_noise(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 5: docs-only PR with explicit documentation
    intent -> no code intent-gap noise (there's no symbol-level
    ChangeUnit for a docs-only commit, so nothing to spuriously flag)."""

    full_name = "test/iv-docs-only"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "README.md").write_text(_README + "\nDocument the payment idempotency behavior.\n")
    commit_all(root, "document payment idempotency behavior in the README")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="Document payment idempotency behavior in the README",
        body=None,
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )

    assert report.gaps == ()


async def test_case_explicit_intent_but_unrelated_change_units_not_mapped(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 9: explicit intent but unrelated ChangeUnits
    -> the unrelated unit is never mapped, coverage is
    INSUFFICIENT_EVIDENCE, never a false gap."""

    full_name = "test/iv-unrelated-units"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "caller.py").write_text(
        'from service import process_payment\n\n\ndef handle_webhook(request, extra_logging=True):\n'
        '    return process_payment(request)\n'
    )
    commit_all(root, "add optional logging flag to webhook handler")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="Prevent duplicate database migration failures",
        body=None,
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )

    assert len(report.claims) == 1
    assert report.coverage[0].status is IntentCoverageStatus.INSUFFICIENT_EVIDENCE
    assert report.gaps == ()


async def test_case_no_pr_metadata_is_a_no_op(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 14: metadata absent -> no-op."""

    full_name = "test/iv-no-metadata"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "v": 2}\n')
    commit_all(root, "change save")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title=None, body=None, change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )
    assert report.claims == ()


async def test_case_already_updated_expected_surface_no_false_positive_gap(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 15: the real, relevant affected surface WAS
    updated in the same diff -> no false-positive gap."""

    full_name = "test/iv-already-updated"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "idempotent": True}\n')
    (root / "service.py").write_text(
        'from repository import save\n\n\ndef process_payment(request):\n'
        '    # idempotent end to end now\n    return save(request)\n'
    )
    (root / "caller.py").write_text(
        'from service import process_payment\n\n\ndef handle_webhook(request):\n'
        '    result = process_payment(request)\n    return result\n'
    )
    commit_all(root, "prevent duplicate webhook payment processing")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="Prevent duplicate webhook payment processing",
        body=None,
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )

    assert report.gaps == ()


async def test_intent_verification_never_calls_a_provider() -> None:
    """Structural proof, mirroring the same discipline already applied
    to patchfrog.change_intelligence/patchfrog.contract_intelligence: no
    LLMProvider import anywhere in this package."""

    import patchfrog.intent_verification as pkg

    assert pkg.__file__ is not None
    package_dir = Path(pkg.__file__).parent
    for path in package_dir.glob("*.py"):
        text = path.read_text()
        assert "LLMProvider" not in text
        assert "generate_structured" not in text
