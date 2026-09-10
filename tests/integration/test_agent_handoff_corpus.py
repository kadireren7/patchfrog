"""Real, DB-backed corpus for Agent Handoff (Milestone T, T1) -- a real
``RepositoryModel``/``ReviewRunModel``/``ReviewCandidateModel``/
``AIFindingProposalModel``/``AIFindingModel``/``CriticVerdictModel`` row
chain, exactly the schema the real reviewer pipeline writes to (mirrors
``tests/integration/test_historical_regression_memory_corpus.py``'s own
``_stage_historical_finding`` pattern), never a hand-constructed
``FindingHandoff`` standing in for a real DB round trip.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import fields
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.agent_handoff.domain import FindingHandoff, compute_handoff_id
from patchfrog.agent_handoff.service import AgentHandoffService, HandoffUnavailableReason
from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.persistence.models.review import (
    AIFindingModel,
    AIFindingProposalModel,
    CriticVerdictModel,
    ReviewCandidateModel,
    ReviewRunModel,
)
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import (
    CriticDecision,
    ProposalStatus,
    ReviewCandidateReason,
    ReviewRunStatus,
)


async def _make_repository(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=1,
        )
        await session.commit()
        return repo.id


async def _stage_finding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    commit_sha: str | None = None,
    evidence: list[dict[str, object]] | None = None,
    static_finding_ids: list[uuid.UUID] | None = None,
    corroborated_by_static: bool = False,
    with_critic_verdict: bool = False,
    title: str = "SQL injection via unsanitized input",
    message: str = "user_input is concatenated directly into the query string",
    impact: str | None = "an attacker can read arbitrary rows",
    suggested_fix: str | None = "use a parameterized query",
) -> tuple[uuid.UUID, uuid.UUID, str]:
    sha = commit_sha or uuid.uuid4().hex[:40].ljust(40, "0")
    async with session_factory() as session:
        run = ReviewRunModel(
            id=uuid.uuid4(), repository_id=repository_id, repository_index_id=uuid.uuid4(),
            commit_sha=sha, config_fingerprint="c" * 64, model_fingerprint="m" * 64,
            incremental_context_fingerprint="i" * 64, status=ReviewRunStatus.SUCCEEDED,
            reviewer_provider="fake", reviewer_model="fake-model",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()

        candidate = ReviewCandidateModel(
            id=uuid.uuid4(), review_run_id=run.id, file_path="app/db.py", symbol_id=None,
            symbol_name="run_query", qualified_name="app.db.run_query", start_line=10, end_line=20,
            changed_lines=json.dumps([12, 13]), reason=ReviewCandidateReason.CHANGED_SYMBOL,
        )
        session.add(candidate)
        await session.flush()

        proposal = AIFindingProposalModel(
            id=uuid.uuid4(), review_run_id=run.id, candidate_id=candidate.id, title=title, message=message,
            category=FindingCategory.SECURITY, severity=Severity.HIGH, confidence=Confidence.HIGH,
            file_path="app/db.py", start_line=12, end_line=13, evidence="[]",
            reasoning_summary="user_input reaches the query without escaping", suggested_fix=suggested_fix,
            impact=impact, status=ProposalStatus.ACCEPTED, agent_role=AgentRole.SECURITY,
        )
        session.add(proposal)
        await session.flush()

        finding = AIFindingModel(
            id=uuid.uuid4(), review_run_id=run.id, proposal_id=proposal.id, candidate_id=candidate.id,
            title=title, message=message, category=FindingCategory.SECURITY, severity=Severity.HIGH,
            confidence=Confidence.HIGH, file_path="app/db.py", start_line=12, end_line=13,
            evidence=json.dumps(evidence if evidence is not None else []),
            reasoning_summary="user_input reaches the query without escaping", suggested_fix=suggested_fix,
            impact=impact, corroborated_by_static=corroborated_by_static,
            static_finding_ids=json.dumps([str(i) for i in (static_finding_ids or [])]),
            agent_role=AgentRole.SECURITY,
        )
        session.add(finding)
        await session.flush()

        if with_critic_verdict:
            verdict = CriticVerdictModel(
                id=uuid.uuid4(), proposal_id=proposal.id, decision=CriticDecision.ACCEPT,
                reasoning_summary="confirmed: raw string concatenation into SQL", provider="fake", model="fake-model",
            )
            session.add(verdict)

        await session.commit()
        return finding.id, run.id, sha


async def test_handoff_id_deterministic_across_two_builds(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    finding_id, run_id, sha = await _stage_finding(session_factory, repository_id=repository_id)

    async with session_factory() as session:
        first, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)
    async with session_factory() as session:
        second, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)

    assert first is not None and second is not None
    assert first.handoff_id == second.handoff_id
    assert first.handoff_id == compute_handoff_id(
        repository_id=repository_id, finding_id=finding_id, review_run_id=run_id, original_commit_sha=sha
    )


async def test_handoff_preserves_exact_original_sha(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    finding_id, run_id, sha = await _stage_finding(session_factory, repository_id=repository_id)

    async with session_factory() as session:
        handoff, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)

    assert handoff is not None
    assert handoff.original_commit_sha == sha
    assert handoff.review_run_id == run_id


async def test_handoff_preserves_finding_identity(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    finding_id, _run_id, _sha = await _stage_finding(session_factory, repository_id=repository_id)

    async with session_factory() as session:
        handoff, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)

    assert handoff is not None
    assert handoff.finding_id == finding_id
    assert handoff.repository_id == repository_id
    assert handoff.file_path == "app/db.py"
    assert handoff.start_line == 12
    assert handoff.end_line == 13
    assert handoff.qualified_name == "app.db.run_query"


async def test_handoff_evidence_is_bounded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    many_evidence = [
        {"file_path": "app/db.py", "start_line": i, "end_line": i, "quoted_text": f"line {i}"} for i in range(20)
    ]
    finding_id, _run_id, _sha = await _stage_finding(session_factory, repository_id=repository_id, evidence=many_evidence)

    async with session_factory() as session:
        handoff, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)

    assert handoff is not None
    assert len(handoff.evidence) <= 5


async def test_handoff_returns_finding_not_found_for_unknown_id(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        handoff, reason = await AgentHandoffService().build_handoff(session, finding_id=uuid.uuid4())

    assert handoff is None
    assert reason is HandoffUnavailableReason.FINDING_NOT_FOUND


async def test_handoff_never_fabricates_missing_impact_or_suggested_fix(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    finding_id, _run_id, _sha = await _stage_finding(
        session_factory, repository_id=repository_id, impact=None, suggested_fix=None,
    )

    async with session_factory() as session:
        handoff, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)

    assert handoff is not None
    assert handoff.impact is None
    assert handoff.suggested_fix is None


async def test_handoff_executable_verification_evidence_is_always_none_in_v1(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """See validation/agent_handoff/latest-summary.md section 1.3 -- an
    honest, documented v1 gap, never fabricated."""

    repository_id = await _make_repository(session_factory, "acme/widgets")
    finding_id, _run_id, _sha = await _stage_finding(session_factory, repository_id=repository_id)

    async with session_factory() as session:
        handoff, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)

    assert handoff is not None
    assert handoff.executable_verification_evidence is None


async def test_handoff_carries_critic_reasoning_summary_when_a_verdict_exists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    finding_id, _run_id, _sha = await _stage_finding(
        session_factory, repository_id=repository_id, with_critic_verdict=True,
    )

    async with session_factory() as session:
        handoff, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)

    assert handoff is not None
    assert handoff.critic_reasoning_summary == "confirmed: raw string concatenation into SQL"


async def test_handoff_critic_reasoning_summary_is_none_without_a_verdict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    finding_id, _run_id, _sha = await _stage_finding(session_factory, repository_id=repository_id)

    async with session_factory() as session:
        handoff, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)

    assert handoff is not None
    assert handoff.critic_reasoning_summary is None


async def test_handoff_static_corroboration_is_preserved(session_factory: async_sessionmaker[AsyncSession]) -> None:
    static_id = uuid.uuid4()
    repository_id = await _make_repository(session_factory, "acme/widgets")
    finding_id, _run_id, _sha = await _stage_finding(
        session_factory, repository_id=repository_id, corroborated_by_static=True, static_finding_ids=[static_id],
    )

    async with session_factory() as session:
        handoff, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)

    assert handoff is not None
    assert handoff.corroborated_by_static is True
    assert handoff.static_finding_ids == (static_id,)


async def test_handoff_secret_shaped_evidence_is_redacted(session_factory: async_sessionmaker[AsyncSession]) -> None:
    secret_evidence = [
        {
            "file_path": "app/db.py", "start_line": 1, "end_line": 1,
            "quoted_text": "TOKEN = 'ghp_1234567890abcdefghijklmnopqrstuvwx'",
        }
    ]
    repository_id = await _make_repository(session_factory, "acme/widgets")
    finding_id, _run_id, _sha = await _stage_finding(session_factory, repository_id=repository_id, evidence=secret_evidence)

    async with session_factory() as session:
        handoff, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)

    assert handoff is not None
    assert "ghp_1234567890abcdefghijklmnopqrstuvwx" not in handoff.evidence[0].quoted_text
    assert "[REDACTED]" in handoff.evidence[0].quoted_text


async def test_handoff_not_published_by_default(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repository(session_factory, "acme/widgets")
    finding_id, _run_id, _sha = await _stage_finding(session_factory, repository_id=repository_id)

    async with session_factory() as session:
        handoff, _ = await AgentHandoffService().build_handoff(session, finding_id=finding_id)

    assert handoff is not None
    assert handoff.published is False
    assert handoff.github_review_id is None


async def test_handoff_schema_never_carries_internal_telemetry_or_agent_orchestration_noise() -> None:
    """Part D/J: token budgets, critic call counts, agent count, raw
    provider-internal confidence, and P/Q/R internal-only signals must
    never become handoff fields."""

    field_names = {f.name for f in fields(FindingHandoff)}
    forbidden_substrings = (
        "token", "latency_ms", "call_count", "agent_role", "trajectory", "cross_pr", "cross_repo",
        "prompt", "thinking",
    )
    for name in field_names:
        for forbidden in forbidden_substrings:
            assert forbidden not in name, f"FindingHandoff.{name} looks like internal/orchestration noise"
