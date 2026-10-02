"""Campaign-level evidence dossier (M10.8).

A machine-readable dict plus a markdown rendering, both derived from a
:class:`CompatibilityCampaign` alone (so they can be rebuilt from persisted
state). Nothing here reads source code, logs or secrets: every value is a
bounded, controlled field already on the campaign.
"""

from __future__ import annotations

from typing import Any

from patchfrog.campaigns.domain import (
    CompatibilityCampaign,
    Freshness,
    OrgClass,
    RepositoryRecord,
    RepoState,
)

DOSSIER_VERSION = 1


def next_action(record: RepositoryRecord) -> str | None:
    """The single most useful human/operator action for a repository, or
    ``None`` when nothing is needed."""

    state = record.state
    if state in (RepoState.NOT_AFFECTED, RepoState.RESOLVED):
        return None
    if state is RepoState.ACCESS_LOST:
        return "restore repository access under the GitHub App installation, then re-run discovery"
    if state is RepoState.STALE:
        return "re-run dependency discovery: evidence is older than the freshness window"
    if state is RepoState.UNKNOWN:
        if record.requires_rediscovery:
            return "run dependency discovery: no usable evidence exists for this repository"
        return "manual review: only version-level or indirect evidence reaches the changed contract"
    if state is RepoState.IMPACTED:
        return "affected, but no migration was generated: enable migration in the workspace policy or provide a checkout"
    if state is RepoState.HUMAN_REQUIRED:
        return record.human_actions[0] if record.human_actions else "a human decision is required for this migration"
    if state is RepoState.FAILED:
        return "investigate the failure" + (f": {record.error}" if record.error else "")
    if state is RepoState.UNVERIFIED:
        return "verification produced insufficient evidence; do not merge without manual checks"
    if state is RepoState.PARTIALLY_VERIFIED:
        return "verification was partial; review the evidence before opening or merging a PR"
    if state is RepoState.VERIFIED:
        return "approve opening the verified migration PR"
    if state is RepoState.PR_OPENED:
        return f"review and merge PR #{record.pr_number} (PatchFrog never merges automatically)"
    return "migration in progress"


def _record_dict(record: RepositoryRecord) -> dict[str, Any]:
    return {
        "repository": record.repository,
        "state": record.state.value,
        "classification": record.org_class.value,
        "freshness": record.freshness.value,
        "unknown_reason": record.unknown_reason.value if record.unknown_reason else None,
        "requires_rediscovery": record.requires_rediscovery,
        "impact": {
            "status": record.impact_status, "direct": record.direct_consumers,
            "transitive": record.transitive_consumers, "potential": record.potential_consumers,
            "severity": record.severity,
        },
        "migration": {
            "status": record.migration_status, "strategy_available": record.migration_strategy_available,
            "plan_fingerprint": record.plan_fingerprint, "patch_fingerprint": record.patch_fingerprint,
        },
        "verification": {
            "outcome": record.verification_outcome, "feasibility": record.verification_feasibility,
            "bundle_fingerprint": record.bundle_fingerprint, "residual_risk": record.residual_risk,
        },
        "publication": {
            "readiness": record.publication_readiness, "status": record.pr_status, "pr_number": record.pr_number,
            "pr_url": record.pr_url,
        },
        "base_commit_sha": record.base_commit_sha,
        "reasons": list(record.reasons),
        "human_actions": list(record.human_actions),
        "next_action": next_action(record),
        "error": record.error,
        "evaluated_at": record.evaluated_at.isoformat() if record.evaluated_at else None,
    }


def campaign_to_dict(campaign: CompatibilityCampaign) -> dict[str, Any]:
    radius = campaign.org_blast_radius
    return {
        "dossier_version": DOSSIER_VERSION,
        "campaign": {
            "id": campaign.campaign_id, "workspace": campaign.workspace_key, "state": campaign.state.value,
            "version": campaign.version, "engine_version": campaign.engine_version,
            "discovered_at": campaign.discovered_at.isoformat(),
        },
        "upstream_change": {
            "fingerprint": campaign.change_fingerprint, "dependency": campaign.dependency_label,
            "provider": campaign.provider_key, "origin": campaign.origin,
            "producer_repository": campaign.producer_repository, "old_version": campaign.old_version,
            "new_version": campaign.new_version, "compatibility": campaign.compatibility,
        },
        "impact": {
            **radius.summary(), "organization_safe": radius.safe, "affected": list(radius.affected),
            "not_affected": list(radius.not_affected), "unknown": list(radius.unknown),
            "unknown_reasons": dict(radius.unknown_reasons),
        },
        "repositories": [_record_dict(r) for r in sorted(campaign.records, key=lambda r: r.repository)],
        "state_counts": campaign.state_counts(),
        "unresolved_risks": list(campaign.unresolved_risks),
        "human_required_actions": [
            {"repository": r.repository, "action": a}
            for r in sorted(campaign.records, key=lambda r: r.repository)
            if r.state is RepoState.HUMAN_REQUIRED for a in (r.human_actions or ("a human decision is required",))
        ],
        "data_freshness": {
            r.repository: r.freshness.value for r in sorted(campaign.records, key=lambda r: r.repository)
        },
        "pull_requests": [
            {"repository": r.repository, "number": r.pr_number, "url": r.pr_url, "status": r.pr_status}
            for r in sorted(campaign.records, key=lambda r: r.repository) if r.pr_number is not None
        ],
    }


def render_campaign_markdown(campaign: CompatibilityCampaign) -> str:
    radius = campaign.org_blast_radius
    lines = [
        f"# Compatibility campaign: {campaign.dependency_label}",
        "",
        f"- **State:** `{campaign.state.value}` (v{campaign.version})",
        f"- **Upstream change:** {campaign.old_version or '?'} -> {campaign.new_version or '?'} "
        f"({campaign.compatibility or 'unclassified'}), fingerprint `{campaign.change_fingerprint[:12]}`",
        f"- **Origin:** {campaign.origin}" + (f" (producer: {campaign.producer_repository})" if campaign.producer_repository else ""),
        f"- **Organization:** {radius.total_enrolled} enrolled, {len(radius.affected)} affected, "
        f"{len(radius.not_affected)} not affected, {len(radius.unknown)} unknown",
    ]
    if not radius.safe:
        lines.append("- **The organization is NOT declared safe.**")
    lines += ["", "## Repositories", "", "| Repository | State | Freshness | Verification | PR |", "|---|---|---|---|---|"]
    for r in sorted(campaign.records, key=lambda x: x.repository):
        pr = f"#{r.pr_number} ({r.pr_status})" if r.pr_number is not None else (r.pr_status or "-")
        lines.append(
            f"| {r.repository} | `{r.state.value}` | {r.freshness.value} | {r.verification_outcome or '-'} | {pr} |"
        )
    actions = [(r.repository, next_action(r)) for r in sorted(campaign.records, key=lambda x: x.repository)]
    actions = [(name, a) for name, a in actions if a]
    if actions:
        lines += ["", "## Required actions", ""]
        lines += [f"- **{name}:** {action}" for name, action in actions]
    if campaign.unresolved_risks:
        lines += ["", "## Unresolved risk", ""]
        lines += [f"- {risk}" for risk in campaign.unresolved_risks]
    stale = [r.repository for r in campaign.records if r.freshness is not Freshness.FRESH]
    if stale:
        lines += ["", "## Data freshness", "", "Evidence for these repositories is not fresh and was not used to claim safety: "
                  + ", ".join(sorted(stale)) + "."]
    unknown_unclassified = [r.repository for r in campaign.records if r.org_class is OrgClass.UNKNOWN]
    if unknown_unclassified and not stale:
        lines += ["", "Some repositories could not be classified from available evidence."]
    return "\n".join(lines) + "\n"


__all__ = ["DOSSIER_VERSION", "campaign_to_dict", "next_action", "render_campaign_markdown"]
