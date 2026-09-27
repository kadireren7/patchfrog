"""Machine-readable (JSON) and human-readable views of a
:class:`~patchfrog.migration_pr.domain.MigrationPRPlan` -- backs
``patchfrog migrations publish --dry-run``'s ``--json`` and text output
(M9.10). Never touches GitHub; a pure rendering of an already-built plan.
"""

from __future__ import annotations

from typing import Any

from patchfrog.migration_pr.domain import MigrationPRPlan


def linkage_to_dict(plan: MigrationPRPlan) -> dict[str, Any]:
    linkage = plan.linkage
    return {
        "identity_key": linkage.identity_key(),
        "change_fingerprint": linkage.change_fingerprint,
        "dependency_key": linkage.dependency_key,
        "repository": linkage.repository,
        "base_commit_sha": linkage.base_commit_sha,
        "plan_fingerprint": linkage.plan_fingerprint,
        "patch_fingerprint": linkage.patch_fingerprint,
        "verification_plan_fingerprint": linkage.verification_plan_fingerprint,
        "bundle_fingerprint": linkage.bundle_fingerprint,
        "engine_version": linkage.engine_version,
    }


def pr_plan_to_dict(plan: MigrationPRPlan) -> dict[str, Any]:
    return {
        "linkage": linkage_to_dict(plan),
        "eligibility": plan.eligibility.value,
        "eligibility_reason": plan.eligibility_reason,
        "may_open_code_pr": plan.may_open_code_pr,
        "branch_name": plan.branch_name,
        "commit_message": plan.commit_message,
        "pr_title": plan.pr_title,
        "pr_body": plan.pr_body,
        "check_title": plan.check_title,
        "check_summary": plan.check_summary,
    }


def render_pr_plan_text(plan: MigrationPRPlan) -> str:
    lines = [
        f"Eligibility: {plan.eligibility.value.upper()}",
        f"  - {plan.eligibility_reason}",
        f"Would open a code PR: {'yes' if plan.may_open_code_pr else 'no'}",
        f"Proposed branch: {plan.branch_name}",
        f"Commit title: {plan.commit_message}",
        f"PR title: {plan.pr_title}",
        f"Check title: {plan.check_title}",
        f"Check summary: {plan.check_summary}",
        "",
        "PR body:",
        "--------",
        plan.pr_body,
    ]
    return "\n".join(lines) + "\n"


__all__ = ["linkage_to_dict", "pr_plan_to_dict", "render_pr_plan_text"]
