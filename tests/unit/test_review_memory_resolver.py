"""Unit coverage for :class:`patchfrog.review_memory.resolver.ReviewMemoryResolver`
-- both phases (pre-review disposition, post-review reconciliation),
fully in-memory, no DB/network."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.domain.code import Language, SymbolKind
from patchfrog.indexing.models import ChangeSet, FileChange, FileChangeType
from patchfrog.review.domain import (
    AIReviewFinding,
    FinalAIFinding,
    ReviewCandidate,
    ReviewCandidateReason,
)
from patchfrog.review_memory.domain import (
    CurrentFinding,
    FindingMemoryStatus,
    IncrementalChangeSet,
    PreReviewDecision,
    PreReviewDisposition,
    ReviewMemoryFinding,
    SymbolContinuityResult,
    SymbolContinuityStatus,
    SymbolSnapshot,
    TransitionReasonCode,
)
from patchfrog.review_memory.resolver import ReviewMemoryResolver

_REPO = uuid.uuid4()
_PR = uuid.uuid4()


def _memory_finding(
    *, symbol_id: uuid.UUID | None, file_path: str = "a.py", start_line: int = 1, end_line: int = 2,
    category: FindingCategory = FindingCategory.CORRECTNESS, severity: Severity = Severity.HIGH,
    title: str = "Bug", message: str = "there is a bug",
) -> ReviewMemoryFinding:
    return ReviewMemoryFinding(
        id=uuid.uuid4(), source_review_run_id=uuid.uuid4(), source_finding_id=uuid.uuid4(),
        repository_id=_REPO, pull_request_id=_PR, first_seen_commit_sha="sha1", last_seen_commit_sha="sha1",
        file_path=file_path, symbol_id=symbol_id, symbol_qualified_name="foo" if symbol_id else None,
        symbol_kind=SymbolKind.FUNCTION if symbol_id else None, category=category, severity=severity,
        title=title, message=message, start_line=start_line, end_line=end_line,
        exact_fingerprint="exact", semantic_family_fingerprint="family", status=FindingMemoryStatus.OPEN,
    )


def _snapshot(*, id_: uuid.UUID, name: str = "foo", file_path: str = "a.py", content_hash: str = "h") -> SymbolSnapshot:
    return SymbolSnapshot(
        id=id_, name=name, qualified_name=name, kind=SymbolKind.FUNCTION, language=Language.PYTHON,
        file_path=file_path, start_line=1, end_line=5, content_hash=content_hash,
    )


def _change_set(*, symbol_changes: tuple[SymbolContinuityResult, ...], file_changes: ChangeSet | None = None) -> IncrementalChangeSet:
    return IncrementalChangeSet(
        previous_commit_sha="sha1", current_commit_sha="sha2",
        file_changes=file_changes or ChangeSet(old_commit_sha="sha1", new_commit_sha="sha2", changes=()),
        symbol_changes=symbol_changes,
    )


def test_deleted_symbol_resolves_immediately() -> None:
    symbol_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id)
    prev_snap = _snapshot(id_=symbol_id)
    continuity = SymbolContinuityResult(
        status=SymbolContinuityStatus.DELETED, previous=prev_snap, current=None, reason="gone"
    )
    decisions = ReviewMemoryResolver().resolve_pre_review(
        previous_findings=[finding], change_set=_change_set(symbol_changes=(continuity,))
    )
    assert len(decisions) == 1
    assert decisions[0].disposition is PreReviewDisposition.RESOLVED_IMMEDIATELY
    assert decisions[0].reason is TransitionReasonCode.SYMBOL_DELETED


def test_ambiguous_symbol_needs_recheck() -> None:
    symbol_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id)
    prev_snap = _snapshot(id_=symbol_id)
    continuity = SymbolContinuityResult(
        status=SymbolContinuityStatus.AMBIGUOUS, previous=prev_snap, current=None, reason="ambiguous"
    )
    decisions = ReviewMemoryResolver().resolve_pre_review(
        previous_findings=[finding], change_set=_change_set(symbol_changes=(continuity,))
    )
    assert decisions[0].disposition is PreReviewDisposition.NEEDS_RECHECK
    assert decisions[0].reason is TransitionReasonCode.AMBIGUOUS_SYMBOL_MATCH


def test_modified_symbol_needs_recheck() -> None:
    symbol_id = uuid.uuid4()
    new_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id)
    prev_snap = _snapshot(id_=symbol_id)
    cur_snap = _snapshot(id_=new_id)
    continuity = SymbolContinuityResult(
        status=SymbolContinuityStatus.MODIFIED, previous=prev_snap, current=cur_snap, reason="body changed"
    )
    decisions = ReviewMemoryResolver().resolve_pre_review(
        previous_findings=[finding], change_set=_change_set(symbol_changes=(continuity,))
    )
    assert decisions[0].disposition is PreReviewDisposition.NEEDS_RECHECK
    assert decisions[0].reason is TransitionReasonCode.SYMBOL_MODIFIED
    assert decisions[0].updated_symbol_id == new_id


def test_unchanged_symbol_without_evidence_confirmation_needs_recheck() -> None:
    """Fail-closed default: an UNCHANGED symbol with an attached finding
    still needs a fresh AI look unless the caller explicitly confirms
    the evidence snippet (``evidence_still_present`` omitted here) --
    see :mod:`patchfrog.review_memory.evidence` for the real,
    deterministic confirmation path."""

    symbol_id = uuid.uuid4()
    new_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id)
    prev_snap = _snapshot(id_=symbol_id)
    cur_snap = _snapshot(id_=new_id)
    continuity = SymbolContinuityResult(
        status=SymbolContinuityStatus.UNCHANGED, previous=prev_snap, current=cur_snap, reason="identical"
    )
    decisions = ReviewMemoryResolver().resolve_pre_review(
        previous_findings=[finding], change_set=_change_set(symbol_changes=(continuity,))
    )
    assert decisions[0].disposition is PreReviewDisposition.NEEDS_RECHECK
    assert decisions[0].reason is TransitionReasonCode.EVIDENCE_REGION_CHANGED


def test_unchanged_symbol_with_evidence_confirmed_carries_forward() -> None:
    symbol_id = uuid.uuid4()
    new_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id)
    prev_snap = _snapshot(id_=symbol_id)
    cur_snap = _snapshot(id_=new_id)
    continuity = SymbolContinuityResult(
        status=SymbolContinuityStatus.UNCHANGED, previous=prev_snap, current=cur_snap, reason="identical"
    )
    decisions = ReviewMemoryResolver().resolve_pre_review(
        previous_findings=[finding], change_set=_change_set(symbol_changes=(continuity,)),
        evidence_still_present={str(finding.id): True},
    )
    assert decisions[0].disposition is PreReviewDisposition.CARRY_FORWARD
    assert decisions[0].reason is TransitionReasonCode.EVIDENCE_CONFIRMED_UNCHANGED
    assert decisions[0].updated_symbol_id == new_id


def test_missing_continuity_result_fails_closed_to_needs_recheck() -> None:
    finding = _memory_finding(symbol_id=uuid.uuid4())
    decisions = ReviewMemoryResolver().resolve_pre_review(
        previous_findings=[finding], change_set=_change_set(symbol_changes=())
    )
    assert decisions[0].disposition is PreReviewDisposition.NEEDS_RECHECK
    assert decisions[0].reason is TransitionReasonCode.PREVIOUS_FINDING_MISSING


def test_module_level_finding_in_deleted_file_resolves_immediately() -> None:
    finding = _memory_finding(symbol_id=None, file_path="gone.py")
    file_changes = ChangeSet(
        old_commit_sha="sha1", new_commit_sha="sha2",
        changes=(FileChange(change_type=FileChangeType.DELETED, path="gone.py"),),
    )
    decisions = ReviewMemoryResolver().resolve_pre_review(
        previous_findings=[finding], change_set=_change_set(symbol_changes=(), file_changes=file_changes)
    )
    assert decisions[0].disposition is PreReviewDisposition.RESOLVED_IMMEDIATELY
    assert decisions[0].reason is TransitionReasonCode.FILE_DELETED


def test_module_level_finding_always_needs_recheck() -> None:
    finding = _memory_finding(symbol_id=None, file_path="a.py")
    decisions = ReviewMemoryResolver().resolve_pre_review(
        previous_findings=[finding], change_set=_change_set(symbol_changes=())
    )
    assert decisions[0].disposition is PreReviewDisposition.NEEDS_RECHECK


def _final_finding(
    *, symbol_id: uuid.UUID | None, file_path: str, category: FindingCategory, severity: Severity,
    message: str, start_line: int = 1, end_line: int = 2,
) -> FinalAIFinding:
    candidate = ReviewCandidate(
        file_path=file_path, symbol_id=symbol_id, symbol_name="foo", qualified_name="foo",
        start_line=start_line, end_line=end_line, changed_lines=(start_line,), static_finding_ids=(),
        reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )
    finding = AIReviewFinding(
        title="t", message=message, category=category, severity=severity, confidence=Confidence.HIGH,
        file_path=file_path, start_line=start_line, end_line=end_line, evidence=(), reasoning_summary="r",
    )
    return FinalAIFinding(
        proposal_id=uuid.uuid4(), candidate_id=uuid.uuid4(), candidate=candidate, finding=finding,
        critic_verdict=None, final_severity=severity, final_confidence=Confidence.HIGH,
        corroborated_by_static=False, static_finding_ids=(),
    )


def _current_finding_from_final(final: FinalAIFinding, *, current_id: uuid.UUID | None = None) -> CurrentFinding:
    return CurrentFinding(
        id=current_id or uuid.uuid4(), symbol_id=final.candidate.symbol_id,
        symbol_qualified_name=final.candidate.qualified_name, symbol_kind=SymbolKind.FUNCTION,
        file_path=final.candidate.file_path, category=final.finding.category, severity=final.final_severity,
        title=final.finding.title, message=final.finding.message, start_line=final.finding.start_line,
        end_line=final.finding.end_line,
    )


def test_reconcile_resolved_immediately_becomes_resolved() -> None:

    finding = _memory_finding(symbol_id=uuid.uuid4())
    pre = PreReviewDecision(
        memory_finding=finding, disposition=PreReviewDisposition.RESOLVED_IMMEDIATELY,
        reason=TransitionReasonCode.SYMBOL_DELETED, detail="gone",
    )
    decisions = ReviewMemoryResolver().reconcile_post_review(pre_review_decisions=[pre], current_findings=[])
    assert decisions[0].new_status is FindingMemoryStatus.RESOLVED
    assert decisions[0].updated_current_finding_id is None


def test_reconcile_carry_forward_stays_carried_forward_without_matching() -> None:

    symbol_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id)
    pre = PreReviewDecision(
        memory_finding=finding, disposition=PreReviewDisposition.CARRY_FORWARD,
        reason=TransitionReasonCode.SYMBOL_UNCHANGED, detail="unchanged", updated_symbol_id=symbol_id,
        updated_file_path="a.py", updated_start_line=1, updated_end_line=2,
    )
    decisions = ReviewMemoryResolver().reconcile_post_review(pre_review_decisions=[pre], current_findings=[])
    assert decisions[0].new_status is FindingMemoryStatus.CARRIED_FORWARD
    assert decisions[0].updated_current_finding_id is None  # no recheck happened, no new finding row


def test_reconcile_needs_recheck_reproduced_is_carried_forward() -> None:

    symbol_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id, message="there is a bug")
    final = _final_finding(
        symbol_id=symbol_id, file_path="a.py", category=FindingCategory.CORRECTNESS,
        severity=Severity.HIGH, message="there is a bug",
    )
    current = _current_finding_from_final(final)
    pre = PreReviewDecision(
        memory_finding=finding, disposition=PreReviewDisposition.NEEDS_RECHECK,
        reason=TransitionReasonCode.SYMBOL_MODIFIED, detail="modified", updated_symbol_id=symbol_id,
    )
    decisions = ReviewMemoryResolver().reconcile_post_review(pre_review_decisions=[pre], current_findings=[current])
    assert decisions[0].new_status is FindingMemoryStatus.CARRIED_FORWARD
    assert decisions[0].reason is TransitionReasonCode.RECHECK_CONFIRMED
    assert decisions[0].updated_current_finding_id == current.id


def test_reconcile_needs_recheck_different_severity_is_changed() -> None:

    symbol_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id, severity=Severity.MEDIUM, message="there is a bug")
    final = _final_finding(
        symbol_id=symbol_id, file_path="a.py", category=FindingCategory.CORRECTNESS,
        severity=Severity.CRITICAL, message="there is a bug",
    )
    current = _current_finding_from_final(final)
    pre = PreReviewDecision(
        memory_finding=finding, disposition=PreReviewDisposition.NEEDS_RECHECK,
        reason=TransitionReasonCode.SYMBOL_MODIFIED, detail="modified", updated_symbol_id=symbol_id,
    )
    decisions = ReviewMemoryResolver().reconcile_post_review(pre_review_decisions=[pre], current_findings=[current])
    assert decisions[0].new_status is FindingMemoryStatus.CHANGED


def test_reconcile_needs_recheck_no_match_is_resolved() -> None:

    symbol_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id)
    pre = PreReviewDecision(
        memory_finding=finding, disposition=PreReviewDisposition.NEEDS_RECHECK,
        reason=TransitionReasonCode.SYMBOL_MODIFIED, detail="modified", updated_symbol_id=symbol_id,
    )
    decisions = ReviewMemoryResolver().reconcile_post_review(pre_review_decisions=[pre], current_findings=[])
    assert decisions[0].new_status is FindingMemoryStatus.RESOLVED
    assert decisions[0].reason is TransitionReasonCode.RECHECK_NO_LONGER_PRESENT


def test_reconcile_carry_forward_with_no_match_stays_zero_ai() -> None:
    """The expected, common path: a CARRY_FORWARD-dispositioned finding
    whose candidate was genuinely never selected this run (incremental
    mode skipped it) -- current_findings has nothing at its key, so the
    deterministic carry-forward is trusted as-is."""

    symbol_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id)
    pre = PreReviewDecision(
        memory_finding=finding, disposition=PreReviewDisposition.CARRY_FORWARD,
        reason=TransitionReasonCode.EVIDENCE_CONFIRMED_UNCHANGED, detail="evidence confirmed",
        updated_symbol_id=symbol_id, updated_file_path="a.py", updated_start_line=1, updated_end_line=2,
    )
    decisions = ReviewMemoryResolver().reconcile_post_review(pre_review_decisions=[pre], current_findings=[])
    assert decisions[0].new_status is FindingMemoryStatus.CARRIED_FORWARD
    assert decisions[0].reason is TransitionReasonCode.EVIDENCE_CONFIRMED_UNCHANGED
    assert decisions[0].updated_current_finding_id is None  # no new finding row exists for this run


def test_reconcile_carry_forward_but_actually_reviewed_reconciles_against_reality() -> None:
    """Regression scenario E: symbol unchanged (evidence confirmed) but
    the candidate was reviewed anyway (e.g. drift/full mode selects
    every current candidate regardless of the pre-review disposition --
    see IncrementalReviewPlanner._full_plan). The AI's actual output for
    this run must never be silently ignored in favor of the deterministic
    carry-forward -- reconcile against what was actually found, exactly
    like a NEEDS_RECHECK match would."""

    symbol_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id, message="there is a bug")
    pre = PreReviewDecision(
        memory_finding=finding, disposition=PreReviewDisposition.CARRY_FORWARD,
        reason=TransitionReasonCode.EVIDENCE_CONFIRMED_UNCHANGED, detail="evidence confirmed",
        updated_symbol_id=symbol_id, updated_file_path="a.py", updated_start_line=1, updated_end_line=2,
    )
    final = _final_finding(
        symbol_id=symbol_id, file_path="a.py", category=FindingCategory.CORRECTNESS,
        severity=Severity.HIGH, message="there is a bug",
    )
    current = _current_finding_from_final(final)
    decisions = ReviewMemoryResolver().reconcile_post_review(pre_review_decisions=[pre], current_findings=[current])
    assert decisions[0].new_status is FindingMemoryStatus.CARRIED_FORWARD
    assert decisions[0].reason is TransitionReasonCode.RECHECK_CONFIRMED  # not the deterministic reason
    assert decisions[0].updated_current_finding_id == current.id  # the real, freshly-reviewed finding row


def test_reconcile_carry_forward_but_review_found_something_different_is_changed() -> None:
    """Same drift/full-mode scenario, but this time the AI's actual
    output genuinely differs (severity changed) -- must resolve to
    CHANGED, never silently stay CARRIED_FORWARD."""

    symbol_id = uuid.uuid4()
    finding = _memory_finding(symbol_id=symbol_id, severity=Severity.MEDIUM, message="there is a bug")
    pre = PreReviewDecision(
        memory_finding=finding, disposition=PreReviewDisposition.CARRY_FORWARD,
        reason=TransitionReasonCode.EVIDENCE_CONFIRMED_UNCHANGED, detail="evidence confirmed",
        updated_symbol_id=symbol_id, updated_file_path="a.py", updated_start_line=1, updated_end_line=2,
    )
    final = _final_finding(
        symbol_id=symbol_id, file_path="a.py", category=FindingCategory.CORRECTNESS,
        severity=Severity.CRITICAL, message="there is a bug",
    )
    current = _current_finding_from_final(final)
    decisions = ReviewMemoryResolver().reconcile_post_review(pre_review_decisions=[pre], current_findings=[current])
    assert decisions[0].new_status is FindingMemoryStatus.CHANGED
    assert decisions[0].updated_current_finding_id == current.id
