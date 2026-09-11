"""Pure tests for Y4's advisory-only personalization effects -- proves,
structurally and behaviorally, that nothing here can force readiness,
change severity, or select a provider (spec tests Y16/Y17/Y18)."""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime

from patchfrog.analysis.domain import FindingCategory
from patchfrog.learning_records.domain import (
    LearningEvidenceRef,
    LearningMaturity,
    LearningSurface,
    LearningType,
    RepositoryLearningRecord,
)
from patchfrog.learning_records.personalization import (
    RepositoryLearningReviewHint,
    noise_advisory_text_for_candidate,
    select_review_hint,
)

_REPO_ID = uuid.uuid4()


def _record(
    *,
    learning_type: LearningType,
    maturity: LearningMaturity,
    file_path: str = "a.py",
    qualified_name: str = "a.f",
    category: FindingCategory = FindingCategory.CORRECTNESS,
) -> RepositoryLearningRecord:
    now = datetime.now(UTC).isoformat()
    return RepositoryLearningRecord(
        id=uuid.uuid4(),
        repository_id=_REPO_ID,
        learning_type=learning_type,
        surface=LearningSurface(file_path=file_path, qualified_name=qualified_name, category=category),
        maturity=maturity,
        support_count=3,
        evidence=(LearningEvidenceRef(finding_id=uuid.uuid4(), review_run_id=uuid.uuid4(), observed_at=now),),
        first_observed_at=now,
        last_observed_at=now,
    )


def test_established_useful_pattern_increases_priority() -> None:
    records = (_record(learning_type=LearningType.USEFUL_FINDING_PATTERN, maturity=LearningMaturity.ESTABLISHED),)
    hint = select_review_hint(records, file_path="a.py", qualified_name="a.f")
    assert hint is RepositoryLearningReviewHint.INCREASE_CANDIDATE_PRIORITY


def test_candidate_maturity_useful_pattern_gives_no_hint() -> None:
    """Only ESTABLISHED influences scheduling -- a single-occasion or
    just-barely-repeated pattern must not."""

    records = (_record(learning_type=LearningType.USEFUL_FINDING_PATTERN, maturity=LearningMaturity.CANDIDATE),)
    assert select_review_hint(records, file_path="a.py", qualified_name="a.f") is RepositoryLearningReviewHint.NONE


def test_retired_useful_pattern_gives_no_hint() -> None:
    records = (_record(learning_type=LearningType.USEFUL_FINDING_PATTERN, maturity=LearningMaturity.RETIRED),)
    assert select_review_hint(records, file_path="a.py", qualified_name="a.f") is RepositoryLearningReviewHint.NONE


def test_no_matching_surface_gives_no_hint() -> None:
    records = (_record(learning_type=LearningType.USEFUL_FINDING_PATTERN, maturity=LearningMaturity.ESTABLISHED),)
    assert select_review_hint(records, file_path="other.py", qualified_name="other.f") is RepositoryLearningReviewHint.NONE


def test_none_qualified_name_gives_no_hint() -> None:
    records = (_record(learning_type=LearningType.USEFUL_FINDING_PATTERN, maturity=LearningMaturity.ESTABLISHED),)
    assert select_review_hint(records, file_path="a.py", qualified_name=None) is RepositoryLearningReviewHint.NONE


def test_noise_suppression_record_never_increases_priority() -> None:
    """A NOISE_SUPPRESSION learning is never mistaken for a
    USEFUL_FINDING_PATTERN one -- the two must never cross-influence."""

    records = (_record(learning_type=LearningType.NOISE_SUPPRESSION, maturity=LearningMaturity.ESTABLISHED),)
    assert select_review_hint(records, file_path="a.py", qualified_name="a.f") is RepositoryLearningReviewHint.NONE


def test_noise_advisory_text_present_for_active_noise_learning() -> None:
    records = (_record(learning_type=LearningType.NOISE_SUPPRESSION, maturity=LearningMaturity.CANDIDATE),)
    text = noise_advisory_text_for_candidate(records, file_path="a.py", qualified_name="a.f")
    assert "false-positive" in text
    assert "never excuses a confirmed defect" in text


def test_noise_advisory_text_empty_for_retired_learning() -> None:
    records = (_record(learning_type=LearningType.NOISE_SUPPRESSION, maturity=LearningMaturity.RETIRED),)
    assert noise_advisory_text_for_candidate(records, file_path="a.py", qualified_name="a.f") == ""


def test_noise_advisory_text_empty_when_no_match() -> None:
    records = (_record(learning_type=LearningType.NOISE_SUPPRESSION, maturity=LearningMaturity.CANDIDATE),)
    assert noise_advisory_text_for_candidate(records, file_path="other.py", qualified_name="other.f") == ""


def test_noise_advisory_text_is_bounded() -> None:
    from patchfrog.learning_records.personalization import MAX_NOISE_ADVISORY_CHARS

    records = (_record(learning_type=LearningType.NOISE_SUPPRESSION, maturity=LearningMaturity.ESTABLISHED),)
    text = noise_advisory_text_for_candidate(records, file_path="a.py", qualified_name="a.f")
    assert len(text) <= MAX_NOISE_ADVISORY_CHARS


def test_neither_function_accepts_a_severity_provider_or_readiness_parameter() -> None:
    """Structural proof (mirrors the same technique used for W+X's
    "Cloud user cannot control provider from repository" test): neither
    function's signature has any parameter that could carry a severity
    override, a provider/model name, or a readiness decision -- there is
    no such value to pass, so neither can ever influence one."""

    for func in (select_review_hint, noise_advisory_text_for_candidate):
        params = set(inspect.signature(func).parameters)
        assert params == {"records", "file_path", "qualified_name"}, func
        forbidden = {"severity", "provider", "model", "decision", "readiness", "score"}
        assert params.isdisjoint(forbidden)
