"""M4 Ultra-Low-Cost Review Engine, end to end through the real
:class:`PullRequestReviewService` (real git diff, real indexing, real
context engine, real persistence) with a scripted
:class:`FakeLLMProvider` -- never a live provider.

Covers: zero-call NO_AI paths (M4.2), single-pass review (M4.3),
sequential reason-carrying escalation (M4.4), on-demand critic (M4.5),
context metrics (M4.6), exact-head reuse + force (M4.7), per-tier budgets
incl. the verification reserve (M4.8), and cost telemetry (M4.9).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.models import DiffFile
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import (
    AIFindingProposalModel,
    ReviewCandidateModel,
    ReviewRunModel,
)
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.config import ReviewConfig
from patchfrog.review.cost_policy import ReviewCostPolicy, ReviewStrategy
from patchfrog.review.domain import ProposalStatus, ReviewRunStatus, ReviewRunSummary
from patchfrog.review.local_diff import diff_against_base
from patchfrog.review.provider import ProviderRequest, ProviderTransientError
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from tests.support.git_repo import commit_all, materialize_fixture_repo

_NO_FINDINGS = ScriptedResponse(raw_json=json.dumps({"findings": []}))
_ACCEPT = ScriptedResponse(
    raw_json=json.dumps(
        {"decision": "accept", "reasoning_summary": "verified", "downgraded_severity": None,
         "downgraded_confidence": None}
    )
)


def _finding(
    *, line: int, quote: str, severity: str = "medium", confidence: str = "high", category: str = "correctness",
    file_path: str = "src/billing.py",
) -> dict[str, Any]:
    return {
        "title": "boundary", "message": "`amount >= balance` rejects exact-balance withdrawals",
        "category": category, "severity": severity, "confidence": confidence, "file_path": file_path,
        "start_line": line, "end_line": line,
        "evidence": [{"file_path": file_path, "start_line": line, "end_line": line, "quoted_text": quote}],
        "reasoning_summary": "The comparison is inverted.", "suggested_fix": None, "impact": None,
    }


def _router(
    reviewer: Callable[[ProviderRequest], ScriptedResponse | Exception],
    *,
    critic: ScriptedResponse = _ACCEPT,
) -> Callable[[ProviderRequest], ScriptedResponse | Exception]:
    def route(request: ProviderRequest) -> ScriptedResponse | Exception:
        if request.schema_name == "critic_verdict":
            return critic
        return reviewer(request)

    return route


async def _setup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mutate: Callable[[Path], None],
) -> tuple[uuid.UUID, str, Path, list[DiffFile], str]:
    full_name = f"test/m4-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=uuid.uuid4().int % (2**62), owner="test",
            name=full_name.split("/")[1], full_name=full_name, installation_id=0,
        )
        await session.commit()
    snapshot = materialize_fixture_repo(tmp_path / "repo", "ai_review_python", full_name=full_name)
    root = snapshot.root_path
    mutate(root)
    commit_sha = commit_all(root, "change under review")
    diff_files = diff_against_base(root, "HEAD~1")
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=row.id, root_path=root, repository_full_name=full_name
    )
    return row.id, commit_sha, root, diff_files, full_name


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


async def _review(
    session_factory: async_sessionmaker[AsyncSession],
    provider: FakeLLMProvider,
    setup: tuple[uuid.UUID, str, Path, list[DiffFile], str],
    *,
    policy: ReviewCostPolicy | None = None,
    force_review: bool = False,
    config: ReviewConfig | None = None,
) -> ReviewRunSummary:
    repository_id, commit_sha, root, diff_files, full_name = setup
    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=provider, critic_provider=provider,
        cost_policy=policy or ReviewCostPolicy(),
    )
    return await service.review_local(
        repository_id=repository_id, root_path=root, repository_full_name=full_name, commit_sha=commit_sha,
        diff_files=diff_files, config=config or ReviewConfig(max_concurrent_requests=1),
        force_review=force_review,
    )


async def _run_row(session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> ReviewRunModel:
    async with session_factory() as session:
        row = await session.get(ReviewRunModel, run_id)
        assert row is not None
        return row


_TINY_CHANGE = ("return amount >= balance", "return amount >= balance or amount == 0")


# -- M4.2 zero-call paths ----------------------------------------------------------


async def test_comment_only_pr_makes_zero_provider_calls_and_still_completes_a_run(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    setup = await _setup(
        session_factory, tmp_path,
        lambda root: _replace(root / "src/billing.py", "# Logical comparison bug", "# Inverted comparison bug"),
    )
    provider = FakeLLMProvider(response_factory=lambda r: AssertionError("no provider call allowed"))
    summary = await _review(session_factory, provider, setup)

    assert provider.calls == []
    assert summary.status is ReviewRunStatus.SUCCEEDED
    assert summary.risk_tier == "no_ai"
    assert summary.no_ai_reason == "comment_only"
    assert summary.critic_calls == 0
    report = summary.cost_report()
    assert report["provider_calls"] == 0 and report["budget_status"] == "within_budget"
    row = await _run_row(session_factory, summary.run_id)
    assert row.status is ReviewRunStatus.SUCCEEDED
    assert (row.risk_tier, row.no_ai_reason, row.provider_calls) == ("no_ai", "comment_only", 0)


async def test_docs_only_pr_makes_zero_provider_calls(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    def mutate(root: Path) -> None:
        (root / "README.md").write_text("# Billing\n")

    setup = await _setup(session_factory, tmp_path, mutate)
    provider = FakeLLMProvider(response_factory=lambda r: AssertionError("no provider call allowed"))
    summary = await _review(session_factory, provider, setup)
    assert provider.calls == []
    assert (summary.risk_tier, summary.no_ai_reason) == ("no_ai", "docs_only")


# -- M4.3 single pass / M4.5 on-demand critic --------------------------------------------


async def test_tiny_clean_pr_makes_exactly_one_unified_call_and_no_critic_call(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    setup = await _setup(session_factory, tmp_path, lambda root: _replace(root / "src/billing.py", *_TINY_CHANGE))
    provider = FakeLLMProvider(response_factory=_router(lambda r: _NO_FINDINGS))
    summary = await _review(session_factory, provider, setup)

    assert [c.schema_name for c in provider.calls] == ["review_response:unified"]
    assert summary.risk_tier == "tiny"
    assert summary.critic_calls == 0
    assert summary.calls_by_role == {AgentRole.UNIFIED: 1}
    assert summary.cost_report()["provider_calls"] == 1
    assert summary.context_initial_tokens > 0
    assert summary.context_expanded_tokens == 0  # TINY never expands context


async def test_low_risk_high_confidence_finding_may_skip_the_critic(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    setup = await _setup(session_factory, tmp_path, lambda root: _replace(root / "src/billing.py", *_TINY_CHANGE))
    finding = _finding(line=14, quote=_TINY_CHANGE[1])
    provider = FakeLLMProvider(
        response_factory=_router(lambda r: ScriptedResponse(raw_json=json.dumps({"findings": [finding]})))
    )
    summary = await _review(session_factory, provider, setup)
    assert summary.accepted_count == 1
    assert summary.critic_calls == 0
    assert len(provider.calls) == 1


async def test_high_risk_finding_is_critic_verified_using_the_verification_reserve(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    setup = await _setup(session_factory, tmp_path, lambda root: _replace(root / "src/billing.py", *_TINY_CHANGE))
    finding = _finding(line=14, quote=_TINY_CHANGE[1], severity="high")
    provider = FakeLLMProvider(
        response_factory=_router(lambda r: ScriptedResponse(raw_json=json.dumps({"findings": [finding]})))
    )
    summary = await _review(session_factory, provider, setup)
    assert [c.schema_name for c in provider.calls] == ["review_response:unified", "critic_verdict"]
    assert summary.risk_tier == "tiny"
    assert summary.critic_calls == 1
    assert summary.accepted_count == 1  # verified, never suppressed for budget


async def test_strict_single_call_tiny_budget_suppresses_rather_than_publishing_unverified(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """An operator who sets TINY to exactly one call accepts that a
    finding needing mandatory verification is suppressed (never published
    unverified) -- the documented trade-off the default avoids."""

    setup = await _setup(session_factory, tmp_path, lambda root: _replace(root / "src/billing.py", *_TINY_CHANGE))
    finding = _finding(line=14, quote=_TINY_CHANGE[1], severity="high")
    provider = FakeLLMProvider(
        response_factory=_router(lambda r: ScriptedResponse(raw_json=json.dumps({"findings": [finding]})))
    )
    from patchfrog.review.cost_policy import parse_tier_budgets

    strict = ReviewCostPolicy(tier_provider_call_budgets=parse_tier_budgets({"tiny": 1}))
    summary = await _review(session_factory, provider, setup, policy=strict)
    assert len(provider.calls) == 1
    assert summary.accepted_count == 0
    async with session_factory() as session:
        statuses = (await session.execute(
            select(AIFindingProposalModel.status).where(AIFindingProposalModel.review_run_id == summary.run_id)
        )).scalars().all()
    assert list(statuses) == [ProposalStatus.SUPPRESSED_BUDGET]


async def test_several_candidates_share_one_single_pass_call_and_findings_are_attributed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    def mutate(root: Path) -> None:
        _replace(root / "src/billing.py", *_TINY_CHANGE)
        _replace(root / "src/billing.py", "        return {}", "        return {'limits': None}")

    setup = await _setup(session_factory, tmp_path, mutate)
    finding = _finding(line=39, quote="return {'limits': None}", confidence="high")
    provider = FakeLLMProvider(
        response_factory=_router(lambda r: ScriptedResponse(raw_json=json.dumps({"findings": [finding]})))
    )
    summary = await _review(session_factory, provider, setup)

    reviewer_calls = [c for c in provider.calls if c.schema_name.startswith("review_response")]
    assert len(reviewer_calls) == 1
    assert reviewer_calls[0].user_prompt.count("Review target: `") == 2
    assert summary.candidate_count == 2
    async with session_factory() as session:
        rows = (await session.execute(
            select(ReviewCandidateModel.symbol_name, AIFindingProposalModel.agent_role)
            .join(AIFindingProposalModel, AIFindingProposalModel.candidate_id == ReviewCandidateModel.id)
            .where(ReviewCandidateModel.review_run_id == summary.run_id)
        )).all()
    assert [tuple(row) for row in rows] == [("load_account_config", AgentRole.UNIFIED)]


# -- M4.7 exact-head reuse ----------------------------------------------------------


async def test_exact_head_rerun_makes_zero_new_calls_and_force_review_bypasses_reuse(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    setup = await _setup(session_factory, tmp_path, lambda root: _replace(root / "src/billing.py", *_TINY_CHANGE))
    provider = FakeLLMProvider(response_factory=_router(lambda r: _NO_FINDINGS))
    first = await _review(session_factory, provider, setup)
    assert len(provider.calls) == 1

    again = await _review(session_factory, provider, setup)
    assert len(provider.calls) == 1  # zero new provider calls
    assert again.reused_existing_run and again.run_id == first.run_id
    assert again.cost_report()["cache_hit"] is True

    forced = await _review(session_factory, provider, setup, force_review=True)
    assert len(provider.calls) == 2
    assert forced.run_id != first.run_id and forced.forced

    other_policy = ReviewCostPolicy(single_pass_max_input_tokens=12_000)
    await _review(session_factory, provider, setup, policy=other_policy)
    assert len(provider.calls) == 3  # a different policy never reuses


# -- M4.8 budgets -------------------------------------------------------------------


async def test_tiny_retry_denied_by_budget_ends_partial_never_clean(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    setup = await _setup(session_factory, tmp_path, lambda root: _replace(root / "src/billing.py", *_TINY_CHANGE))
    provider = FakeLLMProvider(response_factory=_router(lambda r: ProviderTransientError("rate limited")))
    summary = await _review(session_factory, provider, setup, config=ReviewConfig(max_retries=3))
    assert len(provider.calls) == 1  # the retry was denied by the TINY review-phase ceiling
    assert summary.status is ReviewRunStatus.PARTIAL
    assert summary.budget is not None and summary.budget.termination_reason is not None
    assert summary.cost_report()["budget_status"] == "max_provider_calls"


# -- M4.4 escalation ---------------------------------------------------------------------


_AUTH_MODULE = '''"""Session helpers."""


def issue_session(user_id, ttl):
    """Create a session token for user_id valid for ttl seconds."""
    return {"user": user_id, "ttl": ttl}
'''


async def test_high_risk_change_escalates_sequentially_with_persisted_reasons(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = f"test/m4-esc-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=uuid.uuid4().int % (2**62), owner="test",
            name=full_name.split("/")[1], full_name=full_name, installation_id=0,
        )
        await session.commit()
    snapshot = materialize_fixture_repo(tmp_path / "repo", "ai_review_python", full_name=full_name)
    root = snapshot.root_path
    (root / "src/auth").mkdir()
    (root / "src/auth/session.py").write_text(_AUTH_MODULE)
    commit_all(root, "add auth module")
    _replace(root / "src/auth/session.py", "def issue_session(user_id, ttl):", "def issue_session(user_id, ttl, scope):")
    commit_sha = commit_all(root, "change auth signature")
    diff_files = diff_against_base(root, "HEAD~1")
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=row.id, root_path=root, repository_full_name=full_name
    )

    provider = FakeLLMProvider(response_factory=_router(lambda r: _NO_FINDINGS))
    summary = await _review(session_factory, provider, (row.id, commit_sha, root, diff_files, full_name))

    assert summary.risk_tier == "high_risk"
    # Sequential: the single pass first, then ONE reason-carrying
    # specialist. No Correctness re-review of the same candidate -- the
    # single pass already covered correctness/contracts for it.
    assert [c.schema_name for c in provider.calls] == [
        "review_response:unified",
        "review_response:security",
    ]
    assert summary.escalation_reasons == ("security_sensitive_change",)
    assert summary.budget is not None and summary.budget.provider_calls <= 5
    run = await _run_row(session_factory, summary.run_id)
    assert json.loads(run.escalation_reasons) == ["security_sensitive_change"]
    assert "security_sensitive_path" in json.loads(run.risk_signals)


# -- M4.9 telemetry / strategy selection -----------------------------------------------------


async def test_cost_report_answers_every_m4_question_without_source_text(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    setup = await _setup(session_factory, tmp_path, lambda root: _replace(root / "src/billing.py", *_TINY_CHANGE))
    provider = FakeLLMProvider(response_factory=_router(lambda r: _NO_FINDINGS))
    summary = await _review(session_factory, provider, setup)
    report = summary.cost_report()
    for key in (
        "risk_tier", "provider_calls", "reviewer_calls", "critic_calls", "retries", "providers",
        "estimated_input_tokens", "estimated_output_tokens", "estimated_cost_usd", "escalation_reasons",
        "cache_hit", "budget_status",
    ):
        assert key in report
    assert report["providers"] == [{"provider": "fake", "model": "fake-model-1", "calls": 1}]
    assert "amount" not in json.dumps(report)  # never source text


async def test_specialist_fanout_strategy_keeps_the_pre_m4_call_shape(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    setup = await _setup(
        session_factory, tmp_path,
        lambda root: _replace(root / "src/billing.py", "# Logical comparison bug", "# Inverted comparison bug"),
    )
    provider = FakeLLMProvider(response_factory=_router(lambda r: _NO_FINDINGS))
    summary = await _review(
        session_factory, provider, setup, policy=ReviewCostPolicy(strategy=ReviewStrategy.SPECIALIST_FANOUT)
    )
    # No NO_AI shortcut, no UNIFIED role: the legacy per-candidate fan-out.
    assert provider.calls
    assert all(c.schema_name != "review_response:unified" for c in provider.calls)
    assert summary.risk_tier == "no_ai"  # still classified and recorded
    assert summary.review_strategy == "specialist_fanout"
