"""Review-worker-side dispatch to the separate verifier process --
Milestone S6.

Wraps Celery's own (synchronous) ``send_task``/``AsyncResult.get`` as a
clean async interface the review worker's ``_verify()`` closure can await
like any other I/O. The review worker's own Celery app instance is used
purely as a *producer* here -- ``send_task`` dispatches **by name**
(:data:`patchfrog.executable_verification.protocol.VERIFICATION_TASK_NAME`)
onto the verifier's own dedicated queue
(:data:`patchfrog.executable_verification.protocol.VERIFICATION_QUEUE_NAME`),
so the review worker never imports :mod:`apps.verifier.tasks` and never
needs any of the verifier's own (deliberately absent) credentials. Both
apps share the same Redis result backend, which is how the review worker
can fetch a result a completely different process actually produced.

Never falls back to running verification itself. Any dispatch failure --
no verifier consuming the queue, a timeout, a malformed reply, an
identity mismatch -- surfaces as ``None``; the caller (``_verify()`` in
:mod:`patchfrog.review.service`) always renders that as
``VerificationOutcome.SANDBOX_ERROR``, never as a signal to execute the
hostile test itself.
"""

from __future__ import annotations

import asyncio

import structlog
from celery import Celery

from patchfrog.executable_verification.protocol import (
    VERIFICATION_QUEUE_NAME,
    VERIFICATION_TASK_NAME,
    VerificationExecutionRequest,
    VerificationExecutionResult,
    result_matches_request,
)

logger = structlog.get_logger(__name__)


class VerifierDispatcher:
    """Constructed once per Celery task invocation (mirrors every other
    per-request dependency :class:`~patchfrog.review.service.PullRequestReviewService`
    already takes in its constructor) and reused across every candidate
    in one review run."""

    def __init__(self, *, celery_app: Celery, wait_timeout_seconds: float) -> None:
        self._celery_app = celery_app
        self._wait_timeout_seconds = wait_timeout_seconds

    async def dispatch(self, request: VerificationExecutionRequest) -> VerificationExecutionResult | None:
        try:
            async_result = self._celery_app.send_task(
                VERIFICATION_TASK_NAME,
                args=[request.to_wire()],
                queue=VERIFICATION_QUEUE_NAME,
                task_id=request.request_id,
            )
            raw = await asyncio.to_thread(async_result.get, timeout=self._wait_timeout_seconds)
        except Exception as exc:  # any dispatch/backend failure must fail closed, never propagate
            logger.warning(
                "verifier_dispatch_failed", request_id=request.request_id, error=str(exc), error_type=type(exc).__name__,
            )
            return None

        try:
            result = VerificationExecutionResult.from_wire(raw)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("verifier_result_malformed", request_id=request.request_id, error=str(exc))
            return None

        if not result_matches_request(request=request, result=result):
            logger.warning(
                "verifier_result_identity_mismatch",
                expected_request_id=request.request_id, actual_request_id=result.request_id,
            )
            return None

        return result
