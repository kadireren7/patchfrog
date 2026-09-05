"""Controlled corpus for Historical Regression Memory (spec section 35,
minimum 20 scenarios) -- real git repository, real indexing, real
diff-driven :class:`~patchfrog.review.domain.ReviewCandidate` generation,
real Change/Contract/Intent/Test Intelligence output for the *current*
side, and real, persisted (not FakeLLM-authored) historical state for
the *historical* side: a real `ReviewRunModel`/`ReviewCandidateModel`/
`AIFindingProposalModel`/`AIFindingModel` row chain, real
`FeedbackEventModel` rows, and a real, recomputed
`FeedbackAssessmentModel` row via
:func:`patchfrog.feedback.queries.recompute_and_persist_all` -- exactly
Phase 9's own real pipeline, never a hand-constructed
`HistoricalRegressionRecord` standing in for a real DB round trip
(spec section 25's temporal-leakage requirement).

Every case stages three real, separate steps: T1 (a historical finding
exists), T2 (a real feedback event establishes trust), T3 (a later,
independent current review is built and historical records are fetched
fresh from the database).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.change_intelligence.domain import ChangeIntelligenceReport
from patchfrog.change_intelligence.service import build_change_intelligence_report
from patchfrog.contract_intelligence.service import build_contract_intelligence_report
from patchfrog.feedback.domain import (
    ActorIdentity,
    ExplicitCommand,
    FeedbackEvent,
    FeedbackEventType,
    FeedbackSource,
    SignalStrength,
)
from patchfrog.feedback.queries import recompute_and_persist_all
from patchfrog.historical_regression_memory.domain import (
    HistoricalEvidenceStrength,
    HistoricalMatchKind,
)
from patchfrog.historical_regression_memory.service import build_historical_regression_report
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import (
    AIFindingModel,
    AIFindingProposalModel,
    ReviewCandidateModel,
    ReviewRunModel,
)
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.persistence.repositories.feedback import FeedbackEventRepository
from patchfrog.persistence.repositories.repository_index import RepositoryIndexRepository
from patchfrog.review.candidates import ReviewCandidateGenerator
from patchfrog.review.domain import (
    ProposalStatus,
    ReviewCandidate,
    ReviewCandidateReason,
    ReviewRunStatus,
)
from patchfrog.review.local_diff import diff_against_base
from tests.support.git_repo import commit_all, init_git_repo

_README = "# scratch repo\n"


async def _make_repo(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=0,
        )
        await session.commit()
        return repo.id


def _setup_base(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "README.md").write_text(_README)
    init_git_repo(root)
    return root


async def _index_and_group(
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


async def _stage_historical_finding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    repository_index_id: uuid.UUID,
    file_path: str,
    qualified_name: str | None,
    category: FindingCategory = FindingCategory.CORRECTNESS,
    title: str = "forgot idempotency key on retry",
    commit_sha: str | None = None,
) -> uuid.UUID:
    """Real persisted rows -- a real ReviewRunModel/ReviewCandidateModel/
    AIFindingProposalModel/AIFindingModel chain, exactly the schema the
    real reviewer pipeline writes to, just without needing a full
    multi-role FakeLLM conversation for every one of 20 corpus cases.
    Never a hand-constructed HistoricalRegressionRecord standing in for
    this real round trip."""

    sha = commit_sha or uuid.uuid4().hex[:40].ljust(40, "0")
    async with session_factory() as session:
        run = ReviewRunModel(
            id=uuid.uuid4(), repository_id=repository_id, repository_index_id=repository_index_id,
            commit_sha=sha, config_fingerprint="c" * 64, model_fingerprint="m" * 64,
            incremental_context_fingerprint="i" * 64, status=ReviewRunStatus.SUCCEEDED,
            reviewer_provider="fake", reviewer_model="fake-model",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()

        candidate = ReviewCandidateModel(
            id=uuid.uuid4(), review_run_id=run.id, file_path=file_path, symbol_id=None,
            symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
            qualified_name=qualified_name, start_line=1, end_line=5, changed_lines="[1]",
            reason=ReviewCandidateReason.CHANGED_SYMBOL,
        )
        session.add(candidate)
        await session.flush()

        proposal = AIFindingProposalModel(
            id=uuid.uuid4(), review_run_id=run.id, candidate_id=candidate.id, title=title,
            message="a real historical defect", category=category, severity=Severity.MEDIUM,
            confidence=Confidence.HIGH, file_path=file_path, start_line=1, end_line=5, evidence="[]",
            reasoning_summary="root cause", status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        await session.flush()

        finding = AIFindingModel(
            id=uuid.uuid4(), review_run_id=run.id, proposal_id=proposal.id, candidate_id=candidate.id, title=title,
            message="a real historical defect", category=category, severity=Severity.MEDIUM,
            confidence=Confidence.HIGH, file_path=file_path, start_line=1, end_line=5, evidence="[]",
            reasoning_summary="root cause",
        )
        session.add(finding)
        await session.commit()
        return finding.id


async def _stage_feedback(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    finding_id: uuid.UUID,
    command: ExplicitCommand,
) -> None:
    """A real FeedbackEventModel row, plus a real recompute -- T2 of the
    temporal staging (spec section 25). Never skips straight to a
    hand-built FeedbackAssessmentModel row."""

    async with session_factory() as session:
        event = FeedbackEvent(
            repository_id=repository_id, pull_request_id=None, review_run_id=None, publication_id=None,
            review_publication_comment_id=None, finding_id=finding_id, github_review_id=None,
            github_comment_id=None, event_type=FeedbackEventType.EXPLICIT_COMMAND, source=FeedbackSource.REPLY_SYNC,
            external_event_id=f"cmd:{finding_id}:{command.value}:{uuid.uuid4().hex[:8]}",
            raw_signal=f"/patchfrog {command.value}", normalized_signal=command.value,
            signal_strength=SignalStrength.STRONG, actor=ActorIdentity(login="developer", is_bot=False),
            occurred_at=datetime.now(UTC),
        )
        await FeedbackEventRepository().create_if_new(session, event=event)
        await session.commit()

    async with session_factory() as session:
        await recompute_and_persist_all(session, repository_id=repository_id)
        await session.commit()


# ---- 1. Prior FIXED finding, same symbol changed again -> historical candidate ----


async def test_case_prior_fixed_finding_same_symbol_changed_again(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-fixed-same-symbol"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
    historical_index_id = index.id if index is not None else None
    if historical_index_id is None:
        await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
            repository_id=repository_id, root_path=root, repository_full_name=full_name
        )
        async with session_factory() as session:
            index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        historical_index_id = index.id  # type: ignore[union-attr]

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "touch apply_discount again")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    assert len(report.candidates) == 1
    assert report.candidates[0].match_kind is HistoricalMatchKind.SAME_SYMBOL
    assert report.candidates[0].historical_record.evidence_strength is HistoricalEvidenceStrength.CONFIRMED_FIXED


# ---- 2. Prior USEFUL finding, same symbol, current relevance -> historical candidate ----


async def test_case_prior_useful_finding_same_symbol(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-useful-same-symbol"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.USEFUL)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "touch apply_discount again")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    assert len(report.candidates) == 1
    assert report.candidates[0].historical_record.evidence_strength is HistoricalEvidenceStrength.CONFIRMED_USEFUL


# ---- 3. Prior FALSE-POSITIVE finding, same symbol -> no record/candidate ----


async def test_case_prior_false_positive_finding_never_seeds_memory(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-false-positive"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    # Both USEFUL and FALSE_POSITIVE recorded (append-only history) --
    # mandatory fail-closed exclusion still applies (audit section 2).
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.USEFUL)
    await _stage_feedback(
        session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FALSE_POSITIVE
    )

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "touch apply_discount again")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    assert report.trusted_records_considered == ()
    assert report.candidates == ()


# ---- 4. Prior IGNORED finding -> no candidate ----


async def test_case_prior_ignored_finding_never_seeds_memory(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-ignored"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.IGNORE)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "touch apply_discount again")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    assert report.candidates == ()


# ---- 5. Same qualified name, DIFFERENT repository -> no match ----


async def test_case_repository_isolation_same_qualified_name_different_repo(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    other_full_name = "test/hrm-isolation-other"
    other_root = _setup_base(tmp_path / "other")
    (other_root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(other_root, "base")
    other_repository_id = await _make_repo(session_factory, other_full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=other_repository_id, root_path=other_root, repository_full_name=other_full_name
    )
    async with session_factory() as session:
        other_index = await RepositoryIndexRepository().get_active(session, repository_id=other_repository_id)
        assert other_index is not None

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=other_repository_id, repository_index_id=other_index.id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(session_factory, repository_id=other_repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    full_name = "test/hrm-isolation-mine"
    root = _setup_base(tmp_path / "mine")
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "touch apply_discount")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    assert report.trusted_records_considered == ()
    assert report.candidates == ()


# ---- 6. Same file, unrelated symbol -> SAME_FILE (weak), only with CONFIRMED_FIXED ----


async def test_case_same_file_unrelated_symbol_weak_match(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-same-file-weak"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    return order['total']\n\n\ndef apply_tax(order):\n    return order['total']\n"
    )
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_tax",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    # apply_discount changes -- apply_tax (the historical symbol) does not.
    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n\n\ndef apply_tax(order):\n    return order['total']\n"
    )
    commit_all(root, "touch apply_discount, not apply_tax")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    assert len(report.candidates) == 1
    assert report.candidates[0].match_kind is HistoricalMatchKind.SAME_FILE


# ---- 7. Historical record exists, current risky surface untouched -> no candidate ----


async def test_case_historical_record_but_current_surface_untouched(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-untouched-surface"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    return order['total']\n\n\ndef apply_tax(order):\n    return order['total']\n"
    )
    (root / "shipping.py").write_text("def apply_shipping(order):\n    return order['total'] + 5\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_tax",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    # An entirely unrelated file changes -- pricing.py is never touched at all.
    (root / "shipping.py").write_text("def apply_shipping(order):\n    return order['total'] + 10\n")
    commit_all(root, "change shipping only")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    assert report.candidates == ()


# ---- 8. Head A candidate exists; Head B fixes the surface -> candidate disappears ----


async def test_case_stale_candidate_disappears_on_new_exact_head(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-stale-disappears"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    # Head A: touches apply_discount again -- a real candidate.
    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    head_a_sha = commit_all(root, "head A: touch apply_discount")
    change_report_a = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )
    async with session_factory() as session:
        report_a = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report_a.change_units
        )
    assert len(report_a.candidates) == 1

    # Head B: a genuinely new, independent PR starting from head A (a
    # fresh base, not the original distant base_sha) -- a wholly
    # unrelated file changes, apply_discount itself is untouched by
    # *this* PR's own diff.
    (root / "shipping.py").write_text("def apply_shipping(order):\n    return order['total'] + 5\n")
    commit_all(root, "head B: unrelated shipping change")
    change_report_b = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=head_a_sha
    )
    async with session_factory() as session:
        report_b = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report_b.change_units
        )
    assert report_b.candidates == ()


# ---- 9. Old finding without lifecycle trust -> no candidate ----


async def test_case_finding_without_any_feedback_never_seeds_memory(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-no-feedback"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    # No feedback at all -- never recomputed, no feedback_assessments row.

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "touch apply_discount")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    assert report.candidates == ()


# ---- 10. Temporal leakage: the historical finding is not visible before its trust event ----


async def test_case_temporal_leakage_finding_invisible_before_trust_established(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """T1 (finding exists) -> query before T2 -> invisible. T2 (real
    feedback event + real recompute) -> query after -> visible. Proves
    the query never uses future evidence that would not yet have
    existed at an earlier point (spec section 25)."""

    full_name = "test/hrm-temporal-leakage"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "touch apply_discount")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    # T1 (before T2): finding exists, but no trust event yet -- invisible.
    async with session_factory() as session:
        report_before = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )
    assert report_before.candidates == ()

    # T2: a real feedback event + real recompute.
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    # T3: now visible.
    async with session_factory() as session:
        report_after = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )
    assert len(report_after.candidates) == 1


# ---- 11. Prior fixed contract stale-consumer finding; K enrichment, no duplicate ----


async def test_case_historical_enriches_real_k_stale_consumer(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-k-enrich"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "caller.py").write_text(
        "from pricing import apply_discount\n\n\ndef checkout(order):\n    return apply_discount(order)\n"
    )
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="caller.py", qualified_name="checkout",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    # Breaking signature change -- caller.py's checkout is NOT updated (a real K stale consumer).
    (root / "pricing.py").write_text("def apply_discount(order, rate):\n    return order['total'] * rate\n")
    commit_all(root, "require an explicit rate for apply_discount")

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
    assert contract_report.stale_consumers

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units,
            expected_companions=contract_report.stale_consumers,
        )

    assert len(report.candidates) == 1
    assert not report.candidates[0].stands_alone
    assert report.candidates[0].enriches_companion is contract_report.stale_consumers[0]


# ---- 12. Docs-only PR -> no historical regression noise ----


async def test_case_docs_only_change_no_noise(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-docs-only"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    (root / "README.md").write_text("# scratch repo\n\nNow documented.\n")
    commit_all(root, "document the repo")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    assert report.candidates == ()


# ---- 13. Test-only PR -> no unrelated historical production regression ----


async def test_case_test_only_pr_no_unrelated_regression(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-test-only"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n"
    )
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 100}) == 100\n    assert True\n"
    )
    commit_all(root, "strengthen test only")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    assert report.candidates == ()


# ---- 14. Historical security finding, same exact surface -> works under same trust rules ----


async def test_case_historical_security_finding_same_surface(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-security"
    root = _setup_base(tmp_path)
    (root / "auth.py").write_text("def check_token(token):\n    return bool(token)\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="auth.py", qualified_name="check_token", category=FindingCategory.SECURITY,
        title="token comparison was not constant-time",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    (root / "auth.py").write_text("def check_token(token):\n    return len(token) > 0\n")
    commit_all(root, "touch check_token again")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    assert len(report.candidates) == 1
    assert report.candidates[0].historical_record.finding_category is FindingCategory.SECURITY


# ---- 15. Multiple trusted historical findings on the same surface -> bounded/deduped ----


async def test_case_multiple_historical_findings_same_surface_bounded(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-multiple-same-surface"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    for i in range(6):
        finding_id = await _stage_historical_finding(
            session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
            file_path="pricing.py", qualified_name="apply_discount", title=f"bug number {i}",
        )
        await _stage_feedback(
            session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED
        )

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "touch apply_discount again")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    from patchfrog.historical_regression_memory.domain import MAX_HISTORICAL_RECORDS_PER_SURFACE

    assert len(report.candidates) == MAX_HISTORICAL_RECORDS_PER_SURFACE


# ---- 16. Large history -> query bounded, never scans everything in Python ----


async def test_case_large_history_bounded_query(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-large-history"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    for i in range(30):
        finding_id = await _stage_historical_finding(
            session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
            file_path=f"module_{i}.py", qualified_name=f"fn_{i}", title=f"bug number {i}",
        )
        await _stage_feedback(
            session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED
        )

    async with session_factory() as session:
        from patchfrog.historical_regression_memory.queries import fetch_trusted_historical_records

        records = await fetch_trusted_historical_records(session, repository_id=repository_id, limit=10)

    assert len(records) == 10


# ---- 17. Rename/move: symbol renamed since historical finding -> DEFERRED (never guessed) ----


async def test_case_renamed_symbol_never_matched_documented_limitation(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/hrm-renamed"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    # The function is renamed -- same file, different qualified_name.
    (root / "pricing.py").write_text("def apply_loyalty_discount(order):\n    return order['total'] * 0.9\n")
    commit_all(root, "rename apply_discount -> apply_loyalty_discount")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )

    # Falls back to SAME_FILE (still correctly bounded/conservative),
    # never a false SAME_SYMBOL match against the new name.
    assert all(c.match_kind is not HistoricalMatchKind.SAME_SYMBOL for c in report.candidates)


# ---- 18. Real end-to-end review_local pipeline: persisted through to ReviewRunModel ----


async def test_case_review_local_pipeline_persists_historical_regression_memory(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    import json

    from sqlalchemy import select

    from patchfrog.persistence.models.review import ReviewRunModel as _ReviewRunModel
    from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
    from patchfrog.review.service import PullRequestReviewService

    full_name = "test/hrm-pipeline"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    head_sha = commit_all(root, "touch apply_discount again")
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    diff_files = diff_against_base(root, base_sha)

    provider = FakeLLMProvider(response_factory=lambda req: ScriptedResponse(raw_json=json.dumps({"findings": []})))
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider)
    await service.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name,
        commit_sha=head_sha, diff_files=diff_files, base_sha=base_sha,
    )

    async with session_factory() as session:
        runs = (
            await session.execute(
                select(_ReviewRunModel)
                .where(_ReviewRunModel.repository_id == repository_id, _ReviewRunModel.commit_sha == head_sha)
            )
        ).scalars().all()
    run = runs[0]

    assert run.historical_trusted_record_count >= 1
    assert run.historical_regression_candidate_count == 1
    assert run.historical_summary_rendered is True
    assert "same_symbol" in run.historical_match_kind_counts
    assert "Historical context" in (run.change_story or "")


# ---- 19. Telemetry/versioning round trip on a real corpus-built report ----


async def test_case_telemetry_and_versioning_real_report(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from patchfrog.historical_regression_memory.domain import HISTORICAL_REGRESSION_MEMORY_VERSION
    from patchfrog.historical_regression_memory.telemetry import summarize_for_persistence

    full_name = "test/hrm-telemetry"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        historical_index_id = index.id

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=historical_index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "touch apply_discount again")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        report = await build_historical_regression_report(
            session, repository_id=repository_id, change_units=change_report.change_units
        )
    assert report.version == HISTORICAL_REGRESSION_MEMORY_VERSION

    summary = summarize_for_persistence(report)
    assert summary.historical_regression_candidate_count == len(report.candidates) == 1
    assert summary.historical_summary_rendered is True
    assert summary.historical_summary_text is not None


# ---- 20. Structural: no LLM/provider call anywhere in the package ----


def test_historical_regression_memory_never_imports_a_provider() -> None:
    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "patchfrog" / "historical_regression_memory"
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert "LLMProvider" not in (node.module or "") and not any(
                    alias.name == "LLMProvider" for alias in node.names
                ), f"{path} imports LLMProvider -- Historical Regression Memory must add zero provider calls"
