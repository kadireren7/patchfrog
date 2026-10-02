"""Detected upstream change -> one workspace's campaign (M11.5).

``gate -> previous campaign -> run_campaign (M6->M9 per repository) ->
audit trail -> persist``. This is the single-workspace unit of the
continuous pipeline; a hosted service calls it once per subscribing
workspace. Everything deterministic, nothing model-assisted.

Atomicity: the campaign row, the M6 event row and (when repository ids are
supplied) the M7 plan / patch / M8 verification rows are written in **one**
transaction. If any write fails the whole transaction rolls back and the
caller's retry re-runs idempotently -- M9's PR identity makes a repeated
publish an update, never a second PR.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.campaigns.domain import CampaignState, campaign_identity_key
from patchfrog.campaigns.evaluate import RepositoryEvaluation, RepositoryInput, VerifyFn
from patchfrog.campaigns.freshness import FreshnessPolicy
from patchfrog.campaigns.internal import InternalContract
from patchfrog.campaigns.observe import (
    pr_outcome_for_status,
    record_campaign_event,
    record_campaign_records,
    record_campaign_resolved,
    record_pr_outcome,
)
from patchfrog.campaigns.orchestrate import CampaignRunResult, PullRequestPublisher, run_campaign
from patchfrog.campaigns.policy import PublicationAction, WorkspacePolicy
from patchfrog.campaigns.store import CampaignStore
from patchfrog.migration.store import MigrationStore, build_linkage
from patchfrog.migration_verification.service import run_migration_verification
from patchfrog.migration_verification.store import MigrationVerificationStore
from patchfrog.upstream.domain import ChangeRisk, ExternalChangeEvent
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints
from patchfrog.upstream.store import UpstreamChangeStore
from patchfrog.watchers.gate import DEFAULT_MIN_RISK, should_launch_campaign


@dataclass(frozen=True, slots=True)
class WorkspaceChangeResult:
    launched: bool
    reason: str
    run: CampaignRunResult | None = None
    campaign_created: bool = False


async def _persist_audit(session: AsyncSession, event: ExternalChangeEvent, run: CampaignRunResult) -> None:
    events = UpstreamChangeStore()
    migrations = MigrationStore()
    verifications = MigrationVerificationStore()
    event_row, _ = await events.record_event(session, event)
    for evaluation in run.evaluations:
        await _persist_repository(session, event_row.id, evaluation, migrations, verifications)


async def _persist_repository(
    session: AsyncSession, event_id: uuid.UUID, evaluation: RepositoryEvaluation, migrations: MigrationStore,
    verifications: MigrationVerificationStore,
) -> None:
    if evaluation.engine_repository_id is None or evaluation.plan is None or evaluation.root is None:
        return
    plan_row, base_fingerprint, _ = await migrations.record_plan(
        session, event_id=event_id, repository_id=evaluation.engine_repository_id, plan=evaluation.plan,
        root=evaluation.root,
    )
    if evaluation.patch is None:
        return
    linkage = build_linkage(
        evaluation.plan, evaluation.patch, base_content_fingerprint=base_fingerprint,
        plan_fingerprint=plan_row.plan_fingerprint,
    )
    patch_row, _ = await migrations.record_patch(session, plan_id=plan_row.id, patch=evaluation.patch, linkage=linkage)
    if evaluation.bundle is not None:
        await verifications.record_run(session, patch_id=patch_row.id, bundle=evaluation.bundle)


def _observe(run: CampaignRunResult, *, created: bool, previous_state: CampaignState | None, now: datetime) -> None:
    campaign = run.campaign
    if created:
        record_campaign_event("created")
    elif run.changed:
        record_campaign_event("reconciled")
    else:
        record_campaign_event("unchanged")
    if campaign.state is CampaignState.RESOLVED and previous_state is not CampaignState.RESOLVED:
        record_campaign_event("resolved")
        record_campaign_resolved(campaign, now=now)
    record_campaign_records(campaign.records)
    for repository, decision in run.decisions.items():
        if decision.action is PublicationAction.NONE:
            continue
        record_pr_outcome("eligible")
        if decision.action is PublicationAction.AWAIT_APPROVAL:
            record_pr_outcome("awaiting_approval")
        publication = run.publications.get(repository)
        if publication is not None:
            record_pr_outcome(pr_outcome_for_status(publication.status.value))


async def process_change_for_workspace(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    workspace_key: str,
    event: ExternalChangeEvent,
    entries: Sequence[RepositoryInput],
    now: datetime,
    policy: WorkspacePolicy,
    min_risk: ChangeRisk = DEFAULT_MIN_RISK,
    freshness_policy: FreshnessPolicy | None = None,
    hints: ChangeHints = EMPTY_HINTS,
    verify: VerifyFn = run_migration_verification,
    publish: PullRequestPublisher | None = None,
    internal_contract: InternalContract | None = None,
) -> WorkspaceChangeResult:
    if not policy.analyzes:
        record_campaign_event("not_launched")
        return WorkspaceChangeResult(False, "workspace watch mode is off")
    decision = should_launch_campaign(event, min_risk=min_risk)
    if not decision.launch:
        record_campaign_event("not_launched")
        return WorkspaceChangeResult(False, decision.reason)

    identity = campaign_identity_key(workspace_key=workspace_key, change_fingerprint=event.fingerprint)
    store = CampaignStore()
    async with session_factory() as session:
        previous = await store.load(session, identity_key=identity)

    run = await run_campaign(
        workspace_key=workspace_key, event=event, entries=entries, now=now, policy=policy,
        freshness_policy=freshness_policy, hints=hints, verify=verify, publish=publish, previous=previous,
        internal_contract=internal_contract,
    )
    async with session_factory() as session:
        await _persist_audit(session, event, run)
        _, created = await store.upsert(session, run.campaign, recorded_at=now)
        await session.commit()
    _observe(run, created=created, previous_state=previous.state if previous else None, now=now)
    return WorkspaceChangeResult(True, decision.reason, run, created)


__all__ = ["WorkspaceChangeResult", "process_change_for_workspace"]
