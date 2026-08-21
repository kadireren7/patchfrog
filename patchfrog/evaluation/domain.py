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

from patchfrog.analysis.domain import FindingCategory, Severity

#: Bumped whenever the benchmark corpus (fixtures/ground truth) changes
#: materially -- a case added, removed, or re-labeled.
EVALUATION_BENCHMARK_VERSION = 1

#: Bumped whenever this package's own logic (matcher/metrics/regression)
#: changes materially -- never for a fixture/label change alone.
EVALUATION_ENGINE_VERSION = 1


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
