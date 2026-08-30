"""End-to-end Agent Orchestration v1 coverage against the real
``ai_review_python`` fixture, exercising :class:`PullRequestReviewService`
through its real :class:`~patchfrog.review.orchestration.AgentOrchestrator`
wiring with :class:`FakeLLMProvider`. Covers the required test scenarios
from the Agent Orchestration v1 spec not already covered by
``test_review_agent_selection.py``/``test_review_critic_selection.py``/
``test_review_agents_cross_role.py`` (unit-level) or
``test_review_failure_recovery.py``/``test_review_cost_guard.py``
(pre-existing, updated for the new dual-call-per-candidate behavior)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import Confidence
from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import ReviewRunModel
from patchfrog.persistence.repositories import AIFindingProposalRepository, RepositoryRepository
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.config import ReviewConfig
from patchfrog.review.domain import ProposalStatus, ReviewRunStatus
from patchfrog.review.provider import ProviderFatalError, ProviderTransientError
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse, route_by_schema_name
from patchfrog.review.service import PullRequestReviewService
from tests.support.git_repo import materialize_fixture_repo

_ACCEPT_VERDICT = ScriptedResponse(
    raw_json=json.dumps(
        {"decision": "accept", "reasoning_summary": "ok", "downgraded_severity": None, "downgraded_confidence": None}
    )
)
_REJECT_VERDICT = ScriptedResponse(
    raw_json=json.dumps(
        {"decision": "reject", "reasoning_summary": "not supported", "downgraded_severity": None, "downgraded_confidence": None}
    )
)
_NO_FINDINGS = ScriptedResponse(raw_json=json.dumps({"findings": []}))


def _correctness_finding(**overrides: object) -> dict[str, object]:
    finding = {
        "title": "Inverted comparison in can_withdraw",
        "message": "amount >= balance allows a withdrawal larger than the balance to succeed.",
        "category": "correctness",
        "severity": "high",
        "confidence": "high",
        "file_path": "src/billing.py",
        "start_line": 14,
        "end_line": 14,
        "evidence": [
            {"file_path": "src/billing.py", "start_line": 14, "end_line": 14, "quoted_text": "return amount >= balance"}
        ],
        "reasoning_summary": "The comparison operands are reversed.",
        "suggested_fix": "return balance >= amount",
    }
    finding.update(overrides)
    return finding


def _security_finding(**overrides: object) -> dict[str, object]:
    finding = {
        "title": "Unsafe SQL string interpolation",
        "message": "user_id is interpolated directly into the SQL string.",
        "category": "security",
        "severity": "high",
        "confidence": "high",
        "file_path": "src/billing.py",
        "start_line": 14,
        "end_line": 14,
        "evidence": [
            {"file_path": "src/billing.py", "start_line": 14, "end_line": 14, "quoted_text": "return amount >= balance"}
        ],
        "reasoning_summary": "Untrusted input reaches the query without parameterization.",
        "suggested_fix": "use a parameterized query",
    }
    finding.update(overrides)
    return finding


def _diff_marking_lines(file_path: str, lines: list[int]) -> DiffFile:
    diff_lines = tuple(
        DiffLine(line_type=DiffLineType.ADDITION, old_line_number=None, new_line_number=n, content="x")
        for n in lines
    )
    hunk = DiffHunk(
        old_start=1, old_lines=0, new_start=min(lines), new_lines=len(lines),
        section_heading=None, lines=diff_lines,
    )
    return DiffFile(path=file_path, hunks=(hunk,))


async def _setup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    full_name: str,
    force_security_signal: bool = False,
) -> tuple[uuid.UUID, str, Path]:
    """``force_security_signal``: rename ``can_withdraw`` to
    ``authorize_withdraw`` in this test's own isolated fixture copy only
    (never the shared ``ai_review_python`` fixture) -- gives the
    Quality + Cost Guard (patchfrog.review.effort) a real, non-fallback
    security-naming signal (see
    patchfrog.review.agents.selection.AgentSelectionReason.SECURITY_SENSITIVE_NAMING),
    tiering the candidate DEEP so Security is guaranteed to run. Used
    only by tests whose actual purpose is orchestration mechanics
    (critic behavior, contradiction handling, cross-agent validation)
    unrelated to tiering itself -- tiering-selection tests exercise the
    plain, unmodified fixture."""

    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0], name=full_name.split("/")[-1],
            full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-ai-orch-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    commit_sha = snapshot.commit_sha
    if force_security_signal:
        billing_path = snapshot.root_path / "src" / "billing.py"
        billing_path.write_text(billing_path.read_text().replace("can_withdraw", "authorize_withdraw"))
        from tests.support.git_repo import commit_all

        commit_sha = commit_all(snapshot.root_path, "rename for security naming signal")
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    return repository_id, commit_sha, snapshot.root_path


async def _run_run_ids(session_factory: async_sessionmaker[AsyncSession], repository_id: uuid.UUID) -> uuid.UUID:
    async with session_factory() as session:
        return (
            (await session.execute(select(ReviewRunModel.id).where(ReviewRunModel.repository_id == repository_id)))
            .scalars()
            .one()
        )


async def test_correctness_only_finding_survives(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Required scenario 1."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/orch-correctness-only")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(
        response_factory=route_by_schema_name(
            {"review_response:correctness": ScriptedResponse(raw_json=json.dumps({"findings": [_correctness_finding()]}))},
            default=_NO_FINDINGS,
        )
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-correctness-only",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.status == ReviewRunStatus.SUCCEEDED
    assert summary.accepted_count == 1


async def test_security_only_finding_survives(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Required scenario 2."""

    repository_id, commit_sha, root_path = await _setup(
        session_factory, full_name="test/orch-security-only", force_security_signal=True
    )
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(
        response_factory=route_by_schema_name(
            {"review_response:security": ScriptedResponse(raw_json=json.dumps({"findings": [_security_finding()]}))},
            default=_NO_FINDINGS,
        )
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-security-only",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.status == ReviewRunStatus.SUCCEEDED
    assert summary.accepted_count == 1

    async with session_factory() as session:
        run_id = await _run_run_ids(session_factory, repository_id)
        proposals = await AIFindingProposalRepository().list_for_run(session, review_run_id=run_id)
    accepted = [p for p in proposals if p.status == ProposalStatus.ACCEPTED]
    assert len(accepted) == 1
    assert accepted[0].agent_role == AgentRole.SECURITY


async def test_both_agents_return_no_findings(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Required scenario 3."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/orch-no-findings")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(response_factory=lambda req: _NO_FINDINGS)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-no-findings",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.status == ReviewRunStatus.SUCCEEDED
    assert summary.accepted_count == 0
    assert summary.proposals_count == 0


async def test_hallucinated_security_evidence_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Required scenario 10."""

    repository_id, commit_sha, root_path = await _setup(
        session_factory, full_name="test/orch-halluc-security", force_security_signal=True
    )
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(
        response_factory=route_by_schema_name(
            {
                "review_response:security": ScriptedResponse(
                    raw_json=json.dumps({"findings": [_security_finding(evidence=[
                        {"file_path": "src/billing.py", "start_line": 14, "end_line": 14, "quoted_text": "text that never appears"}
                    ])]})
                ),
            },
            default=_NO_FINDINGS,
        )
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-halluc-security",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.accepted_count == 0

    async with session_factory() as session:
        run_id = await _run_run_ids(session_factory, repository_id)
        proposals = await AIFindingProposalRepository().list_for_run(session, review_run_id=run_id)
    rejected = [p for p in proposals if p.status == ProposalStatus.REJECTED_VALIDATION and p.agent_role == AgentRole.SECURITY]
    assert len(rejected) == 1


async def test_hallucinated_correctness_evidence_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Required scenario 11."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/orch-halluc-correctness")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(
        response_factory=route_by_schema_name(
            {
                "review_response:correctness": ScriptedResponse(
                    raw_json=json.dumps({"findings": [_correctness_finding(evidence=[
                        {"file_path": "src/billing.py", "start_line": 14, "end_line": 14, "quoted_text": "text that never appears"}
                    ])]})
                ),
            },
            default=_NO_FINDINGS,
        )
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-halluc-correctness",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.accepted_count == 0

    async with session_factory() as session:
        run_id = await _run_run_ids(session_factory, repository_id)
        proposals = await AIFindingProposalRepository().list_for_run(session, review_run_id=run_id)
    rejected = [p for p in proposals if p.status == ProposalStatus.REJECTED_VALIDATION and p.agent_role == AgentRole.CORRECTNESS]
    assert len(rejected) == 1


async def test_deterministic_evidence_validation_runs_independently_per_agent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required scenario 9: one role hallucinates, the other is valid --
    each is judged solely on its own evidence."""

    repository_id, commit_sha, root_path = await _setup(
        session_factory, full_name="test/orch-mixed-validity", force_security_signal=True
    )
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(
        response_factory=route_by_schema_name(
            {
                "review_response:correctness": ScriptedResponse(
                    raw_json=json.dumps({"findings": [_correctness_finding()]})
                ),
                "review_response:security": ScriptedResponse(
                    raw_json=json.dumps({"findings": [_security_finding(evidence=[
                        {"file_path": "src/billing.py", "start_line": 14, "end_line": 14, "quoted_text": "never appears"}
                    ])]})
                ),
            }
        )
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-mixed-validity",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.accepted_count == 1

    async with session_factory() as session:
        run_id = await _run_run_ids(session_factory, repository_id)
        proposals = await AIFindingProposalRepository().list_for_run(session, review_run_id=run_id)
    by_role = {p.agent_role: p.status for p in proposals}
    assert by_role[AgentRole.CORRECTNESS] == ProposalStatus.ACCEPTED
    assert by_role[AgentRole.SECURITY] == ProposalStatus.REJECTED_VALIDATION


async def test_critic_rejection_works_for_security_role(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Required scenario 13 (security side)."""

    repository_id, commit_sha, root_path = await _setup(
        session_factory, full_name="test/orch-critic-reject-sec", force_security_signal=True
    )
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(
        response_factory=route_by_schema_name(
            {"review_response:security": ScriptedResponse(raw_json=json.dumps({"findings": [_security_finding()]}))},
            default=_NO_FINDINGS,
        )
    )
    critic = FakeLLMProvider(response_factory=lambda req: _REJECT_VERDICT)
    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic
    )
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-critic-reject-sec",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.accepted_count == 0

    async with session_factory() as session:
        run_id = await _run_run_ids(session_factory, repository_id)
        proposals = await AIFindingProposalRepository().list_for_run(session, review_run_id=run_id)
    security_proposal = next(p for p in proposals if p.agent_role == AgentRole.SECURITY)
    assert security_proposal.status == ProposalStatus.REJECTED_CRITIC


async def test_critic_rejection_works_for_correctness_role(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required scenario 13 (correctness side)."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/orch-critic-reject-cor")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(
        response_factory=route_by_schema_name(
            {"review_response:correctness": ScriptedResponse(raw_json=json.dumps({"findings": [_correctness_finding()]}))},
            default=_NO_FINDINGS,
        )
    )
    critic = FakeLLMProvider(response_factory=lambda req: _REJECT_VERDICT)
    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic
    )
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-critic-reject-cor",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.accepted_count == 0

    async with session_factory() as session:
        run_id = await _run_run_ids(session_factory, repository_id)
        proposals = await AIFindingProposalRepository().list_for_run(session, review_run_id=run_id)
    correctness_proposal = next(p for p in proposals if p.agent_role == AgentRole.CORRECTNESS)
    assert correctness_proposal.status == ProposalStatus.REJECTED_CRITIC


async def test_critic_downgrade_works(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Required scenario 14."""

    repository_id, commit_sha, root_path = await _setup(
        session_factory, full_name="test/orch-critic-downgrade", force_security_signal=True
    )
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    downgrade_verdict = ScriptedResponse(
        raw_json=json.dumps(
            {"decision": "downgrade", "reasoning_summary": "overstated", "downgraded_severity": "low", "downgraded_confidence": "medium"}
        )
    )
    reviewer = FakeLLMProvider(
        response_factory=route_by_schema_name(
            {"review_response:security": ScriptedResponse(raw_json=json.dumps({"findings": [_security_finding()]}))},
            default=_NO_FINDINGS,
        )
    )
    critic = FakeLLMProvider(response_factory=lambda req: downgrade_verdict)
    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic
    )
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-critic-downgrade",
        commit_sha=commit_sha, diff_files=diff_files, config=ReviewConfig(min_final_confidence=Confidence.LOW),
    )
    assert summary.accepted_count == 1


async def test_one_specialist_failure_yields_partial_useful_result(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required scenario 16: Correctness succeeds, Security transiently
    fails -- the candidate is still reviewed (not marked failed), and the
    useful Correctness result survives."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/orch-partial-failure")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(
        response_factory=route_by_schema_name(
            {
                "review_response:correctness": ScriptedResponse(
                    raw_json=json.dumps({"findings": [_correctness_finding()]})
                ),
                "review_response:security": ProviderFatalError("security backend unavailable"),
            }
        )
    )
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-partial-failure",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.status == ReviewRunStatus.SUCCEEDED
    assert summary.candidates_failed == 0
    assert summary.accepted_count == 1


async def test_both_specialist_failures_yields_candidate_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required scenario 17."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/orch-both-fail")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(response_factory=lambda req: ProviderFatalError("always fails"))
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-both-fail",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.status == ReviewRunStatus.FAILED
    assert summary.candidates_failed == 1
    assert summary.candidates_reviewed == 0


async def test_global_token_budget_cannot_be_exceeded_by_two_agents(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required scenario 18: a candidate whose combined two-role prompt
    estimate exceeds the remaining budget is skipped entirely (never
    partially reviewed by only one role)."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/orch-budget")
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(response_factory=lambda req: _NO_FINDINGS)
    # A budget too small for even one role's prompt -- both roles must be
    # skipped together for this candidate.
    config = ReviewConfig(max_total_input_tokens=1)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-budget",
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )
    assert summary.candidates_skipped_budget == 1
    assert summary.candidates_reviewed == 0
    assert len(reviewer.calls) == 0


async def test_contradictory_proposals_are_suppressed_when_critic_cannot_resolve(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required scenarios 7 + 8, end-to-end through the real service and
    critic: a contradiction forces critique on both sides; if the critic
    accepts both (cannot confidently resolve which is correct), both are
    suppressed rather than published."""

    repository_id, commit_sha, root_path = await _setup(
        session_factory, full_name="test/orch-contradiction", force_security_signal=True
    )
    diff_files = [_diff_marking_lines("src/billing.py", [14])]

    reviewer = FakeLLMProvider(
        response_factory=route_by_schema_name(
            {
                "review_response:security": ScriptedResponse(
                    raw_json=json.dumps({"findings": [_security_finding(
                        message="input is unsanitized before reaching the query",
                        reasoning_summary="untrusted value flows to the sink unsanitized",
                    )]})
                ),
                "review_response:correctness": ScriptedResponse(
                    raw_json=json.dumps({"findings": [_correctness_finding(
                        message="the sanitizer guarantees this value is already safe",
                        reasoning_summary="input is validated and sanitized upstream",
                    )]})
                ),
            }
        )
    )
    critic = FakeLLMProvider(response_factory=lambda req: _ACCEPT_VERDICT)
    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic
    )
    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-contradiction",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.accepted_count == 0

    async with session_factory() as session:
        run_id = await _run_run_ids(session_factory, repository_id)
        proposals = await AIFindingProposalRepository().list_for_run(session, review_run_id=run_id)
    assert len(proposals) == 2
    assert all(p.status == ProposalStatus.SUPPRESSED_CONTRADICTION for p in proposals)
    # Both were still critiqued -- the critic call did happen for both.
    assert len(critic.calls) == 2


async def test_cost_safety_bounded_call_count_under_scripted_failures(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Section 24 cost-safety regression: orchestration must never turn
    N candidates into an unbounded N x agents x critic x retries call
    explosion. Every reviewer call is scripted to transiently fail (so
    every retry is actually exhausted) -- the total call count must
    still be exactly bounded by the configured
    max_candidates x enabled_roles x (1 + max_retries) formula, never
    more."""

    repository_id, commit_sha, root_path = await _setup(session_factory, full_name="test/orch-cost-safety")
    # Several well-separated module-region candidates (no containing
    # symbol needed) -- see test_review_cost_guard.py's
    # _synthetic_module_diff for the same clustering-avoidance pattern.
    lines = [1 + i * 200 for i in range(10)]
    diff_files = [_diff_marking_lines("src/billing.py", lines)]

    max_candidates = 5
    max_retries = 2
    roles_enabled = 2  # Correctness + Security, both selected by default in v1

    reviewer = FakeLLMProvider(response_factory=lambda req: ProviderTransientError("always transiently fails"))
    config = ReviewConfig(max_candidates=max_candidates, max_retries=max_retries, max_concurrent_requests=1)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)

    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/orch-cost-safety",
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )

    assert summary.candidate_count <= max_candidates
    max_allowed_calls = max_candidates * roles_enabled * (1 + max_retries)
    assert len(reviewer.calls) <= max_allowed_calls
    # Every candidate actually failed (both roles exhausted retries) --
    # confirms the bound above isn't trivially satisfied by an early
    # skip/short-circuit that never actually attempted every candidate.
    assert summary.candidates_failed == summary.candidate_count
