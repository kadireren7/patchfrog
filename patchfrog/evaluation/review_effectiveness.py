"""Review Effectiveness Benchmark foundation (Milestone S, Part F).

Measures reviewer *quality*, separate from implementation-test
correctness. ``patchfrog.evaluation``'s own existing benchmark corpus
(:mod:`patchfrog.evaluation.fixtures`) proves the engine is
deterministic and correct; this proves a different, independent claim:
whether PatchFrog's findings are actually useful. See
``validation/review_effectiveness/latest-summary.md`` for the full
audit and design rationale.

**FakeLLM limitation** (spec Part F4): every case in this corpus is
meant to be run against FakeLLM/oracle, exactly like
``patchfrog.evaluation``'s own existing corpus. **FakeLLM proves
deterministic orchestration and evaluation correctness. It does not
prove real-model review quality.** No claim of real production
precision/recall is ever made from this corpus alone.

**No live provider calls** (spec Part F5): this benchmark never calls
Anthropic or OpenAI, and never calls Gemini absent an explicit,
zero-cost, automated reason to.

This is a **deliberately narrow foundation**, not a complete corpus:
"do not require every category to be fully populated in S" (spec F1).
New cases are added incrementally over time by adding a new JSON file
under ``validation/review_effectiveness/corpus/`` -- the schema is
built to be extensible, not exhaustive today.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from patchfrog.analysis.domain import FindingCategory, Severity

DEFAULT_CORPUS_ROOT = Path(__file__).resolve().parent.parent.parent / "validation" / "review_effectiveness" / "corpus"


class ScenarioCategory(StrEnum):
    """What kind of review *scenario* a case represents -- distinct
    from :class:`~patchfrog.analysis.domain.FindingCategory`, which
    describes what kind of *bug* a finding is. Spec Part F1's own
    category list."""

    CLEAN_PR = "clean_pr"
    LOCAL_CORRECTNESS_BUG = "local_correctness_bug"
    CROSS_FILE_CORRECTNESS_BUG = "cross_file_correctness_bug"
    CONTRACT_BREAK = "contract_break"
    STALE_CONSUMER = "stale_consumer"
    INTENT_MISS = "intent_miss"
    MISSING_TEST_EVIDENCE = "missing_test_evidence"
    TEST_WEAKENING = "test_weakening"
    SECURITY_ISSUE = "security_issue"
    HISTORICAL_REGRESSION = "historical_regression"
    NOISY_NEGATIVE = "noisy_negative"
    DUPLICATE_FINDING = "duplicate_finding"
    EXECUTABLE_VERIFICATION_CASE = "executable_verification_case"


@dataclass(frozen=True, slots=True)
class ReviewEffectivenessGroundTruth:
    """Explicit expected truth for one case -- never LLM-generated (spec
    Part F2). ``must_find``/``must_not_find`` describe symbol-level
    obligations for a real, non-clean case; ``expected_clean`` is the
    positive-control counterpart for a case with no real bug at all."""

    expected_clean: bool = False
    must_find_file_path: str | None = None
    must_find_qualified_name: str | None = None
    expected_finding_category: FindingCategory | None = None
    acceptable_severity_min: Severity | None = None
    acceptable_severity_max: Severity | None = None
    must_not_find_file_path: str | None = None
    must_not_find_qualified_name: str | None = None
    #: Only meaningful for ScenarioCategory.EXECUTABLE_VERIFICATION_CASE
    #: -- the expected patchfrog.executable_verification.domain.VerificationOutcome
    #: value, as a plain string (kept decoupled from that module's own
    #: enum so this schema never needs Milestone S's own package as a
    #: hard import-time dependency).
    expected_execution_outcome: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewEffectivenessCase:
    case_id: str
    category: ScenarioCategory
    description: str
    ground_truth: ReviewEffectivenessGroundTruth


def _parse_ground_truth(raw: dict[str, object]) -> ReviewEffectivenessGroundTruth:
    def _severity(value: object) -> Severity | None:
        return Severity(str(value)) if value is not None else None

    def _category(value: object) -> FindingCategory | None:
        return FindingCategory(str(value)) if value is not None else None

    return ReviewEffectivenessGroundTruth(
        expected_clean=bool(raw.get("expected_clean", False)),
        must_find_file_path=raw.get("must_find_file_path"),  # type: ignore[arg-type]
        must_find_qualified_name=raw.get("must_find_qualified_name"),  # type: ignore[arg-type]
        expected_finding_category=_category(raw.get("expected_finding_category")),
        acceptable_severity_min=_severity(raw.get("acceptable_severity_min")),
        acceptable_severity_max=_severity(raw.get("acceptable_severity_max")),
        must_not_find_file_path=raw.get("must_not_find_file_path"),  # type: ignore[arg-type]
        must_not_find_qualified_name=raw.get("must_not_find_qualified_name"),  # type: ignore[arg-type]
        expected_execution_outcome=raw.get("expected_execution_outcome"),  # type: ignore[arg-type]
    )


def load_case(path: Path) -> ReviewEffectivenessCase:
    raw = json.loads(path.read_text())
    return ReviewEffectivenessCase(
        case_id=raw["case_id"], category=ScenarioCategory(raw["category"]), description=raw["description"],
        ground_truth=_parse_ground_truth(raw.get("ground_truth", {})),
    )


def load_all_cases(corpus_root: Path = DEFAULT_CORPUS_ROOT) -> tuple[ReviewEffectivenessCase, ...]:
    if not corpus_root.is_dir():
        return ()
    cases = [load_case(p) for p in sorted(corpus_root.glob("*.json"))]
    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise ValueError(f"duplicate review-effectiveness case_id: {case.case_id!r}")
        seen.add(case.case_id)
    return tuple(cases)


@dataclass(frozen=True, slots=True)
class ActualCaseOutcome:
    """What actually happened for one case during one benchmark run --
    supplied by the caller (never computed inside this module, which
    stays a pure schema/metrics layer with no provider/orchestration
    dependency of its own)."""

    case_id: str
    findings_reported: int
    matched_must_find: bool
    matched_must_not_find_violation: bool
    duplicate_findings: int
    execution_outcome: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewEffectivenessMetrics:
    """Bounded, explainable metrics -- never an invented/unsupported
    one (spec Part F3)."""

    case_count: int
    clean_case_count: int
    non_clean_case_count: int
    #: Of the non-clean cases, how many had their must-find obligation matched.
    case_recall: float | None
    #: Of the clean cases, how many produced zero findings (no false positive).
    clean_case_precision: float | None
    total_false_positives_on_clean_cases: int
    total_duplicate_findings: int
    total_must_not_find_violations: int


def compute_metrics(
    cases: tuple[ReviewEffectivenessCase, ...], outcomes: dict[str, ActualCaseOutcome]
) -> ReviewEffectivenessMetrics:
    """``outcomes`` maps ``case_id`` to what actually happened -- a case
    with no matching outcome is simply excluded from the count it would
    have contributed to (never a guessed default)."""

    clean_cases = [c for c in cases if c.ground_truth.expected_clean]
    non_clean_cases = [c for c in cases if not c.ground_truth.expected_clean]

    clean_with_outcome = [c for c in clean_cases if c.case_id in outcomes]
    non_clean_with_outcome = [c for c in non_clean_cases if c.case_id in outcomes]

    false_positives_on_clean = sum(
        outcomes[c.case_id].findings_reported for c in clean_with_outcome
    )
    recalled = sum(1 for c in non_clean_with_outcome if outcomes[c.case_id].matched_must_find)
    zero_finding_clean = sum(1 for c in clean_with_outcome if outcomes[c.case_id].findings_reported == 0)
    total_duplicates = sum(outcomes[c.case_id].duplicate_findings for c in cases if c.case_id in outcomes)
    total_violations = sum(
        1 for c in cases if c.case_id in outcomes and outcomes[c.case_id].matched_must_not_find_violation
    )

    return ReviewEffectivenessMetrics(
        case_count=len(cases),
        clean_case_count=len(clean_cases),
        non_clean_case_count=len(non_clean_cases),
        case_recall=(recalled / len(non_clean_with_outcome)) if non_clean_with_outcome else None,
        clean_case_precision=(zero_finding_clean / len(clean_with_outcome)) if clean_with_outcome else None,
        total_false_positives_on_clean_cases=false_positives_on_clean,
        total_duplicate_findings=total_duplicates,
        total_must_not_find_violations=total_violations,
    )
