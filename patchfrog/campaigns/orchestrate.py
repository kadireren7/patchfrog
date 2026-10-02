"""Cross-repository migration orchestration (M10.4) -- pure, no persistence.

``find enrolled repositories -> evaluate each (M6 -> M7 -> M8 -> M9 plan) ->
publish each independently under workspace policy -> aggregate into one
campaign``.

Isolation is structural: every repository is evaluated and published inside
its own ``try``; a failure becomes that repository's ``FAILED`` record and
cannot touch any other. Each repository keeps its own base SHA, patch
fingerprint, verification evidence, PR and publication state.

Re-running with the same inputs yields a campaign equal to the previous one
(same identity, same ``version``) -- no spam. Persistence is
:mod:`patchfrog.campaigns.store`'s job; hosted scheduling is Cloud's.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from patchfrog.campaigns.blast import build_org_blast_radius
from patchfrog.campaigns.domain import (
    BLOCKING_STATES,
    CampaignState,
    CompatibilityCampaign,
    OrgClass,
    RepositoryRecord,
    RepoState,
    bound_reasons,
    campaign_identity_key,
)
from patchfrog.campaigns.evaluate import (
    RepositoryEvaluation,
    RepositoryInput,
    VerifyFn,
    evaluate_all,
    safe_error,
)
from patchfrog.campaigns.freshness import FreshnessPolicy
from patchfrog.campaigns.internal import InternalContract
from patchfrog.campaigns.policy import (
    PublicationAction,
    PublicationDecision,
    WorkspacePolicy,
    decide_publication,
)
from patchfrog.campaigns.state import RepoFacts, derive_campaign_state, derive_repo_state
from patchfrog.migration.domain import GeneratedPatch
from patchfrog.migration_pr.domain import (
    MigrationPREligibility,
    MigrationPRPlan,
    MigrationPullRequest,
)
from patchfrog.migration_pr.publisher import MigrationPRPublicationMode, MigrationPRPublisher
from patchfrog.migration_verification.service import run_migration_verification
from patchfrog.upstream.domain import ExternalChangeEvent
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints


class PullRequestPublisher(Protocol):
    async def __call__(
        self, *, plan: MigrationPRPlan, patch: GeneratedPatch | None, draft: bool
    ) -> MigrationPullRequest: ...


def pr_publisher_from(publisher: MigrationPRPublisher, *, base_branch: str = "main") -> PullRequestPublisher:
    async def publish(*, plan: MigrationPRPlan, patch: GeneratedPatch | None, draft: bool) -> MigrationPullRequest:
        return await publisher.publish(
            plan=plan, patch=patch, mode=MigrationPRPublicationMode.PUBLISH, base_branch=base_branch, draft=draft
        )

    return publish


@dataclass(frozen=True, slots=True)
class CampaignRunResult:
    campaign: CompatibilityCampaign
    evaluations: tuple[RepositoryEvaluation, ...]
    decisions: Mapping[str, PublicationDecision] = field(default_factory=dict)
    publications: Mapping[str, MigrationPullRequest] = field(default_factory=dict)
    #: Repositories excluded from scope on purpose, with the reason.
    excluded: Mapping[str, str] = field(default_factory=dict)
    #: ``None`` for a first run; otherwise whether this run changed the campaign.
    changed: bool = True


def _signature(records: Sequence[RepositoryRecord]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(v for k, v in sorted(vars_of(r).items()) if k != "evaluated_at")
        for r in sorted(records, key=lambda x: x.repository)
    )


def vars_of(record: RepositoryRecord) -> dict[str, object]:
    values = {name: getattr(record, name) for name in record.__slots__}
    # "opened" on the first run and "updated" on every identical re-run is the same fact: one open PR.
    if values["pr_status"] == "updated":
        values["pr_status"] = "opened"
    return values


def _apply_publication(evaluation: RepositoryEvaluation, pr: MigrationPullRequest) -> RepositoryRecord:
    record = evaluation.record
    facts = RepoFacts(
        freshness=record.freshness, org_class=record.org_class, ever_affected=record.ever_affected,
        migration_status=record.migration_status, has_automatic_steps=record.migration_strategy_available,
        patch_generated=record.patch_fingerprint is not None, verification_outcome=record.verification_outcome,
        pr_status=pr.status.value, pr_number=pr.number,
    )
    reasons = list(record.reasons)
    if pr.reason:
        reasons.append(f"publication: {pr.reason}")
    return replace(
        record, state=derive_repo_state(facts), pr_status=pr.status.value, pr_number=pr.number, pr_url=pr.html_url,
        reasons=bound_reasons(reasons),
    )


def _approved(decision: PublicationDecision, eligibility: MigrationPREligibility) -> PublicationDecision:
    """A human approved this repository's pending PR: ``AWAIT_APPROVAL`` becomes a real publication.
    Approval never lowers the bar -- the evaluation it applies to was just recomputed from scratch, and a
    partially verified migration is still opened as a draft."""

    if decision.action is not PublicationAction.AWAIT_APPROVAL:
        return decision
    if eligibility is MigrationPREligibility.OPEN_WITH_OPERATOR_APPROVAL:
        return PublicationDecision(PublicationAction.PUBLISH_DRAFT, "approved by a human; partially verified, so a draft")
    return PublicationDecision(PublicationAction.PUBLISH, "approved by a human")


async def _publish_phase(
    evaluations: Sequence[RepositoryEvaluation],
    *,
    policy: WorkspacePolicy,
    publish: PullRequestPublisher | None,
    approved: frozenset[str] = frozenset(),
) -> tuple[list[RepositoryEvaluation], dict[str, PublicationDecision], dict[str, MigrationPullRequest]]:
    decisions: dict[str, PublicationDecision] = {}
    publications: dict[str, MigrationPullRequest] = {}
    out: list[RepositoryEvaluation] = []
    for evaluation in evaluations:
        plan = evaluation.pr_plan
        if plan is None:
            out.append(evaluation)
            continue
        name = evaluation.record.repository
        decision = decide_publication(policy, plan.eligibility)
        if name in approved:
            decision = _approved(decision, plan.eligibility)
        decisions[name] = decision
        record = replace(
            evaluation.record, reasons=bound_reasons([*evaluation.record.reasons, f"publication policy: {decision.reason}"])
        )
        evaluation = replace(evaluation, record=record)
        if decision.writes_to_github and publish is not None:
            try:
                pr = await publish(plan=plan, patch=evaluation.patch, draft=decision.draft)
            except Exception as exc:
                evaluation = replace(
                    evaluation,
                    record=replace(
                        evaluation.record, state=RepoState.FAILED, error=safe_error(exc),
                        reasons=bound_reasons([*evaluation.record.reasons, "publication raised an error"]),
                    ),
                )
            else:
                publications[name] = pr
                evaluation = replace(evaluation, record=_apply_publication(evaluation, pr))
        out.append(evaluation)
    return out, decisions, publications


def _unresolved_risks(records: Sequence[RepositoryRecord], unknown_count: int) -> tuple[str, ...]:
    risks: list[str] = []
    for record in sorted(records, key=lambda r: r.repository):
        if record.state in BLOCKING_STATES:
            detail = (
                record.human_actions[0] if record.human_actions
                else record.error or (record.reasons[0] if record.reasons else record.state.value)
            )
            risks.append(f"{record.repository}: {record.state.value} -- {detail}"[:400])
    if unknown_count:
        risks.append(f"the organization cannot be declared safe: {unknown_count} repositories could not be classified")
    return tuple(risks)


async def run_campaign(
    *,
    workspace_key: str,
    event: ExternalChangeEvent,
    entries: Sequence[RepositoryInput],
    now: datetime,
    policy: WorkspacePolicy | None = None,
    freshness_policy: FreshnessPolicy | None = None,
    hints: ChangeHints = EMPTY_HINTS,
    verify: VerifyFn = run_migration_verification,
    publish: PullRequestPublisher | None = None,
    previous: CompatibilityCampaign | None = None,
    internal_contract: InternalContract | None = None,
    approved_repositories: frozenset[str] = frozenset(),
) -> CampaignRunResult:
    policy = policy or WorkspacePolicy()
    identity = campaign_identity_key(workspace_key=workspace_key, change_fingerprint=event.fingerprint)

    excluded: dict[str, str] = {}
    scoped: list[RepositoryInput] = []
    for entry in entries:
        name = entry.repository.full_name
        if internal_contract is not None and name == internal_contract.producer_repository:
            excluded[name] = f"producer of internal contract {internal_contract.contract_id!r}; not a consumer"
            continue
        prior = previous.record_for(name) if previous else None
        scoped.append(replace(entry, previous=prior) if prior is not None else entry)

    if not policy.analyzes:
        campaign = CompatibilityCampaign(
            campaign_id=identity, workspace_key=workspace_key, change_fingerprint=event.fingerprint,
            dependency_label=event.target.label, provider_key=event.target.provider_key,
            origin="internal" if internal_contract else "external",
            discovered_at=previous.discovered_at if previous else now, state=CampaignState.DETECTED,
            org_blast_radius=build_org_blast_radius([]), records=(),
            unresolved_risks=("watch mode is off: nothing was analyzed",), version=previous.version if previous else 1,
            producer_repository=internal_contract.producer_repository if internal_contract else None,
            old_version=event.old.version, new_version=event.new.version,
            compatibility=event.classification.compatibility.value,
        )
        return CampaignRunResult(campaign=campaign, evaluations=(), excluded=excluded, changed=previous is None)

    evaluations = await evaluate_all(
        event, scoped, now=now, policy=policy, freshness_policy=freshness_policy, hints=hints, verify=verify
    )
    published, decisions, publications = await _publish_phase(
        evaluations, policy=policy, publish=publish, approved=approved_repositories
    )

    records = tuple(e.record for e in published)
    blast = build_org_blast_radius([e.classification for e in published])
    state = derive_campaign_state(r.state for r in records)
    unknown = sum(1 for r in records if r.org_class is OrgClass.UNKNOWN)
    version = 1
    changed = True
    if previous is not None:
        same = _signature(previous.records) == _signature(records) and previous.state is state
        changed = not same
        version = previous.version if same else previous.version + 1
    campaign = CompatibilityCampaign(
        campaign_id=identity, workspace_key=workspace_key, change_fingerprint=event.fingerprint,
        dependency_label=event.target.label, provider_key=event.target.provider_key,
        origin="internal" if internal_contract else "external",
        discovered_at=previous.discovered_at if previous else now, state=state, org_blast_radius=blast,
        records=records, unresolved_risks=_unresolved_risks(records, unknown), version=version,
        producer_repository=internal_contract.producer_repository if internal_contract else None,
        old_version=event.old.version, new_version=event.new.version,
        compatibility=event.classification.compatibility.value,
    )
    return CampaignRunResult(
        campaign=campaign, evaluations=tuple(published), decisions=decisions, publications=publications,
        excluded=excluded, changed=changed,
    )


__all__ = [
    "CampaignRunResult",
    "PublicationAction",
    "PullRequestPublisher",
    "pr_publisher_from",
    "run_campaign",
]
