"""Orchestrates one migration PR publication attempt end to end (M9.6/M9.7/M9.8).

    MigrationPRPlan (already built by :mod:`patchfrog.migration_pr.planner`)
    -> eligibility gate -> idempotency lookup (identity_key)
    -> [DRY_RUN: stop here, no write]
    -> stale-base check (M9.7) -> integrity check (M9.8)
    -> branch create/update -> PR open/update (M9.6) -> check run

Migration branches are entirely PatchFrog-owned: every publish rebuilds
the branch fresh from the *current* base commit (never a three-way merge,
never preserving a human's own commits on it) -- a branch is only ever
force-moved because nothing but PatchFrog's own generated content is
expected to live there. GitHub and PatchFrog's database cannot share one
transaction, exactly like :mod:`patchfrog.publishing.service` -- the
identity is locked and a durable row exists *before* any GitHub write is
attempted, mirroring that module's own discipline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.domain.github_git import GitTreeEntry
from patchfrog.github.errors import GitHubError, GitHubUnprocessableError
from patchfrog.migration.domain import GeneratedPatch
from patchfrog.migration_pr.check import build_check_input, build_external_id
from patchfrog.migration_pr.domain import (
    MigrationPREligibility,
    MigrationPRPlan,
    MigrationPRStatus,
    MigrationPullRequest,
)
from patchfrog.migration_pr.github_publisher import MigrationGitHubPublisher
from patchfrog.migration_pr.store import MigrationPRStore
from patchfrog.migration_verification.domain import VerificationOutcome


class MigrationPRPublicationMode(StrEnum):
    DRY_RUN = "dry_run"
    PUBLISH = "publish"


def _owner_repository(full_name: str) -> tuple[str, str]:
    owner, _, repository = full_name.partition("/")
    return owner, repository


def _result(
    plan: MigrationPRPlan, *, status: MigrationPRStatus, number: int | None = None, html_url: str | None = None,
    reason: str | None = None, errors: tuple[str, ...] = (),
) -> MigrationPullRequest:
    return MigrationPullRequest(
        id=None, number=number, html_url=html_url, status=status, linkage=plan.linkage, branch_name=plan.branch_name,
        reason=reason, errors=errors,
    )


class MigrationPRPublisher:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: MigrationGitHubPublisher,
        installation_id: int,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._installation_id = installation_id
        self._store = MigrationPRStore()

    async def publish(
        self, *, plan: MigrationPRPlan, patch: GeneratedPatch | None, mode: MigrationPRPublicationMode,
        base_branch: str = "main",
    ) -> MigrationPullRequest:
        identity_key = plan.linkage.identity_key()

        if plan.eligibility is MigrationPREligibility.NOT_ELIGIBLE:
            return await self._finalize(plan, self._result_no_write(plan, MigrationPRStatus.SKIPPED_NOT_ELIGIBLE, plan.eligibility_reason))

        if plan.eligibility is MigrationPREligibility.PLAN_ONLY:
            # Never a code PR, regardless of mode (M9.3: "optionally a
            # plan/report, not a code PR by default" -- no policy knob
            # is offered to change this, unlike PARTIALLY_VERIFIED).
            return await self._finalize(plan, self._result_no_write(plan, MigrationPRStatus.PLANNED, plan.eligibility_reason))

        if patch is None:
            return await self._finalize(
                plan, self._result_no_write(plan, MigrationPRStatus.SKIPPED_NOT_ELIGIBLE, "no generated patch to publish")
            )

        if mode is MigrationPRPublicationMode.DRY_RUN:
            return await self._finalize(plan, self._result_no_write(plan, MigrationPRStatus.DRY_RUN, "dry run: no GitHub write performed"))

        # -- real publish from here -----------------------------------------------------------
        owner, repository = _owner_repository(plan.linkage.repository)
        base_branch_ref = f"heads/{base_branch}"

        try:
            current_base_sha = await self._publisher.get_ref(owner=owner, repository=repository, ref=base_branch_ref)
        except GitHubError as exc:
            return await self._finalize(plan, self._result_no_write(plan, MigrationPRStatus.FAILED, f"could not read base branch: {exc}"))

        if current_base_sha is None:
            return await self._finalize(plan, self._result_no_write(plan, MigrationPRStatus.FAILED, "base branch not found"))

        # M9.7: stale-base protection. Exact match required -- a moved
        # base means the plan/patch/evidence were computed against a
        # repository state that no longer exists; never publish a stale
        # VERIFIED status against a different base (regeneration is
        # required, never assumed compatible).
        if current_base_sha != plan.linkage.base_commit_sha:
            return await self._finalize(
                plan,
                self._result_no_write(
                    plan, MigrationPRStatus.STALE_REQUIRES_REGENERATION,
                    f"base branch moved from {plan.linkage.base_commit_sha!r} to {current_base_sha!r}; regenerate",
                ),
            )

        try:
            base_tree_sha = await self._publisher.get_commit_tree_sha(
                owner=owner, repository=repository, commit_sha=current_base_sha,
            )
            entries = [GitTreeEntry(path=path, content=content) for path, content in sorted(patch.new_contents.items())]
            tree_sha = await self._publisher.create_tree(
                owner=owner, repository=repository, base_tree_sha=base_tree_sha, entries=entries,
            )
            commit_sha = await self._publisher.create_commit(
                owner=owner, repository=repository, message=plan.commit_message, tree_sha=tree_sha,
                parent_sha=current_base_sha,
            )
            branch_ref = f"heads/{plan.branch_name}"
            existing_branch_sha = await self._publisher.get_ref(owner=owner, repository=repository, ref=branch_ref)
            if existing_branch_sha is None:
                try:
                    await self._publisher.create_ref(
                        owner=owner, repository=repository, ref=f"refs/{branch_ref}", sha=commit_sha,
                    )
                except GitHubUnprocessableError:
                    # Lost a race with a concurrent publish -- reconcile
                    # onto whatever now exists rather than failing.
                    await self._publisher.update_ref(
                        owner=owner, repository=repository, ref=branch_ref, sha=commit_sha, force=True,
                    )
            elif existing_branch_sha == commit_sha:
                pass  # identical content already there -- nothing to move.
            else:
                await self._publisher.update_ref(
                    owner=owner, repository=repository, ref=branch_ref, sha=commit_sha, force=True,
                )

            head = f"{owner}:{plan.branch_name}"
            existing_prs = await self._publisher.list_pull_requests(owner=owner, repository=repository, head=head)
            open_pr = next((pr for pr in existing_prs if pr.state == "open"), None)
            closed_pr = next((pr for pr in existing_prs if pr.state != "open"), None)

            if open_pr is not None:
                updated = await self._publisher.update_pull_request(
                    owner=owner, repository=repository, number=open_pr.number, title=plan.pr_title, body=plan.pr_body,
                )
                result = _result(plan, status=MigrationPRStatus.UPDATED, number=updated.number, html_url=updated.html_url)
            elif closed_pr is not None:
                result = _result(
                    plan, status=MigrationPRStatus.NO_OP_UNCHANGED, number=closed_pr.number, html_url=closed_pr.html_url,
                    reason="a PR for this migration already exists but was closed by a human; not reopened automatically",
                )
            else:
                created = await self._publisher.create_pull_request(
                    owner=owner, repository=repository, title=plan.pr_title, body=plan.pr_body,
                    head=plan.branch_name, base=base_branch,
                )
                result = _result(plan, status=MigrationPRStatus.OPENED, number=created.number, html_url=created.html_url)

            check_head_sha = commit_sha
            external_id = build_external_id(
                owner=owner, repository=repository, change_fingerprint=plan.linkage.change_fingerprint,
                patch_fingerprint=plan.linkage.patch_fingerprint,
            )
            check_input = build_check_input(
                head_sha=check_head_sha, external_id=external_id, outcome=_outcome_for_check(plan),
                eligibility=plan.eligibility, title=plan.check_title, summary=plan.check_summary,
                details_url=result.html_url,
            )
            await self._publisher.reconcile_check(owner=owner, repository=repository, head_sha=check_head_sha, check=check_input)
        except GitHubError as exc:
            result = self._result_no_write(plan, MigrationPRStatus.FAILED, str(exc))

        return await self._finalize(plan, result, identity_key=identity_key)

    def _result_no_write(self, plan: MigrationPRPlan, status: MigrationPRStatus, reason: str) -> MigrationPullRequest:
        return _result(plan, status=status, reason=reason)

    async def _finalize(
        self, plan: MigrationPRPlan, result: MigrationPullRequest, *, identity_key: str | None = None,
    ) -> MigrationPullRequest:
        async with self._session_factory() as session:
            row, _created = await self._store.upsert(session, plan=plan, result=result, recorded_at=datetime.now(UTC))
            await session.commit()
        return _result(plan, status=result.status, number=row.pr_number, html_url=row.pr_html_url, reason=result.reason, errors=result.errors)


def _outcome_for_check(plan: MigrationPRPlan) -> VerificationOutcome | None:
    if plan.eligibility is MigrationPREligibility.AUTO_OPEN:
        return VerificationOutcome.VERIFIED
    if plan.eligibility is MigrationPREligibility.OPEN_WITH_OPERATOR_APPROVAL:
        return VerificationOutcome.PARTIALLY_VERIFIED
    return None


__all__ = ["MigrationPRPublicationMode", "MigrationPRPublisher"]
