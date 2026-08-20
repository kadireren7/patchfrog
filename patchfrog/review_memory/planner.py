"""Pure, deterministic incremental candidate selection.

    Phase 5's full candidate list (base...current diff, unchanged since
    Phase 5 -- Phase 7 never touches candidate *generation*)
    + previous-review selection
    + change set (previous_review_commit -> current_commit, NOT
      base -> current -- this is the key difference from Phase 5's own
      diff, and what makes this "incremental since last review" rather
      than "everything changed in the PR so far")
    + resolver's pre-review decisions
    -> which of Phase 5's candidates actually need a fresh AI look

No network, no LLM call, no database session. ``mode`` reflects only
whether candidate-skipping is actually usable (see
:class:`patchfrog.review_memory.domain.PreviousReviewSelection`) -- when
it is not (no usable previous review, or drift), every current candidate
is selected, identical to Phase 5's own behavior with Phase 7 absent.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from patchfrog.review.domain import ReviewCandidate
from patchfrog.review_memory.domain import (
    IncrementalChangeSet,
    IncrementalMetrics,
    IncrementalPlan,
    IncrementalRunMode,
    PreReviewDecision,
    PreReviewDisposition,
    PreviousReviewSelection,
    TransitionReasonCode,
)


class IncrementalReviewPlanner:
    """Stateless; safe to reuse across calls."""

    def plan(
        self,
        *,
        selection: PreviousReviewSelection,
        current_candidates: Sequence[ReviewCandidate],
        change_set: IncrementalChangeSet | None,
        pre_review_decisions: Sequence[PreReviewDecision],
        previous_candidate_count: int,
    ) -> IncrementalPlan:
        if not selection.usable_for_candidate_skipping or change_set is None:
            return self._full_plan(
                selection=selection,
                current_candidates=current_candidates,
                pre_review_decisions=pre_review_decisions,
                previous_candidate_count=previous_candidate_count,
            )

        required_symbol_ids: set[UUID] = set(change_set.changed_symbol_ids)
        for decision in pre_review_decisions:
            if decision.disposition is PreReviewDisposition.NEEDS_RECHECK and decision.updated_symbol_id is not None:
                required_symbol_ids.add(decision.updated_symbol_id)

        changed_file_paths = {c.path for c in change_set.file_changes.changes}

        selected: list[ReviewCandidate] = []
        skipped: list[ReviewCandidate] = []
        for candidate in current_candidates:
            needs_review = (
                (candidate.symbol_id is not None and candidate.symbol_id in required_symbol_ids)
                or (candidate.symbol_id is None and candidate.file_path in changed_file_paths)
            )
            (selected if needs_review else skipped).append(candidate)

        metrics = self._build_metrics(
            previous_candidate_count=previous_candidate_count,
            current_full_candidate_count=len(current_candidates),
            incremental_candidate_count=len(selected),
            candidates_skipped=len(skipped),
            pre_review_decisions=pre_review_decisions,
        )
        return IncrementalPlan(
            mode=IncrementalRunMode.INCREMENTAL,
            selection=selection,
            selected_candidates=tuple(selected),
            skipped_candidates=tuple(skipped),
            pre_review_decisions=tuple(pre_review_decisions),
            metrics=metrics,
        )

    def _full_plan(
        self,
        *,
        selection: PreviousReviewSelection,
        current_candidates: Sequence[ReviewCandidate],
        pre_review_decisions: Sequence[PreReviewDecision],
        previous_candidate_count: int,
    ) -> IncrementalPlan:
        # Drift (usable_for_finding_memory True but candidate-skipping
        # disabled) still carries pre_review_decisions through for
        # post-review reconciliation/publication-suppression purposes --
        # only the *candidate selection* falls back to "everything".
        metrics = self._build_metrics(
            previous_candidate_count=previous_candidate_count,
            current_full_candidate_count=len(current_candidates),
            incremental_candidate_count=len(current_candidates),
            candidates_skipped=0,
            pre_review_decisions=pre_review_decisions,
        )
        return IncrementalPlan(
            mode=IncrementalRunMode.FULL,
            selection=selection,
            selected_candidates=tuple(current_candidates),
            skipped_candidates=(),
            pre_review_decisions=tuple(pre_review_decisions),
            metrics=metrics,
        )

    @staticmethod
    def _build_metrics(
        *,
        previous_candidate_count: int,
        current_full_candidate_count: int,
        incremental_candidate_count: int,
        candidates_skipped: int,
        pre_review_decisions: Sequence[PreReviewDecision],
    ) -> IncrementalMetrics:
        carried = sum(1 for d in pre_review_decisions if d.disposition is PreReviewDisposition.CARRY_FORWARD)
        resolved = sum(1 for d in pre_review_decisions if d.disposition is PreReviewDisposition.RESOLVED_IMMEDIATELY)
        ambiguous = sum(
            1 for d in pre_review_decisions
            if d.disposition is PreReviewDisposition.NEEDS_RECHECK
            and d.reason is TransitionReasonCode.AMBIGUOUS_SYMBOL_MATCH
        )
        return IncrementalMetrics(
            previous_candidate_count=previous_candidate_count,
            current_full_candidate_count=current_full_candidate_count,
            incremental_candidate_count=incremental_candidate_count,
            candidates_skipped_by_memory=candidates_skipped,
            findings_carried_forward=carried,
            findings_changed=0,  # known only after post-review reconciliation
            findings_resolved=resolved,
            findings_new=0,  # known only after post-review reconciliation
            findings_ambiguous=ambiguous,
        )
