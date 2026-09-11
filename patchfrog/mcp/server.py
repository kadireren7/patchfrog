"""The MCP tool surface -- Milestone T (T2): ``list_findings``,
``get_finding_handoff``, ``start_fix_attempt``, ``get_fix_attempt``,
``get_merge_readiness``; Milestone Y/Z (Z17): ``list_repository_learnings``,
``get_effective_policy`` (both read-only -- no governance/learning
mutation tool exists or will ever be added here). See the package
docstring (:mod:`patchfrog.mcp`) for the trust-boundary summary.

**Local trust model** (Part AD): stdio access inherits whatever Unix user
runs ``python -m patchfrog.cli mcp serve`` -- this is full local trust,
not multi-user authorization, and is documented as such rather than
pretended otherwise. Every tool still requires an explicit
``repository_full_name`` and independently re-validates that any
``finding_id``/``fix_attempt_id`` actually belongs to that repository
before returning anything -- defense against ID-guessing/cross-repo
leakage within one operator's own multi-repository database (Part AC), not
a multi-tenant boundary. A cross-repository lookup returns the same
``"not_found"`` shape as a genuinely missing id -- never a distinct
"wrong repository" error that would confirm the id exists elsewhere.

**Adaptation from the milestone spec's literal ``start_fix_attempt(
handoff_id, candidate_fix_commit_sha)`` signature**: ``handoff_id`` is a
one-way hash (:func:`patchfrog.agent_handoff.domain.compute_handoff_id`)
with no lookup table (Part I: no new table for the handoff itself, since
it is always cheaply re-derivable from persisted state). Given only a bare
hash string, PatchFrog cannot invert it back to a finding. The tool below
therefore takes ``finding_id`` (from a prior ``get_finding_handoff`` call)
as the primary identity and re-derives the handoff fresh server-side;
``handoff_id`` is accepted as an optional integrity check -- if supplied
and it does not match the freshly re-derived value, the call fails closed
rather than silently proceeding against a possibly stale/wrong-repository
value.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.agent_handoff.domain import FindingHandoff
from patchfrog.agent_handoff.service import AgentHandoffService
from patchfrog.config.settings import Settings
from patchfrog.executable_verification.dispatch import VerifierDispatcher
from patchfrog.fix_verification.domain import FixAttempt, FixAttemptStatus
from patchfrog.fix_verification.service import FixAttemptValidationError, FixVerificationService
from patchfrog.governance.domain import EffectivePolicy
from patchfrog.governance.precedence import PLATFORM_POLICY, merge_policies
from patchfrog.learning_records.domain import RepositoryLearningRecord
from patchfrog.merge_readiness.domain import MergeReadinessResult
from patchfrog.merge_readiness.service import MergeReadinessService
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.repositories import (
    AIFindingRepository,
    FixAttemptRepository,
    PullRequestRepository,
    RepositoryLearningRecordRepository,
    RepositoryRepository,
    ReviewRunRepository,
)
from patchfrog.review.provider import LLMProvider

#: Bounded -- never "return every finding" (Part P).
MAX_LIST_FINDINGS_LIMIT = 50
DEFAULT_LIST_FINDINGS_LIMIT = 20
#: Bounded -- a handoff's own known-fix-attempts summary, never the full
#: unbounded history (Part R).
MAX_KNOWN_FIX_ATTEMPTS = 10


def _handoff_to_wire(handoff: FindingHandoff) -> dict[str, Any]:
    return {
        "handoff_id": handoff.handoff_id,
        "schema_version": handoff.schema_version,
        "repository_id": str(handoff.repository_id),
        "repository_full_name": handoff.repository_full_name,
        "review_run_id": str(handoff.review_run_id),
        "original_commit_sha": handoff.original_commit_sha,
        "finding_id": str(handoff.finding_id),
        "category": handoff.category.value,
        "severity": handoff.severity.value,
        "confidence": handoff.confidence.value,
        "title": handoff.title,
        "message": handoff.message,
        "reasoning_summary": handoff.reasoning_summary,
        "impact": handoff.impact,
        "suggested_fix": handoff.suggested_fix,
        "file_path": handoff.file_path,
        "start_line": handoff.start_line,
        "end_line": handoff.end_line,
        "qualified_name": handoff.qualified_name,
        "evidence": [asdict(e) for e in handoff.evidence],
        "corroborated_by_static": handoff.corroborated_by_static,
        "static_finding_ids": [str(i) for i in handoff.static_finding_ids],
        "critic_reasoning_summary": handoff.critic_reasoning_summary,
        "suggested_verification_target": handoff.suggested_verification_target,
        "executable_verification_evidence": handoff.executable_verification_evidence,
        "published": handoff.published,
        "github_review_id": handoff.github_review_id,
    }


def _attempt_to_wire(attempt: FixAttempt) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "fix_attempt_id": str(attempt.fix_attempt_id),
        "handoff_id": attempt.handoff_id,
        "finding_id": str(attempt.finding_id),
        "repository_id": str(attempt.repository_id),
        "original_commit_sha": attempt.original_commit_sha,
        "candidate_fix_commit_sha": attempt.candidate_fix_commit_sha,
        "status": attempt.status.value,
        "created_at": attempt.created_at.isoformat(),
        "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
    }
    if attempt.result is not None:
        wire["result"] = {
            "version": attempt.result.version,
            "deterministic_evidence": list(attempt.result.deterministic_evidence),
            "executable_verification_outcome": attempt.result.executable_verification_outcome,
            "remaining_issue_summary": attempt.result.remaining_issue_summary,
            "limitations": list(attempt.result.limitations),
        }
    return wire


def _learning_record_to_wire(record: RepositoryLearningRecord) -> dict[str, Any]:
    return {
        "learning_type": record.learning_type.value,
        "surface": {
            "file_path": record.surface.file_path,
            "qualified_name": record.surface.qualified_name,
            "category": record.surface.category.value,
        },
        "maturity": record.maturity.value,
        "support_count": record.support_count,
        "first_observed_at": record.first_observed_at,
        "last_observed_at": record.last_observed_at,
        "retired_reason": record.retired_reason,
        "explanation": record.explain(),
        "version": record.version,
    }


def _effective_policy_to_wire(policy: EffectivePolicy) -> dict[str, Any]:
    return {
        "security_block_severity_floor": (
            policy.security_block_severity_floor.value if policy.security_block_severity_floor else None
        ),
        "security_requires_human_review_severity_floor": (
            policy.security_requires_human_review_severity_floor.value
            if policy.security_requires_human_review_severity_floor
            else None
        ),
        "security_suppression_forbidden": policy.security_suppression_forbidden,
        "verification_required_categories": sorted(c.value for c in policy.verification_required_categories),
        "verification_required_path_prefixes": sorted(policy.verification_required_path_prefixes),
        "allowed_providers": sorted(policy.allowed_providers) if policy.allowed_providers is not None else None,
        "personalization_enabled": policy.personalization_enabled,
        "contributing_scopes": [s.value for s in policy.contributing_scopes],
        "version": policy.version,
    }


def _readiness_to_wire(result: MergeReadinessResult) -> dict[str, Any]:
    return {
        "decision": result.decision.value,
        "reason_codes": [r.value for r in result.reason_codes],
        "repository_id": str(result.repository_id),
        "pull_request_number": result.pull_request_number,
        "review_run_id": str(result.review_run_id) if result.review_run_id is not None else None,
        "head_sha": result.head_sha,
        "finding_ids": [str(f) for f in result.finding_ids],
        "limitations": list(result.limitations),
        "version": result.version,
    }


class PatchFrogMCPServer:
    """Builds the four-tool MCP surface. One instance per
    ``python -m patchfrog.cli mcp serve`` process."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        verifier_dispatcher: VerifierDispatcher | None = None,
        fix_critic_provider: LLMProvider | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clone_url_factory: Callable[[RepositoryModel], str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._handoff_service = AgentHandoffService()
        self._fix_service = FixVerificationService(
            settings=settings,
            verifier_dispatcher=verifier_dispatcher,
            fix_critic_provider=fix_critic_provider,
            http_client_factory=http_client_factory,
            clone_url_factory=clone_url_factory,
        )
        self._repository_repo = RepositoryRepository()
        self._run_repo = ReviewRunRepository()
        self._finding_repo = AIFindingRepository()
        self._pull_request_repo = PullRequestRepository()
        self._fix_attempt_repo = FixAttemptRepository()
        self._readiness_service = MergeReadinessService()
        self._learning_repo = RepositoryLearningRecordRepository()

        self.mcp: FastMCP = FastMCP(
            name="patchfrog",
            instructions=(
                "Read-only-mostly access to PatchFrog's already-verified code "
                "review findings, fix-verification results, and merge-readiness "
                "decisions for self-hosted repositories. PatchFrog remains the "
                "source of truth for review evidence; this server never writes "
                "source code, never commits or pushes, and never writes to "
                "GitHub. Merge readiness is exact-head-bound: it reflects only "
                "the PR's current head SHA at call time, never a stale decision."
            ),
        )
        self._register_tools()

    def _register_tools(self) -> None:
        @self.mcp.tool()
        async def list_findings(
            repository_full_name: str,
            pull_request_number: int | None = None,
            review_run_id: str | None = None,
            category: str | None = None,
            severity: str | None = None,
            limit: int = DEFAULT_LIST_FINDINGS_LIMIT,
        ) -> dict[str, Any]:
            """List findings from one specific, already-completed PatchFrog
            review run -- identified either directly (``review_run_id``) or
            by ``pull_request_number`` (resolves to that PR's most recent
            completed review). Never "every finding in the repository."
            ``limit`` is capped at 50."""

            return await self._list_findings(
                repository_full_name=repository_full_name,
                pull_request_number=pull_request_number,
                review_run_id=review_run_id,
                category=category,
                severity=severity,
                limit=limit,
            )

        @self.mcp.tool()
        async def get_finding_handoff(repository_full_name: str, finding_id: str) -> dict[str, Any]:
            """Return the bounded, structured evidence for one finding --
            see :mod:`patchfrog.agent_handoff.domain`."""

            return await self._get_finding_handoff(repository_full_name=repository_full_name, finding_id=finding_id)

        @self.mcp.tool()
        async def start_fix_attempt(
            repository_full_name: str,
            finding_id: str,
            candidate_fix_commit_sha: str,
            handoff_id: str | None = None,
        ) -> dict[str, Any]:
            """Independently verify whether ``candidate_fix_commit_sha``
            resolves the finding identified by ``finding_id`` (see the
            module docstring for why ``finding_id``, not a bare
            ``handoff_id``, is the primary identity here). Idempotent: a
            repeat call with the same finding + candidate SHA returns the
            existing result rather than re-verifying."""

            return await self._start_fix_attempt(
                repository_full_name=repository_full_name,
                finding_id=finding_id,
                candidate_fix_commit_sha=candidate_fix_commit_sha,
                handoff_id=handoff_id,
            )

        @self.mcp.tool()
        async def get_fix_attempt(repository_full_name: str, fix_attempt_id: str) -> dict[str, Any]:
            """Poll the status/result of a previously started fix attempt."""

            return await self._get_fix_attempt(
                repository_full_name=repository_full_name, fix_attempt_id=fix_attempt_id
            )

        @self.mcp.tool()
        async def get_merge_readiness(repository_full_name: str, pull_request_number: int) -> dict[str, Any]:
            """Compute PatchFrog's current merge-readiness decision
            (``ready``/``blocked``/``human_review_required``) for this PR's
            *exact current head* -- deterministic over already-persisted
            evidence, never a new review or an LLM call. See
            :mod:`patchfrog.merge_readiness.domain` for the full semantics.
            Always recomputed fresh; never a cached/stale-head result."""

            return await self._get_merge_readiness(
                repository_full_name=repository_full_name, pull_request_number=pull_request_number
            )

        @self.mcp.tool()
        async def list_repository_learnings(repository_full_name: str, include_retired: bool = False) -> dict[str, Any]:
            """Read-only (Milestone Y/Z17): the durable, explainable
            repository-scoped learning records this repository has
            accumulated -- see :mod:`patchfrog.learning_records.domain`.
            Never a mutation tool; there is no ``set_learning`` or
            ``disable_learning`` tool."""

            return await self._list_repository_learnings(
                repository_full_name=repository_full_name, include_retired=include_retired
            )

        @self.mcp.tool()
        async def get_effective_policy(repository_full_name: str) -> dict[str, Any]:
            """Read-only (Milestone Z, Z17): the effective governance
            policy for this repository. A self-hosted deployment has no
            Cloud organization/repository policy layer -- this always
            reflects the platform safety floor alone
            (:data:`patchfrog.governance.precedence.PLATFORM_POLICY`).
            PatchFrog Cloud's own dashboard is where an organization's
            actual effective (platform + org + repo) policy is surfaced.
            Never a mutation tool; there is no ``set_policy``,
            ``disable_security``, ``force_ready``, or ``override_block``
            tool."""

            return await self._get_effective_policy(repository_full_name=repository_full_name)

    async def _list_repository_learnings(
        self, *, repository_full_name: str, include_retired: bool
    ) -> dict[str, Any]:
        async with self._session_factory() as session:
            repository = await self._repository_repo.get_by_full_name(session, full_name=repository_full_name)
            if repository is None:
                return {"error": "repository_not_found"}

            records = await self._learning_repo.list_for_repository(
                session, repository_id=repository.id, include_retired=include_retired
            )
            return {"learnings": [_learning_record_to_wire(r) for r in records]}

    async def _get_effective_policy(self, *, repository_full_name: str) -> dict[str, Any]:
        async with self._session_factory() as session:
            repository = await self._repository_repo.get_by_full_name(session, full_name=repository_full_name)
            if repository is None:
                return {"error": "repository_not_found"}

        policy = merge_policies(PLATFORM_POLICY, None, None)
        return {"effective_policy": _effective_policy_to_wire(policy)}

    async def _list_findings(
        self,
        *,
        repository_full_name: str,
        pull_request_number: int | None,
        review_run_id: str | None,
        category: str | None,
        severity: str | None,
        limit: int,
    ) -> dict[str, Any]:
        bounded_limit = max(1, min(limit, MAX_LIST_FINDINGS_LIMIT))
        async with self._session_factory() as session:
            repository = await self._repository_repo.get_by_full_name(session, full_name=repository_full_name)
            if repository is None:
                return {"error": "repository_not_found"}

            run = None
            if review_run_id is not None:
                try:
                    run_uuid = uuid.UUID(review_run_id)
                except ValueError:
                    return {"error": "malformed_review_run_id"}
                run = await self._run_repo.get_by_id(session, run_id=run_uuid)
                if run is None or run.repository_id != repository.id:
                    return {"error": "review_run_not_found"}
            elif pull_request_number is not None:
                pull_request = await self._pull_request_repo.get_by_repository_and_number(
                    session, repository_id=repository.id, github_pr_number=pull_request_number
                )
                if pull_request is None:
                    return {"error": "pull_request_not_found"}
                run = await self._run_repo.get_latest_succeeded_for_pull_request(
                    session, pull_request_id=pull_request.id
                )
                if run is None:
                    return {"error": "no_completed_review_for_pull_request"}
            else:
                return {"error": "must_provide_review_run_id_or_pull_request_number"}

            findings = await self._finding_repo.list_for_run(session, review_run_id=run.id)
            if category is not None:
                findings = [f for f in findings if f.category.value == category]
            if severity is not None:
                findings = [f for f in findings if f.severity.value == severity]

            return {
                "review_run_id": str(run.id),
                "commit_sha": run.commit_sha,
                "total_matching": len(findings),
                "findings": [
                    {
                        "finding_id": str(f.id),
                        "title": f.title,
                        "category": f.category.value,
                        "severity": f.severity.value,
                        "confidence": f.confidence.value,
                        "file_path": f.file_path,
                        "start_line": f.start_line,
                        "end_line": f.end_line,
                    }
                    for f in findings[:bounded_limit]
                ],
            }

    async def _get_finding_handoff(self, *, repository_full_name: str, finding_id: str) -> dict[str, Any]:
        try:
            finding_uuid = uuid.UUID(finding_id)
        except ValueError:
            return {"error": "malformed_finding_id"}

        async with self._session_factory() as session:
            repository = await self._repository_repo.get_by_full_name(session, full_name=repository_full_name)
            if repository is None:
                return {"error": "repository_not_found"}

            handoff, reason = await self._handoff_service.build_handoff(session, finding_id=finding_uuid)
            if handoff is None:
                return {"error": reason.value if reason is not None else "finding_not_found"}
            if handoff.repository_id != repository.id:
                # Deliberately the same shape as "not found" -- never
                # confirm a finding_id exists in a different repository.
                return {"error": "finding_not_found"}

            attempts = await self._fix_attempt_repo.list_for_finding(session, finding_id=finding_uuid)
            return {
                "handoff": _handoff_to_wire(handoff),
                "known_fix_attempts": [
                    {"fix_attempt_id": str(a.id), "candidate_fix_commit_sha": a.candidate_fix_commit_sha,
                     "status": FixAttemptStatus(a.status).value}
                    for a in attempts[:MAX_KNOWN_FIX_ATTEMPTS]
                ],
            }

    async def _start_fix_attempt(
        self, *, repository_full_name: str, finding_id: str, candidate_fix_commit_sha: str, handoff_id: str | None
    ) -> dict[str, Any]:
        try:
            finding_uuid = uuid.UUID(finding_id)
        except ValueError:
            return {"error": "malformed_finding_id"}

        async with self._session_factory() as session:
            repository = await self._repository_repo.get_by_full_name(session, full_name=repository_full_name)
            if repository is None:
                return {"error": "repository_not_found"}

            handoff, _reason = await self._handoff_service.build_handoff(session, finding_id=finding_uuid)
            if handoff is None or handoff.repository_id != repository.id:
                return {"error": "finding_not_found"}
            if handoff_id is not None and handoff_id != handoff.handoff_id:
                return {"error": "handoff_id_mismatch"}

            try:
                attempt = await self._fix_service.start_fix_attempt(
                    session, handoff=handoff, candidate_fix_commit_sha=candidate_fix_commit_sha
                )
            except FixAttemptValidationError as exc:
                return {"error": "validation_failed", "detail": str(exc)}

            return {"fix_attempt": _attempt_to_wire(attempt)}

    async def _get_fix_attempt(self, *, repository_full_name: str, fix_attempt_id: str) -> dict[str, Any]:
        try:
            attempt_uuid = uuid.UUID(fix_attempt_id)
        except ValueError:
            return {"error": "malformed_fix_attempt_id"}

        async with self._session_factory() as session:
            repository = await self._repository_repo.get_by_full_name(session, full_name=repository_full_name)
            if repository is None:
                return {"error": "repository_not_found"}

            attempt = await self._fix_service.get_fix_attempt(session, fix_attempt_id=attempt_uuid)
            if attempt is None or attempt.repository_id != repository.id:
                return {"error": "fix_attempt_not_found"}

            return {"fix_attempt": _attempt_to_wire(attempt)}

    async def _get_merge_readiness(
        self, *, repository_full_name: str, pull_request_number: int
    ) -> dict[str, Any]:
        async with self._session_factory() as session:
            repository = await self._repository_repo.get_by_full_name(session, full_name=repository_full_name)
            if repository is None:
                return {"error": "repository_not_found"}

            result = await self._readiness_service.evaluate(
                session, repository_id=repository.id, pull_request_number=pull_request_number
            )
            if result is None:
                return {"error": "pull_request_not_found"}

            return {"merge_readiness": _readiness_to_wire(result)}

    async def run_stdio(self) -> None:
        await self.mcp.run_stdio_async()
