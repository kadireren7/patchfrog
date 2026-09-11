"""Y4: bounded, advisory-only personalization effects derived from
durable learning records.

**Scope note** (see ``validation/org_learning_governance/latest-summary.md``
section 4.3): these are pure, fully-tested functions with a stable
signature ready for integration into
:mod:`patchfrog.review.service`'s existing scheduling-order hint pattern
(the same one Trajectory/Cross-PR/Cross-Repo Intelligence already use --
``_requires_critic``'s ``OR`` chain) and its existing per-candidate
evidence-text assembly (the same pattern Milestone O's own
``evidence_text_for_candidate`` uses). Wiring into that specific,
extremely dense, heavily-tested 1800+ line file is a deliberate,
documented follow-up, not done in this milestone -- these functions are
already safe to call and fully covered on their own.

**Every allowed effect here is advisory or ordering-only -- never a hard
filter, never a severity change, never a suppression that removes a
candidate from review.** This is a structural guarantee, not just a
convention: neither function below accepts or returns anything that
could set severity, select a provider, or produce a readiness decision
(see the type signatures -- there is no such parameter to pass).
"""

from __future__ import annotations

from enum import StrEnum

from patchfrog.learning_records.domain import (
    LearningMaturity,
    LearningType,
    RepositoryLearningRecord,
)

#: Bounds how much advisory noise-history text ever reaches a prompt --
#: mirrors every other Intelligence layer's own evidence-text bound.
MAX_NOISE_ADVISORY_CHARS = 400


class RepositoryLearningReviewHint(StrEnum):
    """Mirrors ``TrajectoryReviewHint``/``CrossPRReviewHint``/
    ``CrossRepoReviewHint``'s own two-value shape exactly -- see
    :mod:`patchfrog.trajectory_intelligence.domain`."""

    NONE = "none"
    #: A candidate on a surface with an ESTABLISHED USEFUL_FINDING_PATTERN
    #: learning is reviewed earlier in the run (scheduling order only --
    #: never a change to which candidates are reviewed, never a token/
    #: budget change). Mirrors the existing hint pattern's own
    #: INCREASE_CANDIDATE_PRIORITY semantics precisely.
    INCREASE_CANDIDATE_PRIORITY = "increase_candidate_priority"


def select_review_hint(
    records: tuple[RepositoryLearningRecord, ...], *, file_path: str, qualified_name: str | None
) -> RepositoryLearningReviewHint:
    """Deterministic, O(records)-per-candidate lookup -- no LLM, no I/O.
    Only ``ESTABLISHED`` (never ``CANDIDATE``, never ``RETIRED``) useful
    patterns ever produce a hint: a single-occasion or contradicted
    pattern must never influence scheduling."""

    if qualified_name is None:
        return RepositoryLearningReviewHint.NONE
    for record in records:
        if (
            record.learning_type is LearningType.USEFUL_FINDING_PATTERN
            and record.maturity is LearningMaturity.ESTABLISHED
            and record.surface.file_path == file_path
            and record.surface.qualified_name == qualified_name
        ):
            return RepositoryLearningReviewHint.INCREASE_CANDIDATE_PRIORITY
    return RepositoryLearningReviewHint.NONE


def noise_advisory_text_for_candidate(
    records: tuple[RepositoryLearningRecord, ...], *, file_path: str, qualified_name: str | None
) -> str:
    """Bounded, advisory-only context text for a candidate matching an
    active (``CANDIDATE`` or ``ESTABLISHED``, never ``RETIRED``)
    ``NOISE_SUPPRESSION`` learning -- informs the reviewer/critic that
    this exact surface has repeatedly received false-positive feedback
    here, so they can weigh a speculative finding accordingly. **Never**
    removes the candidate, never lowers its severity, never skips the
    provider call -- the candidate is still reviewed exactly as it would
    be without this text; only the prompt gains one bounded, factual
    sentence of history (Y5: "Strong deterministic/security evidence
    still wins" is true by construction, since nothing here can touch
    evidence at all, only add advisory prose)."""

    if qualified_name is None:
        return ""
    for record in records:
        if (
            record.learning_type is LearningType.NOISE_SUPPRESSION
            and record.maturity is not LearningMaturity.RETIRED
            and record.surface.file_path == file_path
            and record.surface.qualified_name == qualified_name
        ):
            text = (
                f"Repository history: findings on {file_path}::{qualified_name} have been marked "
                f"false-positive in {record.support_count} independent past review(s). Speculative "
                "findings here should meet a higher evidentiary bar; this never excuses a confirmed "
                "defect, a security finding, or a static/executable-verification-confirmed issue."
            )
            return text[:MAX_NOISE_ADVISORY_CHARS]
    return ""
