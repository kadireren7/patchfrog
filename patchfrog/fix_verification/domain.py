"""Domain model for Fix Verification -- Milestone T (T3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

#: A new, independent semantic contract for what FIXED/STILL_PRESENT/
#: INCONCLUSIVE/STALE mean -- distinct from REVIEW_ENGINE_VERSION (normal
#: review candidate/critic/dedup semantics, untouched by this milestone)
#: and VERIFIER_PROTOCOL_VERSION (the wire contract this package's own
#: Executable Verification step reuses unmodified). Bump only when the
#: classification algorithm itself changes in a way that makes an old
#: persisted result's meaning no longer comparable to a new one.
FIX_VERIFICATION_VERSION = 1

#: Part AE -- bounded, enforceable cost controls. A repository with more
#: than this many attempts concurrently in PENDING/VERIFYING is refused a
#: new one (Part AE: "avoid unlimited parallel verifier/provider work").
MAX_ACTIVE_FIX_ATTEMPTS_PER_REPOSITORY = 5

#: Part AE -- a single finding cannot accumulate unbounded fix-attempt
#: history (denial-of-service via repeated verification spam).
MAX_FIX_ATTEMPTS_PER_FINDING = 20


class FixAttemptStatus(StrEnum):
    """Terminal and non-terminal states of one fix attempt. Only the
    states this milestone's algorithm can actually produce -- no
    speculative states for a future capability."""

    PENDING = "pending"
    VERIFYING = "verifying"
    #: The original finding's condition no longer holds under current
    #: evidence -- never "the agent says it fixed it" (Part V).
    FIXED = "fixed"
    #: The original finding's condition still holds under current
    #: evidence.
    STILL_PRESENT = "still_present"
    #: Insufficient evidence to decide either way -- never forced to
    #: FIXED or STILL_PRESENT when the evidence genuinely doesn't say.
    INCONCLUSIVE = "inconclusive"
    #: ``candidate_fix_commit_sha`` could not be proven to be a
    #: descendant of ``original_commit_sha`` -- the two are not validly
    #: comparable (Part V).
    STALE = "stale"
    #: An infrastructure failure (clone/network/git error) prevented
    #: verification from running at all -- distinct from every semantic
    #: outcome above; never silently reported as INCONCLUSIVE.
    ERROR = "error"


class FixEvidenceDirection(StrEnum):
    """Security correction (post-review): the strength/direction of one
    deterministic (or LLM) signal toward or against ``FIXED`` -- never a
    bare ``bool``, so a weak signal can never be silently promoted into a
    terminal ``FIXED`` state by accident. See
    ``validation/agent_handoff/latest-summary.md`` for why "a passing
    targeted test" and "a static rule no longer firing nearby" were
    originally (incorrectly) both treated as sufficient proof of a fix.

    Combination rule (``FixVerificationService._classify``): any
    ``CONFIRMS_PRESENT`` signal wins outright -> ``STILL_PRESENT``,
    conservative by design. Only a ``PROVES_RESOLVED`` signal (with
    nothing contradicting) may produce ``FIXED`` directly. A
    ``SUPPORTS_RESOLVED`` signal *alone* is never sufficient for
    ``FIXED`` -- it only makes the bounded LLM fallback available (never
    skips straight to ``FIXED``, and the fallback's own conservative
    instructions are the actual decision, never this signal by itself).
    """

    #: Strong: proves the original condition still holds. Always wins.
    CONFIRMS_PRESENT = "confirms_present"
    #: Weak: consistent with a fix, but not proof by itself (e.g. a
    #: passing targeted test, or a static rule absent at a safely mapped
    #: surface -- neither rules out a rule-taxonomy mismatch, an
    #: insufficiently targeted test, or the bug simply moving).
    SUPPORTS_RESOLVED = "supports_resolved"
    #: Strong: deterministic proof the original condition no longer
    #: holds. Reserved for a genuinely finding-specific invariant -- in
    #: v1, no static/Executable-Verification signal is strong enough to
    #: reach this (kept, rather than omitted, so the type itself makes
    #: "weak evidence promoted to certain" impossible to do by accident;
    #: see Part V's "what stronger evidence may include" list).
    PROVES_RESOLVED = "proves_resolved"
    #: No signal either way.
    NO_SIGNAL = "no_signal"


#: Non-terminal -- a fix attempt in either of these states is still being
#: worked on and should not be counted as "done" for idempotency/read
#: purposes.
ACTIVE_STATUSES = frozenset({FixAttemptStatus.PENDING, FixAttemptStatus.VERIFYING})

#: Terminal -- every status a fix attempt can end in.
TERMINAL_STATUSES = frozenset(
    {
        FixAttemptStatus.FIXED,
        FixAttemptStatus.STILL_PRESENT,
        FixAttemptStatus.INCONCLUSIVE,
        FixAttemptStatus.STALE,
        FixAttemptStatus.ERROR,
    }
)


@dataclass(frozen=True, slots=True)
class FixAttempt:
    """One durable fix-attempt record (Part AG) -- identity is
    ``(handoff_id, candidate_fix_commit_sha)``, enforced as a unique index
    at the persistence layer (Part AF: idempotent, never a duplicate
    concurrent verification for the same pair)."""

    fix_attempt_id: UUID
    handoff_id: str
    finding_id: UUID
    repository_id: UUID
    original_commit_sha: str
    candidate_fix_commit_sha: str
    status: FixAttemptStatus
    created_at: datetime
    completed_at: datetime | None
    #: Populated only once verification has run (status is terminal) --
    #: see :class:`FixVerificationResult`. ``None`` while PENDING/VERIFYING.
    result: FixVerificationResult | None = None


@dataclass(frozen=True, slots=True)
class FixVerificationResult:
    """The bounded, structured outcome of one verification run -- Part AI.
    Never internal chain-of-thought; ``deterministic_evidence`` is a short
    list of plain-English evidence labels (e.g. "file unchanged between
    commits"), not a log."""

    fix_attempt_id: UUID
    finding_id: UUID
    original_commit_sha: str
    candidate_fix_commit_sha: str
    status: FixAttemptStatus
    version: int = FIX_VERIFICATION_VERSION
    deterministic_evidence: tuple[str, ...] = field(default_factory=tuple)
    executable_verification_outcome: str | None = None
    #: A short, honest summary of what still appears wrong -- only ever
    #: set for STILL_PRESENT, and only ever derived from evidence already
    #: computed above, never fabricated.
    remaining_issue_summary: str | None = None
    #: What this run did *not* prove -- e.g. "did not re-execute against
    #: the original commit" (see
    #: ``validation/agent_handoff/latest-summary.md`` section 1.3) --
    #: printed so a consumer never overtrusts a bounded result.
    limitations: tuple[str, ...] = field(default_factory=tuple)
