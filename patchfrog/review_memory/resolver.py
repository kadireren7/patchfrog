"""Pure, deterministic review-memory resolution.

Two phases, matching the Phase 7 spec's own split between "what needs a
fresh AI look" (decided before any provider call) and "what actually
happened to it" (decided after the AI review's final findings are
known):

    resolve_pre_review(previous findings, change set)
        -> per finding: carry forward / needs recheck / resolved immediately

    [ ... IncrementalReviewPlanner selects candidates for NEEDS_RECHECK
      symbols; PullRequestReviewService runs the actual AI review ... ]

    reconcile_post_review(pre-review decisions, current final findings)
        -> per finding: final MemoryFindingDecision (carried_forward /
           changed / resolved), always with a reason code

No network, no database session, no LLM call in either phase -- both
take plain values and return plain values, so both are fully
unit-testable. The *only* judgment calls resolved here are the
deterministic ones the spec allows (symbol continuity, revalidation);
anything genuinely uncertain resolves to ``NEEDS_RECHECK``/``AMBIGUOUS``,
never a guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from patchfrog.review_memory.domain import (
    CurrentFinding,
    FindingMemoryStatus,
    IncrementalChangeSet,
    MemoryFindingDecision,
    PreReviewDecision,
    PreReviewDisposition,
    ReviewMemoryFinding,
    SymbolContinuityResult,
    SymbolContinuityStatus,
    TransitionReasonCode,
)
from patchfrog.review_memory.symbol_continuity import build_rename_map


class ReviewMemoryResolver:
    """Stateless; safe to reuse across calls."""

    def resolve_pre_review(
        self,
        *,
        previous_findings: Sequence[ReviewMemoryFinding],
        change_set: IncrementalChangeSet,
        evidence_still_present: Mapping[str, bool] | None = None,
    ) -> list[PreReviewDecision]:
        """``evidence_still_present`` maps ``memory_finding.id`` (as a
        string, for plain-dict testability) to whether the finding's
        evidence snippet was independently confirmed still present at
        its (possibly remapped) location in the current commit -- see
        the "Revalidation" section of the spec. Omitted/``None`` entries
        are treated as "not confirmed" (fail closed: needs recheck)."""

        evidence_still_present = evidence_still_present or {}
        by_previous_symbol_id: dict[UUID, SymbolContinuityResult] = {
            r.previous.id: r for r in change_set.symbol_changes if r.previous is not None
        }
        rename_map = build_rename_map(change_set.file_changes)
        deleted_paths = rename_map.deleted_paths
        renamed_paths = rename_map.old_to_new

        decisions: list[PreReviewDecision] = []
        for finding in previous_findings:
            decisions.append(
                self._resolve_one(
                    finding,
                    by_previous_symbol_id=by_previous_symbol_id,
                    deleted_paths=deleted_paths,
                    renamed_paths=renamed_paths,
                    evidence_confirmed=evidence_still_present.get(str(finding.id), False),
                )
            )
        return decisions

    def _resolve_one(
        self,
        finding: ReviewMemoryFinding,
        *,
        by_previous_symbol_id: Mapping[UUID, SymbolContinuityResult],
        deleted_paths: frozenset[str],
        renamed_paths: Mapping[str, str],
        evidence_confirmed: bool,
    ) -> PreReviewDecision:
        if finding.symbol_id is None:
            # Module-region finding -- no symbol to track continuity for;
            # fall back to file-level classification only.
            effective_path = renamed_paths.get(finding.file_path, finding.file_path)
            if effective_path in deleted_paths:
                return PreReviewDecision(
                    memory_finding=finding, disposition=PreReviewDisposition.RESOLVED_IMMEDIATELY,
                    reason=TransitionReasonCode.FILE_DELETED, detail=f"{finding.file_path} deleted",
                )
            if effective_path != finding.file_path:
                # File renamed, content presumed reachable at the new
                # path -- still needs revalidation, never assumed.
                return PreReviewDecision(
                    memory_finding=finding, disposition=PreReviewDisposition.NEEDS_RECHECK,
                    reason=TransitionReasonCode.FILE_RENAMED, detail=f"{finding.file_path} -> {effective_path}",
                    updated_file_path=effective_path,
                )
            return PreReviewDecision(
                memory_finding=finding, disposition=PreReviewDisposition.NEEDS_RECHECK,
                reason=TransitionReasonCode.EVIDENCE_REGION_CHANGED,
                detail="module-level finding with no tracked symbol -- always rechecked",
            )

        continuity = by_previous_symbol_id.get(finding.symbol_id)
        if continuity is None:
            # The symbol this finding was attached to isn't even present
            # in the previous index's symbol-change comparison -- history
            # is incomplete for this finding. Fail closed.
            return PreReviewDecision(
                memory_finding=finding, disposition=PreReviewDisposition.NEEDS_RECHECK,
                reason=TransitionReasonCode.PREVIOUS_FINDING_MISSING,
                detail="no symbol continuity result for this finding's symbol",
            )

        return self._resolve_from_continuity(finding, continuity, evidence_confirmed=evidence_confirmed)

    def _resolve_from_continuity(
        self, finding: ReviewMemoryFinding, continuity: SymbolContinuityResult, *, evidence_confirmed: bool
    ) -> PreReviewDecision:
        status = continuity.status

        if status is SymbolContinuityStatus.DELETED:
            return PreReviewDecision(
                memory_finding=finding, disposition=PreReviewDisposition.RESOLVED_IMMEDIATELY,
                reason=TransitionReasonCode.SYMBOL_DELETED, detail=continuity.reason,
            )

        if status is SymbolContinuityStatus.AMBIGUOUS:
            return PreReviewDecision(
                memory_finding=finding, disposition=PreReviewDisposition.NEEDS_RECHECK,
                reason=TransitionReasonCode.AMBIGUOUS_SYMBOL_MATCH, detail=continuity.reason,
            )

        if status is SymbolContinuityStatus.MODIFIED:
            return PreReviewDecision(
                memory_finding=finding, disposition=PreReviewDisposition.NEEDS_RECHECK,
                reason=TransitionReasonCode.SYMBOL_MODIFIED, detail=continuity.reason,
                updated_symbol_id=continuity.current.id if continuity.current else None,
                updated_file_path=continuity.current.file_path if continuity.current else None,
            )

        # UNCHANGED, MOVED, or RENAMED: the symbol's body is byte-identical
        # to what this finding was originally raised against. Carry
        # forward only if revalidation independently confirms the
        # evidence snippet is still exactly where expected -- never on
        # symbol-continuity alone.
        assert continuity.current is not None
        current = continuity.current
        line_offset = current.start_line - (continuity.previous.start_line if continuity.previous else current.start_line)
        updated_start = finding.start_line + line_offset
        updated_end = finding.end_line + line_offset

        if not evidence_confirmed:
            return PreReviewDecision(
                memory_finding=finding, disposition=PreReviewDisposition.NEEDS_RECHECK,
                reason=TransitionReasonCode.EVIDENCE_REGION_CHANGED,
                detail="symbol body unchanged, but evidence snippet could not be reconfirmed at the mapped location",
                updated_symbol_id=current.id, updated_file_path=current.file_path,
                updated_start_line=updated_start, updated_end_line=updated_end,
            )

        # Evidence independently, deterministically reconfirmed against
        # the exact current commit -- zero AI call needed. Always this
        # one explicit reason code, whatever the underlying continuity
        # classification (unchanged/moved/renamed) was; that classification
        # is preserved in ``detail`` for traceability, never folded into
        # the reason code itself (see TransitionReasonCode.EVIDENCE_CONFIRMED_UNCHANGED).
        return PreReviewDecision(
            memory_finding=finding, disposition=PreReviewDisposition.CARRY_FORWARD,
            reason=TransitionReasonCode.EVIDENCE_CONFIRMED_UNCHANGED,
            detail=f"evidence reconfirmed verbatim at current location ({status.value}: {continuity.reason})",
            updated_symbol_id=current.id, updated_file_path=current.file_path,
            updated_start_line=updated_start, updated_end_line=updated_end,
        )

    def reconcile_post_review(
        self,
        *,
        pre_review_decisions: Sequence[PreReviewDecision],
        current_findings: Sequence[CurrentFinding],
    ) -> list[MemoryFindingDecision]:
        """Combine pre-review dispositions with the actual current run's
        final findings (queried fresh after persistence -- see
        :class:`~patchfrog.review_memory.domain.CurrentFinding`) into the
        complete, final per-finding decision. ``current_findings`` is
        matched against a rechecked finding by symbol identity (or file
        path for module-level findings) plus category -- never by
        re-deriving a fingerprint from a qualified name that may itself
        have changed (a rename is already resolved via ``updated_symbol_id``,
        not by name comparison)."""

        by_symbol_and_category: dict[tuple[UUID | str, str], list[CurrentFinding]] = {}
        for cf in current_findings:
            key = (cf.symbol_id or cf.file_path, cf.category.value)
            by_symbol_and_category.setdefault(key, []).append(cf)

        decisions: list[MemoryFindingDecision] = []
        for pre in pre_review_decisions:
            if pre.disposition is PreReviewDisposition.RESOLVED_IMMEDIATELY:
                decisions.append(
                    MemoryFindingDecision(
                        memory_finding=pre.memory_finding, new_status=FindingMemoryStatus.RESOLVED,
                        reason=pre.reason, detail=pre.detail,
                    )
                )
                continue

            key = (
                pre.updated_symbol_id or pre.updated_file_path or pre.memory_finding.file_path,
                pre.memory_finding.category.value,
            )
            matches = by_symbol_and_category.get(key, [])

            if pre.disposition is PreReviewDisposition.CARRY_FORWARD:
                if not matches:
                    # The normal, expected path: this candidate was
                    # genuinely never sent to the reviewer this run --
                    # zero AI calls, trust the deterministic carry-forward
                    # as-is, evidence unchanged.
                    decisions.append(
                        MemoryFindingDecision(
                            memory_finding=pre.memory_finding, new_status=FindingMemoryStatus.CARRIED_FORWARD,
                            reason=pre.reason, detail=pre.detail,
                            updated_file_path=pre.updated_file_path, updated_start_line=pre.updated_start_line,
                            updated_end_line=pre.updated_end_line, updated_symbol_id=pre.updated_symbol_id,
                        )
                    )
                    continue
                # A CARRY_FORWARD-dispositioned candidate was reviewed
                # anyway -- e.g. drift/full mode selects every current
                # candidate regardless of this pre-review disposition
                # (see IncrementalReviewPlanner._full_plan). Never trust
                # the deterministic carry-forward over what the AI
                # actually produced this run; reconcile against reality
                # exactly like a NEEDS_RECHECK match.
                decisions.append(self._reconcile_against_match(pre, matches[0]))
                continue

            # NEEDS_RECHECK.
            if not matches:
                decisions.append(
                    MemoryFindingDecision(
                        memory_finding=pre.memory_finding, new_status=FindingMemoryStatus.RESOLVED,
                        reason=TransitionReasonCode.RECHECK_NO_LONGER_PRESENT,
                        detail=f"rechecked ({pre.reason.value}); no equivalent finding survived review",
                        updated_file_path=pre.updated_file_path, updated_symbol_id=pre.updated_symbol_id,
                    )
                )
                continue

            decisions.append(self._reconcile_against_match(pre, matches[0]))

        return decisions

    @staticmethod
    def _reconcile_against_match(pre: PreReviewDecision, match: CurrentFinding) -> MemoryFindingDecision:
        same_severity = match.severity == pre.memory_finding.severity
        same_message = _normalize(match.message) == _normalize(pre.memory_finding.message)
        if same_severity and same_message:
            return MemoryFindingDecision(
                memory_finding=pre.memory_finding, new_status=FindingMemoryStatus.CARRIED_FORWARD,
                reason=TransitionReasonCode.RECHECK_CONFIRMED,
                detail=f"rechecked ({pre.reason.value}); AI review reproduced an equivalent finding",
                updated_file_path=match.file_path, updated_start_line=match.start_line,
                updated_end_line=match.end_line, updated_symbol_id=match.symbol_id or pre.updated_symbol_id,
                updated_current_finding_id=match.id, updated_evidence=match.evidence,
            )
        return MemoryFindingDecision(
            memory_finding=pre.memory_finding, new_status=FindingMemoryStatus.CHANGED,
            reason=TransitionReasonCode.RECHECK_CONFIRMED,
            detail=f"rechecked ({pre.reason.value}); same category, different severity/message",
            updated_file_path=match.file_path, updated_start_line=match.start_line,
            updated_end_line=match.end_line, updated_symbol_id=match.symbol_id or pre.updated_symbol_id,
            updated_current_finding_id=match.id, updated_evidence=match.evidence,
        )


def _normalize(text: str) -> str:
    return " ".join(text.split()).strip().lower()
