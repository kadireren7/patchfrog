"""Verification requirement generation from real migration context (M8.2).

Every requirement traces back to either a universal "the patch must not be
obviously broken" concern (syntax/imports) or a specific
:class:`~patchfrog.migration.domain.MigrationStep` this exact patch
applied -- never invented, never "run everything" by default.
"""

from __future__ import annotations

import hashlib

from patchfrog.migration.domain import MigrationPlan, MigrationStep, MigrationStrategy
from patchfrog.migration_verification.domain import (
    TestSelection,
    VerificationRequirement,
    VerificationRequirementKind,
)

#: Strategies whose fix is "the SDK call now matches the new contract" --
#: verified by confirming the old, obsolete call shape no longer appears
#: and (where the contract gives one) the replacement does.
_CONTRACT_CALL_STRATEGIES = frozenset(
    {
        MigrationStrategy.RENAME_SYMBOL,
        MigrationStrategy.REPLACE_ENDPOINT,
        MigrationStrategy.REPLACE_REMOVED_API,
        MigrationStrategy.RENAME_PARAMETER,
        MigrationStrategy.REPLACE_ENUM_VALUE,
        MigrationStrategy.ADAPT_RETURN_FIELD,
        MigrationStrategy.ADAPT_RESPONSE,
        MigrationStrategy.UPDATE_AUTH,
        MigrationStrategy.REMOVE_ARGUMENT,
    }
)
_OBSOLETE_SYMBOL_STRATEGIES = frozenset(
    {MigrationStrategy.RENAME_SYMBOL, MigrationStrategy.REPLACE_ENDPOINT, MigrationStrategy.REPLACE_REMOVED_API}
)
_VERSION_STRATEGIES = frozenset(
    {MigrationStrategy.BUMP_PACKAGE_VERSION, MigrationStrategy.REFRESH_LOCKFILE, MigrationStrategy.VERIFY_UPGRADE}
)


def _requirement_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _requirements_for_step(step: MigrationStep) -> tuple[VerificationRequirement, ...]:
    out: list[VerificationRequirement] = []
    if step.strategy in _OBSOLETE_SYMBOL_STRATEGIES:
        out.append(
            VerificationRequirement(
                requirement_id=_requirement_id("obsolete_symbol", step.step_id),
                kind=VerificationRequirementKind.OBSOLETE_SYMBOL_REMOVED,
                description=f"the obsolete usage {step.target.usage_token!r} no longer remains at {step.target.location}",
                mandatory=True,
                diff_item_keys=step.diff_item_keys,
                usage_site_key=step.usage_site_key,
                source_step_id=step.step_id,
            )
        )
    if step.strategy in _CONTRACT_CALL_STRATEGIES:
        out.append(
            VerificationRequirement(
                requirement_id=_requirement_id("sdk_call", step.step_id),
                kind=VerificationRequirementKind.SDK_CALL_MATCHES_CONTRACT,
                description=f"the modified call at {step.target.location} matches the new SDK surface",
                mandatory=True,
                diff_item_keys=step.diff_item_keys,
                usage_site_key=step.usage_site_key,
                source_step_id=step.step_id,
            )
        )
    if step.strategy is MigrationStrategy.ADD_REQUIRED_PARAMETER:
        out.append(
            VerificationRequirement(
                requirement_id=_requirement_id("required_arg", step.step_id),
                kind=VerificationRequirementKind.REQUIRED_ARGUMENT_PRESENT,
                description=f"the newly required argument is present at {step.target.location}",
                mandatory=True,
                diff_item_keys=step.diff_item_keys,
                usage_site_key=step.usage_site_key,
                source_step_id=step.step_id,
            )
        )
    if step.strategy in _VERSION_STRATEGIES and step.version_constraint is not None:
        out.append(
            VerificationRequirement(
                requirement_id=_requirement_id("version_constraint", step.step_id),
                kind=VerificationRequirementKind.VERSION_CONSTRAINT_COMPATIBLE,
                description=(
                    f"{step.version_constraint.manifest}: {step.version_constraint.package} constraint now "
                    f"allows {step.version_constraint.target}"
                ),
                mandatory=True,
                diff_item_keys=step.diff_item_keys,
                usage_site_key=step.usage_site_key,
                source_step_id=step.step_id,
            )
        )
    return tuple(out)


def generate_requirements(
    plan: MigrationPlan, *, test_selections: tuple[TestSelection, ...]
) -> tuple[VerificationRequirement, ...]:
    """Deterministic requirement generation from ``plan``'s own automatic
    steps and the already-selected targeted tests. A plan with no
    automatic steps at all (fully HUMAN_REQUIRED) yields no requirements
    -- there is nothing PatchFrog itself changed to verify."""

    automatic = plan.automatic_steps
    if not automatic:
        return ()

    requirements: list[VerificationRequirement] = [
        VerificationRequirement(
            requirement_id=_requirement_id("syntax", plan.change_fingerprint),
            kind=VerificationRequirementKind.SYNTAX_VALID,
            description="every file the patch modified remains syntactically valid",
            mandatory=True,
        ),
        VerificationRequirement(
            requirement_id=_requirement_id("imports", plan.change_fingerprint),
            kind=VerificationRequirementKind.IMPORTS_RESOLVE,
            description=(
                "every modified module still imports cleanly (best-effort: PatchFrog's own sandbox may "
                "lack the target repository's third-party dependencies, so a failure here is evidence, "
                "not by itself proof of a broken migration -- never mandatory alone for VERIFIED)"
            ),
            mandatory=False,
        ),
        VerificationRequirement(
            requirement_id=_requirement_id("typecheck", plan.change_fingerprint),
            kind=VerificationRequirementKind.TYPE_CHECK_PASSES,
            description="static type check of the modified files (best-effort; never mandatory alone for VERIFIED)",
            mandatory=False,
        ),
    ]

    for step in automatic:
        requirements.extend(_requirements_for_step(step))

    contract_requirement_ids = tuple(
        r.requirement_id
        for r in requirements
        if r.kind
        in (
            VerificationRequirementKind.OBSOLETE_SYMBOL_REMOVED,
            VerificationRequirementKind.SDK_CALL_MATCHES_CONTRACT,
            VerificationRequirementKind.REQUIRED_ARGUMENT_PRESENT,
            VerificationRequirementKind.VERSION_CONSTRAINT_COMPATIBLE,
        )
    )
    if contract_requirement_ids:
        requirements.append(
            VerificationRequirement(
                requirement_id=_requirement_id("contract_restored", plan.change_fingerprint),
                kind=VerificationRequirementKind.CONTRACT_COMPATIBILITY_RESTORED,
                description="the upstream contract change this migration targets is fully addressed",
                mandatory=True,
                diff_item_keys=tuple(sorted({k for r in requirements for k in r.diff_item_keys})),
            )
        )

    requirements.append(
        VerificationRequirement(
            requirement_id=_requirement_id("unit_tests", plan.change_fingerprint),
            kind=VerificationRequirementKind.UNIT_TESTS_PASS,
            description="targeted unit tests covering the affected consumers pass on the patched state",
            #: Mandatory whenever real code changed -- its status becomes
            #: UNAVAILABLE (never guessed PASSED) when no test evidence
            #: exists at all, which the decision engine treats as
            #: insufficient evidence for VERIFIED, not as a failure.
            mandatory=True,
        )
    )
    requirements.append(
        VerificationRequirement(
            requirement_id=_requirement_id("consumer_behavior", plan.change_fingerprint),
            kind=VerificationRequirementKind.CONSUMER_BEHAVIOR_PRESERVED,
            description="downstream consumer behavior is preserved, where baseline-vs-patched evidence exists",
            mandatory=False,
        )
    )

    return tuple(requirements)


__all__ = ["generate_requirements"]
