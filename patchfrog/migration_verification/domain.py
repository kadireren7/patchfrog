"""Pure domain model for Migration Verification -- M8.

Answers, for one generated migration patch: "what evidence is needed to
show that this migration actually restores compatibility, and what did
PatchFrog actually gather?" No I/O, no LLM -- mirrors every other
Intelligence package's own ``domain.py`` role.

**Governing rule** (mirrors the evidence-combination discipline
:class:`patchfrog.fix_verification.domain.FixEvidenceDirection` reached
only after two real correction rounds -- see
``validation/agent_handoff/latest-summary.md``): a single strong
contradicting signal (a regression, a failed mandatory contract check)
always wins outright. Only strong, direct evidence may reach
``VERIFIED``. Any combination of only weak/generic evidence never
promotes above ``PARTIALLY_VERIFIED``. Missing required evidence is
``UNVERIFIED``, never guessed in either direction. See
:mod:`patchfrog.migration_verification.decision` for the actual rule.

A verification result must explicitly distinguish evidence collected,
checks passed, checks failed, checks not run, checks unavailable, and
residual uncertainty -- see :class:`CheckStatus` and
:class:`VerificationCoverage`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum

from patchfrog.migration.domain import ResidualRisk

#: Bumped whenever requirement-generation, test-selection, execution-plan,
#: baseline-comparison, contract-verification, regression-detection or
#: decision-engine logic changes materially enough that a prior evidence
#: bundle can no longer be considered equivalent to what re-running now
#: would produce. Independent of MIGRATION_ENGINE_VERSION (the plan/patch
#: this verifies) and every review-side version constant.
MIGRATION_VERIFICATION_VERSION = 1

#: Bounded plan -- never "run everything" by default (M8.2).
MAX_VERIFICATION_STEPS_PER_PLAN = 12
MAX_TARGETED_TESTS_PER_PLAN = 8

#: A single step's own wall-clock ceiling and the whole plan's total
#: ceiling -- mirrors patchfrog.executable_verification.domain's own
#: per-attempt / per-run budget pattern.
MAX_STEP_SECONDS = 30.0
MAX_TOTAL_VERIFICATION_SECONDS = 180.0

#: Bounds the evidence excerpt kept from stdout/stderr per step -- a
#: short excerpt for the decision engine/evidence bundle, never a log dump.
MAX_STEP_STDOUT_EXCERPT_BYTES = 4096
MAX_STEP_STDERR_EXCERPT_BYTES = 4096


class VerificationRequirementKind(StrEnum):
    SYNTAX_VALID = "syntax_valid"
    IMPORTS_RESOLVE = "imports_resolve"
    SDK_CALL_MATCHES_CONTRACT = "sdk_call_matches_contract"
    OBSOLETE_SYMBOL_REMOVED = "obsolete_symbol_removed"
    REQUIRED_ARGUMENT_PRESENT = "required_argument_present"
    VERSION_CONSTRAINT_COMPATIBLE = "version_constraint_compatible"
    TYPE_CHECK_PASSES = "type_check_passes"
    UNIT_TESTS_PASS = "unit_tests_pass"
    INTEGRATION_TESTS_PASS = "integration_tests_pass"
    CONTRACT_COMPATIBILITY_RESTORED = "contract_compatibility_restored"
    CONSUMER_BEHAVIOR_PRESERVED = "consumer_behavior_preserved"


class VerificationStepKind(StrEnum):
    SYNTAX_CHECK = "syntax_check"
    IMPORT_CHECK = "import_check"
    TYPE_CHECK = "type_check"
    STATIC_ANALYSIS = "static_analysis"
    TARGETED_UNIT_TEST = "targeted_unit_test"
    TARGETED_INTEGRATION_TEST = "targeted_integration_test"
    CONTRACT_ASSERTION = "contract_assertion"
    PACKAGE_RESOLUTION_CHECK = "package_resolution_check"
    GENERATED_CLIENT_CHECK = "generated_client_check"
    CUSTOM_COMMAND = "custom_command"


class TestSelectionClass(StrEnum):
    """Never claimed to be a perfect mapping -- see
    :mod:`patchfrog.migration_verification.test_selection`'s module
    docstring for exactly what evidence backs each value."""

    DIRECT = "direct"
    TRANSITIVE = "transitive"
    FALLBACK = "fallback"
    UNKNOWN = "unknown"


class CheckStatus(StrEnum):
    """A requirement/step's own bounded outcome -- distinguishes evidence
    that was actually gathered from evidence that simply never existed to
    gather (M8.1's explicit requirement)."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    UNAVAILABLE = "unavailable"


class BaselineComparisonOutcome(StrEnum):
    BASELINE_FAIL_PATCHED_PASS = "baseline_fail_patched_pass"
    BASELINE_PASS_PATCHED_PASS = "baseline_pass_patched_pass"
    BASELINE_FAIL_PATCHED_FAIL_DIFFERENTLY = "baseline_fail_patched_fail_differently"
    BASELINE_PASS_PATCHED_FAIL = "baseline_pass_patched_fail"
    BASELINE_UNAVAILABLE = "baseline_unavailable"


class EvidenceStrength(StrEnum):
    """Never let many weak checks masquerade as one strong proof (M8.10)
    -- the decision engine only ever promotes to ``STRONG`` from a real
    direct signal, not an accumulation of ``WEAK`` ones."""

    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    NONE = "none"


class VerificationOutcome(StrEnum):
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    REGRESSION_DETECTED = "regression_detected"
    HUMAN_REQUIRED = "human_required"


@dataclass(frozen=True, slots=True)
class VerificationRequirement:
    requirement_id: str
    kind: VerificationRequirementKind
    description: str
    #: A failed/unresolved mandatory requirement blocks VERIFIED; a
    #: non-mandatory one only ever affects evidence strength/residual risk.
    mandatory: bool
    diff_item_keys: tuple[str, ...] = ()
    usage_site_key: str | None = None
    #: The exact :class:`patchfrog.migration.domain.MigrationStep` this
    #: requirement traces back to, when it corresponds to one step
    #: (``None`` for universal/aggregate requirements) -- lets
    #: :mod:`patchfrog.migration_verification.contract` reuse the
    #: generator's own :class:`~patchfrog.migration.domain.StepResult`
    #: directly instead of re-deriving the same evidence with string
    #: matching.
    source_step_id: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationStep:
    step_id: str
    kind: VerificationStepKind
    requirement_ids: tuple[str, ...]
    #: An explicit argv -- never a shell string, never caller-assembled
    #: interpolation. Executed only through
    #: :class:`patchfrog.executable_verification.sandbox.VerificationSandbox`.
    command: tuple[str, ...]
    timeout_seconds: float
    reason: str
    blocks_verified: bool
    #: Set only for ``TARGETED_UNIT_TEST``/``TARGETED_INTEGRATION_TEST``
    #: steps -- carries the selection's own DIRECT/TRANSITIVE/FALLBACK
    #: classification through to evidence-strength scoring (M8.10)
    #: without re-parsing ``reason``.
    test_classification: TestSelectionClass | None = None


@dataclass(frozen=True, slots=True)
class TestSelection:
    test_path: str
    classification: TestSelectionClass
    reason: str


@dataclass(frozen=True, slots=True)
class MigrationVerificationPlan:
    change_fingerprint: str
    patch_fingerprint: str
    requirements: tuple[VerificationRequirement, ...]
    steps: tuple[VerificationStep, ...]
    test_selections: tuple[TestSelection, ...]
    engine_version: int = MIGRATION_VERIFICATION_VERSION

    @property
    def mandatory_requirement_ids(self) -> frozenset[str]:
        return frozenset(r.requirement_id for r in self.requirements if r.mandatory)

    def fingerprint(self) -> str:
        payload = {
            "engine": self.engine_version,
            "change": self.change_fingerprint,
            "patch": self.patch_fingerprint,
            "requirements": sorted(r.requirement_id for r in self.requirements),
            "steps": sorted(s.step_id for s in self.steps),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class StepEvidence:
    step_id: str
    kind: VerificationStepKind
    requirement_ids: tuple[str, ...]
    status: CheckStatus
    exit_code: int | None
    duration_ms: float
    stdout_excerpt: str
    stderr_excerpt: str
    detail: str


@dataclass(frozen=True, slots=True)
class ContractCheckResult:
    requirement_id: str
    description: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class BaselineComparison:
    requirement_id: str
    baseline_status: CheckStatus
    patched_status: CheckStatus
    outcome: BaselineComparisonOutcome
    detail: str


@dataclass(frozen=True, slots=True)
class VerificationCoverage:
    mandatory_requirement_ids: tuple[str, ...]
    satisfied_requirement_ids: tuple[str, ...]
    failed_requirement_ids: tuple[str, ...]
    not_run_requirement_ids: tuple[str, ...]
    unavailable_requirement_ids: tuple[str, ...]

    @property
    def unresolved_mandatory(self) -> tuple[str, ...]:
        satisfied = set(self.satisfied_requirement_ids)
        return tuple(r for r in self.mandatory_requirement_ids if r not in satisfied)

    @property
    def failed_mandatory(self) -> tuple[str, ...]:
        failed = set(self.failed_requirement_ids)
        return tuple(r for r in self.mandatory_requirement_ids if r in failed)


@dataclass(frozen=True, slots=True)
class MigrationEvidenceBundle:
    """First-class, reproducible, machine-readable evidence bundle
    (M8.9). Persists bounded summaries, hashes, exit status and timing --
    never raw logs beyond a short bounded excerpt, never secrets."""

    version: int
    change_fingerprint: str
    repository: str
    repository_head_sha: str
    patch_fingerprint: str
    plan_fingerprint: str
    plan: MigrationVerificationPlan
    step_evidence: tuple[StepEvidence, ...]
    contract_checks: tuple[ContractCheckResult, ...]
    baseline_comparisons: tuple[BaselineComparison, ...]
    coverage: VerificationCoverage
    evidence_strength: EvidenceStrength
    residual_risk: ResidualRisk
    outcome: VerificationOutcome
    outcome_reasons: tuple[str, ...]
    generated_at_iso: str
    unresolved_requirement_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def bundle_fingerprint(self) -> str:
        """Ties this exact evidence to an exact patch/change/repo state --
        never reused for a different patch (M9.8's integrity check)."""

        payload = {
            "version": self.version,
            "change": self.change_fingerprint,
            "repository": self.repository,
            "head_sha": self.repository_head_sha,
            "patch": self.patch_fingerprint,
            "plan": self.plan_fingerprint,
            "outcome": self.outcome.value,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


__all__ = [
    "MAX_STEP_SECONDS",
    "MAX_STEP_STDERR_EXCERPT_BYTES",
    "MAX_STEP_STDOUT_EXCERPT_BYTES",
    "MAX_TARGETED_TESTS_PER_PLAN",
    "MAX_TOTAL_VERIFICATION_SECONDS",
    "MAX_VERIFICATION_STEPS_PER_PLAN",
    "MIGRATION_VERIFICATION_VERSION",
    "BaselineComparison",
    "BaselineComparisonOutcome",
    "CheckStatus",
    "ContractCheckResult",
    "EvidenceStrength",
    "MigrationEvidenceBundle",
    "MigrationVerificationPlan",
    "ResidualRisk",
    "StepEvidence",
    "TestSelection",
    "TestSelectionClass",
    "VerificationCoverage",
    "VerificationOutcome",
    "VerificationRequirement",
    "VerificationRequirementKind",
    "VerificationStep",
    "VerificationStepKind",
]
