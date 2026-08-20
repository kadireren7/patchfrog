"""Unit coverage for :class:`patchfrog.review_memory.planner.IncrementalReviewPlanner`
-- candidate selection logic, fully in-memory, no DB/network/LLM."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.domain.code import Language, SymbolKind
from patchfrog.indexing.models import ChangeSet, FileChange, FileChangeType
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason
from patchfrog.review_memory.domain import (
    FindingMemoryStatus,
    IncrementalChangeSet,
    IncrementalRunMode,
    PreReviewDecision,
    PreReviewDisposition,
    PreviousGenerationCandidate,
    PreviousReviewSelection,
    ReviewMemoryFinding,
    SymbolContinuityResult,
    SymbolContinuityStatus,
    SymbolSnapshot,
    TransitionReasonCode,
)
from patchfrog.review_memory.planner import IncrementalReviewPlanner

_REPO_INDEX_ID = uuid.uuid4()
_REPO = uuid.uuid4()
_PR = uuid.uuid4()


def _memory_finding() -> ReviewMemoryFinding:
    return ReviewMemoryFinding(
        id=uuid.uuid4(), source_review_run_id=uuid.uuid4(), source_finding_id=uuid.uuid4(),
        repository_id=_REPO, pull_request_id=_PR, first_seen_commit_sha="sha1", last_seen_commit_sha="sha1",
        file_path="a.py", symbol_id=uuid.uuid4(), symbol_qualified_name="foo", symbol_kind=SymbolKind.FUNCTION,
        category=FindingCategory.CORRECTNESS, severity=Severity.HIGH, title="t", message="m",
        start_line=1, end_line=2, exact_fingerprint="e", semantic_family_fingerprint="f",
        status=FindingMemoryStatus.OPEN,
    )


def _candidate(*, symbol_id: uuid.UUID | None, file_path: str = "a.py", name: str | None = "foo") -> ReviewCandidate:
    return ReviewCandidate(
        file_path=file_path, symbol_id=symbol_id, symbol_name=name, qualified_name=name,
        start_line=1, end_line=5, changed_lines=(1,), static_finding_ids=(),
        reason=ReviewCandidateReason.CHANGED_SYMBOL if symbol_id else ReviewCandidateReason.CHANGED_MODULE_REGION,
    )


def _snapshot(*, id_: uuid.UUID, file_path: str = "a.py") -> SymbolSnapshot:
    return SymbolSnapshot(
        id=id_, name="foo", qualified_name="foo", kind=SymbolKind.FUNCTION, language=Language.PYTHON,
        file_path=file_path, start_line=1, end_line=5, content_hash="h",
    )


def _usable_selection() -> PreviousReviewSelection:
    return PreviousReviewSelection(
        candidate=PreviousGenerationCandidate(
            generation_id=uuid.uuid4(), review_run_id=uuid.uuid4(), commit_sha="sha1",
            repository_index_id=_REPO_INDEX_ID, memory_compatibility_fingerprint="fp",
        ),
        ancestry_verified=True, compatibility_ok=True, usable_for_candidate_skipping=True,
        usable_for_finding_memory=True, invalidation_reason=None, detail="ok",
    )


def _unusable_selection(reason: TransitionReasonCode) -> PreviousReviewSelection:
    return PreviousReviewSelection(
        candidate=None, ancestry_verified=False, compatibility_ok=False,
        usable_for_candidate_skipping=False, usable_for_finding_memory=False,
        invalidation_reason=reason, detail="unusable",
    )


def test_no_usable_previous_review_selects_every_candidate_as_full() -> None:
    candidates = (_candidate(symbol_id=uuid.uuid4()), _candidate(symbol_id=uuid.uuid4()))
    plan = IncrementalReviewPlanner().plan(
        selection=_unusable_selection(TransitionReasonCode.NO_PREVIOUS_REVIEW),
        current_candidates=candidates, change_set=None, pre_review_decisions=(), previous_candidate_count=0,
    )
    assert plan.mode is IncrementalRunMode.FULL
    assert plan.selected_candidates == candidates
    assert plan.skipped_candidates == ()


def test_drift_disables_skipping_even_though_change_set_exists() -> None:
    changed_id = uuid.uuid4()
    unchanged_id = uuid.uuid4()
    candidates = (_candidate(symbol_id=changed_id), _candidate(symbol_id=unchanged_id))
    prev_snap = _snapshot(id_=uuid.uuid4())
    change_set = IncrementalChangeSet(
        previous_commit_sha="sha1", current_commit_sha="sha2",
        file_changes=ChangeSet(old_commit_sha="sha1", new_commit_sha="sha2", changes=()),
        symbol_changes=(
            SymbolContinuityResult(
                status=SymbolContinuityStatus.MODIFIED, previous=prev_snap,
                current=_snapshot(id_=changed_id), reason="modified",
            ),
        ),
    )
    selection = PreviousReviewSelection(
        candidate=PreviousGenerationCandidate(
            generation_id=uuid.uuid4(), review_run_id=uuid.uuid4(), commit_sha="sha1",
            repository_index_id=_REPO_INDEX_ID, memory_compatibility_fingerprint="fp",
        ),
        ancestry_verified=True, compatibility_ok=False, usable_for_candidate_skipping=False,
        usable_for_finding_memory=True, invalidation_reason=TransitionReasonCode.MODEL_DRIFT, detail="drift",
    )
    plan = IncrementalReviewPlanner().plan(
        selection=selection, current_candidates=candidates, change_set=change_set,
        pre_review_decisions=(), previous_candidate_count=2,
    )
    # Drift disables candidate skipping -> every current candidate reviewed,
    # exactly like the no-memory-at-all case, even though a real change
    # set was computed (still used for post-review reconciliation).
    assert plan.mode is IncrementalRunMode.FULL
    assert plan.selected_candidates == candidates
    assert plan.selection.usable_for_finding_memory is True


def test_unchanged_candidate_with_no_attached_finding_is_skipped() -> None:
    unchanged_id = uuid.uuid4()
    changed_id = uuid.uuid4()
    candidates = (_candidate(symbol_id=unchanged_id, name="clean"), _candidate(symbol_id=changed_id, name="dirty"))
    change_set = IncrementalChangeSet(
        previous_commit_sha="sha1", current_commit_sha="sha2",
        file_changes=ChangeSet(old_commit_sha="sha1", new_commit_sha="sha2", changes=(
            FileChange(change_type=FileChangeType.MODIFIED, path="a.py"),
        )),
        symbol_changes=(
            SymbolContinuityResult(
                status=SymbolContinuityStatus.UNCHANGED, previous=_snapshot(id_=unchanged_id),
                current=_snapshot(id_=unchanged_id), reason="same",
            ),
            SymbolContinuityResult(
                status=SymbolContinuityStatus.MODIFIED, previous=_snapshot(id_=uuid.uuid4()),
                current=_snapshot(id_=changed_id), reason="changed",
            ),
        ),
    )
    plan = IncrementalReviewPlanner().plan(
        selection=_usable_selection(), current_candidates=candidates, change_set=change_set,
        pre_review_decisions=(), previous_candidate_count=2,
    )
    assert plan.mode is IncrementalRunMode.INCREMENTAL
    selected_names = {c.symbol_name for c in plan.selected_candidates}
    assert selected_names == {"dirty"}
    skipped_names = {c.symbol_name for c in plan.skipped_candidates}
    assert skipped_names == {"clean"}


def test_needs_recheck_symbol_is_selected_even_though_unchanged() -> None:
    """A symbol that's UNCHANGED but has an attached open finding still
    needs a fresh AI look -- selection must union changed_symbol_ids with
    every NEEDS_RECHECK decision's updated_symbol_id."""

    unchanged_but_tracked = uuid.uuid4()
    candidates = (_candidate(symbol_id=unchanged_but_tracked, name="tracked"),)
    change_set = IncrementalChangeSet(
        previous_commit_sha="sha1", current_commit_sha="sha2",
        file_changes=ChangeSet(old_commit_sha="sha1", new_commit_sha="sha2", changes=()),
        symbol_changes=(
            SymbolContinuityResult(
                status=SymbolContinuityStatus.UNCHANGED, previous=_snapshot(id_=unchanged_but_tracked),
                current=_snapshot(id_=unchanged_but_tracked), reason="same",
            ),
        ),
    )
    pre_decision = PreReviewDecision(
        memory_finding=_memory_finding(),
        disposition=PreReviewDisposition.NEEDS_RECHECK, reason=TransitionReasonCode.EVIDENCE_REGION_CHANGED,
        detail="needs recheck", updated_symbol_id=unchanged_but_tracked,
    )
    plan = IncrementalReviewPlanner().plan(
        selection=_usable_selection(), current_candidates=candidates, change_set=change_set,
        pre_review_decisions=(pre_decision,), previous_candidate_count=1,
    )
    assert len(plan.selected_candidates) == 1
    assert plan.skipped_candidates == ()


def test_module_region_candidate_selected_only_if_its_file_changed() -> None:
    module_candidate = _candidate(symbol_id=None, file_path="untouched.py", name=None)
    changed_module_candidate = _candidate(symbol_id=None, file_path="touched.py", name=None)
    change_set = IncrementalChangeSet(
        previous_commit_sha="sha1", current_commit_sha="sha2",
        file_changes=ChangeSet(old_commit_sha="sha1", new_commit_sha="sha2", changes=(
            FileChange(change_type=FileChangeType.MODIFIED, path="touched.py"),
        )),
        symbol_changes=(),
    )
    plan = IncrementalReviewPlanner().plan(
        selection=_usable_selection(), current_candidates=(module_candidate, changed_module_candidate),
        change_set=change_set, pre_review_decisions=(), previous_candidate_count=2,
    )
    assert plan.selected_candidates == (changed_module_candidate,)
    assert plan.skipped_candidates == (module_candidate,)


def test_metrics_count_dispositions_correctly() -> None:
    decisions = (
        PreReviewDecision(
            memory_finding=_memory_finding(),
            disposition=PreReviewDisposition.CARRY_FORWARD, reason=TransitionReasonCode.SYMBOL_UNCHANGED,
            detail="d",
        ),
        PreReviewDecision(
            memory_finding=_memory_finding(),
            disposition=PreReviewDisposition.RESOLVED_IMMEDIATELY, reason=TransitionReasonCode.SYMBOL_DELETED,
            detail="d",
        ),
        PreReviewDecision(
            memory_finding=_memory_finding(),
            disposition=PreReviewDisposition.NEEDS_RECHECK, reason=TransitionReasonCode.AMBIGUOUS_SYMBOL_MATCH,
            detail="d",
        ),
    )
    plan = IncrementalReviewPlanner().plan(
        selection=_usable_selection(), current_candidates=(),
        change_set=IncrementalChangeSet(
            previous_commit_sha="sha1", current_commit_sha="sha2",
            file_changes=ChangeSet(old_commit_sha="sha1", new_commit_sha="sha2", changes=()), symbol_changes=(),
        ),
        pre_review_decisions=decisions, previous_candidate_count=5,
    )
    assert plan.metrics.findings_carried_forward == 1
    assert plan.metrics.findings_resolved == 1
    assert plan.metrics.findings_ambiguous == 1
    assert plan.metrics.previous_candidate_count == 5


def test_skipped_candidates_breakdown_distinguishes_finding_free_from_evidence_confirmed() -> None:
    """provider_calls_avoided must be provably attributable to two
    distinct causes: a symbol with no open finding at all (the original
    incremental-savings mechanism) vs. a symbol with an open finding
    whose evidence was deterministically reconfirmed (the zero-AI-call
    carry-forward this phase adds) -- never conflated into one opaque
    count."""

    finding_free_id = uuid.uuid4()
    evidence_confirmed_id = uuid.uuid4()
    dirty_id = uuid.uuid4()
    candidates = (
        _candidate(symbol_id=finding_free_id, name="clean"),
        _candidate(symbol_id=evidence_confirmed_id, name="tracked_unchanged"),
        _candidate(symbol_id=dirty_id, name="dirty"),
    )
    change_set = IncrementalChangeSet(
        previous_commit_sha="sha1", current_commit_sha="sha2",
        file_changes=ChangeSet(old_commit_sha="sha1", new_commit_sha="sha2", changes=(
            FileChange(change_type=FileChangeType.MODIFIED, path="a.py"),
        )),
        symbol_changes=(
            SymbolContinuityResult(
                status=SymbolContinuityStatus.UNCHANGED, previous=_snapshot(id_=finding_free_id),
                current=_snapshot(id_=finding_free_id), reason="same",
            ),
            SymbolContinuityResult(
                status=SymbolContinuityStatus.UNCHANGED, previous=_snapshot(id_=evidence_confirmed_id),
                current=_snapshot(id_=evidence_confirmed_id), reason="same",
            ),
            SymbolContinuityResult(
                status=SymbolContinuityStatus.MODIFIED, previous=_snapshot(id_=uuid.uuid4()),
                current=_snapshot(id_=dirty_id), reason="changed",
            ),
        ),
    )
    carry_forward_decision = PreReviewDecision(
        memory_finding=_memory_finding(), disposition=PreReviewDisposition.CARRY_FORWARD,
        reason=TransitionReasonCode.EVIDENCE_CONFIRMED_UNCHANGED, detail="evidence confirmed",
        updated_symbol_id=evidence_confirmed_id,
    )
    plan = IncrementalReviewPlanner().plan(
        selection=_usable_selection(), current_candidates=candidates, change_set=change_set,
        pre_review_decisions=(carry_forward_decision,), previous_candidate_count=3,
    )
    assert {c.symbol_name for c in plan.selected_candidates} == {"dirty"}
    assert {c.symbol_name for c in plan.skipped_candidates} == {"clean", "tracked_unchanged"}
    assert plan.metrics.candidates_skipped_by_memory == 2
    assert plan.metrics.provider_calls_avoided == 2
    assert plan.metrics.candidates_skipped_finding_free == 1
    assert plan.metrics.candidates_skipped_evidence_confirmed == 1
