"""Cross-role duplicate merging and contradiction detection.

Two specialist agents look at the *same* shared evidence package (see
:mod:`patchfrog.review.agents.evidence`) and may both flag the same
underlying bug, described from two angles -- e.g. Correctness: "shell
argument construction is incorrect"; Security: "untrusted value reaches
shell invocation". When that happens, PatchFrog must publish at most one
user-facing finding for it (see section 6/9 of the Agent Orchestration
v1 spec), preferring the richer/more actionable categorization rather
than picking arbitrarily by execution order.

A rarer, distinct case is a genuine *contradiction*: two proposals about
the same code that assert incompatible facts (one claims a value is
sanitized/safe, the other claims it is unsanitized/unsafe). Unlike a
same-root-cause duplicate, a contradiction cannot be resolved by picking
"the richer one" -- it needs the critic to look at both claims against
the shared evidence and decide. If the critic still can't resolve it,
PatchFrog suppresses both rather than publish contradictory comments.

Every rule here is a fixed, explainable, deterministic function of the
finding text/evidence/category already in hand -- never a provider call.
"No provider call may decide dedup itself."
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.analysis.domain import Confidence, Severity
from patchfrog.review.agents.proposal import AgentProposal
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import AIReviewFinding, CriticDecision, ValidationOutcome

_SEVERITY_RANK = {
    Severity.INFO: 0, Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3, Severity.CRITICAL: 4,
}
_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}

#: One side asserts the code IS protected/safe.
_SAFETY_ASSERTION_KEYWORDS = (
    "sanitiz", "escape", "validated", "guarantee", "already checked", "already verified",
    "is safe", "is trusted", "not exploitable", "no risk", "cannot reach", "properly encoded",
)
#: The other side asserts the code is NOT protected/unsafe. Deliberately
#: does not overlap with the safety-assertion list above (e.g. neither
#: list contains a bare substring of the other) so a single piece of text
#: can't trivially match both sides.
_RISK_CLAIM_KEYWORDS = (
    "unsanitiz", "untrusted", "unvalidated", "unsafe", "not sanitized", "not escaped",
    "vulnerable", "exploit", "attacker-controlled", "injection",
)

#: Cross-role suppression reasons, recorded on
#: :attr:`AgentProposal.suppressed_reason` -- distinct strings so
#: persistence/audit can tell a cross-role merge apart from an
#: unresolved contradiction.
CROSS_ROLE_DUPLICATE = "cross_role_duplicate"
UNRESOLVED_CONTRADICTION = "unresolved_contradiction"


def _overlaps_location(a: AIReviewFinding, b: AIReviewFinding) -> bool:
    if a.file_path != b.file_path:
        return False
    return not (a.end_line < b.start_line or b.end_line < a.start_line)


def _shared_verbatim_evidence(a: AIReviewFinding, b: AIReviewFinding) -> bool:
    a_quotes = {e.quoted_text.strip() for e in a.evidence}
    b_quotes = {e.quoted_text.strip() for e in b.evidence}
    return bool(a_quotes & b_quotes)


def is_contradiction(a: AgentProposal, b: AgentProposal) -> bool:
    """Two cross-role proposals over overlapping, evidence-sharing code
    that assert lexically opposite safety claims -- e.g. one says a
    value is unsanitized, the other says a sanitizer guarantees safety.
    A narrower, stronger signal than :func:`is_same_root_cause`: sharing
    evidence and disagreeing about the *same* fact, not just describing
    the same location differently."""

    if a.role == b.role:
        return False
    fa, fb = a.validated.finding, b.validated.finding
    if not _overlaps_location(fa, fb) or not _shared_verbatim_evidence(fa, fb):
        return False

    text_a = f"{fa.message} {fa.reasoning_summary} {fa.impact or ''}".lower()
    text_b = f"{fb.message} {fb.reasoning_summary} {fb.impact or ''}".lower()
    a_risk, a_safe = _matches(text_a, _RISK_CLAIM_KEYWORDS), _matches(text_a, _SAFETY_ASSERTION_KEYWORDS)
    b_risk, b_safe = _matches(text_b, _RISK_CLAIM_KEYWORDS), _matches(text_b, _SAFETY_ASSERTION_KEYWORDS)
    return (a_risk and b_safe) or (b_risk and a_safe)


def _matches(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in text for k in keywords)


def is_same_root_cause(a: AgentProposal, b: AgentProposal) -> bool:
    """Two cross-role, valid proposals over overlapping code that are
    very likely describing the *same* underlying bug from two angles --
    never true for a genuine contradiction (see :func:`is_contradiction`,
    checked first)."""

    if a.role == b.role or is_contradiction(a, b):
        return False
    fa, fb = a.validated.finding, b.validated.finding
    if not _overlaps_location(fa, fb):
        return False
    if fa.category == fb.category:
        return True
    return _shared_verbatim_evidence(fa, fb)


def preferred(a: AgentProposal, b: AgentProposal) -> AgentProposal:
    """Deterministic winner between two same-root-cause proposals.
    Security's categorization wins when the security-role proposal is
    itself genuinely categorized as security (the shell-argument example
    in the module docstring); otherwise higher severity, then higher
    confidence, then a total, stable tie-break."""

    fa, fb = a.validated.finding, b.validated.finding

    if {a.role, b.role} == {AgentRole.SECURITY, AgentRole.CORRECTNESS}:
        security_side = a if a.role is AgentRole.SECURITY else b
        if security_side.validated.finding.category.value == "security" and fa.category != fb.category:
            return security_side

    if _SEVERITY_RANK[fa.severity] != _SEVERITY_RANK[fb.severity]:
        return a if _SEVERITY_RANK[fa.severity] > _SEVERITY_RANK[fb.severity] else b
    if _CONFIDENCE_RANK[fa.confidence] != _CONFIDENCE_RANK[fb.confidence]:
        return a if _CONFIDENCE_RANK[fa.confidence] > _CONFIDENCE_RANK[fb.confidence] else b
    return a if (fa.title, a.role.value) <= (fb.title, b.role.value) else b


@dataclass(frozen=True, slots=True)
class CrossRoleGroupingResult:
    #: Same length and order as the input -- some entries may have a new
    #: ``suppressed_reason`` (:data:`CROSS_ROLE_DUPLICATE`) set.
    proposals: tuple[AgentProposal, ...]
    #: Indices into ``proposals`` that are part of an unresolved-so-far
    #: contradiction group -- these must be force-critiqued regardless
    #: of :class:`patchfrog.review.critic_selection.CriticSelectionPolicy`.
    contradiction_indices: frozenset[int]


def group_cross_role(proposals: tuple[AgentProposal, ...]) -> CrossRoleGroupingResult:
    """Pairwise-compare every still-valid, not-yet-suppressed proposal
    pair from *different* roles for one candidate. Deterministic:
    processing order is fixed by a stable sort key, and the outcome for
    any given pair never depends on iteration order (see
    :func:`preferred`'s total ordering)."""

    valid_indices = sorted(
        (i for i, p in enumerate(proposals) if p.validated.outcome == ValidationOutcome.VALID),
        key=lambda i: (
            proposals[i].validated.finding.file_path,
            proposals[i].validated.finding.start_line,
            proposals[i].role.value,
            proposals[i].validated.finding.title,
        ),
    )

    suppressed_reason: dict[int, str] = {}
    contradiction_indices: set[int] = set()

    for pos, i in enumerate(valid_indices):
        for j in valid_indices[pos + 1 :]:
            if proposals[i].role == proposals[j].role:
                continue
            if i in suppressed_reason or j in suppressed_reason:
                continue
            a, b = proposals[i], proposals[j]
            if is_contradiction(a, b):
                contradiction_indices.add(i)
                contradiction_indices.add(j)
                continue
            if is_same_root_cause(a, b):
                winner = preferred(a, b)
                loser = j if winner is a else i
                suppressed_reason[loser] = CROSS_ROLE_DUPLICATE

    result = tuple(
        p.suppressed(suppressed_reason[k]) if k in suppressed_reason else p
        for k, p in enumerate(proposals)
    )
    return CrossRoleGroupingResult(proposals=result, contradiction_indices=frozenset(contradiction_indices))


def resolve_unresolved_contradictions(
    proposals: tuple[AgentProposal, ...], *, contradiction_indices: frozenset[int]
) -> tuple[AgentProposal, ...]:
    """Called after the critic has run on every contradiction-group
    member. If two or more members of the same contradiction group are
    still not rejected by the critic (the critic could not confidently
    say one side is wrong), suppress all of them --
    :data:`UNRESOLVED_CONTRADICTION` -- rather than publish contradictory
    comments. A contradiction group where the critic rejected all but
    one member is resolved: that survivor proceeds normally."""

    if not contradiction_indices:
        return proposals

    def _rejected(p: AgentProposal) -> bool:
        verdict = p.critic_verdict
        return verdict is not None and verdict.decision == CriticDecision.REJECT

    still_standing = [
        i for i in contradiction_indices if proposals[i].suppressed_reason is None and not _rejected(proposals[i])
    ]
    if len(still_standing) <= 1:
        return proposals

    result = list(proposals)
    for i in still_standing:
        result[i] = result[i].suppressed(UNRESOLVED_CONTRADICTION)
    return tuple(result)
