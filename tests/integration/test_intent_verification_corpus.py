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

from patchfrog.change_intelligence.domain import (
    ChangeIntelligenceReport,
    CompanionReasonCode,
    CompanionStatus,
)
from patchfrog.change_intelligence.service import build_change_intelligence_report
from patchfrog.contract_intelligence.domain import ContractIntelligenceReport
from patchfrog.contract_intelligence.service import build_contract_intelligence_report
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.intent_verification.domain import (
    MAX_INTENT_CLAIMS,
    IntentCoverageStatus,
    IntentGapReasonCode,
    IntentSourceKind,
)
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


async def _index_generate_group_and_contract(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    root: Path,
    full_name: str,
    base_sha: str,
) -> tuple[ChangeIntelligenceReport, ContractIntelligenceReport]:
    """Like :func:`_index_generate_and_group`, but also runs the real
    Contract & Blast Radius Intelligence path (base-commit fetch via
    local ``git show``, real `ContractDelta`/stale-consumer detection)
    -- needed to prove Intent Verification's dedup against a *real* K
    stale consumer, not a hand-built one."""

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
        contract_report = await build_contract_intelligence_report(
            session, candidates=candidates, change_units=change_report.change_units,
            base_sha=base_sha, local=True, root_path=root,
        )
    return change_report, contract_report


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
    forgotten -> a real PotentialIntentGap.

    Uses a *callee* relationship deliberately (``process_payment`` calls
    ``schedule_retry``), not a caller one: J's own
    ``CALLER_NOT_UPDATED`` companion heuristic already tracks every real
    *caller* of a changed symbol (see the dedup fix this corrected
    round added -- a caller-direction affected-surface node is always
    already companion-owned, so `PotentialIntentGap` would be a
    redundant duplicate there). A callee is the genuinely novel signal
    this milestone adds: J's companions never track "did the changed
    symbol's own callee get updated too.\""""

    full_name = "test/iv-path-forgotten"
    root, _ = await _setup_repo(tmp_path)
    (root / "retry_worker.py").write_text('def schedule_retry(request):\n    return True\n')
    (root / "service.py").write_text(
        'from repository import save\nfrom retry_worker import schedule_retry\n\n\n'
        'def process_payment(request):\n    schedule_retry(request)\n    return save(request)\n'
    )
    base_sha = commit_all(root, "process_payment schedules a retry via retry_worker")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "idempotent": True}\n')
    (root / "service.py").write_text(
        'from repository import save\nfrom retry_worker import schedule_retry\n\n\n'
        'def process_payment(request):\n    if request.get("id") in _seen:\n        return None\n'
        '    schedule_retry(request)\n    return save(request)\n\n\n_seen = set()\n'
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
    assert any("schedule_retry" in (g.expected_surface.qualified_name or "") for g in report.gaps)
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


async def test_case_real_contract_stale_consumer_dedup(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 3, using the *real* J+K stack (not a hand-
    built ExpectedCompanionChange like the unit test): `save` gains a
    required `retries` parameter (a real K `ContractDelta`,
    REQUIRED_PARAMETER_ADDED), `process_payment` -- its real, unchanged
    caller -- is left as a real K `CONTRACT_CONSUMER_NOT_UPDATED` stale
    consumer AND a real J `CALLER_NOT_UPDATED` companion (the same
    caller edge, detected independently by both packages). An intent
    claim mapped to this unit must reference *both* existing companions
    via `relevant_companion_candidates` -- and construct **zero** new
    `PotentialIntentGap` objects for that same real surface."""

    full_name = "test/iv-real-contract-dedup"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text(
        'def save(request, retries):\n    return {"ok": True, "retries": retries}\n'
    )
    commit_all(root, "add required retries parameter to save")

    change_report, contract_report = await _index_generate_group_and_contract(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    assert len(contract_report.deltas) == 1
    assert contract_report.deltas[0].is_potentially_breaking
    missing_stale_consumers = [
        c for c in contract_report.stale_consumers
        if c.status is CompanionStatus.MISSING and c.expected_qualified_name == "process_payment"
    ]
    assert missing_stale_consumers, "fixture bug: expected a real K stale consumer for process_payment"

    combined_companions = change_report.expected_companions + contract_report.stale_consumers
    report = build_intent_verification_report(
        title="Add configurable retries to the save operation",
        body=None,
        change_units=change_report.change_units,
        contract_deltas=contract_report.deltas,
        expected_companions=combined_companions,
    )

    assert len(report.claims) == 1
    assert report.coverage[0].status is IntentCoverageStatus.PARTIAL_EVIDENCE
    assert report.coverage[0].relevant_contract_deltas == contract_report.deltas
    missing_relevant = [
        c for c in report.coverage[0].relevant_companion_candidates if c.status is CompanionStatus.MISSING
    ]
    assert any(c.expected_qualified_name == "process_payment" for c in missing_relevant)
    # The core dedup proof: no second, near-duplicate PotentialIntentGap
    # for the exact same surface J/K already both flagged.
    assert report.gaps == ()


async def test_case_refactor_intent_behavior_preserved_no_fabricated_gap(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec correction section 8: an explicit refactor intent, with every
    real graph-linked surface genuinely updated together, must never
    fabricate a behavioral requirement or a gap merely because the word
    "refactor" appears."""

    full_name = "test/iv-refactor-negative"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text(
        'def save(request):\n    """Shared persistence helper."""\n    return {"ok": True}\n'
    )
    (root / "service.py").write_text(
        'from repository import save\n\n\ndef process_payment(request):\n'
        '    """Delegates to the shared save helper."""\n    return save(request)\n'
    )
    (root / "caller.py").write_text(
        'from service import process_payment\n\n\ndef handle_webhook(request):\n'
        '    """Delegates to process_payment, now sharing helper logic."""\n'
        '    return process_payment(request)\n'
    )
    commit_all(root, "refactor payment processing to share helper logic and docstrings")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="Refactor payment processing to share helper logic",
        body=None,
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )

    assert len(report.claims) == 1  # "refactor payment processing to share helper logic" is not a bare placeholder
    assert report.gaps == ()


async def test_case_error_handling_intent_missing_test_surface(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 7: explicit error-handling intent, the
    handler is updated, but the real graph-linked test file
    (`test_service.py`, matched to `service.py` by Change Intelligence's
    own filename-pattern heuristic) is not -> the existing
    `TEST_NOT_UPDATED` companion is referenced via
    `relevant_companion_candidates`, and -- the dedup proof -- no second
    `PotentialIntentGap` is created for that same test surface even
    though the claim's own terms (``service``) lexically match the
    TEST-relation affected-surface node too."""

    full_name = "test/iv-error-handling-test-missing"
    root, base_sha = await _setup_repo(tmp_path)
    (root / "test_service.py").write_text(
        "from service import process_payment\n\n\ndef test_process_payment():\n    assert process_payment({}) is not None\n"
    )
    base_sha = commit_all(root, "add test_service.py")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "service.py").write_text(
        'from repository import save\n\n\ndef process_payment(request):\n'
        '    if not request:\n        raise ValueError("invalid payment request")\n'
        '    return save(request)\n'
    )
    commit_all(root, "reject invalid payment requests in the service with a clear error")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="Reject invalid payment requests in the service with a clear error",
        body=None,
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )

    assert len(report.claims) == 1
    missing_test_companions = [
        c for c in report.coverage[0].relevant_companion_candidates
        if c.status is CompanionStatus.MISSING and c.reason_code is CompanionReasonCode.TEST_NOT_UPDATED
    ]
    assert missing_test_companions, "fixture bug: expected a real TEST_NOT_UPDATED companion"
    # Dedup proof: the test surface is referenced via the companion, not
    # duplicated as a PotentialIntentGap.
    assert not any(
        g.expected_surface.file_path == "test_service.py" for g in report.gaps
    )


async def test_case_multiple_enumerated_goals_bounded_real_corpus(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 10, using the real pipeline: a PR body that
    explicitly enumerates multiple goals as a bullet list never exceeds
    MAX_INTENT_CLAIMS, even against a real multi-file ChangeUnit."""

    full_name = "test/iv-multi-goal"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "idempotent": True}\n')
    (root / "service.py").write_text(
        'from repository import save\n\n\ndef process_payment(request):\n'
        '    if request.get("id") in _seen:\n        return None\n    return save(request)\n\n\n_seen = set()\n'
    )
    commit_all(root, "prevent duplicate payment processing")

    body = (
        "This PR does the following:\n"
        "- Prevent duplicate webhook payment processing\n"
        "- Reject expired sessions after logout\n"
        "- Allow reconnect attempts with configurable retry limits\n"
        "- typo\n"
    )
    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="Multiple improvements", body=body,
        change_units=change_report.change_units, expected_companions=change_report.expected_companions,
    )

    assert len(report.claims) <= MAX_INTENT_CLAIMS
    assert len(report.claims) == 3  # the 3 sufficient bullets; "typo" is dropped
    assert all(c.source.source_kind is IntentSourceKind.PR_BODY for c in report.claims)
    # Exactly one bullet lexically maps to the real "process_payment" unit.
    mapped_count = sum(1 for c in report.coverage if c.mapped_change_unit_ids)
    assert mapped_count == 1


async def test_case_title_body_contradiction_real_corpus(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec correction section 2: a real corpus proof of the
    deterministic body-precedence policy -- title and body describe
    materially different behavior; only the body's claim is used, and
    it maps against the real ChangeUnit graph on its own terms."""

    full_name = "test/iv-contradiction"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "idempotent": True}\n')
    (root / "service.py").write_text(
        'from repository import save\n\n\ndef process_payment(request):\n'
        '    if request.get("id") in _seen:\n        return None\n    return save(request)\n\n\n_seen = set()\n'
    )
    commit_all(root, "prevent duplicate payment processing")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="Allow duplicate payment processing for testing purposes",
        body="Prevent duplicate payment processing during webhook retries by making save idempotent.",
        change_units=change_report.change_units,
        expected_companions=change_report.expected_companions,
    )

    assert len(report.claims) == 1
    assert report.claims[0].source.source_kind is IntentSourceKind.PR_BODY
    assert "Prevent duplicate" in report.claims[0].normalized_statement
    assert report.coverage[0].mapped_change_unit_ids  # the body's real terms do map


async def test_case_meaningful_title_only_real_corpus(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 12, real corpus: no PR body at all, a
    meaningful title alone is usable and maps against the real graph."""

    full_name = "test/iv-title-only"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "idempotent": True}\n')
    commit_all(root, "prevent duplicate payment processing")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="Prevent duplicate payment processing in save", body=None,
        change_units=change_report.change_units, expected_companions=change_report.expected_companions,
    )

    assert len(report.claims) == 1
    assert report.claims[0].source.source_kind is IntentSourceKind.PR_TITLE
    assert report.coverage[0].mapped_change_unit_ids


async def test_case_vague_title_meaningful_body_real_corpus(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec section 29 case 13, real corpus: a vague title with a
    meaningful body -- the body establishes usable intent on its own."""

    full_name = "test/iv-body-only"
    root, base_sha = await _setup_repo(tmp_path)
    repository_id = await _make_repo(session_factory, full_name)

    (root / "repository.py").write_text('def save(request):\n    return {"ok": True, "idempotent": True}\n')
    commit_all(root, "prevent duplicate payment processing")

    change_report = await _index_generate_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    report = build_intent_verification_report(
        title="fix stuff",
        body="This PR makes the save operation idempotent to prevent duplicate payment processing.",
        change_units=change_report.change_units, expected_companions=change_report.expected_companions,
    )

    assert len(report.claims) == 1
    assert report.claims[0].source.source_kind is IntentSourceKind.PR_BODY
    assert report.coverage[0].mapped_change_unit_ids


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
