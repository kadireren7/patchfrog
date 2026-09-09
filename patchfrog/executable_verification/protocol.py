"""Verifier wire protocol -- Milestone S6, Production Execution Enablement.

A narrow, independently-versioned request/result contract for handing a
*bounded* verification job from the trusted review worker to a
credential-minimal verifier process/container over the existing Celery/
Redis queue. See ``validation/production_execution/latest-summary.md``
for the full trust-boundary audit, architecture decision, and the
security correction round this exact shape was hardened by.

**This is transport only.** The verifier's own execution logic is
entirely unchanged from Milestone S --
:func:`patchfrog.executable_verification.service.execute_against_snapshot`
is called exactly as before; only *which process* calls it, and *how the
repository content gets there*, are new. ``ExecutableVerificationReport``/
``ExecutableVerificationEvidence`` remain the one, unconverted true result
model everywhere except this thin wire boundary -- never a second,
competing result model.

**Deliberately excluded from this contract** (spec Part F's own explicit
list): no provider key, no GitHub App private key, no arbitrary
environment, no arbitrary shell command, no arbitrary mounts, no PR body,
no raw secrets. A request carries only what execution genuinely needs:
identity, exact commit SHA, verification kind/target, an *opaque artifact
identifier* (never a raw filesystem path -- the verifier resolves it under
its own operator-configured staging root; see
:mod:`apps.verifier.tasks` for why a bare path was rejected) plus its
content digest, and bounded timeout data.

**Strict wire parsing** (this module's own security correction): a
malformed or adversarial wire payload must never be silently coerced into
something plausible-looking. ``bool("false") == True`` is exactly the
class of Python-coercion trap this module refuses to let through --
every field's actual JSON-decoded type is checked before use, and
anything unexpected raises, which the caller
(:class:`patchfrog.executable_verification.dispatch.VerifierDispatcher`,
:func:`apps.verifier.tasks._run_verification`) always turns into a fail
-closed rejection, never a best-effort parse.

**Protocol version is enforced, not merely carried.** A request whose
``protocol_version`` does not exactly equal
:data:`VERIFIER_PROTOCOL_VERSION` is rejected by the verifier before any
execution; a result whose ``protocol_version`` doesn't match is rejected
by the review worker before being trusted. No range tolerance -- this is
the initial, still-unreleased v1 contract; a mismatch is always a bug or
an attack, never a compatibility case to smooth over.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from patchfrog.executable_verification.domain import (
    MAX_STDERR_EXCERPT_BYTES,
    MAX_STDOUT_EXCERPT_BYTES,
    VerificationKind,
    VerificationOutcome,
)

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

#: Defensive wire-level bounds -- independent of (and tighter than or
#: equal to) the sandbox's own MAX_VERIFICATION_SECONDS/MAX_*_EXCERPT_BYTES
#: enforcement, so a malformed or adversarial payload is rejected at parse
#: time, before it ever reaches execution or is trusted as a result.
_MIN_WIRE_TIMEOUT_SECONDS = 0.1
_MAX_WIRE_TIMEOUT_SECONDS = 300.0
_MAX_WIRE_DURATION_MS = 600_000.0
#: A generous multiple over the sandbox's own excerpt caps -- allows for
#: the "...(truncated)" marker and UTF-8 multi-byte slop, while still
#: rejecting a wildly oversized payload a malformed/malicious verifier
#: reply should never be able to smuggle through.
_MAX_WIRE_EXCERPT_BYTES = 4 * max(MAX_STDOUT_EXCERPT_BYTES, MAX_STDERR_EXCERPT_BYTES)


class ProtocolValidationError(ValueError):
    """A wire payload failed strict validation -- the caller must treat
    this exactly like any other unrecoverable dispatch/parse failure
    (fail closed), never attempt a best-effort partial parse."""


def _require_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolValidationError(f"{key!r} must be a non-empty string, got {value!r}")
    return value


def _require_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    # bool is a subclass of int in Python -- explicitly excluded so a
    # stray `true`/`false` in the wire payload is never silently accepted
    # as 1/0.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolValidationError(f"{key!r} must be an int, got {value!r}")
    return value


def _require_bool(data: dict[str, object], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ProtocolValidationError(f"{key!r} must be a bool, got {value!r} ({type(value).__name__})")
    return value


def _require_finite_float(data: dict[str, object], key: str, *, minimum: float, maximum: float) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolValidationError(f"{key!r} must be numeric, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise ProtocolValidationError(f"{key!r} must be finite, got {result!r}")
    if not (minimum <= result <= maximum):
        raise ProtocolValidationError(f"{key!r}={result!r} out of bounds [{minimum}, {maximum}]")
    return result


def _require_optional_int(data: dict[str, object], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolValidationError(f"{key!r} must be an int or null, got {value!r}")
    return value


def _require_bounded_str(data: dict[str, object], key: str, *, max_bytes: int) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ProtocolValidationError(f"{key!r} must be a string, got {value!r}")
    if len(value.encode("utf-8")) > max_bytes:
        raise ProtocolValidationError(f"{key!r} exceeds {max_bytes} bytes")
    return value


def _require_verification_kind(data: dict[str, object], key: str) -> VerificationKind:
    value = data.get(key)
    if not isinstance(value, str):
        raise ProtocolValidationError(f"{key!r} must be a string, got {value!r}")
    try:
        return VerificationKind(value)
    except ValueError as exc:
        raise ProtocolValidationError(f"{key!r}={value!r} is not a known VerificationKind") from exc


def _require_verification_outcome(data: dict[str, object], key: str) -> VerificationOutcome:
    value = data.get(key)
    if not isinstance(value, str):
        raise ProtocolValidationError(f"{key!r} must be a string, got {value!r}")
    try:
        return VerificationOutcome(value)
    except ValueError as exc:
        raise ProtocolValidationError(f"{key!r}={value!r} is not a known VerificationOutcome") from exc


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
    command -- see this module's own docstring.

    ``artifact_id`` is an opaque identifier (never a raw filesystem
    path) -- the verifier resolves it under its own operator-configured
    staging root and rejects anything that would escape it (path
    traversal, symlink escape). ``artifact_digest`` is the deterministic
    content-manifest digest
    (:func:`patchfrog.executable_verification.snapshot_staging.compute_artifact_digest`)
    the review worker computed over the credential-free artifact right
    after staging it -- the verifier recomputes the identical digest over
    its own disposable copy before ever executing anything."""

    request_id: str
    protocol_version: int
    repository_id: str
    review_run_id: str
    commit_sha: str
    verification_kind: VerificationKind
    test_target_path: str
    artifact_id: str
    artifact_digest: str
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
            "artifact_id": self.artifact_id,
            "artifact_digest": self.artifact_digest,
            "timeout_seconds": self.timeout_seconds,
        }

    @staticmethod
    def from_wire(data: dict[str, object]) -> VerificationExecutionRequest:
        return VerificationExecutionRequest(
            request_id=_require_str(data, "request_id"),
            protocol_version=_require_int(data, "protocol_version"),
            repository_id=_require_str(data, "repository_id"),
            review_run_id=_require_str(data, "review_run_id"),
            commit_sha=_require_str(data, "commit_sha"),
            verification_kind=_require_verification_kind(data, "verification_kind"),
            test_target_path=_require_str(data, "test_target_path"),
            artifact_id=_require_str(data, "artifact_id"),
            artifact_digest=_require_str(data, "artifact_digest"),
            timeout_seconds=_require_finite_float(
                data, "timeout_seconds", minimum=_MIN_WIRE_TIMEOUT_SECONDS, maximum=_MAX_WIRE_TIMEOUT_SECONDS,
            ),
        )


def enforce_request_protocol_version(request: VerificationExecutionRequest) -> bool:
    """The verifier's own gate: a request whose protocol_version does not
    exactly match VERIFIER_PROTOCOL_VERSION is never executed. No range
    tolerance for this still-unreleased v1 contract."""

    return request.protocol_version == VERIFIER_PROTOCOL_VERSION


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
            request_id=_require_str(data, "request_id"),
            protocol_version=_require_int(data, "protocol_version"),
            commit_sha=_require_str(data, "commit_sha"),
            verification_kind=_require_verification_kind(data, "verification_kind"),
            test_target_path=_require_str(data, "test_target_path"),
            outcome=_require_verification_outcome(data, "outcome"),
            exit_code=_require_optional_int(data, "exit_code"),
            stdout_excerpt=_require_bounded_str(data, "stdout_excerpt", max_bytes=_MAX_WIRE_EXCERPT_BYTES),
            stderr_excerpt=_require_bounded_str(data, "stderr_excerpt", max_bytes=_MAX_WIRE_EXCERPT_BYTES),
            duration_ms=_require_finite_float(data, "duration_ms", minimum=0.0, maximum=_MAX_WIRE_DURATION_MS),
            timed_out=_require_bool(data, "timed_out"),
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
    infrastructure failure, never partially trusted.

    Includes protocol_version: a result claiming a different protocol
    version than the request that produced it is rejected outright, on
    top of the verifier's own independent
    :func:`enforce_request_protocol_version` gate on the request side."""

    return (
        result.request_id == request.request_id
        and result.protocol_version == request.protocol_version
        and result.protocol_version == VERIFIER_PROTOCOL_VERSION
        and result.commit_sha == request.commit_sha
        and result.verification_kind == request.verification_kind
        and result.test_target_path == request.test_target_path
    )
