"""PR body / Change Dossier rendering (M9.4).

A concise, evidence-backed summary -- never raw logs, never AI prose.
Every section is built directly from already-computed M6/M7/M8 evidence
(:class:`~patchfrog.upstream.domain.ExternalChangeEvent`,
:class:`~patchfrog.upstream.blast_radius.BlastRadius`,
:class:`~patchfrog.migration.domain.MigrationPlan`/``GeneratedPatch``,
:class:`~patchfrog.migration_verification.domain.MigrationEvidenceBundle`).
"""

from __future__ import annotations

from patchfrog.migration.domain import GeneratedPatch, MigrationPlan
from patchfrog.migration_pr.domain import MigrationPREligibility, MigrationPRLinkage
from patchfrog.migration_pr.marker import render_marker, sanitize_untrusted_text
from patchfrog.migration_verification.domain import (
    CheckStatus,
    MigrationEvidenceBundle,
    VerificationStepKind,
)
from patchfrog.upstream.blast_radius import BlastRadius
from patchfrog.upstream.domain import ExternalChangeEvent


def _dependency_label(event: ExternalChangeEvent) -> str:
    target = event.target
    parts = [p for p in (target.provider_key, target.package_name, target.dependency_key) if p]
    return parts[0] if parts else "unknown dependency"


def render_pr_title(event: ExternalChangeEvent, *, repository: str) -> str:
    label = _dependency_label(event)
    old_version = event.old.version or "?"
    new_version = event.new.version or "?"
    return f"PatchFrog: migrate {label} {old_version} -> {new_version}"


def _upstream_section(event: ExternalChangeEvent) -> list[str]:
    lines = [
        "## Upstream change", "",
        f"- Dependency: `{_dependency_label(event)}`",
        f"- Version: `{event.old.version or '?'}` -> `{event.new.version or '?'}`",
        f"- Compatibility classification: **{event.classification.risk.value.upper()}** "
        f"({event.classification.compatibility.value})",
    ]
    if event.classification.reasons:
        lines.append(f"- Reasons: {', '.join(sanitize_untrusted_text(r) for r in event.classification.reasons)}")
    breaking = [item for item in event.diff if item.compatibility.value == "breaking"][:10]
    if breaking:
        lines.append("")
        lines.append("Breaking changes addressed by this migration:")
        for item in breaking:
            lines.append(f"- `{item.location}`: {sanitize_untrusted_text(item.explanation)}")
    return lines


def _impact_section(blast_radii: tuple[BlastRadius, ...]) -> list[str]:
    lines = ["", "## Impact", ""]
    if not blast_radii:
        lines.append("- No blast radius evidence was computed for this migration.")
        return lines
    for radius in blast_radii:
        summary = radius.summary()
        lines.append(
            f"- `{radius.dependency_key}`: {summary['direct_sites']} direct usage site(s), "
            f"{summary['direct']} direct symbol(s), {summary['transitive']} transitively affected, "
            f"{summary['related_tests']} related test file(s) across {summary['modules']} module(s)"
        )
    return lines


def _migration_section(plan: MigrationPlan, patch: GeneratedPatch | None) -> list[str]:
    lines = ["", "## Migration", ""]
    strategies = sorted({s.strategy.value for s in plan.automatic_steps})
    lines.append(f"- Strategies applied: {', '.join(strategies) if strategies else 'none (no automatic steps)'}")
    if patch is not None:
        lines.append(f"- Files changed: {', '.join(f'`{f}`' for f in patch.modified_files) or 'none'}")
    if plan.unresolved_steps:
        lines.append(f"- Human-required steps ({len(plan.unresolved_steps)}):")
        for step in plan.unresolved_steps[:10]:
            lines.append(f"  - `{step.target.location}`: {sanitize_untrusted_text(step.residual_uncertainty)}")
    return lines


def _verification_section(bundle: MigrationEvidenceBundle) -> list[str]:
    lines = ["", "## Verification", "", f"- Final outcome: **{bundle.outcome.value.upper()}**"]
    for reason in bundle.outcome_reasons:
        lines.append(f"  - {sanitize_untrusted_text(reason)}")

    test_steps = [e for e in bundle.step_evidence if e.kind in (
        VerificationStepKind.TARGETED_UNIT_TEST, VerificationStepKind.TARGETED_INTEGRATION_TEST
    )]
    if test_steps:
        passed = sum(1 for e in test_steps if e.status is CheckStatus.PASSED)
        lines.append(f"- Targeted tests: {passed}/{len(test_steps)} passed")

    if bundle.contract_checks:
        passed_contract = sum(1 for c in bundle.contract_checks if c.status is CheckStatus.PASSED)
        lines.append(f"- Contract checks: {passed_contract}/{len(bundle.contract_checks)} passed")

    type_checks = [e for e in bundle.step_evidence if e.kind is VerificationStepKind.TYPE_CHECK]
    if type_checks:
        lines.append(f"- Type check: {type_checks[0].status.value}")

    if bundle.baseline_comparisons:
        lines.append("- Baseline vs. patched evidence:")
        for comparison in bundle.baseline_comparisons:
            lines.append(f"  - {comparison.baseline_status.value} -> {comparison.patched_status.value} "
                        f"({comparison.outcome.value})")
    lines.append(f"- Evidence strength: {bundle.evidence_strength.value}")
    return lines


def render_pr_body(
    *,
    event: ExternalChangeEvent,
    plan: MigrationPlan,
    patch: GeneratedPatch | None,
    bundle: MigrationEvidenceBundle,
    blast_radii: tuple[BlastRadius, ...],
    linkage: MigrationPRLinkage,
    eligibility: MigrationPREligibility,
) -> str:
    sections: list[str] = []
    if eligibility is MigrationPREligibility.OPEN_WITH_OPERATOR_APPROVAL:
        sections.append(
            "> **PARTIALLY VERIFIED** -- this migration was opened under an explicit operator policy that "
            "allows publishing partially-verified migrations. Review the Verification section below carefully "
            "before merging.\n"
        )
    sections.extend(_upstream_section(event))
    sections.extend(_impact_section(blast_radii))
    sections.extend(_migration_section(plan, patch))
    sections.extend(_verification_section(bundle))
    sections.extend([
        "", "## Residual risk", "", f"- **{bundle.residual_risk.value.upper()}**",
        "", "## Evidence identity", "",
        f"- Upstream change fingerprint: `{linkage.change_fingerprint}`",
        f"- Patch fingerprint: `{linkage.patch_fingerprint}`",
        f"- Verification plan fingerprint: `{linkage.verification_plan_fingerprint}`",
        f"- Evidence bundle fingerprint: `{bundle.bundle_fingerprint}`",
        f"- Repository base SHA: `{linkage.base_commit_sha}`",
        "",
        render_marker(linkage.identity_key()),
    ])
    return "\n".join(sections) + "\n"


def render_check_summary(bundle: MigrationEvidenceBundle) -> tuple[str, str]:
    title = f"Migration verification: {bundle.outcome.value.replace('_', ' ')}"
    summary = f"PatchFrog {bundle.outcome.value.replace('_', ' ')} this migration (evidence strength: " \
             f"{bundle.evidence_strength.value}, residual risk: {bundle.residual_risk.value})."
    return title, summary


__all__ = ["render_check_summary", "render_pr_body", "render_pr_title"]
