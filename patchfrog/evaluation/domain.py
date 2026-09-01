"""Pure domain model for PatchFrog's quality evaluation harness (Phase 8).

Mirrors every other engine's own ``domain.py`` role: the stable,
persistence-independent, network-independent boundary everything in
:mod:`patchfrog.evaluation` shares. Nothing here does I/O, calls an LLM,
or opens a database session -- that all lives in :mod:`patchfrog.evaluation.runner`.

Deliberately reuses :class:`patchfrog.analysis.domain.FindingCategory`/
``Severity`` rather than inventing a parallel taxonomy -- a benchmark
finding means the same category/severity as a real one.

Core principle (see the module docstring of
:mod:`patchfrog.evaluation.matcher`): every benchmark case has an
explicit, human-authored ground truth. No LLM is ever the judge for the
canonical score -- matching is deterministic and explainable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.effort_types import ReviewEffortTier

#: Bumped whenever the benchmark corpus (fixtures/ground truth) changes
#: materially -- a case added, removed, or re-labeled.
EVALUATION_BENCHMARK_VERSION = 1

#: Bumped whenever this package's own logic (matcher/metrics/regression)
#: changes materially -- never for a fixture/label change alone.
#:
#: Bumped to 2 for the Evaluation & Telemetry Intelligence milestone:
#: :class:`EvaluationIdentity` gained four new fields
#: (``context_engine_version``, ``quality_cost_policy_version``,
#: ``quality_cost_guard_enabled``, ``context_config_identity``) that
#: participate in comparison compatibility (see
#: :mod:`patchfrog.evaluation.regression`), and the evaluation cost/
#: efficiency reporting shape changed materially enough that a v1-shaped
#: baseline is no longer directly comparable.
EVALUATION_ENGINE_VERSION = 2


class EvaluationMode(StrEnum):
    """What subset of the production pipeline one evaluation run
    exercises. See the module docstring of :mod:`patchfrog.evaluation.runner`."""

    STATIC_ONLY = "static_only"
    AI_ONLY = "ai_only"
    FULL_PIPELINE = "full_pipeline"
    INCREMENTAL = "incremental"


class GroundTruthSource(StrEnum):
    """Which subsystem is expected to catch an :class:`ExpectedFinding`.
    Lets the harness measure static-only coverage, AI-only value, and
    total system coverage without punishing the AI for not repeating a
    deterministic static finding the final user-visible system still
    reports correctly (via ``FULL_PIPELINE``, which sees both)."""

    STATIC_EXPECTED = "static_expected"
    AI_EXPECTED = "ai_expected"
    EITHER = "either"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class Language(StrEnum):
    PYTHON = "python"
    C = "c"
    CPP = "cpp"


class PredictionSource(StrEnum):
    """Which subsystem actually produced one :class:`PredictedFinding`."""

    STATIC = "static"
    AI = "ai"


class MatchOutcome(StrEnum):
    """Classification of one predicted finding against the case's ground
    truth. Never a bare boolean -- every prediction gets an explicit,
    reported disposition."""

    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"
    DUPLICATE = "duplicate"
    #: References evidence/location not supported by the benchmark
    #: fixture -- the hallucination signal. See the module docstring of
    #: :mod:`patchfrog.evaluation.matcher`.
    UNSUPPORTED = "unsupported"
    OUT_OF_SCOPE = "out_of_scope"


class ExpectedOutcome(StrEnum):
    FOUND = "found"
    MISSED = "missed"


class CaseStatus(StrEnum):
    """Execution-level status of one case run -- orthogonal to match
    quality (a case can PASS execution and still score badly on
    precision/recall; those are computed separately by the matcher).
    Infrastructure failures are never counted as false negatives (see
    :mod:`patchfrog.evaluation.metrics`)."""

    #: Ran to completion, zero predicted findings.
    PASSED = "passed"
    #: Ran to completion, one or more predicted findings.
    COMPLETED_WITH_FINDINGS = "completed_with_findings"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    FIXTURE_ERROR = "fixture_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


_TERMINAL_ERROR_STATUSES = (
    CaseStatus.TIMEOUT,
    CaseStatus.PROVIDER_ERROR,
    CaseStatus.FIXTURE_ERROR,
    CaseStatus.INFRASTRUCTURE_ERROR,
)


class SeverityLevel(IntEnum):
    """Ordinal ranking for "within one level"/overstatement/understatement
    severity scoring -- never string-compared."""

    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


_SEVERITY_LEVEL: dict[Severity, SeverityLevel] = {
    Severity.INFO: SeverityLevel.INFO,
    Severity.LOW: SeverityLevel.LOW,
    Severity.MEDIUM: SeverityLevel.MEDIUM,
    Severity.HIGH: SeverityLevel.HIGH,
    Severity.CRITICAL: SeverityLevel.CRITICAL,
}


def severity_level(severity: Severity) -> SeverityLevel:
    return _SEVERITY_LEVEL[severity]


@dataclass(frozen=True, slots=True)
class ExpectedFinding:
    """One human-authored ground-truth expectation within an
    :class:`EvaluationCase`.

    Matching (see :mod:`patchfrog.evaluation.matcher`) never requires
    exact prose -- ``issue_family`` is a reporting/grouping tag, not a
    literal field production findings carry; the real deterministic
    match signal is (file, symbol, category, line-range overlap or
    tolerance, optional evidence substring).
    """

    id: str
    category: FindingCategory
    file: str
    issue_family: str
    symbol: str | None = None
    severity: Severity | None = None
    severity_min: Severity | None = None
    severity_max: Severity | None = None
    line: int | None = None
    line_end: int | None = None
    line_tolerance: int = 3
    evidence_contains: str | None = None
    ground_truth_source: GroundTruthSource = GroundTruthSource.EITHER
    notes: str = ""
    #: Security-review-quality ground truth (all optional, backward
    #: compatible with every pre-existing case) -- consumed only by
    #: :mod:`patchfrog.evaluation.security_quality`, never by the core
    #: TP/FP matcher (:mod:`patchfrog.evaluation.matcher` stays
    #: prose-independent by design; these are a second, separate scoring
    #: pass over the *content* of an already-matched true positive).
    expected_root_cause_concept: str | None = None
    expected_impact_concept: str | None = None
    acceptable_remediation_direction: str | None = None
    #: The actual finding's severity must never exceed this -- the
    #: severity-overstatement trap (Phase 8 spec section 25).
    max_justified_severity: Severity | None = None
    #: Substrings (case-insensitive) that must never appear in the
    #: accepted finding's message/reasoning/impact -- exaggerated claims
    #: the ground truth explicitly forbids (e.g. "remote code execution"
    #: for a case that is not actually RCE).
    forbidden_exaggerated_claims: tuple[str, ...] = field(default_factory=tuple)

    @property
    def effective_line_range(self) -> tuple[int, int] | None:
        if self.line is None:
            return None
        end = self.line_end if self.line_end is not None else self.line
        return (self.line - self.line_tolerance, end + self.line_tolerance)

    def severity_matches(self, actual: Severity) -> bool:
        if self.severity is not None:
            return actual is self.severity
        below_min = self.severity_min is not None and severity_level(actual) < severity_level(self.severity_min)
        above_max = self.severity_max is not None and severity_level(actual) > severity_level(self.severity_max)
        return not (below_min or above_max)


@dataclass(frozen=True, slots=True)
class ForbiddenFinding:
    """A category/issue-family a case's ground truth explicitly says
    must never be reported -- e.g. a style-only nitpick on a case that's
    otherwise about a real correctness bug. A prediction matching a
    forbidden rule (and no expected finding) is always FALSE_POSITIVE,
    reported with ``forbidden_reason`` set for explainability."""

    reason: str
    category: FindingCategory | None = None
    issue_family: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """One benchmark case -- a fixture repository/file plus its
    human-authored, committed ground truth. See
    :mod:`patchfrog.evaluation.fixtures` for how these are loaded and
    validated."""

    id: str
    title: str
    description: str
    language: Language
    fixture: str
    difficulty: Difficulty
    tags: tuple[str, ...] = field(default_factory=tuple)
    expected: tuple[ExpectedFinding, ...] = field(default_factory=tuple)
    forbidden: tuple[ForbiddenFinding, ...] = field(default_factory=tuple)
    notes: str = ""

    @property
    def is_clean(self) -> bool:
        """A "negative"/clean case: no real bug expected at all. See the
        Phase 8 spec's "at least 30-40% of initial cases should be
        intentionally clean" requirement -- tracked explicitly here
        rather than inferred, since a case can have zero *required*
        findings while still allowing an optional one (not used today,
        but the distinction matters for :mod:`patchfrog.evaluation.metrics`'s
        clean-case pass rate, which only ever counts truly expectation-free
        cases)."""

        return len(self.expected) == 0


@dataclass(frozen=True, slots=True)
class PredictedFinding:
    """A normalized projection of one real predicted finding -- either a
    Phase 3 static :class:`~patchfrog.persistence.models.analysis.FindingModel`
    row or a Phase 5 :class:`~patchfrog.persistence.models.review.AIFindingModel`
    row -- so :mod:`patchfrog.evaluation.matcher` never needs to know
    which subsystem produced it."""

    source: PredictionSource
    category: FindingCategory
    severity: Severity
    title: str
    message: str
    file_path: str
    start_line: int
    end_line: int
    symbol_qualified_name: str | None
    evidence_text: str
    #: Security-review-quality fields (Phase 8 spec follow-up:
    #: "Security Review Quality Refinement") -- consumed by
    #: :mod:`patchfrog.evaluation.security_quality`, never by the core
    #: matcher. All optional so every pre-existing construction site
    #: (which predates these fields) keeps working unchanged.
    confidence: Confidence | None = None
    reasoning_summary: str = ""
    impact: str | None = None
    suggested_fix: str | None = None
    #: The specialist role (see :mod:`patchfrog.review.agents.roles`)
    #: that produced this finding -- ``None`` for a static finding
    #: (``source is PredictionSource.STATIC``) or an AI finding
    #: predating Agent Orchestration v1. Never used by the core matcher
    #: (which must stay ground-truth/role agnostic) -- purely for
    #: provenance reporting (spec section 17).
    agent_role: AgentRole | None = None


@dataclass(frozen=True, slots=True)
class PredictionOutcome:
    prediction: PredictedFinding
    outcome: MatchOutcome
    matched_expected_id: str | None
    detail: str
    forbidden_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ExpectedFindingOutcome:
    expected: ExpectedFinding
    outcome: ExpectedOutcome
    matched_prediction_index: int | None
    detail: str


@dataclass(frozen=True, slots=True)
class AnalyzerExecutionSummary:
    """One analyzer's execution outcome for one case's static-analysis
    pass -- mirrors :class:`~patchfrog.persistence.models.analysis.AnalyzerExecutionModel`
    but decoupled from the ORM, so :mod:`patchfrog.evaluation.metrics` can
    aggregate per-analyzer coverage (attempted/succeeded/failed/skipped/
    unsupported, raw findings produced) across every case in a run
    without depending on persistence internals. See Phase 8 spec section
    45: a missing analyzer (UNSUPPORTED) must be reported as a missing
    capability, never silently treated as "zero findings"."""

    analyzer: str
    status: str
    raw_findings_count: int


@dataclass(frozen=True, slots=True)
class CaseResult:
    """The complete outcome of running one :class:`EvaluationCase` once,
    under one :class:`EvaluationMode` (and one critic on/off setting)."""

    case_id: str
    mode: EvaluationMode
    status: CaseStatus
    duration_ms: float
    predictions: tuple[PredictionOutcome, ...] = field(default_factory=tuple)
    expected_outcomes: tuple[ExpectedFindingOutcome, ...] = field(default_factory=tuple)
    #: Findings the reviewer *proposed* before Phase 5 validation/critic/
    #: confidence filtering -- used to compare pre- vs. post-validation
    #: hallucination rate (see :mod:`patchfrog.evaluation.metrics`).
    proposals_before_validation: tuple[PredictedFinding, ...] = field(default_factory=tuple)
    error: str | None = None
    critic_enabled: bool = True
    candidates_generated: int = 0
    candidates_reviewed: int = 0
    candidates_skipped: int = 0
    provider_calls: int = 0
    reviewer_input_tokens: int = 0
    reviewer_output_tokens: int = 0
    #: Per-specialist-role call counts and token usage (see
    #: :mod:`patchfrog.review.orchestration`) -- lets Agent Orchestration
    #: be measured later (spec section 17) without re-deriving it from
    #: raw persisted proposals. Empty for STATIC_ONLY/no-AI runs.
    calls_by_role: dict[AgentRole, int] = field(default_factory=dict)
    reviewer_input_tokens_by_role: dict[AgentRole, int] = field(default_factory=dict)
    reviewer_output_tokens_by_role: dict[AgentRole, int] = field(default_factory=dict)
    #: Populated whenever static analysis actually ran for this case
    #: (STATIC_ONLY/FULL_PIPELINE) -- see :mod:`patchfrog.evaluation.metrics`'s
    #: per-analyzer coverage computation.
    analyzer_executions: tuple[AnalyzerExecutionSummary, ...] = field(default_factory=tuple)
    #: Quality + Cost Guard (:mod:`patchfrog.review.effort`, Milestone F)
    #: aggregates -- lets the evaluation harness compare "current/uniform
    #: effort" against "quality-cost guard" runs (spec sections 23/24)
    #: without duplicating any production tiering logic. Empty/zero for
    #: STATIC_ONLY/no-AI runs, exactly like the role-provenance fields
    #: above.
    candidates_by_tier: dict[ReviewEffortTier, int] = field(default_factory=dict)
    candidates_escalated: int = 0
    critic_calls: int = 0
    reviewer_thinking_tokens: int = 0
    critic_thinking_tokens: int = 0
    retries_consumed: int = 0

    @property
    def is_error(self) -> bool:
        return self.status in _TERMINAL_ERROR_STATUSES


@dataclass(frozen=True, slots=True)
class IncrementalScenarioResult:
    """One multi-commit incremental-review benchmark scenario's outcome
    -- see the module docstring of :mod:`patchfrog.evaluation.runner`'s
    incremental benchmark section."""

    scenario_id: str
    description: str
    passed: bool
    provider_calls_full: int
    provider_calls_incremental: int
    provider_calls_avoided: int
    #: A memory finding carried forward (or resolved) that does not
    #: match this scenario's known-correct expectation for that commit
    #: -- e.g. carried forward when the bug was actually fixed, or
    #: resolved when the bug is still present. Per the Phase 8 spec: "a
    #: wrong carry-forward is worse than an extra LLM call" -- target is
    #: always zero.
    unsafe_carry_forward: bool
    detail: str


@dataclass(frozen=True, slots=True)
class FixtureInfo:
    """The materialized fixture's real, on-disk shape -- what
    :mod:`patchfrog.evaluation.matcher`'s hallucination check compares
    predictions against. Built once per case by
    :mod:`patchfrog.evaluation.runner`, never re-derived from a
    prediction's own claims."""

    valid_file_paths: frozenset[str]
    file_line_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class EvaluationIdentity:
    """Everything that must match for two evaluation runs to be
    comparable -- see :mod:`patchfrog.evaluation.regression`'s refusal
    to silently compare incompatible identities."""

    evaluation_benchmark_version: int
    evaluation_engine_version: int
    review_engine_version: int
    review_prompt_version: int
    review_policy_version: int
    incremental_review_engine_version: int
    review_memory_version: int
    reviewer_provider: str
    reviewer_model: str
    critic_enabled: bool
    static_toolchain_available: bool
    mode: EvaluationMode
    case_fixture_hashes: dict[str, str] = field(default_factory=dict)
    #: The four fields below (Evaluation & Telemetry Intelligence
    #: milestone, spec section 18) close a real comparison-compatibility
    #: gap: none of the fields above distinguish a Context Engine version
    #: bump, a Quality + Cost Guard policy change, a guard-on run from a
    #: fixed "uniform baseline" ablation run, or one context ablation
    #: variant (fixed depth-1, fixed depth-2, adaptive, kind-restricted)
    #: from another. Without these, two runs that differ in exactly the
    #: dimension being ablated could be silently treated as identical
    #: baselines -- see :mod:`patchfrog.evaluation.regression`.
    context_engine_version: int = 0
    quality_cost_policy_version: int = 0
    #: ``True`` for every real review and for the default evaluation
    #: path; ``False`` only for the evaluation harness's fixed "uniform
    #: baseline" ablation (:func:`patchfrog.review.effort.uniform_baseline_decision`).
    quality_cost_guard_enabled: bool = True
    #: :meth:`patchfrog.context.config.ContextConfig.fingerprint` of
    #: whatever ``context_config_override`` a run used, or the literal
    #: string ``"default"`` when no override was supplied (production-
    #: equivalent context config) -- so a fixed-depth-1 run, a fixed-
    #: depth-2 run, an adaptive run, and a kind-restricted ablation
    #: variant each get a distinct identity.
    context_config_identity: str = "default"


@dataclass(frozen=True, slots=True)
class EvaluationRunResult:
    """The complete, machine-readable output of one evaluation run --
    what :mod:`patchfrog.evaluation.reporting` serializes to JSON/Markdown
    and what :mod:`patchfrog.evaluation.regression` compares against a
    prior baseline. Deliberately file-artifact-first (no database
    persistence) -- see the module docstring of
    :mod:`patchfrog.evaluation.reporting`."""

    identity: EvaluationIdentity
    generated_at: str
    duration_ms: float
    case_results: tuple[CaseResult, ...]
    incremental_scenarios: tuple[IncrementalScenarioResult, ...] = field(default_factory=tuple)
