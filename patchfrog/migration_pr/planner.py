"""Builds the complete, deterministic :class:`MigrationPRPlan` (M9.1/M9.4)
from already-computed M6/M7/M8 evidence -- ties together branch naming
(M9.2), eligibility policy (M9.3), and dossier rendering (M9.4)."""

from __future__ import annotations

from patchfrog.migration.domain import GeneratedPatch, MigrationPlan
from patchfrog.migration_pr.branch import migration_branch_name
from patchfrog.migration_pr.domain import MigrationPRLinkage, MigrationPRPlan, MigrationPRPolicy
from patchfrog.migration_pr.dossier import render_check_summary, render_pr_body, render_pr_title
from patchfrog.migration_pr.eligibility import determine_eligibility
from patchfrog.migration_pr.integrity import verify_evidence_integrity
from patchfrog.migration_verification.domain import MigrationEvidenceBundle
from patchfrog.upstream.blast_radius import BlastRadius
from patchfrog.upstream.domain import ExternalChangeEvent


def build_pr_plan(
    *,
    event: ExternalChangeEvent,
    plan: MigrationPlan,
    patch: GeneratedPatch | None,
    bundle: MigrationEvidenceBundle,
    blast_radii: tuple[BlastRadius, ...],
    repository: str,
    base_commit_sha: str,
    #: The persisted M7 plan fingerprint (see
    #: :meth:`patchfrog.migration.store.MigrationStore.record_plan`),
    #: when already known -- purely informational here (M9 idempotency
    #: keys on ``change_fingerprint``/``engine_version``, never this),
    #: kept only so the evidence-identity section of the dossier can cite
    #: it. Empty when the caller has not persisted a plan.
    plan_fingerprint: str = "",
    policy: MigrationPRPolicy | None = None,
) -> MigrationPRPlan:
    verify_evidence_integrity(event=event, patch=patch, bundle=bundle, base_commit_sha=base_commit_sha)

    policy = policy or MigrationPRPolicy()
    eligibility, reason = determine_eligibility(bundle.outcome, policy=policy)

    linkage = MigrationPRLinkage(
        change_fingerprint=event.fingerprint, dependency_key=event.target.dependency_key, repository=repository,
        base_commit_sha=base_commit_sha, plan_fingerprint=plan_fingerprint,
        patch_fingerprint=patch.fingerprint if patch is not None else "",
        verification_plan_fingerprint=bundle.plan.fingerprint(), bundle_fingerprint=bundle.bundle_fingerprint,
    )
    branch_name = migration_branch_name(provider=event.target.provider_key, change_fingerprint=event.fingerprint)
    pr_title = render_pr_title(event, repository=repository)
    pr_body = render_pr_body(
        event=event, plan=plan, patch=patch, bundle=bundle, blast_radii=blast_radii, linkage=linkage,
        eligibility=eligibility,
    )
    check_title, check_summary = render_check_summary(bundle)
    dependency_label = event.target.provider_key or event.target.package_name or event.target.dependency_key or "dependency"
    commit_message = f"PatchFrog: migrate {dependency_label} ({event.old.version or '?'} -> {event.new.version or '?'})"

    return MigrationPRPlan(
        linkage=linkage, eligibility=eligibility, eligibility_reason=reason, branch_name=branch_name,
        commit_message=commit_message, pr_title=pr_title, pr_body=pr_body, check_title=check_title,
        check_summary=check_summary,
    )


__all__ = ["build_pr_plan"]
