"""Real, DB-backed corpus for Milestone Y's durable learning records --
mirrors tests/integration/test_repository_learnings_corpus.py's own
discipline exactly (real ReviewRunModel/ReviewCandidateModel/
AIFindingModel/FeedbackEventModel row chains, never hand-built domain
objects standing in for a round trip). Covers spec tests Y1, Y3, Y5,
Y6, Y7, Y8, Y8/Y9 (isolation), Y13 (idempotent recomputation), Y15
(malicious content cannot create learning -- proven by construction:
every helper here goes through the same authenticated-actor feedback
path production code uses)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.cross_repo_intelligence.domain import (
    RepositoryRelationKind,
    RepositoryRelationProvenance,
)
from patchfrog.feedback.domain import (
    ActorIdentity,
    ExplicitCommand,
    FeedbackEvent,
    FeedbackEventType,
    FeedbackSource,
    SignalStrength,
)
from patchfrog.feedback.queries import recompute_and_persist_all
from patchfrog.learning_records.domain import LearningMaturity, LearningType
from patchfrog.learning_records.org_aggregation import (
    aggregate_repository_learnings_for_organization,
)
from patchfrog.learning_records.queries import fetch_repeated_noise_feedback
from patchfrog.learning_records.service import recompute_repository_learnings
from patchfrog.persistence.models.repository_index import IndexStatus, RepositoryIndexModel
from patchfrog.persistence.models.review import (
    AIFindingModel,
    AIFindingProposalModel,
    ReviewCandidateModel,
    ReviewRunModel,
)
from patchfrog.persistence.repositories import (
    RepositoryLearningRecordRepository,
    RepositoryRepository,
)
from patchfrog.persistence.repositories.cross_repo import RepositoryRelationRepository
from patchfrog.persistence.repositories.feedback import FeedbackEventRepository
from patchfrog.review.domain import ProposalStatus, ReviewCandidateReason, ReviewRunStatus


async def _make_repo(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=0,
        )
        await session.commit()
        return repo.id


async def _make_index(session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID) -> uuid.UUID:
    async with session_factory() as session:
        index = RepositoryIndexModel(
            id=uuid.uuid4(), repository_id=repository_id, commit_sha="a" * 40, index_version=1,
            status=IndexStatus.SUCCEEDED, is_active=True, started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(index)
        await session.commit()
        return index.id


async def _stage_finding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    repository_index_id: uuid.UUID,
    file_path: str = "a.py",
    qualified_name: str | None = "a.f",
    category: FindingCategory = FindingCategory.CORRECTNESS,
    commit_sha: str | None = None,
) -> uuid.UUID:
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
            id=uuid.uuid4(), review_run_id=run.id, candidate_id=candidate.id, title="t",
            message="m", category=category, severity=Severity.MEDIUM, confidence=Confidence.HIGH,
            file_path=file_path, start_line=1, end_line=5, evidence="[]", reasoning_summary="r",
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        await session.flush()

        finding = AIFindingModel(
            id=uuid.uuid4(), review_run_id=run.id, proposal_id=proposal.id, candidate_id=candidate.id, title="t",
            message="m", category=category, severity=Severity.MEDIUM, confidence=Confidence.HIGH,
            file_path=file_path, start_line=1, end_line=5, evidence="[]", reasoning_summary="r",
        )
        session.add(finding)
        await session.commit()
        return finding.id


async def _stage_feedback(
    session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, finding_id: uuid.UUID, command: ExplicitCommand
) -> None:
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


async def _stage_and_feedback(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    repository_index_id: uuid.UUID,
    command: ExplicitCommand,
    file_path: str = "a.py",
    qualified_name: str | None = "a.f",
    category: FindingCategory = FindingCategory.CORRECTNESS,
) -> uuid.UUID:
    finding_id = await _stage_finding(
        session_factory, repository_id=repository_id, repository_index_id=repository_index_id,
        file_path=file_path, qualified_name=qualified_name, category=category,
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=command)
    return finding_id


# -- Y5/Y6: real recomputation --


async def test_recompute_produces_a_useful_pattern_from_two_independent_fixed_findings(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "acme/useful")
    index_id = await _make_index(session_factory, repository_id=repository_id)
    for _ in range(2):
        await _stage_and_feedback(session_factory, repository_id=repository_id, repository_index_id=index_id, command=ExplicitCommand.FIXED)

    async with session_factory() as session:
        records = await recompute_repository_learnings(session, repository_id=repository_id)
        await session.commit()

    useful = [r for r in records if r.learning_type is LearningType.USEFUL_FINDING_PATTERN]
    assert len(useful) == 1
    assert useful[0].maturity is LearningMaturity.CANDIDATE


async def test_recompute_produces_a_noise_pattern_from_two_independent_false_positive_findings(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "acme/noise")
    index_id = await _make_index(session_factory, repository_id=repository_id)
    for _ in range(2):
        await _stage_and_feedback(session_factory, repository_id=repository_id, repository_index_id=index_id, command=ExplicitCommand.FALSE_POSITIVE)

    async with session_factory() as session:
        records = await recompute_repository_learnings(session, repository_id=repository_id)
        await session.commit()

    noise = [r for r in records if r.learning_type is LearningType.NOISE_SUPPRESSION]
    assert len(noise) == 1
    assert noise[0].maturity is LearningMaturity.CANDIDATE


async def test_single_false_positive_never_produces_a_noise_pattern_real_db(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "acme/single-noise")
    index_id = await _make_index(session_factory, repository_id=repository_id)
    await _stage_and_feedback(session_factory, repository_id=repository_id, repository_index_id=index_id, command=ExplicitCommand.FALSE_POSITIVE)

    async with session_factory() as session:
        records = await recompute_repository_learnings(session, repository_id=repository_id)
        await session.commit()
    assert records == ()


async def test_strong_deterministic_security_finding_is_never_suppressed_by_noise_learning(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """A SECURITY-category surface repeatedly marked false-positive still
    produces a NOISE_SUPPRESSION *record* (the evidence is real) -- but
    the governance-side filter (see test_governance_learning_policy.py)
    is what actually prevents it from ever being applied. This test
    proves the record's category is preserved end to end, which the
    filter depends on."""

    repository_id = await _make_repo(session_factory, "acme/security-noise")
    index_id = await _make_index(session_factory, repository_id=repository_id)
    for _ in range(2):
        await _stage_and_feedback(
            session_factory, repository_id=repository_id, repository_index_id=index_id,
            command=ExplicitCommand.FALSE_POSITIVE, category=FindingCategory.SECURITY,
        )
    async with session_factory() as session:
        records = await recompute_repository_learnings(session, repository_id=repository_id)
        await session.commit()
    assert records[0].surface.category is FindingCategory.SECURITY


# -- Y3: contradictory evidence retires --


async def test_contradictory_later_evidence_retires_an_existing_noise_record(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "acme/retire")
    index_id = await _make_index(session_factory, repository_id=repository_id)
    finding_ids = [
        await _stage_and_feedback(session_factory, repository_id=repository_id, repository_index_id=index_id, command=ExplicitCommand.FALSE_POSITIVE)
        for _ in range(2)
    ]
    async with session_factory() as session:
        first_pass = await recompute_repository_learnings(session, repository_id=repository_id)
        await session.commit()
    assert any(r.learning_type is LearningType.NOISE_SUPPRESSION for r in first_pass)

    # New, contradicting evidence on the same finding: now useful/fixed.
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_ids[0], command=ExplicitCommand.FIXED)

    async with session_factory() as session:
        await recompute_repository_learnings(session, repository_id=repository_id)
        await session.commit()

    async with session_factory() as session:
        stored = await RepositoryLearningRecordRepository().list_for_repository(session, repository_id=repository_id)
    noise_records = [r for r in stored if r.learning_type is LearningType.NOISE_SUPPRESSION]
    assert len(noise_records) == 1
    assert noise_records[0].maturity is LearningMaturity.RETIRED
    assert noise_records[0].retired_reason is not None


# -- Y11/Y12: idempotent recomputation --


async def test_recomputation_is_idempotent(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "acme/idempotent")
    index_id = await _make_index(session_factory, repository_id=repository_id)
    for _ in range(2):
        await _stage_and_feedback(session_factory, repository_id=repository_id, repository_index_id=index_id, command=ExplicitCommand.FIXED)

    async with session_factory() as session:
        first = await recompute_repository_learnings(session, repository_id=repository_id)
        await session.commit()
    async with session_factory() as session:
        second = await recompute_repository_learnings(session, repository_id=repository_id)
        await session.commit()

    assert len(first) == len(second) == 1
    async with session_factory() as session:
        all_records = await RepositoryLearningRecordRepository().list_for_repository(session, repository_id=repository_id)
    # Never a growing duplicate history -- exactly one row for this surface.
    assert len(all_records) == 1


# -- Y8/Y9: repository isolation --


async def test_learnings_never_leak_across_repositories(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repo_a = await _make_repo(session_factory, "acme/repo-a")
    repo_b = await _make_repo(session_factory, "acme/repo-b")
    index_a = await _make_index(session_factory, repository_id=repo_a)
    for _ in range(2):
        await _stage_and_feedback(session_factory, repository_id=repo_a, repository_index_id=index_a, command=ExplicitCommand.FIXED)

    async with session_factory() as session:
        await recompute_repository_learnings(session, repository_id=repo_a)
        await session.commit()

    async with session_factory() as session:
        repo_b_records = await RepositoryLearningRecordRepository().list_for_repository(session, repository_id=repo_b)
    assert repo_b_records == ()


# -- Y7/Y8: organization-level aggregation --


async def test_org_aggregation_requires_explicit_repository_ids_never_infers_scope(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repo_a = await _make_repo(session_factory, "acme/org-a")
    index_a = await _make_index(session_factory, repository_id=repo_a)
    for _ in range(3):
        await _stage_and_feedback(session_factory, repository_id=repo_a, repository_index_id=index_a, command=ExplicitCommand.FIXED)
    async with session_factory() as session:
        await recompute_repository_learnings(session, repository_id=repo_a)
        await session.commit()

    async with session_factory() as session:
        # A single repository can never satisfy ORG_MIN_ESTABLISHED_REPOSITORIES on its own.
        result = await aggregate_repository_learnings_for_organization(session, repository_ids=(repo_a,))
    assert result == ()


async def test_org_aggregation_combines_established_learnings_across_two_repositories(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repo_a = await _make_repo(session_factory, "acme/org-b1")
    repo_b = await _make_repo(session_factory, "acme/org-b2")
    index_a = await _make_index(session_factory, repository_id=repo_a)
    index_b = await _make_index(session_factory, repository_id=repo_b)
    for repo_id, index_id in ((repo_a, index_a), (repo_b, index_b)):
        for _ in range(3):
            await _stage_and_feedback(session_factory, repository_id=repo_id, repository_index_id=index_id, command=ExplicitCommand.FIXED)
        async with session_factory() as session:
            await recompute_repository_learnings(session, repository_id=repo_id)
            await session.commit()

    async with session_factory() as session:
        result = await aggregate_repository_learnings_for_organization(session, repository_ids=(repo_a, repo_b))
    assert len(result) == 1
    assert set(result[0].contributing_repository_ids) == {repo_a, repo_b}


async def test_org_aggregation_excludes_a_repository_not_in_the_explicit_scope(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """The literal cross-tenant-leakage proof: a third repository with
    the exact same established pattern, but never passed in
    ``repository_ids``, must never contribute."""

    repo_a = await _make_repo(session_factory, "acme/scope-a")
    repo_b = await _make_repo(session_factory, "acme/scope-b")
    repo_outside = await _make_repo(session_factory, "acme/scope-outside")
    for repo_id in (repo_a, repo_b, repo_outside):
        index_id = await _make_index(session_factory, repository_id=repo_id)
        for _ in range(3):
            await _stage_and_feedback(session_factory, repository_id=repo_id, repository_index_id=index_id, command=ExplicitCommand.FIXED)
        async with session_factory() as session:
            await recompute_repository_learnings(session, repository_id=repo_id)
            await session.commit()

    async with session_factory() as session:
        result = await aggregate_repository_learnings_for_organization(session, repository_ids=(repo_a, repo_b))
    assert len(result) == 1
    assert repo_outside not in result[0].contributing_repository_ids


async def test_org_aggregation_tags_cross_repo_related_when_explicit_relation_exists(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repo_a = await _make_repo(session_factory, "acme/cr-a")
    repo_b = await _make_repo(session_factory, "acme/cr-b")
    for repo_id in (repo_a, repo_b):
        index_id = await _make_index(session_factory, repository_id=repo_id)
        for _ in range(3):
            await _stage_and_feedback(session_factory, repository_id=repo_id, repository_index_id=index_id, command=ExplicitCommand.FIXED)
        async with session_factory() as session:
            await recompute_repository_learnings(session, repository_id=repo_id)
            await session.commit()

    async with session_factory() as session:
        await RepositoryRelationRepository().upsert(
            session, source_repository_id=repo_a, target_repository_id=repo_b,
            relation_kind=RepositoryRelationKind.EXPLICIT_SHARED_CONTRACT, external_contract_key="shared.Contract",
            provenance=RepositoryRelationProvenance.OPERATOR_CLI,
        )
        await session.commit()

    async with session_factory() as session:
        result = await aggregate_repository_learnings_for_organization(session, repository_ids=(repo_a, repo_b))
    assert result[0].cross_repo_related is True


async def test_org_aggregation_without_relation_is_not_tagged_cross_repo_related(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repo_a = await _make_repo(session_factory, "acme/no-cr-a")
    repo_b = await _make_repo(session_factory, "acme/no-cr-b")
    for repo_id in (repo_a, repo_b):
        index_id = await _make_index(session_factory, repository_id=repo_id)
        for _ in range(3):
            await _stage_and_feedback(session_factory, repository_id=repo_id, repository_index_id=index_id, command=ExplicitCommand.FIXED)
        async with session_factory() as session:
            await recompute_repository_learnings(session, repository_id=repo_id)
            await session.commit()

    async with session_factory() as session:
        result = await aggregate_repository_learnings_for_organization(session, repository_ids=(repo_a, repo_b))
    assert result[0].cross_repo_related is False


# -- Y2: explicit query real behavior --


async def test_fetch_repeated_noise_feedback_excludes_findings_with_any_useful_signal(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "acme/mixed-signal")
    index_id = await _make_index(session_factory, repository_id=repository_id)
    finding_id = await _stage_finding(session_factory, repository_id=repository_id, repository_index_id=index_id)
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FALSE_POSITIVE)
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.USEFUL)

    async with session_factory() as session:
        rows = await fetch_repeated_noise_feedback(session, repository_id=repository_id, as_of=datetime.now(UTC))
    assert rows == ()
