"""GitHub feedback synchronization (Phase 9) -- the only ingestion path.

Deliberately poll-only, never webhook-driven. See the Phase 9 permissions
audit in ``docs/feedback.md``: GitHub has no webhook event for reaction
add/remove at all (true regardless of App configuration), and PatchFrog's
App is currently subscribed only to the ``pull_request`` webhook event,
not ``pull_request_review_comment``/``pull_request_review_thread``. Every
call in this module uses only the App's existing
``contents:read``/``metadata:read``/``pull_requests:write`` grant -- no
new permission was requested, and none of this can be triggered by a
webhook today; it always runs on demand via
``python -m patchfrog.cli feedback sync --pr <number>``.

Always safe to re-run: every raw-event insert is idempotent (see
:meth:`patchfrog.persistence.repositories.feedback.FeedbackEventRepository.create_if_new`),
and ``github_comment_id`` enrichment (Phase 9 spec section 10) only ever
fills a currently-``NULL`` column, so a repeated sync against an
already-enriched publication is a no-op there too.

Every bot actor (``user.type == "Bot"`` -- this covers PatchFrog's own
bot identity along with any other bot commenting on the PR) is filtered
out before a reaction/reply/command is ever recorded as feedback (Phase 9
spec section 38).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.github_feedback import GitHubActorType, GitHubReviewComment
from patchfrog.domain.pull_request import PullRequestRef
from patchfrog.feedback.attribution import PublicationCommentKey, match_comments_to_publication
from patchfrog.feedback.commands import parse_explicit_command
from patchfrog.feedback.domain import (
    ActorIdentity,
    FeedbackEvent,
    FeedbackEventType,
    FeedbackSource,
    SignalStrength,
    normalize_reaction,
)
from patchfrog.github.client import GitHubClient
from patchfrog.github.errors import GitHubNotFoundError
from patchfrog.ops import metrics
from patchfrog.persistence.models.feedback import FeedbackEventModel
from patchfrog.persistence.models.publishing import (
    ReviewPublicationCommentModel,
    ReviewPublicationModel,
)
from patchfrog.persistence.models.pull_request import PullRequestModel
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.review_memory import ReviewMemoryFindingModel
from patchfrog.persistence.repositories.feedback import FeedbackEventRepository
from patchfrog.persistence.repositories.review_publication_comment import (
    ReviewPublicationCommentRepository,
)
from patchfrog.publishing.domain import ReviewPublicationStatus
from patchfrog.review_memory.domain import FindingMemoryStatus

_LIFECYCLE_EVENT_BY_STATUS: dict[FindingMemoryStatus, FeedbackEventType] = {
    FindingMemoryStatus.CARRIED_FORWARD: FeedbackEventType.FINDING_CODE_UNCHANGED,
    FindingMemoryStatus.CHANGED: FeedbackEventType.FINDING_CODE_CHANGED,
    FindingMemoryStatus.RESOLVED: FeedbackEventType.FINDING_DISAPPEARED,
}


@dataclass(frozen=True, slots=True)
class FeedbackSyncResult:
    repository_id: uuid.UUID
    pull_request_number: int
    events_observed: int
    events_ingested: int
    duplicate_events_ignored: int
    unattributed_events: int
    github_comment_ids_enriched: int


class GitHubFeedbackSyncService:
    def __init__(
        self, *, session_factory: async_sessionmaker[AsyncSession], github_client: GitHubClient
    ) -> None:
        self._session_factory = session_factory
        self._github_client = github_client
        self._event_repo = FeedbackEventRepository()
        self._comment_repo = ReviewPublicationCommentRepository()

    async def sync_pull_request(
        self, *, repository_id: uuid.UUID, pull_request_number: int
    ) -> FeedbackSyncResult:
        async with self._session_factory() as session:
            repository = await session.get(RepositoryModel, repository_id)
            if repository is None:
                raise ValueError(f"no repository with id {repository_id}")
            pull_request = (
                await session.execute(
                    select(PullRequestModel).where(
                        PullRequestModel.repository_id == repository_id,
                        PullRequestModel.github_pr_number == pull_request_number,
                    )
                )
            ).scalar_one_or_none()
            if pull_request is None:
                raise ValueError(
                    f"no pull request #{pull_request_number} ingested for repository {repository_id}"
                )
            publications = list(
                (
                    await session.execute(
                        select(ReviewPublicationModel).where(
                            ReviewPublicationModel.pull_request_id == pull_request.id,
                            ReviewPublicationModel.status == ReviewPublicationStatus.PUBLISHED,
                        )
                    )
                )
                .scalars()
                .all()
            )

        ref = PullRequestRef(owner=repository.owner, repository=repository.name, number=pull_request_number)
        installation_id = repository.installation_id

        counters = _Counters()

        github_comments = await self._github_client.list_pull_request_review_comments(
            installation_id=installation_id, ref=ref
        )

        async with self._session_factory() as session:
            for publication in publications:
                enriched_ids = await self._enrich_github_comment_ids(
                    session, publication=publication, github_comments=github_comments
                )
                counters.enriched += enriched_ids

                pub_comments = list(
                    (
                        await session.execute(
                            select(ReviewPublicationCommentModel).where(
                                ReviewPublicationCommentModel.review_publication_id == publication.id,
                                ReviewPublicationCommentModel.github_comment_id.is_not(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

                for pub_comment in pub_comments:
                    await self._sync_reactions(
                        session,
                        repository_id=repository_id,
                        pull_request=pull_request,
                        publication=publication,
                        pub_comment=pub_comment,
                        installation_id=installation_id,
                        ref=ref,
                        counters=counters,
                    )
                    await self._sync_replies_and_commands(
                        session,
                        repository_id=repository_id,
                        pull_request=pull_request,
                        publication=publication,
                        pub_comment=pub_comment,
                        github_comments=github_comments,
                        counters=counters,
                    )

            await session.flush()

            await self._sync_thread_resolution(
                session,
                repository_id=repository_id,
                pull_request=pull_request,
                publications=publications,
                installation_id=installation_id,
                ref=ref,
                counters=counters,
            )
            await self._sync_finding_lifecycle(
                session,
                repository_id=repository_id,
                pull_request=pull_request,
                publications=publications,
                counters=counters,
            )
            await self._sync_pr_lifecycle(
                session,
                repository_id=repository_id,
                pull_request=pull_request,
                installation_id=installation_id,
                ref=ref,
                counters=counters,
            )

            await session.commit()

        return FeedbackSyncResult(
            repository_id=repository_id,
            pull_request_number=pull_request_number,
            events_observed=counters.observed,
            events_ingested=counters.ingested,
            duplicate_events_ignored=counters.duplicates,
            unattributed_events=counters.unattributed,
            github_comment_ids_enriched=counters.enriched,
        )

    async def _enrich_github_comment_ids(
        self,
        session: AsyncSession,
        *,
        publication: ReviewPublicationModel,
        github_comments: list[GitHubReviewComment],
    ) -> int:
        unenriched = list(
            (
                await session.execute(
                    select(ReviewPublicationCommentModel).where(
                        ReviewPublicationCommentModel.review_publication_id == publication.id,
                        ReviewPublicationCommentModel.github_comment_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not unenriched:
            return 0

        candidates = [
            PublicationCommentKey(id=c.id, path=c.path, line=c.line, side=c.side, body_hash=c.body_hash)
            for c in unenriched
        ]
        matches = match_comments_to_publication(github_comments=github_comments, candidates=candidates)
        for comment_id, github_comment_id in matches.items():
            await self._comment_repo.set_github_comment_id(
                session, comment_id=comment_id, github_comment_id=github_comment_id  # type: ignore[arg-type]
            )
        return len(matches)

    async def _sync_reactions(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        pull_request: PullRequestModel,
        publication: ReviewPublicationModel,
        pub_comment: ReviewPublicationCommentModel,
        installation_id: int,
        ref: PullRequestRef,
        counters: _Counters,
    ) -> None:
        gh_id = pub_comment.github_comment_id
        assert gh_id is not None

        try:
            reactions = await self._github_client.list_review_comment_reactions(
                installation_id=installation_id, ref=ref, comment_id=gh_id
            )
        except GitHubNotFoundError:
            # The comment PatchFrog published was deleted on GitHub out
            # from under us -- fail gracefully for this one comment,
            # never abort syncing every other finding's feedback in the
            # same PR (Phase 9 spec section 42). Any reactions already
            # ingested from a prior sync remain in place, untouched --
            # this is a tombstone, not a retraction.
            return
        active_reaction_ids: set[int] = set()

        for reaction in reactions:
            counters.observed += 1
            if reaction.actor.actor_type is GitHubActorType.BOT:
                continue
            active_reaction_ids.add(reaction.id)
            hint = normalize_reaction(reaction.content)
            event = FeedbackEvent(
                repository_id=repository_id,
                pull_request_id=pull_request.id,
                review_run_id=publication.review_run_id,
                publication_id=publication.id,
                review_publication_comment_id=pub_comment.id,
                finding_id=pub_comment.finding_id,
                github_review_id=publication.github_review_id,
                github_comment_id=gh_id,
                event_type=FeedbackEventType.REACTION_ADDED,
                source=FeedbackSource.REACTION_SYNC,
                external_event_id=f"reaction:{reaction.id}",
                raw_signal=reaction.content.value,
                normalized_signal=hint.value,
                signal_strength=SignalStrength.WEAK,
                actor=ActorIdentity(login=reaction.actor.login, is_bot=False),
                occurred_at=reaction.created_at,
                metadata={"reaction_id": str(reaction.id)},
            )
            counters.record(await self._event_repo.create_if_new(session, event=event), attributed=pub_comment.finding_id is not None)

        previously_added = (
            await session.execute(
                select(FeedbackEventModel).where(
                    FeedbackEventModel.review_publication_comment_id == pub_comment.id,
                    FeedbackEventModel.event_type == FeedbackEventType.REACTION_ADDED,
                )
            )
        ).scalars().all()

        now = datetime.now(UTC)
        for prior in previously_added:
            prior_reaction_id = json.loads(prior.event_metadata).get("reaction_id")
            if prior_reaction_id is None or int(prior_reaction_id) in active_reaction_ids:
                continue
            counters.observed += 1
            removed_event = FeedbackEvent(
                repository_id=repository_id,
                pull_request_id=pull_request.id,
                review_run_id=publication.review_run_id,
                publication_id=publication.id,
                review_publication_comment_id=pub_comment.id,
                finding_id=pub_comment.finding_id,
                github_review_id=publication.github_review_id,
                github_comment_id=gh_id,
                event_type=FeedbackEventType.REACTION_REMOVED,
                source=FeedbackSource.REACTION_SYNC,
                external_event_id=f"reaction_removed:{prior_reaction_id}",
                raw_signal=prior.raw_signal,
                normalized_signal=prior.normalized_signal,
                signal_strength=SignalStrength.WEAK,
                actor=ActorIdentity(login=prior.actor_login, is_bot=False),
                occurred_at=now,
                metadata={"reaction_id": str(prior_reaction_id)},
            )
            counters.record(await self._event_repo.create_if_new(session, event=removed_event), attributed=True)

    async def _sync_replies_and_commands(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        pull_request: PullRequestModel,
        publication: ReviewPublicationModel,
        pub_comment: ReviewPublicationCommentModel,
        github_comments: list[GitHubReviewComment],
        counters: _Counters,
    ) -> None:
        gh_id = pub_comment.github_comment_id
        assert gh_id is not None
        replies = [c for c in github_comments if c.in_reply_to_id == gh_id]

        for reply in replies:
            counters.observed += 1
            if reply.actor.actor_type is GitHubActorType.BOT:
                continue

            reply_event = FeedbackEvent(
                repository_id=repository_id,
                pull_request_id=pull_request.id,
                review_run_id=publication.review_run_id,
                publication_id=publication.id,
                review_publication_comment_id=pub_comment.id,
                finding_id=pub_comment.finding_id,
                github_review_id=publication.github_review_id,
                github_comment_id=gh_id,
                event_type=FeedbackEventType.COMMENT_REPLY,
                source=FeedbackSource.REPLY_SYNC,
                external_event_id=f"comment:{reply.id}",
                raw_signal="",
                normalized_signal="developer_engaged",
                signal_strength=SignalStrength.WEAK,
                actor=ActorIdentity(login=reply.actor.login, is_bot=False),
                occurred_at=reply.created_at,
                metadata={"reply_comment_id": str(reply.id)},
            )
            counters.record(
                await self._event_repo.create_if_new(session, event=reply_event),
                attributed=pub_comment.finding_id is not None,
            )

            command = parse_explicit_command(reply.body)
            if command is None:
                continue

            counters.observed += 1
            command_event = FeedbackEvent(
                repository_id=repository_id,
                pull_request_id=pull_request.id,
                review_run_id=publication.review_run_id,
                publication_id=publication.id,
                review_publication_comment_id=pub_comment.id,
                finding_id=pub_comment.finding_id,
                github_review_id=publication.github_review_id,
                github_comment_id=gh_id,
                event_type=FeedbackEventType.EXPLICIT_COMMAND,
                source=FeedbackSource.REPLY_SYNC,
                external_event_id=f"comment:{reply.id}:command",
                raw_signal=command.value,
                normalized_signal=command.value,
                signal_strength=SignalStrength.STRONG,
                actor=ActorIdentity(login=reply.actor.login, is_bot=False),
                occurred_at=reply.created_at,
                metadata={"reply_comment_id": str(reply.id)},
            )
            counters.record(
                await self._event_repo.create_if_new(session, event=command_event),
                attributed=pub_comment.finding_id is not None,
            )

    async def _sync_thread_resolution(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        pull_request: PullRequestModel,
        publications: list[ReviewPublicationModel],
        installation_id: int,
        ref: PullRequestRef,
        counters: _Counters,
    ) -> None:
        if not publications:
            return
        thread_statuses = await self._github_client.list_review_thread_statuses(
            installation_id=installation_id, ref=ref
        )
        if not thread_statuses:
            return

        publication_ids = [p.id for p in publications]
        pub_comments = (
            await session.execute(
                select(ReviewPublicationCommentModel).where(
                    ReviewPublicationCommentModel.review_publication_id.in_(publication_ids),
                    ReviewPublicationCommentModel.github_comment_id.is_not(None),
                )
            )
        ).scalars().all()
        comment_by_github_id = {c.github_comment_id: c for c in pub_comments}
        publication_by_id = {p.id: p for p in publications}

        for status in thread_statuses:
            if status.first_comment_id is None:
                continue
            pub_comment = comment_by_github_id.get(status.first_comment_id)
            if pub_comment is None:
                continue  # thread's first comment isn't one of ours -- not our finding

            latest_prior = (
                await session.execute(
                    select(FeedbackEventModel)
                    .where(
                        FeedbackEventModel.review_publication_comment_id == pub_comment.id,
                        FeedbackEventModel.event_type.in_(
                            [FeedbackEventType.THREAD_RESOLVED, FeedbackEventType.THREAD_REOPENED]
                        ),
                    )
                    .order_by(FeedbackEventModel.occurred_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            currently_resolved = status.is_resolved
            if latest_prior is None:
                if not currently_resolved:
                    # A thread that has never been resolved and still
                    # isn't is not a transition -- it's the ordinary
                    # default state. Recording this as THREAD_REOPENED
                    # would falsely imply it was once resolved and is now
                    # open again.
                    continue
            else:
                previously_resolved = latest_prior.event_type == FeedbackEventType.THREAD_RESOLVED
                if currently_resolved == previously_resolved:
                    continue  # no transition since last sync

            counters.observed += 1
            now = datetime.now(UTC)
            event_type = FeedbackEventType.THREAD_RESOLVED if currently_resolved else FeedbackEventType.THREAD_REOPENED
            publication = publication_by_id[pub_comment.review_publication_id]
            event = FeedbackEvent(
                repository_id=repository_id,
                pull_request_id=pull_request.id,
                review_run_id=publication.review_run_id,
                publication_id=publication.id,
                review_publication_comment_id=pub_comment.id,
                finding_id=pub_comment.finding_id,
                github_review_id=publication.github_review_id,
                github_comment_id=pub_comment.github_comment_id,
                event_type=event_type,
                source=FeedbackSource.THREAD_SYNC,
                external_event_id=f"thread:{status.first_comment_id}:{event_type.value}:{now.isoformat()}",
                raw_signal="resolved" if currently_resolved else "reopened",
                normalized_signal="closed" if currently_resolved else "open",
                signal_strength=SignalStrength.WEAK,
                actor=ActorIdentity(login="", is_bot=False),
                occurred_at=now,
                metadata={},
            )
            counters.record(await self._event_repo.create_if_new(session, event=event), attributed=pub_comment.finding_id is not None)

    async def _sync_finding_lifecycle(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        pull_request: PullRequestModel,
        publications: list[ReviewPublicationModel],
        counters: _Counters,
    ) -> None:
        if not publications:
            return

        publication_ids = [p.id for p in publications]
        publication_by_id = {p.id: p for p in publications}
        all_comments = (
            await session.execute(
                select(ReviewPublicationCommentModel).where(
                    ReviewPublicationCommentModel.review_publication_id.in_(publication_ids)
                )
            )
        ).scalars().all()

        finding_ids = {c.finding_id for c in all_comments}
        if not finding_ids:
            return

        memory_rows = (
            await session.execute(
                select(ReviewMemoryFindingModel).where(
                    ReviewMemoryFindingModel.pull_request_id == pull_request.id,
                    ReviewMemoryFindingModel.source_finding_id.in_(finding_ids),
                )
            )
        ).scalars().all()

        publication_by_comment_finding: dict[uuid.UUID, tuple[ReviewPublicationModel, ReviewPublicationCommentModel]] = {
            c.finding_id: (publication_by_id[c.review_publication_id], c) for c in all_comments
        }

        now = datetime.now(UTC)
        for memory_row in memory_rows:
            event_type = _LIFECYCLE_EVENT_BY_STATUS.get(memory_row.status)
            if event_type is None:
                continue  # OPEN/SUPERSEDED/AMBIGUOUS -- no lifecycle signal to emit yet
            pair = publication_by_comment_finding.get(memory_row.source_finding_id)
            if pair is None:
                continue
            publication, pub_comment = pair
            counters.observed += 1
            event = FeedbackEvent(
                repository_id=repository_id,
                pull_request_id=pull_request.id,
                review_run_id=publication.review_run_id,
                publication_id=publication.id,
                review_publication_comment_id=pub_comment.id,
                finding_id=memory_row.source_finding_id,
                github_review_id=publication.github_review_id,
                github_comment_id=pub_comment.github_comment_id,
                event_type=event_type,
                source=FeedbackSource.REVIEW_MEMORY,
                external_event_id=f"memory:{memory_row.id}:{memory_row.status.value}:{memory_row.updated_at.isoformat()}",
                raw_signal=memory_row.status.value,
                normalized_signal=memory_row.status.value,
                signal_strength=SignalStrength.MEDIUM,
                actor=ActorIdentity(login="", is_bot=False),
                occurred_at=now,
                metadata={},
            )
            counters.record(await self._event_repo.create_if_new(session, event=event), attributed=True)

    async def _sync_pr_lifecycle(
        self,
        session: AsyncSession,
        *,
        repository_id: uuid.UUID,
        pull_request: PullRequestModel,
        installation_id: int,
        ref: PullRequestRef,
        counters: _Counters,
    ) -> None:
        metadata = await self._github_client.get_pull_request(installation_id=installation_id, ref=ref)
        if metadata.state != "closed":
            return

        event_type = FeedbackEventType.PR_MERGED if metadata.merged else FeedbackEventType.PR_CLOSED
        counters.observed += 1
        event = FeedbackEvent(
            repository_id=repository_id,
            pull_request_id=pull_request.id,
            review_run_id=None,
            publication_id=None,
            review_publication_comment_id=None,
            finding_id=None,
            github_review_id=None,
            github_comment_id=None,
            event_type=event_type,
            source=FeedbackSource.PR_LIFECYCLE_SYNC,
            external_event_id=f"pr:{ref.number}:{event_type.value}",
            raw_signal="merged" if metadata.merged else "closed",
            normalized_signal=event_type.value,
            signal_strength=SignalStrength.WEAK,
            actor=ActorIdentity(login="", is_bot=False),
            occurred_at=datetime.now(UTC),
            metadata={},
        )
        counters.record(await self._event_repo.create_if_new(session, event=event), attributed=False)


class _Counters:
    __slots__ = ("duplicates", "enriched", "ingested", "observed", "unattributed")

    def __init__(self) -> None:
        self.observed = 0
        self.ingested = 0
        self.duplicates = 0
        self.unattributed = 0
        self.enriched = 0

    def record(self, model: FeedbackEventModel | None, *, attributed: bool) -> None:
        if model is not None:
            self.ingested += 1
            metrics.feedback_events_total.labels(event_type=model.event_type.value).inc()
            if not attributed:
                self.unattributed += 1
        else:
            self.duplicates += 1
