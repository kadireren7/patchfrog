"""Evaluate one repository against one upstream change (M10.4).

This composes the existing M6 -> M7 -> M8 -> M9 functions; it adds no new
analysis. Its job is isolation and honesty:

* a repository whose evidence is not fresh is **not analyzed at all** --
  stale evidence is never reused, and it is never reported as unaffected;
* any exception while evaluating one repository is captured on *that*
  repository's record (state ``FAILED``) and never propagates to others;
* nothing is ever written to a checkout and no provider is ever called.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from patchfrog.campaigns.blast import RepositoryClassification, classify_repository
from patchfrog.campaigns.domain import (
    MAX_ERROR_CHARS,
    EnrolledRepository,
    Freshness,
    OrgClass,
    RepositoryRecord,
    RepoState,
    bound_reasons,
)
from patchfrog.campaigns.freshness import FreshnessPolicy, assess_freshness
from patchfrog.campaigns.policy import WorkspacePolicy
from patchfrog.campaigns.state import RepoFacts, derive_repo_state
from patchfrog.dependencies.domain import DependencyInventory
from patchfrog.migration.domain import GeneratedPatch, MigrationPlan
from patchfrog.migration.generator import generate_patch
from patchfrog.migration.planner import plan_migration
from patchfrog.migration_pr.domain import MigrationPRPlan
from patchfrog.migration_pr.planner import build_pr_plan
from patchfrog.migration_verification.domain import CheckStatus, MigrationEvidenceBundle
from patchfrog.migration_verification.service import run_migration_verification
from patchfrog.upstream.domain import CompatibilityClass, ExternalChangeEvent
from patchfrog.upstream.hints import EMPTY_HINTS, ChangeHints
from patchfrog.upstream.workspace import (
    RegistryDependency,
    RepositoryImpact,
    analyze_inventory,
    analyze_repository,
    registry_impact,
)

VerifyFn = Callable[..., Awaitable[MigrationEvidenceBundle]]

_CREDENTIAL_URL = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")
_TOKEN_SHAPES = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{20,})\b")


def safe_error(exc: BaseException) -> str:
    """A bounded, credential-scrubbed one-line description of an exception."""

    text = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    text = _CREDENTIAL_URL.sub(r"\1***@", text)
    text = _TOKEN_SHAPES.sub("***", text)
    return text[:MAX_ERROR_CHARS]


@dataclass(frozen=True, slots=True)
class RepositoryInput:
    """What the caller can supply for one enrolled repository.

    ``root`` is a checkout the caller acquired at ``commit_sha`` (Cloud does
    this with an installation token; the CLI points at a local path).
    Without it, only registry evidence can be used: impact can be
    classified but no patch can be generated."""

    repository: EnrolledRepository
    root: Path | None = None
    inventory: DependencyInventory | None = None
    registry_rows: tuple[RegistryDependency, ...] = ()
    commit_sha: str | None = None
    extra_workspace_paths: tuple[Path, ...] = ()
    #: The record from an earlier run of this campaign, if any.
    previous: RepositoryRecord | None = None
    #: The engine ``repositories`` row id, when the caller has one. Only used to
    #: persist the M7/M8 audit trail (:mod:`patchfrog.campaigns.ingest`).
    engine_repository_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class RepositoryEvaluation:
    """The record plus the rich objects a publisher/dossier may need. Only
    ``record`` is persisted."""

    record: RepositoryRecord
    classification: RepositoryClassification
    impact: RepositoryImpact | None = None
    inventory: DependencyInventory | None = None
    plan: MigrationPlan | None = None
    patch: GeneratedPatch | None = None
    bundle: MigrationEvidenceBundle | None = None
    pr_plan: MigrationPRPlan | None = None
    human_actions: tuple[str, ...] = field(default_factory=tuple)
    #: The checkout the plan/patch were computed against (for the audit trail).
    root: Path | None = None
    engine_repository_id: uuid.UUID | None = None


def _impact(
    event: ExternalChangeEvent, entry: RepositoryInput, hints: ChangeHints
) -> tuple[RepositoryImpact | None, DependencyInventory | None]:
    name = entry.repository.full_name
    sha = entry.commit_sha or entry.repository.last_discovery_commit_sha
    if entry.root is not None and entry.inventory is not None:
        return analyze_inventory(entry.inventory, event, hints=hints, root=entry.root), entry.inventory
    if entry.root is not None:
        return analyze_repository(entry.root, event, hints=hints, repository=name, commit_sha=sha)
    if entry.inventory is not None:
        return analyze_inventory(entry.inventory, event, hints=hints), entry.inventory
    if entry.registry_rows:
        workspace = registry_impact(entry.registry_rows, event, hints=hints, repositories=[name])
        return (workspace.repositories[0] if workspace.repositories else None), None
    return None, None


def _severity(event: ExternalChangeEvent, impact: RepositoryImpact) -> str:
    if impact.direct_count > 0:
        return event.classification.compatibility.value
    if impact.potential_count > 0:
        return CompatibilityClass.POTENTIALLY_BREAKING.value
    return "none"


def _human_actions(plan: MigrationPlan) -> tuple[str, ...]:
    return tuple(
        f"{step.target.location}: {step.required_change}"[:300] for step in plan.unresolved_steps
    )[:10]


def _base_record(
    entry: RepositoryInput, *, freshness: Freshness, classification: RepositoryClassification, now: datetime
) -> RepositoryRecord:
    previous = entry.previous
    return RepositoryRecord(
        repository=entry.repository.full_name, state=RepoState.UNKNOWN, org_class=classification.org_class,
        freshness=freshness, unknown_reason=classification.unknown_reason,
        base_commit_sha=entry.commit_sha or entry.repository.last_discovery_commit_sha,
        ever_affected=bool(previous and previous.ever_affected), reasons=bound_reasons([classification.reason]),
        evaluated_at=now,
    )


async def evaluate_repository(
    event: ExternalChangeEvent,
    entry: RepositoryInput,
    *,
    now: datetime,
    policy: WorkspacePolicy | None = None,
    freshness_policy: FreshnessPolicy | None = None,
    hints: ChangeHints = EMPTY_HINTS,
    verify: VerifyFn = run_migration_verification,
) -> RepositoryEvaluation:
    policy = policy or WorkspacePolicy()
    freshness = assess_freshness(entry.repository, now=now, policy=freshness_policy)
    try:
        return await _evaluate(event, entry, freshness=freshness, now=now, policy=policy, hints=hints, verify=verify)
    except Exception as exc:
        classification = RepositoryClassification(
            entry.repository.full_name, OrgClass.UNKNOWN, None, "evaluation failed", False
        )
        base = _base_record(entry, freshness=freshness, classification=classification, now=now)
        record = _replace(
            base, state=RepoState.FAILED, error=safe_error(exc),
            reasons=bound_reasons(["evaluation raised an error; this repository is isolated and unresolved"]),
        )
        return RepositoryEvaluation(record=record, classification=classification)


def _replace(record: RepositoryRecord, **changes: Any) -> RepositoryRecord:
    return replace(record, **changes)


async def _evaluate(
    event: ExternalChangeEvent,
    entry: RepositoryInput,
    *,
    freshness: Freshness,
    now: datetime,
    policy: WorkspacePolicy,
    hints: ChangeHints,
    verify: VerifyFn,
) -> RepositoryEvaluation:
    name = entry.repository.full_name
    if freshness is not Freshness.FRESH:
        classification = classify_repository(name, freshness=freshness, impact=None)
        record = _base_record(entry, freshness=freshness, classification=classification, now=now)
        state = derive_repo_state(RepoFacts(freshness=freshness, org_class=classification.org_class,
                                            ever_affected=record.ever_affected))
        return RepositoryEvaluation(record=_replace(record, state=state), classification=classification)

    impact, inventory = _impact(event, entry, hints)
    classification = classify_repository(name, freshness=freshness, impact=impact)
    record = _base_record(entry, freshness=freshness, classification=classification, now=now)
    if impact is not None:
        transitive = sum(len(r.transitive) for r in impact.blast_radii)
        record = _replace(
            record, impact_status=impact.status.value, direct_consumers=impact.direct_count,
            potential_consumers=impact.potential_count, transitive_consumers=transitive,
            severity=_severity(event, impact),
            base_commit_sha=impact.commit_sha or record.base_commit_sha,
        )
    ever_affected = record.ever_affected or classification.org_class is OrgClass.AFFECTED
    record = _replace(record, ever_affected=ever_affected)

    if classification.org_class is not OrgClass.AFFECTED:
        state = derive_repo_state(RepoFacts(freshness=freshness, org_class=classification.org_class,
                                            ever_affected=record.ever_affected))
        return RepositoryEvaluation(
            record=_replace(record, state=state), classification=classification, impact=impact, inventory=inventory
        )

    assert impact is not None
    facts = RepoFacts(freshness=freshness, org_class=OrgClass.AFFECTED, ever_affected=True)
    if not policy.migrates or entry.root is None or inventory is None:
        why = "watch policy is detect_only: no migration is generated" if not policy.migrates else (
            "no repository checkout was available; a migration cannot be generated from registry evidence alone"
        )
        return RepositoryEvaluation(
            record=_replace(record, state=derive_repo_state(facts), reasons=bound_reasons([*record.reasons, why])),
            classification=classification, impact=impact, inventory=inventory,
        )

    plan = plan_migration(event, impact, inventory, hints=hints)
    patch = generate_patch(plan, entry.root) if plan.automatic_steps else None
    base_sha = record.base_commit_sha or ""
    bundle = await verify(
        plan=plan, patch=patch, blast_radii=impact.blast_radii, root=entry.root, commit_sha=base_sha,
        extra_workspace_paths=entry.extra_workspace_paths,
    )
    pr_plan = build_pr_plan(
        event=event, plan=plan, patch=patch, bundle=bundle, blast_radii=impact.blast_radii,
        repository=name, base_commit_sha=base_sha, policy=policy.pr_policy(),
    )
    facts = RepoFacts(
        freshness=freshness, org_class=OrgClass.AFFECTED, ever_affected=True, migration_status=plan.status.value,
        has_automatic_steps=bool(plan.automatic_steps), patch_generated=patch is not None and patch.is_candidate,
        verification_outcome=bundle.outcome.value,
    )
    sandbox = any(e.status in (CheckStatus.PASSED, CheckStatus.FAILED) for e in bundle.step_evidence)
    record = _replace(
        record, state=derive_repo_state(facts), migration_status=plan.status.value,
        migration_strategy_available=bool(plan.automatic_steps),
        verification_feasibility="sandbox" if sandbox else "static_only",
        verification_outcome=bundle.outcome.value, publication_readiness=pr_plan.eligibility.value,
        plan_fingerprint=plan.fingerprint(""), patch_fingerprint=patch.fingerprint if patch else None,
        bundle_fingerprint=bundle.bundle_fingerprint, residual_risk=bundle.residual_risk.value,
        reasons=bound_reasons([*record.reasons, *bundle.outcome_reasons]), human_actions=_human_actions(plan),
    )
    return RepositoryEvaluation(
        record=record, classification=classification, impact=impact, inventory=inventory, plan=plan, patch=patch,
        bundle=bundle, pr_plan=pr_plan, human_actions=_human_actions(plan), root=entry.root,
        engine_repository_id=entry.engine_repository_id,
    )


async def evaluate_all(
    event: ExternalChangeEvent,
    entries: Sequence[RepositoryInput],
    *,
    now: datetime,
    policy: WorkspacePolicy | None = None,
    freshness_policy: FreshnessPolicy | None = None,
    hints: ChangeHints = EMPTY_HINTS,
    verify: VerifyFn = run_migration_verification,
) -> tuple[RepositoryEvaluation, ...]:
    results = [
        await evaluate_repository(
            event, entry, now=now, policy=policy, freshness_policy=freshness_policy, hints=hints, verify=verify
        )
        for entry in sorted(entries, key=lambda e: e.repository.full_name)
    ]
    return tuple(results)


__all__ = [
    "RepositoryEvaluation",
    "RepositoryInput",
    "VerifyFn",
    "evaluate_all",
    "evaluate_repository",
    "safe_error",
]
