"""Pure (no DB) derivation tests for Milestone Y's service layer --
covers spec tests Y1/Y2/Y3/Y5/Y6/Y16 at the function-signature level."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from patchfrog.analysis.domain import FindingCategory
from patchfrog.historical_regression_memory.domain import (
    HistoricalEvidenceStrength,
    HistoricalRegressionRecord,
)
from patchfrog.learning_records.domain import LearningMaturity, LearningType
from patchfrog.learning_records.queries import NoiseFeedbackRow
from patchfrog.learning_records.service import (
    derive_noise_suppression_records,
    derive_useful_pattern_records,
)

_REPO_ID = uuid.uuid4()


def _historical_record(*, run_id: uuid.UUID, file_path: str = "a.py", qualified_name: str = "a.f") -> HistoricalRegressionRecord:
    return HistoricalRegressionRecord(
        historical_finding_id=uuid.uuid4(),
        repository_id=_REPO_ID,
        historical_review_run_id=run_id,
        historical_commit_sha="a" * 40,
        source_file_path=file_path,
        source_qualified_name=qualified_name,
        finding_category=FindingCategory.CORRECTNESS,
        evidence_strength=HistoricalEvidenceStrength.CONFIRMED_USEFUL,
        bounded_evidence_fingerprint="fp",
        observed_at=datetime.now(UTC).isoformat(),
    )


def _noise_row(
    *,
    run_id: uuid.UUID,
    file_path: str = "a.py",
    qualified_name: str | None = "a.f",
    category: FindingCategory = FindingCategory.CORRECTNESS,
) -> NoiseFeedbackRow:
    return NoiseFeedbackRow(
        finding_id=uuid.uuid4(), file_path=file_path, qualified_name=qualified_name, category=category,
        review_run_id=run_id, observed_at=datetime.now(UTC),
    )


# -- Useful pattern (Y6) --


def test_single_trusted_record_never_produces_a_useful_pattern() -> None:
    records = derive_useful_pattern_records(repository_id=_REPO_ID, trusted_records=(_historical_record(run_id=uuid.uuid4()),))
    assert records == ()


def test_two_independent_trusted_records_produce_a_candidate_useful_pattern() -> None:
    records = derive_useful_pattern_records(
        repository_id=_REPO_ID,
        trusted_records=(_historical_record(run_id=uuid.uuid4()), _historical_record(run_id=uuid.uuid4())),
    )
    assert len(records) == 1
    assert records[0].learning_type is LearningType.USEFUL_FINDING_PATTERN
    assert records[0].maturity is LearningMaturity.CANDIDATE
    assert records[0].support_count == 2


def test_three_independent_trusted_records_produce_an_established_useful_pattern() -> None:
    records = derive_useful_pattern_records(
        repository_id=_REPO_ID,
        trusted_records=tuple(_historical_record(run_id=uuid.uuid4()) for _ in range(3)),
    )
    assert len(records) == 1
    assert records[0].maturity is LearningMaturity.ESTABLISHED


def test_same_review_run_never_counts_twice_for_useful_pattern() -> None:
    """Two findings from the *same* review run are not independent
    repetition -- mirrors Milestone O's own independence rule."""

    run_id = uuid.uuid4()
    records = derive_useful_pattern_records(
        repository_id=_REPO_ID,
        trusted_records=(_historical_record(run_id=run_id), _historical_record(run_id=run_id)),
    )
    assert records == ()


# -- Noise suppression (Y5) --


def test_single_false_positive_row_never_produces_a_noise_learning() -> None:
    records = derive_noise_suppression_records(repository_id=_REPO_ID, rows=(_noise_row(run_id=uuid.uuid4()),))
    assert records == ()


def test_two_independent_false_positive_rows_produce_a_candidate_noise_learning() -> None:
    records = derive_noise_suppression_records(
        repository_id=_REPO_ID, rows=(_noise_row(run_id=uuid.uuid4()), _noise_row(run_id=uuid.uuid4()))
    )
    assert len(records) == 1
    assert records[0].learning_type is LearningType.NOISE_SUPPRESSION
    assert records[0].maturity is LearningMaturity.CANDIDATE
    assert records[0].support_count == 2


def test_three_independent_false_positive_rows_produce_established() -> None:
    records = derive_noise_suppression_records(
        repository_id=_REPO_ID, rows=tuple(_noise_row(run_id=uuid.uuid4()) for _ in range(3))
    )
    assert records[0].maturity is LearningMaturity.ESTABLISHED


def test_same_review_run_never_counts_twice_for_noise_pattern() -> None:
    run_id = uuid.uuid4()
    records = derive_noise_suppression_records(
        repository_id=_REPO_ID, rows=(_noise_row(run_id=run_id), _noise_row(run_id=run_id))
    )
    assert records == ()


def test_different_surfaces_never_combined() -> None:
    records = derive_noise_suppression_records(
        repository_id=_REPO_ID,
        rows=(
            _noise_row(run_id=uuid.uuid4(), file_path="a.py", qualified_name="a.f"),
            _noise_row(run_id=uuid.uuid4(), file_path="b.py", qualified_name="b.f"),
        ),
    )
    assert records == ()


def test_different_category_on_same_symbol_never_combined() -> None:
    """Category is part of surface identity -- two findings on the same
    symbol but a different category must never combine into one
    fabricated pattern (mirrors Milestone O's own explicit correction)."""

    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    records = derive_noise_suppression_records(
        repository_id=_REPO_ID,
        rows=(
            _noise_row(run_id=run_a, category=FindingCategory.CORRECTNESS),
            _noise_row(run_id=run_b, category=FindingCategory.SECURITY),
        ),
    )
    assert records == ()


def test_finding_with_no_qualified_name_never_participates() -> None:
    records = derive_noise_suppression_records(
        repository_id=_REPO_ID,
        rows=(
            _noise_row(run_id=uuid.uuid4(), qualified_name=None),
            _noise_row(run_id=uuid.uuid4(), qualified_name=None),
        ),
    )
    assert records == ()


def test_noise_learning_repository_id_is_always_the_input_repository() -> None:
    records = derive_noise_suppression_records(
        repository_id=_REPO_ID, rows=(_noise_row(run_id=uuid.uuid4()), _noise_row(run_id=uuid.uuid4()))
    )
    assert records[0].repository_id == _REPO_ID
