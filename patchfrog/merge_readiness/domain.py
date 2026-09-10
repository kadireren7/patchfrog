"""Merge Readiness domain -- Milestone V.

:class:`MergeReadinessDecision` is a synthesis layer over PatchFrog's own
already-persisted evidence, never a new review engine: it answers
"given everything PatchFrog knows about THIS exact PR head, what action
does the evidence support?" It does **not** generate findings, create an
arbitrary numeric score, replace GitHub branch protection, guarantee
correctness, or automatically merge/approve.

**Exact-head binding is the core invariant**: a :class:`MergeReadinessResult`
belongs to one specific ``head_sha``. If the PR's head has since moved,
the result must never be shown as current -- see
:meth:`patchfrog.merge_readiness.service.MergeReadinessService.evaluate`,
which always recomputes fresh rather than returning a persisted result
for a stale head.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

#: Bumped only when this module's own durable semantic contract changes
#: materially (decision precedence, reason-code meaning, or the
#: persisted shape below) -- never mechanically. See the final report
#: for this milestone's own bump/non-bump justification.
MERGE_READINESS_VERSION = 1


class MergeReadinessDecision(StrEnum):
    """Exactly three outcomes -- deliberately no ``UNKNOWN``:
    ``HUMAN_REVIEW_REQUIRED`` already honestly represents every case
    where PatchFrog cannot safely decide, so a fourth "I don't know"
    state would only duplicate it."""

    #: PatchFrog completed the required review for this exact head and
    #: found no currently unresolved evidence that requires blocking or
    #: human escalation under the current policy. **Never** "PatchFrog
    #: guarantees this code is correct."
    READY = "ready"
    #: At least one strong, concrete, unresolved blocking finding exists
    #: at this exact head (see :mod:`patchfrog.merge_readiness.service`'s
    #: own precedence rules for exactly what qualifies).
    BLOCKED = "blocked"
    #: PatchFrog cannot safely decide READY or BLOCKED -- the review is
    #: stale/incomplete, evidence conflicts, or a high-impact finding's
    #: status is genuinely ambiguous. This is an expected, healthy
    #: outcome, not a failure mode to eliminate.
    HUMAN_REVIEW_REQUIRED = "human_review_required"


class MergeReadinessReasonCode(StrEnum):
    """Bounded, typed rationale -- never freeform prose."""

    #: No unresolved evidence exists at this exact head -- the only
    #: reason code that ever accompanies READY.
    NO_UNRESOLVED_EVIDENCE = "no_unresolved_evidence"
    #: At least one accepted, still-open CRITICAL/HIGH finding (or a
    #: MEDIUM security finding treated as concrete enough, see the
    #: service's own severity policy) has no same-exact-head FIXED
    #: FixAttempt and no human dismissal.
    UNRESOLVED_BLOCKING_FINDING = "unresolved_blocking_finding"
    #: No review run exists at this exact PR head at all (the PR has
    #: never been reviewed at this SHA, or the head has moved since the
    #: last review that ran).
    STALE_REVIEW = "stale_review"
    #: A review run exists at this exact head, but it did not finish
    #: (``RUNNING``) or a required step failed (``FAILED``/``PARTIAL``)
    #: -- "no findings" here never means "no unresolved evidence."
    REVIEW_INCOMPLETE = "review_incomplete"
    #: A would-be-blocking finding's only fix evidence at this exact
    #: head is a ``FixAttempt`` that resolved ``INCONCLUSIVE`` -- neither
    #: still-blocking proof nor a clearance, so neither BLOCKED nor
    #: READY is honest.
    HIGH_IMPACT_INCONCLUSIVE = "high_impact_inconclusive"
    #: A blocking finding has an in-progress or unresolved fix
    #: verification at this exact head (``PENDING``/``VERIFYING``) --
    #: distinct from ``HIGH_IMPACT_INCONCLUSIVE`` (a completed but
    #: undecided verification).
    BLOCKING_FIX_NOT_VERIFIED = "blocking_fix_not_verified"


@dataclass(frozen=True, slots=True)
class MergeReadinessResult:
    """One readiness decision, always bound to an exact commit SHA."""

    decision: MergeReadinessDecision
    reason_codes: tuple[MergeReadinessReasonCode, ...]
    repository_id: UUID
    pull_request_number: int
    #: ``None`` only when :data:`MergeReadinessReasonCode.STALE_REVIEW`
    #: applies because no review run exists at all for this PR.
    review_run_id: UUID | None
    head_sha: str
    #: The specific findings this decision turns on -- bounded to
    #: whichever findings are actually blocking/escalating, never every
    #: finding on the run.
    finding_ids: tuple[UUID, ...]
    limitations: tuple[str, ...]
    version: int = MERGE_READINESS_VERSION
