"""Deterministic migration planner (M7 / M7.1 / M7.2).

For every affected consumer x every diff item that reaches it, one rule
decides the strategy, the auto-fix eligibility and -- only when the
change is fully determined by the contract or an explicit hint -- a typed
:class:`EditOperation`:

==============================  ===================  ==================
change                          strategy             eligibility
==============================  ===================  ==================
SDK symbol renamed (hint)       RENAME_SYMBOL        AUTO_SAFE
SDK argument renamed (hint)     RENAME_PARAMETER     AUTO_SAFE
module moved (hint)             MOVE_IMPORT          AUTO_SAFE
enum value replaced (hint)      REPLACE_ENUM_VALUE   AUTO_SAFE
package version (minor/patch,   BUMP_PACKAGE_VERSION AUTO_SAFE
or major with structure)
new required argument with a    ADD_REQUIRED_...     AUTO_WITH_REVIEW
hinted value source
result field renamed (Python)   ADAPT_RETURN_FIELD   AUTO_WITH_REVIEW
endpoint moved/replaced         REPLACE_ENDPOINT     AUTO_WITH_REVIEW
major bump, no structure        BUMP + VERIFY_UPGRADE AUTO_WITH_REVIEW +
                                                     HUMAN_REQUIRED
new required value, no source,  --                   HUMAN_REQUIRED
removed API/argument, response
shape, auth, unknown, indirect
arguments
lockfile refresh, JS result     --                   UNSUPPORTED
fields, class renames
==============================  ===================  ==================

The planner never invents a value, never touches credentials and never
plans an edit outside an affected consumer (or the dependency's own
manifest declaration).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from patchfrog.dependencies.domain import (
    DependencyInventory,
    EvidenceType,
    ExternalDependency,
)
from patchfrog.migration.domain import (
    AutoFixEligibility,
    EditOperation,
    MigrationPlan,
    MigrationStatus,
    MigrationStep,
    MigrationStrategy,
    MigrationTarget,
    ResidualRisk,
    VersionConstraint,
    step_identity,
)
from patchfrog.upstream.blast_radius import BlastRadius
from patchfrog.upstream.calls import language_for
from patchfrog.upstream.consumers import AffectedConsumer, ImpactKind
from patchfrog.upstream.domain import (
    CompatibilityClass,
    ContractDiffItem,
    DiffItemKind,
    ExternalChangeEvent,
    ExternalChangeKind,
    SubjectKind,
)
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints, parse_operation
from patchfrog.upstream.workspace import RepositoryImpact, RepositoryImpactStatus

AUTO_SAFE = AutoFixEligibility.AUTO_SAFE
AUTO_WITH_REVIEW = AutoFixEligibility.AUTO_WITH_REVIEW
HUMAN = AutoFixEligibility.HUMAN_REQUIRED
UNSUPPORTED = AutoFixEligibility.UNSUPPORTED

_CALLS = frozenset({EvidenceType.SDK_CALL.value})
_HTTP = frozenset({EvidenceType.HTTP_ENDPOINT.value, EvidenceType.OPENAPI_PATH_REFERENCE.value})
_PACKAGE_KINDS = frozenset({
    DiffItemKind.PACKAGE_MAJOR_BUMP, DiffItemKind.PACKAGE_MINOR_BUMP, DiffItemKind.PACKAGE_PATCH_BUMP,
    DiffItemKind.PACKAGE_PRERELEASE_CHANGE, DiffItemKind.PACKAGE_DOWNGRADE, DiffItemKind.PACKAGE_VERSION_UNPARSEABLE,
})
_REQUIRED_KINDS = frozenset({
    DiffItemKind.SDK_PARAMETER_ADDED_REQUIRED, DiffItemKind.SDK_PARAMETER_BECAME_REQUIRED,
    DiffItemKind.PARAMETER_ADDED_REQUIRED, DiffItemKind.PARAMETER_BECAME_REQUIRED,
    DiffItemKind.REQUEST_FIELD_ADDED_REQUIRED, DiffItemKind.REQUEST_FIELD_BECAME_REQUIRED,
})
_RESPONSE_PREFIXES = ("response_", "schema_")
_REWRITABLE_MANIFEST_ECOSYSTEMS = frozenset({"pypi", "npm"})
_AUTH_PREFIXES = ("auth_", "security_scheme_")
_ARGUMENT_LEVEL = frozenset({
    DiffItemKind.SDK_PARAMETER_RENAMED, DiffItemKind.SDK_PARAMETER_REMOVED, DiffItemKind.SDK_PARAMETER_TYPE_CHANGED,
    DiffItemKind.SDK_ENUM_NARROWED, DiffItemKind.SDK_ENUM_VALUE_REPLACED, DiffItemKind.PARAMETER_RENAMED,
    DiffItemKind.REQUEST_FIELD_RENAMED, *_REQUIRED_KINDS,
})


@dataclass(frozen=True, slots=True)
class _Rule:
    strategy: MigrationStrategy
    eligibility: AutoFixEligibility
    operation: EditOperation | None
    proposed: str
    residual: str


def _literal_code(value: object) -> str:
    """A hint literal as source text: JSON is valid for both Python
    (after the three keyword spellings) and JS."""

    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    return json.dumps(value)


def _placeholders(path: str) -> int:
    return path.count("{")


def _rule(item: ContractDiffItem, consumer: AffectedConsumer, hints: ChangeHints) -> _Rule:
    site = consumer.site
    evidence = site.evidence_type.value
    language = language_for(site.file_path) or "none"
    kind = item.kind
    member = item.subject.member
    replacement = item.replacement

    if consumer.impact is ImpactKind.POTENTIAL and kind in _ARGUMENT_LEVEL:
        return _Rule(MigrationStrategy.MANUAL_REVIEW, HUMAN, None,
                     f"check whether this call passes '{member}' and apply: {item.explanation}",
                     "arguments are passed indirectly; the change cannot be located statically")

    if kind is DiffItemKind.SDK_SYMBOL_RENAMED and replacement:
        if evidence in _CALLS:
            return _Rule(MigrationStrategy.RENAME_SYMBOL, AUTO_SAFE,
                         EditOperation.of("rename_call_chain", old=site.token, new=replacement),
                         f"call `{replacement}` instead of `{site.token}`", "none (one-to-one rename)")
        return _Rule(MigrationStrategy.RENAME_SYMBOL, UNSUPPORTED, None,
                     f"use `{replacement}` instead of `{site.token}`",
                     "class/constructor renames also need import updates; not automated")
    if kind is DiffItemKind.SDK_PARAMETER_RENAMED and member and replacement:
        return _Rule(MigrationStrategy.RENAME_PARAMETER, AUTO_SAFE,
                     EditOperation.of("rename_keyword", old=member, new=replacement),
                     f"pass `{replacement}` instead of `{member}`", "none (one-to-one rename)")
    if kind in (DiffItemKind.PARAMETER_RENAMED, DiffItemKind.REQUEST_FIELD_RENAMED) and member and replacement:
        if evidence in _CALLS:
            return _Rule(MigrationStrategy.RENAME_PARAMETER, AUTO_SAFE,
                         EditOperation.of("rename_keyword", old=member, new=replacement),
                         f"pass `{replacement}` instead of `{member}`", "none (one-to-one rename)")
        return _Rule(MigrationStrategy.RENAME_PARAMETER, HUMAN, None,
                     f"send `{replacement}` instead of `{member}`",
                     "the request payload is built outside a locatable SDK call")
    if kind in _REQUIRED_KINDS and member:
        symbol = site.token if evidence in _CALLS else None
        source = hints.required_value(item.subject.name or "", member) or (
            hints.required_value(symbol, member) if symbol else None)
        if symbol and source is not None:
            if source.has_value:
                # Raw JSON: each rewriter renders it in its own language.
                operation = EditOperation.of("add_keyword", name=member, value_json=json.dumps(source.value))
                proposed = f"pass `{member}={_literal_code(source.value)}` (value from hints)"
            else:
                operation = EditOperation.of("add_keyword", name=member, from_parameter=source.from_parameter or "")
                proposed = f"pass `{member}` with the value of `{source.from_parameter}` (from hints)"
            return _Rule(MigrationStrategy.ADD_REQUIRED_PARAMETER, AUTO_WITH_REVIEW, operation, proposed,
                         "the value comes from hints; confirm it is right for this call site")
        return _Rule(MigrationStrategy.ADD_REQUIRED_PARAMETER, HUMAN, None,
                     f"provide the newly required `{member}`",
                     "no deterministic source for the new value; PatchFrog never invents one")
    if kind is DiffItemKind.SDK_ENUM_VALUE_REPLACED and member and replacement and item.old:
        old_value = json.loads(item.old) if item.old.startswith('"') else item.old
        return _Rule(MigrationStrategy.REPLACE_ENUM_VALUE, AUTO_SAFE,
                     EditOperation.of("replace_keyword_literal", name=member, old=str(old_value), new=replacement),
                     f"pass `{member}={replacement!r}` instead of `{old_value!r}`", "none (explicit value mapping)")
    if kind is DiffItemKind.SDK_RETURN_FIELD_RENAMED and member and replacement:
        old_field = member.removeprefix("returns.")
        if language == "python":
            return _Rule(MigrationStrategy.ADAPT_RETURN_FIELD, AUTO_WITH_REVIEW,
                         EditOperation.of("rename_result_field", old=old_field, new=replacement),
                         f"read `.{replacement}` instead of `.{old_field}` from the result",
                         "only uses of the result inside the calling function are rewritten; results passed "
                         "elsewhere are not followed")
        return _Rule(MigrationStrategy.ADAPT_RETURN_FIELD, UNSUPPORTED, None,
                     f"read `{replacement}` instead of `{old_field}` from the result",
                     "result-field rewrites are implemented for Python only")
    if kind is DiffItemKind.SDK_MODULE_MOVED and replacement and item.subject.name:
        return _Rule(MigrationStrategy.MOVE_IMPORT, AUTO_SAFE,
                     EditOperation.of("rename_import_module", old=item.subject.name, new=replacement),
                     f"import from `{replacement}` instead of `{item.subject.name}`", "none (module move)")
    if kind is DiffItemKind.ENDPOINT_PATH_CHANGED and replacement and item.subject.path and item.subject.method:
        new_method, new_path = parse_operation(replacement)
        if evidence not in _HTTP:
            return _Rule(MigrationStrategy.REPLACE_ENDPOINT, HUMAN, None, f"target {replacement}",
                         "the endpoint is reached through an SDK method; the SDK itself must change")
        if new_method != item.subject.method or _placeholders(new_path) != _placeholders(item.subject.path):
            return _Rule(MigrationStrategy.REPLACE_ENDPOINT, HUMAN, None, f"target {replacement}",
                         "HTTP method or path parameters differ; the request itself must change")
        return _Rule(MigrationStrategy.REPLACE_ENDPOINT, AUTO_WITH_REVIEW,
                     EditOperation.of("replace_path_literal", old=item.subject.path, new=new_path),
                     f"request `{new_path}` instead of `{item.subject.path}`",
                     "the replacement endpoint may differ in behavior; review the response handling")
    if kind in (DiffItemKind.SDK_SYMBOL_REMOVED, DiffItemKind.ENDPOINT_REMOVED, DiffItemKind.OPERATION_REMOVED,
                DiffItemKind.SDK_MODULE_REMOVED):
        return _Rule(MigrationStrategy.REPLACE_REMOVED_API, HUMAN, None,
                     f"replace the removed {item.subject.describe()}",
                     "removed with no known replacement")
    if kind in (DiffItemKind.SDK_PARAMETER_REMOVED, DiffItemKind.PARAMETER_REMOVED,
                DiffItemKind.REQUEST_FIELD_REMOVED, DiffItemKind.REQUEST_BODY_REMOVED):
        return _Rule(MigrationStrategy.REMOVE_ARGUMENT, HUMAN, None,
                     f"stop sending `{member or 'the request body'}` (or find its replacement)",
                     "dropping an input changes behavior; a person must decide")
    if kind.value.startswith(_AUTH_PREFIXES):
        return _Rule(MigrationStrategy.UPDATE_AUTH, HUMAN, None, item.explanation,
                     "credentials and auth configuration are never changed automatically")
    if kind.value.startswith(_RESPONSE_PREFIXES) or kind is DiffItemKind.SDK_RETURN_SHAPE_CHANGED:
        return _Rule(MigrationStrategy.ADAPT_RESPONSE, HUMAN, None,
                     f"adapt how the result is used: {item.explanation}",
                     "response handling depends on business logic")
    return _Rule(MigrationStrategy.MANUAL_REVIEW, HUMAN, None, item.explanation,
                 "unknown compatibility" if item.compatibility is CompatibilityClass.UNKNOWN
                 else "no deterministic migration for this change")


def _tests_for(consumer: AffectedConsumer, radii: Iterable[BlastRadius]) -> tuple[str, ...]:
    tests: set[str] = set()
    for radius in radii:
        for test in radius.related_tests:
            if test.covers in (consumer.file_path, consumer.location) or test.covers.startswith(
                    consumer.file_path + "::"):
                tests.add(test.file_path)
        # Tests that call an affected symbol transitively.
        for node in radius.nodes:
            if node.is_test and consumer.location in " ".join(node.reasons):
                tests.add(node.file_path)
    return tuple(sorted(tests))


def _describe_usage(consumer: AffectedConsumer, item: ContractDiffItem) -> str:
    site = consumer.site
    head = f"{site.evidence_type.value.replace('_', ' ')} `{site.token}` at {site.file_path}:{site.line or 0}"
    if item.subject.member and not item.subject.member.startswith("returns."):
        return f"{head} (argument `{item.subject.member}`)"
    return head


def _consumer_steps(
    event: ExternalChangeEvent, impact: RepositoryImpact, hints: ChangeHints
) -> list[MigrationStep]:
    items = event.items_by_key()
    merged: dict[tuple[str, str, str], MigrationStep] = {}
    for consumer_impact in impact.consumer_impacts:
        for consumer in consumer_impact.affected:
            for key in consumer.diff_item_keys:
                item = items[key]
                if item.subject.kind is SubjectKind.PACKAGE:
                    continue  # plan-level (manifest) step, below
                rule = _rule(item, consumer, hints)
                target = MigrationTarget(
                    repository=impact.repository, file_path=consumer.file_path, symbol=consumer.symbol,
                    line=consumer.site.line, usage_token=consumer.site.token,
                    evidence_type=consumer.site.evidence_type.value,
                    language=language_for(consumer.file_path) or "none",
                )
                identity = (consumer.site_key, rule.strategy.value,
                            json.dumps(rule.operation.as_dict() if rule.operation else None, sort_keys=True))
                existing = merged.get(identity)
                item_keys = tuple(sorted({key, *(existing.diff_item_keys if existing else ())}))
                kinds = tuple(sorted({item.kind.value, *(existing.diff_item_kinds if existing else ())}))
                merged[identity] = MigrationStep(
                    step_id=step_identity(target, rule.strategy, rule.operation, item_keys),
                    target=target,
                    strategy=rule.strategy,
                    eligibility=rule.eligibility,
                    diff_item_keys=item_keys,
                    diff_item_kinds=kinds,
                    current_usage=_describe_usage(consumer, item),
                    required_change=item.explanation if existing is None
                    else f"{existing.required_change}; {item.explanation}",
                    proposed_change=rule.proposed,
                    tests_to_update=_tests_for(consumer, impact.blast_radii),
                    residual_uncertainty=rule.residual,
                    operation=rule.operation,
                    usage_site_key=consumer.site_key,
                )
    return list(merged.values())


def _manifest_declaration(dependency: ExternalDependency) -> tuple[str, int | None, str] | None:
    for evidence in dependency.evidence:
        if evidence.evidence_type is EvidenceType.MANIFEST_DECLARATION:
            return evidence.file_path, evidence.line, evidence.token
    return None


def _package_steps(
    event: ExternalChangeEvent,
    impact: RepositoryImpact,
    inventory: DependencyInventory,
    *,
    structural_steps_unresolved: bool,
) -> list[MigrationStep]:
    version_items = [i for i in event.diff if i.kind in _PACKAGE_KINDS and i.replacement]
    if not version_items:
        return []
    item = version_items[0]
    structural = event.kind is ExternalChangeKind.CONTRACT_REVISION
    matched = {m.identity.key for m in impact.matches}
    steps: list[MigrationStep] = []
    by_key = inventory.by_key()
    for key in sorted(matched):
        dependency = by_key.get(key)
        if dependency is None:
            continue
        declaration = _manifest_declaration(dependency)
        if declaration is None:
            continue
        manifest, line, package = declaration
        if dependency.ecosystem.value not in _REWRITABLE_MANIFEST_ECOSYSTEMS:
            eligibility, residual = UNSUPPORTED, f"{dependency.ecosystem.value} manifests are not rewritten"
        elif item.kind in (DiffItemKind.PACKAGE_DOWNGRADE, DiffItemKind.PACKAGE_VERSION_UNPARSEABLE):
            eligibility, residual = HUMAN, "downgrades and unparseable versions need a person to choose the constraint"
        elif item.kind in (DiffItemKind.PACKAGE_MINOR_BUMP, DiffItemKind.PACKAGE_PATCH_BUMP) and \
                item.compatibility is CompatibilityClass.NON_BREAKING:
            eligibility, residual = AUTO_SAFE, "none (semver-compatible bump)"
        elif structural and structural_steps_unresolved:
            # Installing the new version while a consumer still needs a
            # manual change would break it: the bump is not safe on its own.
            eligibility, residual = AUTO_WITH_REVIEW, "some consumers still need manual changes for this version"
        elif structural:
            eligibility, residual = AUTO_SAFE, "every structural change is covered by an automatic step"
        else:
            eligibility, residual = AUTO_WITH_REVIEW, "major upgrade without structural evidence of what changed"
        target = MigrationTarget(impact.repository, manifest, None, line, package, "manifest_declaration", "manifest")
        new_version = item.replacement or ""
        operation = (
            EditOperation.of("bump_manifest_version", package=package, ecosystem=dependency.ecosystem.value,
                             version=new_version)
            if eligibility in (AUTO_SAFE, AUTO_WITH_REVIEW) else None
        )
        steps.append(
            MigrationStep(
                step_id=step_identity(target, MigrationStrategy.BUMP_PACKAGE_VERSION, operation, (item.key,)),
                target=target,
                strategy=MigrationStrategy.BUMP_PACKAGE_VERSION,
                eligibility=eligibility,
                diff_item_keys=(item.key,),
                diff_item_kinds=(item.kind.value,),
                current_usage=f"{package} declared as `{dependency.version.declared or 'unpinned'}` in {manifest}",
                required_change=item.explanation,
                proposed_change=f"require {package} {new_version}",
                tests_to_update=(),
                residual_uncertainty=residual,
                operation=operation,
                version_constraint=VersionConstraint(manifest, package, dependency.ecosystem.value,
                                                     dependency.version.declared, new_version),
            )
        )
        if dependency.version.resolved_in:
            lock_target = MigrationTarget(impact.repository, dependency.version.resolved_in, None, None, package,
                                          "lockfile_resolution", "manifest")
            steps.append(
                MigrationStep(
                    step_id=step_identity(lock_target, MigrationStrategy.REFRESH_LOCKFILE, None, (item.key,)),
                    target=lock_target,
                    strategy=MigrationStrategy.REFRESH_LOCKFILE,
                    eligibility=UNSUPPORTED,
                    diff_item_keys=(item.key,),
                    diff_item_kinds=(item.kind.value,),
                    current_usage=f"{package} resolved to {dependency.version.resolved} in "
                                  f"{dependency.version.resolved_in}",
                    required_change="the lockfile must match the new constraint",
                    proposed_change=f"regenerate {dependency.version.resolved_in} with the package manager",
                    tests_to_update=(),
                    residual_uncertainty="lockfiles are regenerated by the package manager, never hand-edited",
                )
            )
        if not structural and item.compatibility is not CompatibilityClass.NON_BREAKING:
            potential = [c for i in impact.consumer_impacts for c in i.potential]
            verify_target = MigrationTarget(impact.repository, manifest, None, None, package, "package", "none")
            steps.append(
                MigrationStep(
                    step_id=step_identity(verify_target, MigrationStrategy.VERIFY_UPGRADE, None, (item.key,)),
                    target=verify_target,
                    strategy=MigrationStrategy.VERIFY_UPGRADE,
                    eligibility=HUMAN,
                    diff_item_keys=(item.key,),
                    diff_item_kinds=(item.kind.value,),
                    current_usage=f"{len(potential)} usage(s) of {package}: "
                                  + ", ".join(sorted({c.location for c in potential})[:10]),
                    required_change=f"verify {package} {new_version} against every usage "
                                    "(release notes, changelog, tests)",
                    proposed_change="supply an SDK surface or contract for the new version to let PatchFrog "
                                    "plan structural steps",
                    tests_to_update=tuple(sorted({t.file_path for r in impact.blast_radii for t in r.related_tests})),
                    residual_uncertainty="no structural evidence of what changed",
                )
            )
    return steps


def _residual(steps: list[MigrationStep], event: ExternalChangeEvent) -> ResidualRisk:
    if any(s.eligibility in (HUMAN, UNSUPPORTED) and s.strategy is not MigrationStrategy.REFRESH_LOCKFILE
           for s in steps):
        return ResidualRisk.HIGH
    if any(s.eligibility is AUTO_WITH_REVIEW for s in steps) or event.kind is ExternalChangeKind.VERSION_UPDATE:
        return ResidualRisk.MEDIUM
    return ResidualRisk.LOW


def planned_status(steps: Iterable[MigrationStep]) -> MigrationStatus:
    steps = list(steps)
    if not steps:
        return MigrationStatus.NOT_REQUIRED
    if any(s.auto_fix for s in steps):
        return MigrationStatus.PLANNED
    if all(s.eligibility is UNSUPPORTED for s in steps):
        return MigrationStatus.UNSUPPORTED
    return MigrationStatus.HUMAN_REQUIRED


def plan_migration(
    event: ExternalChangeEvent,
    impact: RepositoryImpact,
    inventory: DependencyInventory,
    *,
    hints: ChangeHints = EMPTY_HINTS,
) -> MigrationPlan:
    if impact.status is RepositoryImpactStatus.UNAFFECTED:
        steps: list[MigrationStep] = []
        notes: tuple[str, ...] = (f"not affected: {impact.reason}",)
    else:
        consumer_steps = _consumer_steps(event, impact, hints)
        steps = consumer_steps + _package_steps(
            event, impact, inventory, structural_steps_unresolved=any(not s.auto_fix for s in consumer_steps)
        )
        notes = tuple(impact.notes)
    steps.sort(key=lambda s: (s.target.file_path, s.target.line or 0, s.strategy.value, s.step_id))
    return MigrationPlan(
        change_fingerprint=event.fingerprint,
        repository=impact.repository,
        commit_sha=impact.commit_sha,
        dependency_keys=tuple(sorted(m.identity.key for m in impact.matches)),
        steps=tuple(steps),
        residual_risk=_residual(steps, event) if steps else ResidualRisk.LOW,
        status=planned_status(steps),
        evidence={
            "diff_items": tuple(sorted({k for s in steps for k in s.diff_item_keys})),
            "usage_sites": tuple(sorted({s.usage_site_key for s in steps if s.usage_site_key})),
        },
        notes=notes,
    )


__all__ = ["plan_migration", "planned_status"]
