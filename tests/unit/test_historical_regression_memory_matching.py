"""Unit tests for :mod:`patchfrog.historical_regression_memory.matching`
-- deterministic, pure candidate derivation. Every case is a hand-built
:class:`~patchfrog.change_intelligence.domain.ChangeUnit`/
:class:`~patchfrog.historical_regression_memory.domain.HistoricalRegressionRecord`,
no database, no LLM.
"""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import FindingCategory
from patchfrog.change_intelligence.domain import (
    AffectedRelation,
    AffectedSymbolRef,
    ChangeKind,
    ChangeUnit,
    CompanionReasonCode,
    CompanionStatus,
    ExpectedCompanionChange,
)
from patchfrog.contract_intelligence.domain import (
    BreakingCharacteristic,
    ContractDelta,
    ContractKind,
)
from patchfrog.historical_regression_memory.domain import (
    MAX_HISTORICAL_RECORDS_PER_SURFACE,
    MAX_HISTORICAL_REGRESSION_CANDIDATES,
    HistoricalEvidenceStrength,
    HistoricalMatchKind,
    HistoricalRegressionReasonCode,
    HistoricalRegressionRecord,
)
from patchfrog.historical_regression_memory.matching import derive_historical_regression_candidates
from patchfrog.intent_verification.domain import IntentGapReasonCode, PotentialIntentGap
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason
from patchfrog.test_intelligence.domain import (
    PotentialTestGap,
    TestEvidence,
    TestExpectation,
    TestExpectationReasonCode,
)

_REPO_ID = uuid.uuid4()


def _record(
    *,
    file_path: str = "service.py",
    qualified_name: str | None = "process_payment",
    strength: HistoricalEvidenceStrength = HistoricalEvidenceStrength.CONFIRMED_FIXED,
) -> HistoricalRegressionRecord:
    return HistoricalRegressionRecord(
        historical_finding_id=uuid.uuid4(), repository_id=_REPO_ID, historical_review_run_id=uuid.uuid4(),
        historical_commit_sha="a" * 40, source_file_path=file_path, source_qualified_name=qualified_name,
        finding_category=FindingCategory.CORRECTNESS, evidence_strength=strength,
        bounded_evidence_fingerprint="forgot idempotency key", observed_at="2026-01-01T00:00:00+00:00",
    )


def _candidate(*, file_path: str, qualified_name: str | None) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=uuid.uuid4() if qualified_name else None,
        symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
        static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )


def _affected(*, file_path: str, qualified_name: str | None) -> AffectedSymbolRef:
    return AffectedSymbolRef(
        file_path=file_path, qualified_name=qualified_name,
        symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
        relation=AffectedRelation.DIRECTLY_DEPENDENT, distance=1, reason="directly calls the changed symbol",
    )


def test_same_symbol_match_for_directly_changed_candidate() -> None:
    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    record = _record(strength=HistoricalEvidenceStrength.CONFIRMED_FIXED)
    candidates = derive_historical_regression_candidates(trusted_records=(record,), change_units=(unit,))
    assert len(candidates) == 1
    assert candidates[0].match_kind is HistoricalMatchKind.SAME_SYMBOL
    assert candidates[0].reason_code is HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_SYMBOL
    assert candidates[0].stands_alone


def test_same_symbol_match_confirmed_useful() -> None:
    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    record = _record(strength=HistoricalEvidenceStrength.CONFIRMED_USEFUL)
    candidates = derive_historical_regression_candidates(trusted_records=(record,), change_units=(unit,))
    assert len(candidates) == 1
    assert candidates[0].reason_code is HistoricalRegressionReasonCode.PREVIOUS_USEFUL_FINDING_SAME_SYMBOL


def test_same_qualified_name_in_same_file_for_affected_not_changed() -> None:
    """RetryWorker.run is affected/unchanged while process_payment (a
    different symbol, same unit) is directly changed in the same file."""

    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
        affected_surface=(_affected(file_path="service.py", qualified_name="RetryWorker.run"),),
    )
    record = _record(file_path="service.py", qualified_name="RetryWorker.run")
    candidates = derive_historical_regression_candidates(trusted_records=(record,), change_units=(unit,))
    assert len(candidates) == 1
    assert candidates[0].match_kind is HistoricalMatchKind.SAME_QUALIFIED_NAME_IN_SAME_FILE


def test_graph_related_surface_for_affected_in_different_file() -> None:
    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
        affected_surface=(_affected(file_path="retry_worker.py", qualified_name="RetryWorker.run"),),
    )
    record = _record(file_path="retry_worker.py", qualified_name="RetryWorker.run")
    candidates = derive_historical_regression_candidates(trusted_records=(record,), change_units=(unit,))
    assert len(candidates) == 1
    assert candidates[0].match_kind is HistoricalMatchKind.GRAPH_RELATED_SURFACE
    assert candidates[0].reason_code is HistoricalRegressionReasonCode.PREVIOUS_REGRESSION_RELATED_SURFACE


def test_same_file_weak_match_requires_confirmed_fixed_not_useful() -> None:
    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    fixed_record = _record(
        file_path="service.py", qualified_name="unrelated_old_symbol", strength=HistoricalEvidenceStrength.CONFIRMED_FIXED
    )
    candidates = derive_historical_regression_candidates(trusted_records=(fixed_record,), change_units=(unit,))
    assert len(candidates) == 1
    assert candidates[0].match_kind is HistoricalMatchKind.SAME_FILE
    assert candidates[0].reason_code is HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_FILE

    useful_record = _record(
        file_path="service.py", qualified_name="unrelated_old_symbol", strength=HistoricalEvidenceStrength.CONFIRMED_USEFUL
    )
    assert derive_historical_regression_candidates(trusted_records=(useful_record,), change_units=(unit,)) == ()


def test_no_match_when_neither_file_nor_symbol_present() -> None:
    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    record = _record(file_path="unrelated.py", qualified_name="unrelated_fn")
    assert derive_historical_regression_candidates(trusted_records=(record,), change_units=(unit,)) == ()


def test_dedup_enriches_existing_missing_companion_never_stands_alone() -> None:
    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
        affected_surface=(_affected(file_path="retry_worker.py", qualified_name="RetryWorker.run"),),
    )
    companion = ExpectedCompanionChange(
        change_unit_id="u1", source_qualified_name="process_payment", source_file_path="service.py",
        expected_qualified_name="RetryWorker.run", expected_file_path="retry_worker.py",
        reason_code=CompanionReasonCode.CALLER_NOT_UPDATED, reason="calls it", evidence="call edge",
        status=CompanionStatus.MISSING,
    )
    record = _record(file_path="retry_worker.py", qualified_name="RetryWorker.run")
    candidates = derive_historical_regression_candidates(
        trusted_records=(record,), change_units=(unit,), expected_companions=(companion,)
    )
    assert len(candidates) == 1
    assert candidates[0].enriches_companion is companion
    assert not candidates[0].stands_alone


def test_dedup_enriches_existing_intent_gap() -> None:
    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    gap = PotentialIntentGap(
        intent_claim_id="c1", change_unit_id="u1",
        expected_surface=_affected(file_path="retry_worker.py", qualified_name="RetryWorker.run"),
        reason_code=IntentGapReasonCode.EXPECTED_SURFACE_UNCHANGED, evidence="unchanged",
    )
    record = _record(file_path="retry_worker.py", qualified_name="RetryWorker.run")
    candidates = derive_historical_regression_candidates(
        trusted_records=(record,), change_units=(unit,), intent_gaps=(gap,)
    )
    assert len(candidates) == 1
    assert candidates[0].enriches_intent_gap is gap
    assert not candidates[0].stands_alone


def test_dedup_enriches_existing_test_gap() -> None:
    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    expectation = TestExpectation(
        change_unit_id="u1", source_qualified_name="process_payment", source_file_path="service.py",
        reason_code=TestExpectationReasonCode.NO_TEST_SURFACE_FOUND, reason="no test",
        evidence=TestEvidence(reason_code=TestExpectationReasonCode.NO_TEST_SURFACE_FOUND, bounded_text="no test"),
        status=CompanionStatus.MISSING,
    )
    test_gap = PotentialTestGap(change_unit_id="u1", expectation=expectation)
    record = _record(file_path="service.py", qualified_name="process_payment")
    candidates = derive_historical_regression_candidates(
        trusted_records=(record,), change_units=(unit,), test_gaps=(test_gap,)
    )
    assert len(candidates) == 1
    assert candidates[0].enriches_test_gap is test_gap
    assert not candidates[0].stands_alone


def test_contract_delta_blast_radius_feeds_pool() -> None:
    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.CONTRACT,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    delta = ContractDelta(
        contract_id="c1", qualified_name="process_payment", file_path="service.py", kind=ContractKind.FUNCTION,
        change_unit_id="u1", before_signature="def process_payment(order):",
        after_signature="def process_payment(order, rate):",
        characteristics=(BreakingCharacteristic.REQUIRED_PARAMETER_ADDED,), evidence="signature changed",
        blast_radius=(_affected(file_path="caller.py", qualified_name="checkout"),),
    )
    record = _record(file_path="caller.py", qualified_name="checkout")
    candidates = derive_historical_regression_candidates(
        trusted_records=(record,), change_units=(unit,), contract_deltas=(delta,)
    )
    assert len(candidates) == 1
    assert candidates[0].match_kind is HistoricalMatchKind.GRAPH_RELATED_SURFACE


def test_bounded_records_per_surface() -> None:
    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    records = tuple(
        _record(file_path="service.py", qualified_name="process_payment") for _ in range(MAX_HISTORICAL_RECORDS_PER_SURFACE + 5)
    )
    candidates = derive_historical_regression_candidates(trusted_records=records, change_units=(unit,))
    assert len(candidates) == MAX_HISTORICAL_RECORDS_PER_SURFACE


def test_bounded_total_candidates() -> None:
    units = tuple(
        ChangeUnit(
            id=f"u{i}", title="payment", change_kind=ChangeKind.BEHAVIOR,
            changed_candidates=(_candidate(file_path=f"s{i}.py", qualified_name=f"fn_{i}"),),
        )
        for i in range(MAX_HISTORICAL_REGRESSION_CANDIDATES + 5)
    )
    records = tuple(_record(file_path=f"s{i}.py", qualified_name=f"fn_{i}") for i in range(MAX_HISTORICAL_REGRESSION_CANDIDATES + 5))
    candidates = derive_historical_regression_candidates(trusted_records=records, change_units=units)
    assert len(candidates) == MAX_HISTORICAL_REGRESSION_CANDIDATES


def test_repository_isolation_is_the_caller_query_boundary_no_cross_repo_leakage_in_matching() -> None:
    """Matching itself has no repository concept -- isolation is
    enforced entirely by :func:`patchfrog.historical_regression_memory.queries.fetch_trusted_historical_records`'s
    own mandatory ``repository_id`` filter (see the integration corpus
    for the real, DB-backed proof). This test documents that matching
    never re-derives or second-guesses repository scoping -- it simply
    trusts whatever records it was given."""

    unit = ChangeUnit(
        id="u1", title="payment", change_kind=ChangeKind.BEHAVIOR,
        changed_candidates=(_candidate(file_path="service.py", qualified_name="process_payment"),),
    )
    other_repo_record = HistoricalRegressionRecord(
        historical_finding_id=uuid.uuid4(), repository_id=uuid.uuid4(), historical_review_run_id=uuid.uuid4(),
        historical_commit_sha="b" * 40, source_file_path="service.py", source_qualified_name="process_payment",
        finding_category=FindingCategory.CORRECTNESS, evidence_strength=HistoricalEvidenceStrength.CONFIRMED_FIXED,
        bounded_evidence_fingerprint="from another repo", observed_at="2026-01-01T00:00:00+00:00",
    )
    # Matching still matches -- it has no repository_id field of its own
    # to check. This is exactly why the query layer's filter is
    # mandatory, never optional -- see the integration corpus.
    candidates = derive_historical_regression_candidates(trusted_records=(other_repo_record,), change_units=(unit,))
    assert len(candidates) == 1
