"""Deterministic coverage for
:func:`patchfrog.publishing.queries.get_current_active_findings` --
Phase 6/7's merged "canonical current active findings" query.

Closes a real gap found live during Milestone H's own production dogfood
(see ``validation/production_e2e/latest-summary.md``'s "known
limitation" section, now resolved): a finding that Phase 7
(:mod:`patchfrog.review_memory`) zero-AI-call carried forward into a
review run -- symbol continuity UNCHANGED, evidence reconfirmed verbatim
at that run's own exact commit -- was never copied into that run's own
``ai_findings`` rows, so publishing (which previously only ever read a
run's own fresh findings) could never publish it even once a publish
gate opened on a later head. These tests hand-craft
:class:`~patchfrog.persistence.models.review_memory.ReviewMemoryFindingModel`
/ publication history rows directly (mirroring
``tests/integration/test_publishing_idempotency.py``'s own pattern for
:class:`~patchfrog.persistence.models.publishing.ReviewPublicationModel`)
so every edge case can be exercised cheaply and deterministically, with
no live LLM and no real git repo required -- the real, end-to-end
(incremental-review + publish) proof of the same mechanism lives in
``tests/integration/test_publishing_carried_forward_findings.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.domain.code import SymbolKind
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.publishing import ReviewPublicationModel
from patchfrog.persistence.models.review import AIFindingModel
from patchfrog.persistence.models.review_memory import ReviewMemoryFindingModel
from patchfrog.persistence.repositories.review_publication_comment import (
    ReviewPublicationCommentRepository,
)
from patchfrog.publishing.domain import (
    PublicationDisposition,
    ReviewPublicationComment,
    ReviewPublicationMode,
    ReviewPublicationStatus,
)
from patchfrog.publishing.queries import get_current_active_findings
from patchfrog.review.providers.fake import FakeLLMProvider
from patchfrog.review.service import PullRequestReviewService
from patchfrog.review_memory.domain import FindingMemoryStatus
from tests.support.git_repo import commit_all
from tests.support.publishing import (
    ReviewedPullRequest,
    diff_marking_lines,
    finding_json,
    scripted_findings_response,
    setup_reviewed_pull_request,
)


async def _make_memory_row(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    pull_request_id: uuid.UUID,
    source_review_run_id: uuid.UUID,
    source_finding_id: uuid.UUID,
    current_review_run_id: uuid.UUID | None,
    current_finding_id: uuid.UUID | None,
    status: FindingMemoryStatus,
    commit_sha: str = "c" * 40,
    semantic_family_fingerprint: str | None = None,
) -> ReviewMemoryFindingModel:
    model = ReviewMemoryFindingModel(
        repository_id=repository_id,
        pull_request_id=pull_request_id,
        source_review_run_id=source_review_run_id,
        source_finding_id=source_finding_id,
        current_finding_id=current_finding_id,
        current_review_run_id=current_review_run_id,
        first_seen_commit_sha="a" * 40,
        last_seen_commit_sha=commit_sha,
        file_path="src/billing.py",
        symbol_id=None,
        symbol_qualified_name="billing.can_withdraw",
        symbol_kind=SymbolKind.FUNCTION,
        category=FindingCategory.CORRECTNESS,
        severity=Severity.HIGH,
        title="carried finding",
        message="carried finding message",
        start_line=14,
        end_line=14,
        exact_fingerprint=uuid.uuid4().hex,
        semantic_family_fingerprint=semantic_family_fingerprint or uuid.uuid4().hex,
        status=status,
    )
    session.add(model)
    await session.commit()
    return model


async def _mark_actually_published(
    session: AsyncSession,
    *,
    review_run_id: uuid.UUID,
    repository_id: uuid.UUID,
    pull_request_id: uuid.UUID,
    pull_request_number: int,
    head_sha: str,
    finding_id: uuid.UUID,
    disposition: PublicationDisposition = PublicationDisposition.INLINE,
    status: ReviewPublicationStatus = ReviewPublicationStatus.PUBLISHED,
) -> None:
    """Directly persists a publication + comment row shaped exactly like
    a real successful publish would leave behind -- deliberately not
    going through :class:`~patchfrog.publishing.service.ReviewPublicationService`
    so these tests stay a pure, fast unit of the query under test."""

    publication = ReviewPublicationModel(
        review_run_id=review_run_id,
        repository_id=repository_id,
        pull_request_id=pull_request_id,
        pull_request_number=pull_request_number,
        head_sha=head_sha,
        mode=ReviewPublicationMode.PUBLISH,
        publication_policy_fingerprint=uuid.uuid4().hex,
        status=status,
        started_at=datetime.now(UTC),
    )
    session.add(publication)
    await session.commit()

    comment = ReviewPublicationComment(
        finding_id=finding_id,
        fingerprint=uuid.uuid4().hex,
        path="src/billing.py",
        body="body",
        severity=Severity.HIGH,
        disposition=disposition,
        reason="test setup",
    )
    await ReviewPublicationCommentRepository().create(
        session, review_publication_id=publication.id, comment=comment
    )
    await session.commit()


async def _second_run(
    session_factory: async_sessionmaker[AsyncSession], *, tmp_path: Path, full_name: str
) -> tuple[ReviewedPullRequest, AIFindingModel, uuid.UUID]:
    """A real PR with one real ``ai_findings`` row from run1 (findingA),
    plus a real, independent run2 with zero fresh findings of its own --
    the exact shape a zero-AI-call carried-forward run leaves behind."""

    reviewed1 = await setup_reviewed_pull_request(
        session_factory,
        full_name=full_name,
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )
    assert len(reviewed1.findings) == 1
    finding_a = reviewed1.findings[0]

    # A real second commit (config-only -- billing.py itself is
    # untouched) so RepositoryIndexingService has a matching index for
    # the new head, exactly as a real "publish gate opened later" commit
    # would look.
    (reviewed1.root_path / "NOTES.md").write_text("config-only follow-up commit\n")
    commit2_sha = commit_all(reviewed1.root_path, "config-only follow-up")
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=reviewed1.repository_id, root_path=reviewed1.root_path, repository_full_name=full_name
    )

    provider2 = FakeLLMProvider(response_factory=lambda req: scripted_findings_response([]))
    service2 = PullRequestReviewService(session_factory=session_factory, reviewer_provider=provider2)
    summary2 = await service2.review_local(
        repository_id=reviewed1.repository_id,
        root_path=reviewed1.root_path,
        repository_full_name=full_name,
        commit_sha=commit2_sha,
        diff_files=[diff_marking_lines("src/billing.py", [14])],
        pull_request_id=reviewed1.pull_request_id,
    )
    return reviewed1, finding_a, summary2.run_id


async def test_zero_call_carried_forward_finding_becomes_publishable(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed1, finding_a, run2_id = await _second_run(
        session_factory, tmp_path=tmp_path, full_name="test/current-active-1"
    )

    async with session_factory() as session:
        await _make_memory_row(
            session,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            source_review_run_id=reviewed1.review_run_id,
            source_finding_id=finding_a.id,
            current_review_run_id=run2_id,
            current_finding_id=finding_a.id,
            status=FindingMemoryStatus.CARRIED_FORWARD,
        )

    async with session_factory() as session:
        findings, already_reported = await get_current_active_findings(session, review_run_id=run2_id)

    assert {f.finding_id for f in findings} == {finding_a.id}
    assert already_reported == frozenset()


async def test_already_published_carried_forward_finding_is_suppressed_not_readded(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed1, finding_a, run2_id = await _second_run(
        session_factory, tmp_path=tmp_path, full_name="test/current-active-2"
    )

    async with session_factory() as session:
        await _make_memory_row(
            session,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            source_review_run_id=reviewed1.review_run_id,
            source_finding_id=finding_a.id,
            current_review_run_id=run2_id,
            current_finding_id=finding_a.id,
            status=FindingMemoryStatus.CARRIED_FORWARD,
        )
        await _mark_actually_published(
            session,
            review_run_id=reviewed1.review_run_id,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            pull_request_number=reviewed1.pull_request_number,
            head_sha=reviewed1.commit_sha,
            finding_id=finding_a.id,
        )

    async with session_factory() as session:
        findings, already_reported = await get_current_active_findings(session, review_run_id=run2_id)

    assert findings == []
    assert already_reported == {finding_a.id}


async def test_dry_run_only_publication_does_not_count_as_actually_published(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A DRY_RUN-status publication (or a PUBLISHED-mode attempt that
    never reached PUBLISHED status) never wrote anything real to GitHub
    -- it must never suppress a later genuine publish."""

    reviewed1, finding_a, run2_id = await _second_run(
        session_factory, tmp_path=tmp_path, full_name="test/current-active-3"
    )

    async with session_factory() as session:
        await _make_memory_row(
            session,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            source_review_run_id=reviewed1.review_run_id,
            source_finding_id=finding_a.id,
            current_review_run_id=run2_id,
            current_finding_id=finding_a.id,
            status=FindingMemoryStatus.CARRIED_FORWARD,
        )
        await _mark_actually_published(
            session,
            review_run_id=reviewed1.review_run_id,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            pull_request_number=reviewed1.pull_request_number,
            head_sha=reviewed1.commit_sha,
            finding_id=finding_a.id,
            status=ReviewPublicationStatus.DRY_RUN,
        )

    async with session_factory() as session:
        findings, already_reported = await get_current_active_findings(session, review_run_id=run2_id)

    assert {f.finding_id for f in findings} == {finding_a.id}
    assert already_reported == frozenset()


async def test_already_reported_disposition_does_not_count_as_actually_published(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """An ``ALREADY_REPORTED`` comment never wrote anything to GitHub
    either (it is the suppression outcome itself) -- must never be
    treated as its own justification for suppressing again."""

    reviewed1, finding_a, run2_id = await _second_run(
        session_factory, tmp_path=tmp_path, full_name="test/current-active-4"
    )

    async with session_factory() as session:
        await _make_memory_row(
            session,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            source_review_run_id=reviewed1.review_run_id,
            source_finding_id=finding_a.id,
            current_review_run_id=run2_id,
            current_finding_id=finding_a.id,
            status=FindingMemoryStatus.CARRIED_FORWARD,
        )
        await _mark_actually_published(
            session,
            review_run_id=reviewed1.review_run_id,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            pull_request_number=reviewed1.pull_request_number,
            head_sha=reviewed1.commit_sha,
            finding_id=finding_a.id,
            disposition=PublicationDisposition.ALREADY_REPORTED,
        )

    async with session_factory() as session:
        findings, already_reported = await get_current_active_findings(session, review_run_id=run2_id)

    assert {f.finding_id for f in findings} == {finding_a.id}
    assert already_reported == frozenset()


async def test_resolved_finding_is_never_carried_or_published(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed1, finding_a, run2_id = await _second_run(
        session_factory, tmp_path=tmp_path, full_name="test/current-active-5"
    )

    async with session_factory() as session:
        await _make_memory_row(
            session,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            source_review_run_id=reviewed1.review_run_id,
            source_finding_id=finding_a.id,
            current_review_run_id=run2_id,
            current_finding_id=None,  # cleared on RESOLVED -- see apply_transition
            status=FindingMemoryStatus.RESOLVED,
        )

    async with session_factory() as session:
        findings, already_reported = await get_current_active_findings(session, review_run_id=run2_id)

    assert findings == []
    assert already_reported == frozenset()


async def test_changed_finding_requires_real_rereview_never_blindly_carried(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``CHANGED`` means evidence materially differs -- Phase 5 already
    re-reviewed it for real this run (its fresh row, if accepted, comes
    back from :func:`get_publishable_findings` on its own merits); the
    memory-merge path must never separately re-add it."""

    reviewed1, finding_a, run2_id = await _second_run(
        session_factory, tmp_path=tmp_path, full_name="test/current-active-6"
    )

    async with session_factory() as session:
        await _make_memory_row(
            session,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            source_review_run_id=reviewed1.review_run_id,
            source_finding_id=finding_a.id,
            current_review_run_id=run2_id,
            current_finding_id=finding_a.id,
            status=FindingMemoryStatus.CHANGED,
        )

    async with session_factory() as session:
        findings, already_reported = await get_current_active_findings(session, review_run_id=run2_id)

    assert findings == []
    assert already_reported == frozenset()


async def test_ambiguous_continuity_never_carried_or_published(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed1, finding_a, run2_id = await _second_run(
        session_factory, tmp_path=tmp_path, full_name="test/current-active-7"
    )

    async with session_factory() as session:
        await _make_memory_row(
            session,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            source_review_run_id=reviewed1.review_run_id,
            source_finding_id=finding_a.id,
            current_review_run_id=run2_id,
            current_finding_id=finding_a.id,
            status=FindingMemoryStatus.AMBIGUOUS,
        )

    async with session_factory() as session:
        findings, already_reported = await get_current_active_findings(session, review_run_id=run2_id)

    assert findings == []
    assert already_reported == frozenset()


async def test_memory_row_scoped_to_a_different_run_is_ignored(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A ``CARRIED_FORWARD`` row whose ``current_review_run_id`` points
    at some *other* run must never leak into this run's publishable set
    -- the merge is always scoped to the exact run being published."""

    reviewed1, finding_a, run2_id = await _second_run(
        session_factory, tmp_path=tmp_path, full_name="test/current-active-8"
    )

    async with session_factory() as session:
        await _make_memory_row(
            session,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            source_review_run_id=reviewed1.review_run_id,
            source_finding_id=finding_a.id,
            current_review_run_id=uuid.uuid4(),  # some other run entirely
            current_finding_id=finding_a.id,
            status=FindingMemoryStatus.CARRIED_FORWARD,
        )

    async with session_factory() as session:
        findings, already_reported = await get_current_active_findings(session, review_run_id=run2_id)

    assert findings == []
    assert already_reported == frozenset()


async def test_fresh_and_carried_findings_are_never_double_added(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A finding_id already present in this run's own fresh
    ``ai_findings`` (the RECHECK_CONFIRMED path, where the resolver
    calls ``_reconcile_against_match`` and sets ``current_finding_id`` to
    a *fresh* row created this run) must never be appended a second
    time."""

    reviewed1 = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/current-active-9",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )
    finding_a = reviewed1.findings[0]

    async with session_factory() as session:
        # This run's own memory row happens to be CARRIED_FORWARD and
        # point back at THIS SAME run's own fresh finding (a real recheck
        # that reconfirmed the identical finding) -- get_publishable_findings
        # already returns it once; the merge must not add it again.
        await _make_memory_row(
            session,
            repository_id=reviewed1.repository_id,
            pull_request_id=reviewed1.pull_request_id,
            source_review_run_id=reviewed1.review_run_id,
            source_finding_id=finding_a.id,
            current_review_run_id=reviewed1.review_run_id,
            current_finding_id=finding_a.id,
            status=FindingMemoryStatus.CARRIED_FORWARD,
        )

    async with session_factory() as session:
        findings, already_reported = await get_current_active_findings(
            session, review_run_id=reviewed1.review_run_id
        )

    assert [f.finding_id for f in findings] == [finding_a.id]  # exactly once
    assert already_reported == frozenset()


async def test_no_memory_rows_at_all_behaves_exactly_like_get_publishable_findings(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed1 = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/current-active-10",
        changed_lines=[14],
        response_factory=lambda req: scripted_findings_response([finding_json()]),
        tmp_root=tmp_path,
    )
    finding_a = reviewed1.findings[0]

    async with session_factory() as session:
        findings, already_reported = await get_current_active_findings(
            session, review_run_id=reviewed1.review_run_id
        )

    assert [f.finding_id for f in findings] == [finding_a.id]
    assert already_reported == frozenset()
