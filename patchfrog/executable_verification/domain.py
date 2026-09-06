"""Pure domain model for Executable Verification -- no I/O, no LLM
(mirrors every other Intelligence package's own ``domain.py`` role).

**Product principle**: move from "the code evidence strongly suggests
this bug" toward "where safely possible, PatchFrog executed a bounded,
targeted verification and observed evidence supporting or contradicting
the hypothesis." This is not arbitrary code execution, not a CI
replacement, not an unrestricted test runner, not a shell agent, and not
an autonomous code-modification system. See
``validation/executable_verification/latest-summary.md`` for the full
audit, threat model, and scope decision behind every constant and enum
value below.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Bumped whenever eligibility, sandbox isolation shape, or result
#: classification logic changes materially enough that a prior report
#: can no longer be considered equivalent to what re-running now would
#: produce.
EXECUTABLE_VERIFICATION_VERSION = 1

#: Shared across the whole review run (mirrors the existing
#: ``budget_lock``/``budget_state`` token-budget pattern in
#: patchfrog.review.orchestration) -- never an unbounded number of
#: executions per review.
MAX_VERIFICATIONS_PER_REVIEW = 5

#: A single verification attempt's own wall-clock ceiling, passed
#: straight through to run_sandboxed.
MAX_VERIFICATION_SECONDS = 30.0

#: The whole review run's total verification wall-clock ceiling --
#: checked before each new attempt, never just per-attempt.
MAX_TOTAL_VERIFICATION_SECONDS = 90.0

#: Bounds the evidence excerpt kept from stdout/stderr -- far below
#: run_sandboxed's own 5 MiB streaming cap; verification evidence is a
#: short excerpt for the critic, never a log dump.
MAX_STDOUT_EXCERPT_BYTES = 8192
MAX_STDERR_EXCERPT_BYTES = 8192

#: Resource limits passed to `prlimit` for the sandboxed process.
MAX_SANDBOX_PROCESSES = 64
MAX_SANDBOX_ADDRESS_SPACE_BYTES = 1_073_741_824  # 1 GiB
MAX_SANDBOX_CPU_SECONDS = 20


class VerificationKind(StrEnum):
    """Only `EXISTING_TARGETED_TEST` is ever constructed in v1 -- see
    ``validation/executable_verification/latest-summary.md`` section 5
    for why `STATIC_REPRODUCTION`/`GENERATED_TARGETED_TEST` are
    deferred. Both are kept on the enum for forward documentation only,
    exactly mirroring every other Intelligence package's own deferred
    -kind precedent."""

    EXISTING_TARGETED_TEST = "existing_targeted_test"
    #: Deferred -- see latest-summary.md section 5.
    STATIC_REPRODUCTION = "static_reproduction"
    #: Deferred -- see latest-summary.md section 5/E20. A generated
    #: test's own correctness is an unverified variable; validating
    #: that correctly is a materially larger project than this
    #: foundation.
    GENERATED_TARGETED_TEST = "generated_targeted_test"


class VerificationOutcome(StrEnum):
    """A failing command alone is never automatically a confirmed bug
    -- see latest-summary.md section 8 for the exact classification
    rules empirically verified against a real sandbox."""

    #: The known, targeted test file genuinely failed on this exact
    #: head, after collection (dependency availability) already
    #: succeeded.
    CONFIRMED_FAILURE = "confirmed_failure"
    #: The known, targeted test file passed. Never treated as universal
    #: proof the hypothesis is wrong -- the test may not exercise the
    #: specific claim.
    PASSED = "passed"
    #: run_sandboxed's own timed_out flag fired. Never treated as a bug.
    TIMEOUT = "timeout"
    #: Collection itself failed (missing dependency, usage error, no
    #: tests found) -- the real test was never attempted.
    UNSUPPORTED = "unsupported"
    #: An unexpected but non-fatal outcome (e.g. an exit code collection
    #: should have already ruled out) -- no conclusion drawn.
    INCONCLUSIVE = "inconclusive"
    #: The sandbox itself could not run (unshare/prlimit unavailable,
    #: snapshot acquisition failed) -- fail closed, never an unisolated
    #: fallback.
    SANDBOX_ERROR = "sandbox_error"


@dataclass(frozen=True, slots=True)
class ExecutableVerificationEvidence:
    """Bounded, redacted evidence from one verification attempt. Never
    persists full unrestricted logs, environment dumps, credentials, or
    arbitrary binary output -- only a short, size-capped excerpt."""

    kind: VerificationKind
    outcome: VerificationOutcome
    test_target_path: str
    commit_sha: str
    exit_code: int | None
    stdout_excerpt: str
    stderr_excerpt: str
    duration_ms: float
    timed_out: bool


@dataclass(frozen=True, slots=True)
class ExecutableVerificationReport:
    """The result for one candidate's one verification attempt --
    unlike every other Intelligence package, this is deliberately
    per-candidate, not per-review-run: verification only ever runs
    after a real specialist proposal exists for one specific candidate
    (latest-summary.md section 7), so there is no meaningful
    "review-run-level" report shape to build ahead of time."""

    version: int
    attempted: bool
    evidence: ExecutableVerificationEvidence | None = None
