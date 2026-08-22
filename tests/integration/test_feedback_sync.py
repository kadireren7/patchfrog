"""Integration coverage for :mod:`patchfrog.feedback.sync` -- the real
GitHub-polling ingestion path, against real Phase 5/6 pipeline output
(two genuine published findings, via ``tests.support.publishing``) and a
duck-typed fake GitHub client (mirrors ``FakeReviewPublisher``'s
structural-typing convention).

Covers: github_comment_id enrichment, per-finding attribution never
crossing findings (Phase 9 spec section 39), bot self-feedback filtering
(section 38), idempotent duplicate sync (section 12), reaction removal
(section 14), explicit command parsing end-to-end, thread resolution
transitions, and PR lifecycle events.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.github_feedback import (
    GitHubActor,
    GitHubActorType,
    GitHubReaction,
    GitHubReactionContent,
    GitHubReviewComment,
    GitHubReviewThreadStatus,
)
from patchfrog.domain.pull_request import PullRequestMetadata, PullRequestRef
from patchfrog.feedback.domain import FeedbackEventType
from patchfrog.feedback.queries import get_feedback_for_finding, get_feedback_summary_for_finding
from patchfrog.feedback.sync import GitHubFeedbackSyncService
from patchfrog.github.errors import GitHubNotFoundError
from patchfrog.persistence.models.publishing import (
    ReviewPublicationCommentModel,
    ReviewPublicationModel,
)
from patchfrog.publishing.config import PublicationConfig
from patchfrog.publishing.domain import ReviewPublicationMode
from patchfrog.publishing.fake_publisher import FakeReviewPublisher
from patchfrog.publishing.service import ReviewPublicationService
from tests.support.publishing import (
    ReviewedPullRequest,
    finding_json,
    scripted_findings_response,
    setup_reviewed_pull_request,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_HUMAN = GitHubActor(login="developer", actor_type=GitHubActorType.USER)
_BOT = GitHubActor(login="patchfrog[bot]", actor_type=GitHubActorType.BOT)


def _pr_metadata(*, number: int, head_sha: str, state: str = "open", merged: bool = False) -> PullRequestMetadata:
    return PullRequestMetadata(
        number=number, title="t", body=None, author="a", base_branch="main", head_branch="feature",
        base_sha="0" * 40, head_sha=head_sha, html_url="https://github.com/test/repo/pull/1",
        state=state, merged=merged,
    )


class _FakeFeedbackGitHubClient:
    """Duck-typed stand-in for :class:`patchfrog.github.client.GitHubClient`
    -- satisfies :class:`GitHubFeedbackSyncService`'s structural needs,
    mirroring :class:`patchfrog.publishing.fake_publisher.FakeReviewPublisher`'s
    convention exactly."""

    def __init__(self, *, pull_request: PullRequestMetadata) -> None:
        self.review_comments: list[GitHubReviewComment] = []
        self.reactions_by_comment_id: dict[int, list[GitHubReaction]] = {}
        self.thread_statuses: list[GitHubReviewThreadStatus] = []
        self.pull_request = pull_request
        self.deleted_comment_ids: set[int] = set()

    async def list_pull_request_review_comments(self, *, installation_id: int, ref: PullRequestRef) -> list[GitHubReviewComment]:
        return list(self.review_comments)

    async def list_review_comment_reactions(
        self, *, installation_id: int, ref: PullRequestRef, comment_id: int
    ) -> list[GitHubReaction]:
        if comment_id in self.deleted_comment_ids:
            raise GitHubNotFoundError(f"comment {comment_id} not found")
        return list(self.reactions_by_comment_id.get(comment_id, []))

    async def list_review_thread_statuses(self, *, installation_id: int, ref: PullRequestRef) -> list[GitHubReviewThreadStatus]:
        return list(self.thread_statuses)

    async def get_pull_request(self, *, installation_id: int, ref: PullRequestRef) -> PullRequestMetadata:
        return self.pull_request


async def _publish_two_findings(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[ReviewedPullRequest, FakeReviewPublisher, PullRequestMetadata]:
    reviewed = await setup_reviewed_pull_request(
        session_factory,
        full_name="test/feedback-sync",
        changed_lines=[14, 21],
        response_factory=lambda req: scripted_findings_response(
            [
                finding_json(
                    title="Inverted comparison", message="backwards", file_path="src/billing.py",
                    start_line=14, end_line=14, quoted_text="return amount >= balance",
                ),
                finding_json(
                    title="Wrong status on failure", message="marks paid on failure",
                    file_path="src/billing.py", start_line=21, end_line=21, quoted_text='order.status = "paid"',
                ),
            ]
        ),
        tmp_root=tmp_path,
    )
    assert len(reviewed.findings) == 2

    pr_metadata = _pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha)
    publisher = FakeReviewPublisher(pull_request=pr_metadata, changed_files=reviewed.changed_files)
    service = ReviewPublicationService(session_factory=session_factory, publisher=publisher)
    result = await service.publish(
        review_run_id=reviewed.review_run_id, mode=ReviewPublicationMode.PUBLISH, config=PublicationConfig(enabled=True)
    )
    assert result.published_inline == 2
    assert len(publisher.publish_calls) == 1

    return reviewed, publisher, pr_metadata


def _github_comments_from_publish_call(publisher: FakeReviewPublisher, *, start_id: int = 5001) -> list[GitHubReviewComment]:
    comments = []
    for i, sent in enumerate(publisher.publish_calls[0].comments):
        comments.append(
            GitHubReviewComment(
                id=start_id + i, path=sent.path, line=sent.line, original_line=sent.line,
                side=sent.side.value, body=sent.body, actor=_BOT, in_reply_to_id=None,
                pull_request_review_id=9000,
                created_at=_NOW, updated_at=_NOW,
            )
        )
    return comments


async def test_sync_enriches_github_comment_id_deterministically(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed, publisher, pr_metadata = await _publish_two_findings(session_factory, tmp_path)
    gh_comments = _github_comments_from_publish_call(publisher)
    fake_client = _FakeFeedbackGitHubClient(pull_request=pr_metadata)
    fake_client.review_comments = gh_comments

    sync = GitHubFeedbackSyncService(session_factory=session_factory, github_client=fake_client)  # type: ignore[arg-type]
    result = await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)

    assert result.github_comment_ids_enriched == 2

    async with session_factory() as session:
        pub = (await session.execute(select(ReviewPublicationModel))).scalars().one()
        pub_comments = (
            await session.execute(
                select(ReviewPublicationCommentModel).where(ReviewPublicationCommentModel.review_publication_id == pub.id)
            )
        ).scalars().all()
        assert all(c.github_comment_id is not None for c in pub_comments)
        assert {c.github_comment_id for c in pub_comments} == {5001, 5002}


async def test_reactions_never_cross_findings(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    """Phase 9 spec section 39: two findings in one review -- a reaction
    on comment A must attribute only to finding A, never finding B."""

    reviewed, publisher, pr_metadata = await _publish_two_findings(session_factory, tmp_path)
    gh_comments = _github_comments_from_publish_call(publisher)
    fake_client = _FakeFeedbackGitHubClient(pull_request=pr_metadata)
    fake_client.review_comments = gh_comments
    fake_client.reactions_by_comment_id[5001] = [
        GitHubReaction(id=1, content=GitHubReactionContent.PLUS_ONE, actor=_HUMAN, created_at=_NOW)
    ]
    fake_client.reactions_by_comment_id[5002] = [
        GitHubReaction(id=2, content=GitHubReactionContent.MINUS_ONE, actor=_HUMAN, created_at=_NOW)
    ]

    sync = GitHubFeedbackSyncService(session_factory=session_factory, github_client=fake_client)  # type: ignore[arg-type]
    await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)

    finding_a, finding_b = reviewed.findings[0], reviewed.findings[1]
    async with session_factory() as session:
        summary_a = await get_feedback_summary_for_finding(session, finding_id=finding_a.id)
        summary_b = await get_feedback_summary_for_finding(session, finding_id=finding_b.id)

    # Whichever comment maps to which finding, exactly one finding got
    # the positive reaction and the other got the negative -- never both,
    # never neither.
    positives = {finding_a.id: summary_a.positive_reactions, finding_b.id: summary_b.positive_reactions}
    negatives = {finding_a.id: summary_a.negative_reactions, finding_b.id: summary_b.negative_reactions}
    assert sorted(positives.values()) == [0, 1]
    assert sorted(negatives.values()) == [0, 1]
    assert list(positives.values()) != list(negatives.values())  # not both on the same finding


async def test_bot_reactions_and_replies_are_never_ingested_as_feedback(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed, publisher, pr_metadata = await _publish_two_findings(session_factory, tmp_path)
    gh_comments = _github_comments_from_publish_call(publisher)
    bot_reply = GitHubReviewComment(
        id=6001, path=gh_comments[0].path, line=gh_comments[0].line, original_line=gh_comments[0].line,
        side=gh_comments[0].side, body="/patchfrog useful", actor=_BOT, in_reply_to_id=5001,
        pull_request_review_id=None, created_at=_NOW, updated_at=_NOW,
    )
    fake_client = _FakeFeedbackGitHubClient(pull_request=pr_metadata)
    fake_client.review_comments = [*gh_comments, bot_reply]
    fake_client.reactions_by_comment_id[5001] = [
        GitHubReaction(id=1, content=GitHubReactionContent.PLUS_ONE, actor=_BOT, created_at=_NOW)
    ]

    sync = GitHubFeedbackSyncService(session_factory=session_factory, github_client=fake_client)  # type: ignore[arg-type]
    result = await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)

    assert result.events_ingested == 0  # bot reaction and bot reply both filtered before ever being ingested

    async with session_factory() as session:
        finding_a = reviewed.findings[0]
        events = await get_feedback_for_finding(session, finding_id=finding_a.id)
    assert events == []


async def test_duplicate_sync_never_creates_duplicate_events(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed, publisher, pr_metadata = await _publish_two_findings(session_factory, tmp_path)
    gh_comments = _github_comments_from_publish_call(publisher)
    fake_client = _FakeFeedbackGitHubClient(pull_request=pr_metadata)
    fake_client.review_comments = gh_comments
    fake_client.reactions_by_comment_id[5001] = [
        GitHubReaction(id=1, content=GitHubReactionContent.PLUS_ONE, actor=_HUMAN, created_at=_NOW)
    ]

    sync = GitHubFeedbackSyncService(session_factory=session_factory, github_client=fake_client)  # type: ignore[arg-type]
    first = await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)
    second = await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)

    assert first.events_ingested >= 1
    assert second.events_ingested == 0
    assert second.duplicate_events_ignored >= first.events_ingested

    async with session_factory() as session:
        finding_a = reviewed.findings[0]
        summary = await get_feedback_summary_for_finding(session, finding_id=finding_a.id)
    assert summary.positive_reactions == 1  # not 2


async def test_reaction_removal_is_detected_on_a_later_sync(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed, publisher, pr_metadata = await _publish_two_findings(session_factory, tmp_path)
    gh_comments = _github_comments_from_publish_call(publisher)
    fake_client = _FakeFeedbackGitHubClient(pull_request=pr_metadata)
    fake_client.review_comments = gh_comments
    fake_client.reactions_by_comment_id[5001] = [
        GitHubReaction(id=1, content=GitHubReactionContent.PLUS_ONE, actor=_HUMAN, created_at=_NOW)
    ]

    sync = GitHubFeedbackSyncService(session_factory=session_factory, github_client=fake_client)  # type: ignore[arg-type]
    await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)

    # Reaction removed on GitHub -- next sync must see it gone.
    fake_client.reactions_by_comment_id[5001] = []
    await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)

    finding_a = reviewed.findings[0]
    async with session_factory() as session:
        summary = await get_feedback_summary_for_finding(session, finding_id=finding_a.id)
        events = await get_feedback_for_finding(session, finding_id=finding_a.id)

    assert summary.positive_reactions == 0
    event_types = [e.event_type for e in events]
    assert FeedbackEventType.REACTION_ADDED in event_types
    assert FeedbackEventType.REACTION_REMOVED in event_types


async def test_explicit_command_reply_is_parsed_end_to_end(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed, publisher, pr_metadata = await _publish_two_findings(session_factory, tmp_path)
    gh_comments = _github_comments_from_publish_call(publisher)
    reply = GitHubReviewComment(
        id=7001, path=gh_comments[0].path, line=gh_comments[0].line, original_line=gh_comments[0].line,
        side=gh_comments[0].side, body="/patchfrog false-positive", actor=_HUMAN, in_reply_to_id=5001,
        pull_request_review_id=None, created_at=_NOW, updated_at=_NOW,
    )
    fake_client = _FakeFeedbackGitHubClient(pull_request=pr_metadata)
    fake_client.review_comments = [*gh_comments, reply]

    sync = GitHubFeedbackSyncService(session_factory=session_factory, github_client=fake_client)  # type: ignore[arg-type]
    await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)

    finding_a = reviewed.findings[0]
    async with session_factory() as session:
        summary = await get_feedback_summary_for_finding(session, finding_id=finding_a.id)

    assert summary.explicit_false_positive == 1
    assert summary.developer_replies == 1  # the reply itself is also recorded
    assert summary.assessment.correctness_signal.value == "negative"


async def test_never_resolved_thread_is_not_recorded_as_reopened(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A thread that has never been resolved and still isn't is not a
    transition -- it's the ordinary default state. Recording it as
    THREAD_REOPENED would falsely imply it was once resolved."""

    reviewed, publisher, pr_metadata = await _publish_two_findings(session_factory, tmp_path)
    gh_comments = _github_comments_from_publish_call(publisher)
    fake_client = _FakeFeedbackGitHubClient(pull_request=pr_metadata)
    fake_client.review_comments = gh_comments
    fake_client.thread_statuses = [GitHubReviewThreadStatus(first_comment_id=5001, is_resolved=False)]

    sync = GitHubFeedbackSyncService(session_factory=session_factory, github_client=fake_client)  # type: ignore[arg-type]
    result = await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)

    finding_a = reviewed.findings[0]
    async with session_factory() as session:
        events = await get_feedback_for_finding(session, finding_id=finding_a.id)

    assert FeedbackEventType.THREAD_REOPENED not in [e.event_type for e in events]
    assert result.events_ingested == 0


async def test_thread_resolution_transition_and_idempotent_resync(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed, publisher, pr_metadata = await _publish_two_findings(session_factory, tmp_path)
    gh_comments = _github_comments_from_publish_call(publisher)
    fake_client = _FakeFeedbackGitHubClient(pull_request=pr_metadata)
    fake_client.review_comments = gh_comments
    fake_client.thread_statuses = [GitHubReviewThreadStatus(first_comment_id=5001, is_resolved=True)]

    sync = GitHubFeedbackSyncService(session_factory=session_factory, github_client=fake_client)  # type: ignore[arg-type]
    first = await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)
    second = await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)

    finding_a = reviewed.findings[0]
    async with session_factory() as session:
        summary = await get_feedback_summary_for_finding(session, finding_id=finding_a.id)

    assert summary.thread_resolved is True
    assert summary.assessment.resolution_signal.value == "closed"
    assert summary.assessment.correctness_signal.value == "unknown"  # resolution is never correctness proof
    assert first.events_ingested >= 1
    assert second.events_ingested == 0  # no state transition on the second sync -- nothing new to record


async def test_pr_merged_event_has_no_finding_attribution(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    reviewed, publisher, _pr_meta = await _publish_two_findings(session_factory, tmp_path)
    gh_comments = _github_comments_from_publish_call(publisher)
    fake_client = _FakeFeedbackGitHubClient(
        pull_request=_pr_metadata(number=reviewed.pull_request_number, head_sha=reviewed.commit_sha, state="closed", merged=True)
    )
    fake_client.review_comments = gh_comments

    sync = GitHubFeedbackSyncService(session_factory=session_factory, github_client=fake_client)  # type: ignore[arg-type]
    result = await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)

    assert result.unattributed_events == 1  # the PR_MERGED event itself has no finding_id


async def test_a_deleted_github_comment_does_not_abort_the_whole_sync(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Phase 9 spec section 42: a GitHub comment deleted out from under
    PatchFrog must fail gracefully for that one comment, never abort
    syncing every other finding's feedback in the same PR."""

    reviewed, publisher, pr_metadata = await _publish_two_findings(session_factory, tmp_path)
    gh_comments = _github_comments_from_publish_call(publisher)
    fake_client = _FakeFeedbackGitHubClient(pull_request=pr_metadata)
    fake_client.review_comments = gh_comments
    fake_client.deleted_comment_ids = {5001}  # finding A's comment was deleted on GitHub
    fake_client.reactions_by_comment_id[5002] = [
        GitHubReaction(id=1, content=GitHubReactionContent.PLUS_ONE, actor=_HUMAN, created_at=_NOW)
    ]

    sync = GitHubFeedbackSyncService(session_factory=session_factory, github_client=fake_client)  # type: ignore[arg-type]
    result = await sync.sync_pull_request(repository_id=reviewed.repository_id, pull_request_number=reviewed.pull_request_number)

    finding_b = reviewed.findings[1]
    async with session_factory() as session:
        summary_b = await get_feedback_summary_for_finding(session, finding_id=finding_b.id)

    assert summary_b.positive_reactions == 1  # finding B's feedback still made it through
    assert result.events_ingested >= 1
