"""Verifier wire protocol -- Milestone S6, Production Execution Enablement.

A narrow, independently-versioned request/result contract for handing a
*bounded* verification job from the trusted review worker to a
credential-minimal verifier process/container over the existing Celery/
Redis queue. See ``validation/production_execution/latest-summary.md``
for the full trust-boundary audit and architecture decision.

**This is transport only.** The verifier's own execution logic is
entirely unchanged from Milestone S --
:func:`patchfrog.executable_verification.service.build_executable_verification_report`
is called exactly as before; only *which process* calls it, and *how the
repository snapshot gets there*, are new. ``ExecutableVerificationReport``/
``ExecutableVerificationEvidence`` remain the one, unconverted true result
model everywhere except this thin wire boundary -- never a second,
competing result model.

**Deliberately excluded from this contract** (spec Part F's own explicit
list): no provider key, no GitHub App private key, no arbitrary
environment, no arbitrary shell command, no arbitrary mounts, no PR body,
no raw secrets. A request carries only what execution genuinely needs:
identity, exact commit SHA, verification kind/target, a pre-staged
snapshot location, and bounded timeout data. Snapshot integrity is
verified from the commit SHA itself against real on-disk content (see
:mod:`patchfrog.executable_verification.snapshot_staging`) -- no
separate digest field is needed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from patchfrog.executable_verification.domain import VerificationKind, VerificationOutcome

#: Independently versioned from EXECUTABLE_VERIFICATION_VERSION -- this
#: describes the *wire shape* of the request/result contract crossing the
#: review-worker/verifier process boundary, not the execution/report
#: semantic contract EXECUTABLE_VERIFICATION_VERSION already owns. Bump
#: only when a field is added/removed/reinterpreted in a way that makes an
#: old request or result no longer safely parseable by the other side.
VERIFIER_PROTOCOL_VERSION = 1

#: The Celery queue name the verifier-only worker consumes and the review
#: worker never does -- see apps/verifier/celery_app.py. A fixed constant,
#: never repository- or environment-derived, so a malicious .patchfrog.yml
#: can never redirect verification work onto an unintended queue.
VERIFICATION_QUEUE_NAME = "patchfrog-verification"

#: The Celery task name the verifier registers and the review worker
#: dispatches to *by name* (celery_app.send_task) -- the review worker
#: never imports apps.verifier's own task module, so it never needs (and
#: never gets) any of the verifier's own credential-minimal settings.
VERIFICATION_TASK_NAME = "patchfrog.verify_candidate"


def compute_request_id(
    *,
    repository_id: str,
    review_run_id: str,
    commit_sha: str,
    file_path: str,
    qualified_name: str | None,
    verification_kind: VerificationKind,
    test_target_path: str,
) -> str:
    """A deterministic identity for one logical verification request --
    used as the Celery task_id itself (Part O: retry idempotency, never
    cross-head result caching, which Milestone S already forbids and this
    does not reopen). The same logical request (same review run, same
    exact head, same candidate surface, same target) always produces the
    same id; a different head, run, or candidate always produces a
    different one."""

    raw = "|".join(
        (
            repository_id,
            review_run_id,
            commit_sha,
            file_path,
            qualified_name or "",
            verification_kind.value,
            test_target_path,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VerificationExecutionRequest:
    """Everything the verifier needs, and nothing more. Never carries a
    provider key, GitHub credential, arbitrary environment, or arbitrary
    command -- see this module's own docstring."""

    request_id: str
    protocol_version: int
    repository_id: str
    review_run_id: str
    commit_sha: str
    verification_kind: VerificationKind
    test_target_path: str
    snapshot_path: str
    timeout_seconds: float

    def to_wire(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "protocol_version": self.protocol_version,
            "repository_id": self.repository_id,
            "review_run_id": self.review_run_id,
            "commit_sha": self.commit_sha,
            "verification_kind": self.verification_kind.value,
            "test_target_path": self.test_target_path,
            "snapshot_path": self.snapshot_path,
            "timeout_seconds": self.timeout_seconds,
        }

    @staticmethod
    def from_wire(data: dict[str, object]) -> VerificationExecutionRequest:
        return VerificationExecutionRequest(
            request_id=str(data["request_id"]),
            protocol_version=int(data["protocol_version"]),  # type: ignore[call-overload]
            repository_id=str(data["repository_id"]),
            review_run_id=str(data["review_run_id"]),
            commit_sha=str(data["commit_sha"]),
            verification_kind=VerificationKind(str(data["verification_kind"])),
            test_target_path=str(data["test_target_path"]),
            snapshot_path=str(data["snapshot_path"]),
            timeout_seconds=float(data["timeout_seconds"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class VerificationExecutionResult:
    """The verifier's own bounded reply -- never a filesystem dump, an
    environment dump, or unlimited logs (Part K's own explicit
    prohibition). Carries exactly the fields
    :class:`~patchfrog.executable_verification.domain.ExecutableVerificationEvidence`
    already carries, plus the identity fields the review worker needs to
    check result authenticity (Part L) before trusting it."""

    request_id: str
    protocol_version: int
    commit_sha: str
    verification_kind: VerificationKind
    test_target_path: str
    outcome: VerificationOutcome
    exit_code: int | None
    stdout_excerpt: str
    stderr_excerpt: str
    duration_ms: float
    timed_out: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "protocol_version": self.protocol_version,
            "commit_sha": self.commit_sha,
            "verification_kind": self.verification_kind.value,
            "test_target_path": self.test_target_path,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
        }

    @staticmethod
    def from_wire(data: dict[str, object]) -> VerificationExecutionResult:
        return VerificationExecutionResult(
            request_id=str(data["request_id"]),
            protocol_version=int(data["protocol_version"]),  # type: ignore[call-overload]
            commit_sha=str(data["commit_sha"]),
            verification_kind=VerificationKind(str(data["verification_kind"])),
            test_target_path=str(data["test_target_path"]),
            outcome=VerificationOutcome(str(data["outcome"])),
            exit_code=data["exit_code"],  # type: ignore[arg-type]
            stdout_excerpt=str(data["stdout_excerpt"]),
            stderr_excerpt=str(data["stderr_excerpt"]),
            duration_ms=float(data["duration_ms"]),  # type: ignore[arg-type]
            timed_out=bool(data["timed_out"]),
        )


def result_matches_request(
    *, request: VerificationExecutionRequest, result: VerificationExecutionResult
) -> bool:
    """Result authenticity check (Part L): the trusted review worker must
    never accept a result whose identity doesn't match what it actually
    asked for. No cryptographic signing -- the transport is the
    operator's own trusted internal Redis, not a public network (Part L
    explicitly allows skipping signing "unless architecture genuinely
    needs it"); mismatched identity is rejected exactly like an
    infrastructure failure, never partially trusted."""

    return (
        result.request_id == request.request_id
        and result.commit_sha == request.commit_sha
        and result.verification_kind == request.verification_kind
        and result.test_target_path == request.test_target_path
    )
