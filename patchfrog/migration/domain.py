"""Migration plan / patch domain model (M7).

A :class:`MigrationPlan` says, for one repository and one upstream change,
what has to change at every affected consumer: the current usage, the
required compatibility change, the proposed target behavior, the files
and symbols to edit, the tests likely to need updates, version
constraints, residual uncertainty -- and whether an automatic fix is
safe. Values are never invented: a step with no deterministic source for
a new value is ``HUMAN_REQUIRED``.

A :class:`MigrationResult` adds the generated patch, its linkage to the
originating change (for M8 verification and M9 PR idempotency) and the
deterministic safety-gate results. None of this is "verified" in the
runtime sense -- that is M8.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: Bumped when planning/eligibility/patch-generation semantics change such
#: that a previously-recorded plan or patch fingerprint is no longer what
#: re-running would produce.
MIGRATION_ENGINE_VERSION = 1


class MigrationStrategy(StrEnum):
    RENAME_SYMBOL = "rename_symbol"
    RENAME_PARAMETER = "rename_parameter"
    ADD_REQUIRED_PARAMETER = "add_required_parameter"
    REPLACE_ENUM_VALUE = "replace_enum_value"
    MOVE_IMPORT = "move_import"
    REPLACE_ENDPOINT = "replace_endpoint"
    ADAPT_RETURN_FIELD = "adapt_return_field"
    BUMP_PACKAGE_VERSION = "bump_package_version"
    REFRESH_LOCKFILE = "refresh_lockfile"
    VERIFY_UPGRADE = "verify_upgrade"
    ADAPT_RESPONSE = "adapt_response"
    UPDATE_AUTH = "update_auth"
    REMOVE_ARGUMENT = "remove_argument"
    REPLACE_REMOVED_API = "replace_removed_api"
    MANUAL_REVIEW = "manual_review"


class AutoFixEligibility(StrEnum):
    #: Mechanical, one-to-one, fully determined by the contract/hints.
    AUTO_SAFE = "auto_safe"
    #: Deterministic, but a reviewer should confirm the semantics.
    AUTO_WITH_REVIEW = "auto_with_review"
    #: Needs a decision/value/business logic PatchFrog cannot derive.
    HUMAN_REQUIRED = "human_required"
    #: A mechanical change this engine does not implement.
    UNSUPPORTED = "unsupported"


AUTOMATIC = frozenset({AutoFixEligibility.AUTO_SAFE, AutoFixEligibility.AUTO_WITH_REVIEW})


class MigrationStatus(StrEnum):
    #: The repository is not affected -- nothing to do.
    NOT_REQUIRED = "not_required"
    PLANNED = "planned"
    PATCH_GENERATED = "patch_generated"
    PARTIAL = "partial"
    HUMAN_REQUIRED = "human_required"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class StepOutcome(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    #: The code already has the target shape (idempotent re-run) or the
    #: call does not use the changed argument.
    NOT_NEEDED = "not_needed"
    #: Not automatic (human-required / unsupported).
    SKIPPED = "skipped"
    FAILED = "failed"


class ResidualRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PatchOrigin(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL_ASSISTED = "model_assisted"


@dataclass(frozen=True, slots=True)
class MigrationTarget:
    repository: str
    file_path: str
    symbol: str | None
    line: int | None
    #: The usage-site token (call chain, import, path literal, package).
    usage_token: str
    evidence_type: str
    #: ``python`` | ``javascript`` | ``manifest`` | ``none``.
    language: str

    @property
    def location(self) -> str:
        return f"{self.file_path}::{self.symbol}" if self.symbol else self.file_path


@dataclass(frozen=True, slots=True)
class EditOperation:
    """A typed, serializable description of a deterministic edit. ``kind``
    selects the rewriter; ``params`` is sorted (stable fingerprints)."""

    kind: str
    params: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, kind: str, **params: str) -> EditOperation:
        return cls(kind, tuple(sorted(params.items())))

    def get(self, name: str) -> str:
        return dict(self.params)[name]

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "params": dict(self.params)}


@dataclass(frozen=True, slots=True)
class VersionConstraint:
    manifest: str
    package: str
    ecosystem: str
    current: str | None
    target: str


@dataclass(frozen=True, slots=True)
class MigrationStep:
    step_id: str
    target: MigrationTarget
    strategy: MigrationStrategy
    eligibility: AutoFixEligibility
    diff_item_keys: tuple[str, ...]
    diff_item_kinds: tuple[str, ...]
    current_usage: str
    required_change: str
    proposed_change: str
    tests_to_update: tuple[str, ...]
    residual_uncertainty: str
    operation: EditOperation | None = None
    version_constraint: VersionConstraint | None = None
    usage_site_key: str | None = None

    @property
    def auto_fix(self) -> bool:
        return self.eligibility in AUTOMATIC and self.operation is not None


def step_identity(target: MigrationTarget, strategy: MigrationStrategy, operation: EditOperation | None,
                  item_keys: tuple[str, ...]) -> str:
    payload = [target.file_path, target.line or 0, target.usage_token, strategy.value,
               operation.as_dict() if operation else None, list(item_keys)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    change_fingerprint: str
    repository: str
    commit_sha: str | None
    dependency_keys: tuple[str, ...]
    steps: tuple[MigrationStep, ...]
    residual_risk: ResidualRisk
    status: MigrationStatus
    #: Evidence the plan rests on: diff item keys and consumer site keys.
    evidence: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    engine_version: int = MIGRATION_ENGINE_VERSION

    @property
    def automatic_steps(self) -> tuple[MigrationStep, ...]:
        return tuple(s for s in self.steps if s.auto_fix)

    @property
    def unresolved_steps(self) -> tuple[MigrationStep, ...]:
        return tuple(s for s in self.steps if not s.auto_fix)

    def fingerprint(self, base_content_fingerprint: str) -> str:
        payload = {
            "engine": self.engine_version,
            "change": self.change_fingerprint,
            "repository": self.repository,
            "commit": self.commit_sha or "",
            "base": base_content_fingerprint,
            "steps": sorted(s.step_id for s in self.steps),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class StepResult:
    step_id: str
    outcome: StepOutcome
    detail: str
    edits: int = 0


@dataclass(frozen=True, slots=True)
class SafetyGateResult:
    gate: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PatchLinkage:
    """Everything M8 verification and M9 PR idempotency key on."""

    change_fingerprint: str
    dependency_keys: tuple[str, ...]
    diff_item_keys: tuple[str, ...]
    usage_site_keys: tuple[str, ...]
    repository: str
    base_commit_sha: str | None
    #: sha256 over the original content of every file the patch touches.
    base_content_fingerprint: str
    plan_fingerprint: str
    patch_fingerprint: str
    engine_version: int = MIGRATION_ENGINE_VERSION


@dataclass(frozen=True, slots=True)
class GeneratedPatch:
    origin: PatchOrigin
    unified_diff: str
    modified_files: tuple[str, ...]
    #: file -> new content (in memory only; never written by the engine).
    new_contents: Mapping[str, str]
    step_results: tuple[StepResult, ...]
    safety: tuple[SafetyGateResult, ...]

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.unified_diff.encode()).hexdigest()

    @property
    def is_candidate(self) -> bool:
        """All safety gates passed and something changed. Still *not*
        runtime-verified (M8)."""

        return bool(self.unified_diff) and all(g.passed for g in self.safety)


@dataclass(frozen=True, slots=True)
class MigrationResult:
    plan: MigrationPlan
    status: MigrationStatus
    patch: GeneratedPatch | None
    linkage: PatchLinkage | None
    residual_risk: ResidualRisk
    unresolved: tuple[MigrationStep, ...]
    #: A separate, clearly-labelled model-assisted proposal (M7.4), never
    #: merged into the deterministic patch.
    assisted_patch: GeneratedPatch | None = None


__all__ = [
    "AUTOMATIC",
    "MIGRATION_ENGINE_VERSION",
    "AutoFixEligibility",
    "EditOperation",
    "GeneratedPatch",
    "MigrationPlan",
    "MigrationResult",
    "MigrationStatus",
    "MigrationStep",
    "MigrationStrategy",
    "MigrationTarget",
    "PatchLinkage",
    "PatchOrigin",
    "ResidualRisk",
    "SafetyGateResult",
    "StepOutcome",
    "StepResult",
    "VersionConstraint",
    "step_identity",
]
