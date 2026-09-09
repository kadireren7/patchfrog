"""Fix Verification service and algorithm -- Milestone T (T3).

``start_fix_attempt`` validates (Part U, fail closed) and creates/returns
an idempotent :class:`~patchfrog.fix_verification.domain.FixAttempt`
(Part AF), then runs verification synchronously end to end (bounded --
every step below has its own existing timeout: the S6 verifier dispatch's
own ``wait_timeout_seconds``, the ancestry fetch's own timeout, one
bounded LLM call at most). The ``PENDING``/``VERIFYING`` states already
model an async dispatch-then-poll split for a future milestone to adopt
without a domain change; v1 keeps it simple -- an MCP tool call is not
too different in shape from `patchfrog.cli`'s own synchronous commands.

Algorithm (deterministic-first, Part V/W/X/Y/Z/AA) -- see
``validation/agent_handoff/latest-summary.md`` section 2.3 for the full
design rationale:

1. Prove ``candidate_fix_commit_sha`` is a real descendant of
   ``original_commit_sha`` (reuses
   :func:`patchfrog.repository.ancestry.verify_ancestor_with_diff`
   unmodified) -- not provable -> ``STALE``.
2. If the flagged file did not change at all between the two commits ->
   ``STILL_PRESENT`` (the flagged code is byte-identical; it cannot have
   newly become fixed). Otherwise, continue.
3. If statically corroborated: re-run the original analyzer, scoped to
   one file, against the new head (:mod:`patchfrog.fix_verification.
   static_recheck`).
4. If Executable-Verification-eligible: dispatch to the S6 verifier
   against the candidate SHA only (never the original SHA -- see
   ``validation/agent_handoff/latest-summary.md`` section 1.3).
5. Combine: any contradicting deterministic signal -> ``STILL_PRESENT``.
   Any confirming signal with none contradicting -> ``FIXED``. No
   deterministic signal at all -> one bounded LLM call
   (:mod:`patchfrog.fix_verification.critic`), itself failing closed to
   ``INCONCLUSIVE``.

Any infrastructure failure (clone/network/git error) at any point ->
``ERROR``, never silently reported as one of the semantic outcomes.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.agent_handoff.domain import FindingHandoff
from patchfrog.config.settings import Settings
from patchfrog.executable_verification.dispatch import VerifierDispatcher
from patchfrog.executable_verification.domain import VerificationKind, VerificationOutcome
from patchfrog.executable_verification.eligibility import determine_verification_target
from patchfrog.executable_verification.protocol import (
    VERIFIER_PROTOCOL_VERSION,
    VerificationExecutionRequest,
    compute_request_id,
)
from patchfrog.executable_verification.snapshot_staging import (
    ArtifactExportError,
    compute_artifact_digest,
    export_artifact,
)
from patchfrog.fix_verification.critic import FixCriticDecision, judge_fix
from patchfrog.fix_verification.domain import (
    MAX_ACTIVE_FIX_ATTEMPTS_PER_REPOSITORY,
    MAX_FIX_ATTEMPTS_PER_FINDING,
    TERMINAL_STATUSES,
    FixAttempt,
    FixAttemptStatus,
    FixVerificationResult,
)
from patchfrog.fix_verification.static_recheck import recheck_static_finding
from patchfrog.github.auth import InstallationTokenProvider
from patchfrog.parsing.detect import detect_language
from patchfrog.persistence.models.analysis import FindingModel
from patchfrog.persistence.models.fix_attempt import FixAttemptModel
from patchfrog.persistence.models.repository import RepositoryModel
from patchfrog.persistence.models.review import AIFindingModel, ReviewCandidateModel
from patchfrog.persistence.repositories.fix_attempt import FixAttemptRepository
from patchfrog.repository.ancestry import verify_ancestor_with_diff
from patchfrog.repository.git import GitError
from patchfrog.repository.snapshot import RepositorySnapshotProvider
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason
from patchfrog.review.provider import LLMProvider

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_EV_TIMEOUT_SECONDS = 30.0
_CODE_EXCERPT_CONTEXT_LINES = 5
_MAX_CODE_EXCERPT_BYTES = 4000


class FixAttemptValidationError(Exception):
    """A ``start_fix_attempt`` request failed validation (Part U) -- fail
    closed, never a partially-created attempt."""


def _reconstruct_candidate(candidate_model: ReviewCandidateModel) -> ReviewCandidate:
    return ReviewCandidate(
        file_path=candidate_model.file_path,
        symbol_id=candidate_model.symbol_id,
        symbol_name=candidate_model.symbol_name,
        qualified_name=candidate_model.qualified_name,
        start_line=candidate_model.start_line,
        end_line=candidate_model.end_line,
        changed_lines=tuple(json.loads(candidate_model.changed_lines or "[]")),
        static_finding_ids=(),
        reason=ReviewCandidateReason(candidate_model.reason),
    )


def _read_excerpt(path: Path, *, start_line: int, end_line: int) -> str | None:
    if not path.is_file():
        return None
    lines = path.read_text(errors="replace").splitlines()
    lo = max(0, start_line - 1 - _CODE_EXCERPT_CONTEXT_LINES)
    hi = min(len(lines), end_line + _CODE_EXCERPT_CONTEXT_LINES)
    excerpt = "\n".join(lines[lo:hi])
    encoded = excerpt.encode("utf-8")
    if len(encoded) > _MAX_CODE_EXCERPT_BYTES:
        excerpt = encoded[:_MAX_CODE_EXCERPT_BYTES].decode("utf-8", errors="ignore") + "...(truncated)"
    return excerpt


def _to_domain(model: FixAttemptModel) -> FixAttempt:
    result: FixVerificationResult | None = None
    if model.status in TERMINAL_STATUSES:
        result = FixVerificationResult(
            fix_attempt_id=model.id,
            finding_id=model.finding_id,
            original_commit_sha=model.original_commit_sha,
            candidate_fix_commit_sha=model.candidate_fix_commit_sha,
            status=model.status,
            deterministic_evidence=tuple(json.loads(model.deterministic_evidence or "[]")),
            executable_verification_outcome=model.executable_verification_outcome,
            remaining_issue_summary=model.remaining_issue_summary,
            limitations=tuple(json.loads(model.limitations or "[]")),
        )
    return FixAttempt(
        fix_attempt_id=model.id,
        handoff_id=model.handoff_id,
        finding_id=model.finding_id,
        repository_id=model.repository_id,
        original_commit_sha=model.original_commit_sha,
        candidate_fix_commit_sha=model.candidate_fix_commit_sha,
        status=model.status,
        created_at=model.created_at,
        completed_at=model.completed_at,
        result=result,
    )


class FixVerificationService:
    def __init__(
        self,
        *,
        settings: Settings,
        verifier_dispatcher: VerifierDispatcher | None,
        fix_critic_provider: LLMProvider | None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clone_url_factory: Callable[[RepositoryModel], str] | None = None,
    ) -> None:
        self._settings = settings
        self._verifier_dispatcher = verifier_dispatcher
        self._fix_critic_provider = fix_critic_provider
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=settings.github_api_timeout_seconds)
        )
        #: Overridable purely for tests -- exercising the real acquisition/
        #: ancestry/git-diff path against a real, local bare git remote
        #: instead of a real GitHub network call, exactly like Milestone
        #: S6's own corpus tests use `file://` remotes with a synthetic
        #: token. Production always uses the real default.
        self._clone_url_factory = clone_url_factory or (
            lambda repository: f"https://github.com/{repository.full_name}.git"
        )
        self._repo = FixAttemptRepository()

    async def start_fix_attempt(
        self, session: AsyncSession, *, handoff: FindingHandoff, candidate_fix_commit_sha: str
    ) -> FixAttempt:
        if not _SHA_PATTERN.match(candidate_fix_commit_sha):
            raise FixAttemptValidationError(
                f"candidate_fix_commit_sha must be a 40-character lowercase hex commit SHA, "
                f"got {candidate_fix_commit_sha!r}"
            )

        existing = await self._repo.get_existing(
            session, handoff_id=handoff.handoff_id, candidate_fix_commit_sha=candidate_fix_commit_sha
        )
        if existing is None:
            active_count = await self._repo.count_active_for_repository(
                session, repository_id=handoff.repository_id
            )
            if active_count >= MAX_ACTIVE_FIX_ATTEMPTS_PER_REPOSITORY:
                raise FixAttemptValidationError(
                    f"repository already has {active_count} active fix attempts "
                    f"(max {MAX_ACTIVE_FIX_ATTEMPTS_PER_REPOSITORY})"
                )
            total_for_finding = await self._repo.count_for_finding(session, finding_id=handoff.finding_id)
            if total_for_finding >= MAX_FIX_ATTEMPTS_PER_FINDING:
                raise FixAttemptValidationError(
                    f"finding already has {total_for_finding} fix attempts (max {MAX_FIX_ATTEMPTS_PER_FINDING})"
                )

        model, created = await self._repo.create(
            session,
            handoff_id=handoff.handoff_id,
            finding_id=handoff.finding_id,
            repository_id=handoff.repository_id,
            original_commit_sha=handoff.original_commit_sha,
            candidate_fix_commit_sha=candidate_fix_commit_sha,
        )
        await session.commit()

        if created:
            model = await self._run_verification(session, fix_attempt=model, handoff=handoff)
            await session.commit()

        return _to_domain(model)

    async def get_fix_attempt(self, session: AsyncSession, *, fix_attempt_id: uuid.UUID) -> FixAttempt | None:
        model = await self._repo.get_by_id(session, fix_attempt_id=fix_attempt_id)
        if model is None:
            return None
        return _to_domain(model)

    async def _mint_token(self, *, installation_id: int) -> str:
        async with self._http_client_factory() as http_client:
            token_provider = InstallationTokenProvider(
                http_client=http_client,
                app_id=self._settings.github_app_id,
                private_key=self._settings.github_private_key,
                api_base_url=self._settings.github_api_base_url,
            )
            return await token_provider.get_token(installation_id)

    async def _run_verification(
        self, session: AsyncSession, *, fix_attempt: FixAttemptModel, handoff: FindingHandoff
    ) -> FixAttemptModel:
        fix_attempt_id = fix_attempt.id
        candidate_fix_commit_sha = fix_attempt.candidate_fix_commit_sha

        await self._repo.mark_verifying(session, fix_attempt_id=fix_attempt_id)
        await session.commit()

        repository = await session.get(RepositoryModel, handoff.repository_id)
        if repository is None:
            return await self._repo.mark_error(
                session, fix_attempt_id=fix_attempt_id, error_message="repository no longer exists"
            )

        try:
            token = await self._mint_token(installation_id=repository.installation_id)
            clone_url = self._clone_url_factory(repository)

            ancestry_result, change_set = verify_ancestor_with_diff(
                clone_url=clone_url,
                ancestor_sha=handoff.original_commit_sha,
                descendant_sha=candidate_fix_commit_sha,
                token=token,
            )
            if not (ancestry_result.verified and ancestry_result.is_ancestor):
                return await self._repo.mark_result(
                    session,
                    fix_attempt_id=fix_attempt_id,
                    status=FixAttemptStatus.STALE,
                    deterministic_evidence=(f"ancestry check: {ancestry_result.detail}",),
                    executable_verification_outcome=None,
                    remaining_issue_summary=None,
                    limitations=(),
                )

            # verify_ancestor_with_diff's own identical-commit short-circuit
            # (Part U's explicit no-op comparison: candidate_fix_commit_sha
            # == original_commit_sha) proves ancestry without ever fetching
            # anything, so it returns no ChangeSet even though ancestry is
            # verified and true -- the only case that combination can occur
            # in. Nothing can have changed if it's literally the same
            # commit, so this is equivalent to an empty ChangeSet.
            file_changed = change_set is not None and any(
                c.path == handoff.file_path or c.previous_path == handoff.file_path for c in change_set.changes
            )
            evidence: list[str] = []
            if not file_changed:
                evidence.append(f"{handoff.file_path} is byte-identical between the original and candidate commits")
                return await self._repo.mark_result(
                    session,
                    fix_attempt_id=fix_attempt_id,
                    status=FixAttemptStatus.STILL_PRESENT,
                    deterministic_evidence=tuple(evidence),
                    executable_verification_outcome=None,
                    remaining_issue_summary="the flagged code has not changed since the original finding",
                    limitations=(),
                )
            evidence.append(f"{handoff.file_path} changed between the original and candidate commits")

            provider = RepositorySnapshotProvider()
            with provider.acquire(
                clone_url=clone_url,
                commit_sha=candidate_fix_commit_sha,
                repository_full_name=repository.full_name,
                token=token,
            ) as snapshot:
                static_signal = await self._static_signal(
                    session, handoff=handoff, checkout_path=snapshot.root_path, evidence=evidence
                )
                ev_signal, ev_outcome = await self._ev_signal(
                    session,
                    handoff=handoff,
                    repository=repository,
                    fix_attempt_id=fix_attempt_id,
                    candidate_fix_commit_sha=candidate_fix_commit_sha,
                    token=token,
                    evidence=evidence,
                )
                status, remaining_issue, limitations = await self._classify(
                    static_signal=static_signal,
                    ev_signal=ev_signal,
                    handoff=handoff,
                    checkout_path=snapshot.root_path,
                    evidence=evidence,
                )
        except GitError as exc:
            return await self._repo.mark_error(session, fix_attempt_id=fix_attempt_id, error_message=str(exc))

        return await self._repo.mark_result(
            session,
            fix_attempt_id=fix_attempt_id,
            status=status,
            deterministic_evidence=tuple(evidence),
            executable_verification_outcome=ev_outcome,
            remaining_issue_summary=remaining_issue,
            limitations=tuple(limitations),
        )

    async def _static_signal(
        self, session: AsyncSession, *, handoff: FindingHandoff, checkout_path: Path, evidence: list[str]
    ) -> bool | None:
        """``True`` confirms FIXED, ``False`` confirms STILL_PRESENT,
        ``None`` means no signal either way."""

        if not handoff.corroborated_by_static or not handoff.static_finding_ids:
            return None
        finding_model = await session.get(FindingModel, handoff.static_finding_ids[0])
        if finding_model is None:
            return None
        language = detect_language(relative_path=finding_model.file_path)
        if language is None:
            return None
        still_fires = await recheck_static_finding(
            checkout_path=checkout_path,
            file_path=finding_model.file_path,
            source_analyzer=finding_model.source_analyzer,
            rule_id=finding_model.rule_id,
            original_start_line=finding_model.start_line,
            original_end_line=finding_model.end_line,
            language=language,
        )
        if still_fires is None:
            return None
        if still_fires:
            evidence.append(f"static rule {finding_model.rule_id} ({finding_model.source_analyzer}) still fires")
            return False
        evidence.append(f"static rule {finding_model.rule_id} ({finding_model.source_analyzer}) no longer fires")
        return True

    async def _ev_signal(
        self,
        session: AsyncSession,
        *,
        handoff: FindingHandoff,
        repository: RepositoryModel,
        fix_attempt_id: uuid.UUID,
        candidate_fix_commit_sha: str,
        token: str,
        evidence: list[str],
    ) -> tuple[bool | None, str | None]:
        """``True`` confirms FIXED, ``False`` confirms STILL_PRESENT,
        ``None`` means no signal either way."""

        if self._verifier_dispatcher is None:
            return None, None

        finding_model = await session.get(AIFindingModel, handoff.finding_id)
        if finding_model is None:
            return None, None
        candidate_model = await session.get(ReviewCandidateModel, finding_model.candidate_id)
        if candidate_model is None:
            return None, None
        candidate = _reconstruct_candidate(candidate_model)
        target = determine_verification_target(candidate=candidate, expected_companions=())
        if target is None:
            return None, None

        destination_root = Path(self._settings.verification_snapshot_root or tempfile.gettempdir())
        try:
            artifact_dir = export_artifact(
                clone_url=self._clone_url_factory(repository),
                commit_sha=candidate_fix_commit_sha,
                repository_full_name=repository.full_name,
                token=token,
                destination_root=destination_root,
            )
        except ArtifactExportError:
            return None, None

        try:
            digest = compute_artifact_digest(artifact_dir)
            request = VerificationExecutionRequest(
                request_id=compute_request_id(
                    repository_id=str(handoff.repository_id),
                    review_run_id=f"fix-attempt-{fix_attempt_id}",
                    commit_sha=candidate_fix_commit_sha,
                    file_path=candidate.file_path,
                    qualified_name=candidate.qualified_name,
                    verification_kind=VerificationKind.EXISTING_TARGETED_TEST,
                    test_target_path=target,
                ),
                protocol_version=VERIFIER_PROTOCOL_VERSION,
                repository_id=str(handoff.repository_id),
                review_run_id=f"fix-attempt-{fix_attempt_id}",
                commit_sha=candidate_fix_commit_sha,
                verification_kind=VerificationKind.EXISTING_TARGETED_TEST,
                test_target_path=target,
                artifact_id=artifact_dir.name,
                artifact_digest=digest,
                timeout_seconds=_EV_TIMEOUT_SECONDS,
            )
            result = await self._verifier_dispatcher.dispatch(request)
        finally:
            shutil.rmtree(artifact_dir, ignore_errors=True)

        if result is None:
            return None, None
        if result.outcome is VerificationOutcome.PASSED:
            evidence.append(f"executable verification of {target}: PASSED")
            return True, result.outcome.value
        if result.outcome is VerificationOutcome.CONFIRMED_FAILURE:
            evidence.append(f"executable verification of {target}: CONFIRMED_FAILURE")
            return False, result.outcome.value
        evidence.append(f"executable verification of {target}: {result.outcome.value} (no signal)")
        return None, result.outcome.value

    async def _classify(
        self,
        *,
        static_signal: bool | None,
        ev_signal: bool | None,
        handoff: FindingHandoff,
        checkout_path: Path,
        evidence: list[str],
    ) -> tuple[FixAttemptStatus, str | None, list[str]]:
        limitations = [
            "did not re-execute against the original commit for a true before/after comparison "
            "(see validation/agent_handoff/latest-summary.md section 1.3)"
        ]
        signals = [s for s in (static_signal, ev_signal) if s is not None]
        if False in signals:
            return (
                FixAttemptStatus.STILL_PRESENT,
                "at least one deterministic check still detects the original condition",
                limitations,
            )
        if True in signals:
            return FixAttemptStatus.FIXED, None, limitations

        if self._fix_critic_provider is None:
            limitations.append("no fix-verification provider configured -- no deterministic signal was available")
            return FixAttemptStatus.INCONCLUSIVE, None, limitations

        excerpt = _read_excerpt(
            checkout_path / handoff.file_path, start_line=handoff.start_line, end_line=handoff.end_line
        )
        if excerpt is None:
            limitations.append("the flagged file no longer exists at this location")
            return FixAttemptStatus.INCONCLUSIVE, None, limitations

        judged = await judge_fix(
            self._fix_critic_provider,
            title=handoff.title,
            message=handoff.message,
            reasoning_summary=handoff.reasoning_summary,
            file_path=handoff.file_path,
            new_code_excerpt=excerpt,
        )
        if judged is None:
            limitations.append("fix-verification model call failed")
            return FixAttemptStatus.INCONCLUSIVE, None, limitations

        decision, reasoning = judged
        evidence.append(f"fix-verification model judgment: {decision.value} -- {reasoning}")
        if decision is FixCriticDecision.FIXED:
            return FixAttemptStatus.FIXED, None, limitations
        if decision is FixCriticDecision.STILL_PRESENT:
            return FixAttemptStatus.STILL_PRESENT, reasoning, limitations
        return FixAttemptStatus.INCONCLUSIVE, None, limitations
