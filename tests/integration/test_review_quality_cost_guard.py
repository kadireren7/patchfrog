"""End-to-end Quality + Cost Guard (Milestone F) coverage against the
real ``ai_review_python`` fixture, through :class:`PullRequestReviewService`
and its real :class:`~patchfrog.review.orchestration.AgentOrchestrator`
wiring with :class:`FakeLLMProvider` -- same conventions as
``test_review_orchestration_end_to_end.py``. Covers required test
scenarios not already covered at the unit level by
``test_review_effort.py``/``test_review_operator_hard_caps.py``: tier-
aware retry caps, per-role output-token ceilings, critic budget
exhaustion safely suppressing rather than publishing, persistence of
tier/escalation/cost fields, and budget-reservation safety under real
concurrency."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.diff.models import DiffFile, DiffHunk, DiffLine, DiffLineType
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import ReviewCandidateModel, ReviewRunModel
from patchfrog.persistence.repositories import AIFindingProposalRepository, RepositoryRepository
from patchfrog.review.config import ReviewConfig
from patchfrog.review.domain import ProposalStatus
from patchfrog.review.effort_types import ReviewEffortTier
from patchfrog.review.provider import ProviderTransientError
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from tests.support.git_repo import commit_all, materialize_fixture_repo

_NO_FINDINGS = ScriptedResponse(raw_json=json.dumps({"findings": []}))


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


def _review_target(user_prompt: str) -> str | None:
    for line in user_prompt.splitlines():
        if line.startswith("Review target: `"):
            return line.split("`")[1]
    return None


def _targets_symbol(review_target: str | None, symbol: str) -> bool:
    if review_target is None:
        return False
    return review_target == symbol or review_target.endswith(f".{symbol}") or review_target.endswith(f"::{symbol}")


async def _setup_light_and_deep_candidates(
    session_factory: async_sessionmaker[AsyncSession], *, full_name: str
) -> tuple[uuid.UUID, str, Path]:
    """One isolated fixture copy with two distinct-tier candidates in a
    single diff/run: ``can_withdraw`` (unmodified -- no static findings,
    no security-naming signal -> LIGHT) and ``apply_payment_result``
    renamed to ``authorize_payment_result`` (real security-naming signal
    -> DEEP). Never touches the shared ``ai_review_python`` fixture --
    only this test's own materialized copy."""

    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0], name=full_name.split("/")[-1],
            full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-qcg-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    billing_path = snapshot.root_path / "src" / "billing.py"
    billing_path.write_text(billing_path.read_text().replace("apply_payment_result", "authorize_payment_result"))
    commit_sha = commit_all(snapshot.root_path, "rename for security naming signal")

    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )
    return repository_id, commit_sha, snapshot.root_path


async def _candidate_tiers(
    session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID
) -> dict[str | None, ReviewCandidateModel]:
    async with session_factory() as session:
        rows = (
            (await session.execute(select(ReviewCandidateModel).join(ReviewRunModel).where(
                ReviewRunModel.repository_id == repository_id
            )))
            .scalars()
            .all()
        )
    return {row.symbol_name: row for row in rows}


async def test_light_tier_retry_cap_is_lower_than_deep_tier_retry_cap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required scenario: retry cap by tier. LIGHT allows at most one
    retry regardless of the configured ceiling; DEEP/STANDARD may use
    the full configured ceiling. Every call transiently fails, so total
    attempts made == 1 + retries actually consumed for each candidate."""

    repository_id, commit_sha, root_path = await _setup_light_and_deep_candidates(
        session_factory, full_name="test/qcg-retry-cap"
    )
    diff_files = [_diff_marking_lines("src/billing.py", [14, 22])]

    reviewer = FakeLLMProvider(response_factory=lambda req: ProviderTransientError("always transiently fails"))
    config = ReviewConfig(max_retries=4, max_concurrent_requests=1)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)

    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/qcg-retry-cap",
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )
    assert summary.candidates_failed == summary.candidate_count

    light_attempts = sum(
        1 for c in reviewer.calls if _targets_symbol(_review_target(c.user_prompt), "can_withdraw")
    )
    deep_attempts = sum(
        1 for c in reviewer.calls if _targets_symbol(_review_target(c.user_prompt), "authorize_payment_result")
    )
    # LIGHT: Correctness-only (Security dropped, no real signal), retry
    # capped at min(1, max_retries) = 1 -> exactly 2 attempts (1 + 1 retry).
    assert light_attempts == 2
    # DEEP: both roles selected, retry uses the full configured ceiling
    # (4) -> exactly 5 attempts per role = 10 total.
    assert deep_attempts == 10


async def test_output_token_ceiling_is_never_doubled_across_two_concurrent_roles(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required scenario: per-candidate output ceiling respected. A DEEP
    candidate's two concurrently-run roles must never together be
    allowed to request more than the configured
    max_output_tokens_per_candidate -- the real bug this milestone fixes
    (previously each role received the full ceiling independently)."""

    repository_id, commit_sha, root_path = await _setup_light_and_deep_candidates(
        session_factory, full_name="test/qcg-output-ceiling"
    )
    diff_files = [_diff_marking_lines("src/billing.py", [22])]  # DEEP candidate only

    reviewer = FakeLLMProvider(response_factory=lambda req: _NO_FINDINGS)
    config = ReviewConfig(max_output_tokens_per_candidate=4_000)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)

    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/qcg-output-ceiling",
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )
    assert summary.candidates_reviewed == 1
    assert len(reviewer.calls) == 2  # both roles selected (DEEP)

    combined_requested = sum(c.max_output_tokens for c in reviewer.calls)
    assert combined_requested <= config.max_output_tokens_per_candidate
    # Confirm this is a real, non-trivial split (not just "both happen to
    # be 1") -- each role got a meaningful share.
    assert all(c.max_output_tokens > 0 for c in reviewer.calls)


async def test_light_tier_output_ceiling_is_smaller_than_deep_tier_per_role_ceiling(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup_light_and_deep_candidates(
        session_factory, full_name="test/qcg-output-by-tier"
    )
    diff_files = [_diff_marking_lines("src/billing.py", [14, 22])]

    reviewer = FakeLLMProvider(response_factory=lambda req: _NO_FINDINGS)
    config = ReviewConfig(max_output_tokens_per_candidate=4_000, max_concurrent_requests=1)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)

    await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/qcg-output-by-tier",
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )

    light_ceiling = next(
        c.max_output_tokens for c in reviewer.calls if _targets_symbol(_review_target(c.user_prompt), "can_withdraw")
    )
    deep_ceilings = [
        c.max_output_tokens for c in reviewer.calls
        if _targets_symbol(_review_target(c.user_prompt), "authorize_payment_result")
    ]
    assert all(light_ceiling < d for d in deep_ceilings)


async def test_mandatory_critic_verification_unavailable_suppresses_risky_finding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required scenario: budget exhaustion before mandatory verification
    must suppress, never publish unverified. A HIGH-severity finding
    always requires critique (CriticSelectionPolicy rule 2, independent
    of tier) -- if the run's remaining budget can't reserve that critic
    call, the proposal is suppressed rather than accepted."""

    repository_id, commit_sha, root_path = await _setup_light_and_deep_candidates(
        session_factory, full_name="test/qcg-critic-budget"
    )
    diff_files = [_diff_marking_lines("src/billing.py", [14])]  # LIGHT candidate

    high_severity_finding = {
        "title": "Inverted comparison", "message": "m", "category": "correctness",
        "severity": "high", "confidence": "high", "file_path": "src/billing.py", "start_line": 14, "end_line": 14,
        "evidence": [{"file_path": "src/billing.py", "start_line": 14, "end_line": 14, "quoted_text": "return amount >= balance"}],
        "reasoning_summary": "r", "suggested_fix": None,
    }
    # Deliberately huge scripted *actual* usage -- dominates the running
    # budget total after reconciliation regardless of the real (small)
    # prompt's estimate, so the critic's own reservation deterministically
    # fails against a tight max_total_input_tokens ceiling.
    reviewer = FakeLLMProvider(
        response_factory=lambda req: ScriptedResponse(
            raw_json=json.dumps({"findings": [high_severity_finding]}), input_tokens=100_000,
        )
    )
    critic = FakeLLMProvider(response_factory=lambda req: ScriptedResponse(raw_json=json.dumps(
        {"decision": "accept", "reasoning_summary": "ok", "downgraded_severity": None, "downgraded_confidence": None}
    )))
    config = ReviewConfig(max_total_input_tokens=100_000)
    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic
    )

    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/qcg-critic-budget",
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )
    assert summary.accepted_count == 0
    assert len(critic.calls) == 0  # never actually called -- suppressed before the call, not after a failure

    async with session_factory() as session:
        run_id = (
            (await session.execute(select(ReviewRunModel.id).where(ReviewRunModel.repository_id == repository_id)))
            .scalars().one()
        )
        proposals = await AIFindingProposalRepository().list_for_run(session, review_run_id=run_id)
    assert len(proposals) == 1
    assert proposals[0].status == ProposalStatus.SUPPRESSED_BUDGET


async def test_effort_tier_and_cost_metrics_are_persisted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required scenario: candidate tier/reasons persistence, run-level
    tier-count/critic-call/retry aggregates persisted and reconstructable
    for telemetry (spec section 22)."""

    repository_id, commit_sha, root_path = await _setup_light_and_deep_candidates(
        session_factory, full_name="test/qcg-persistence"
    )
    diff_files = [_diff_marking_lines("src/billing.py", [14, 22])]

    reviewer = FakeLLMProvider(response_factory=lambda req: _NO_FINDINGS)
    critic = FakeLLMProvider(response_factory=lambda req: ScriptedResponse(raw_json=json.dumps(
        {"decision": "accept", "reasoning_summary": "ok", "downgraded_severity": None, "downgraded_confidence": None}
    )))
    service = PullRequestReviewService(
        session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic
    )

    summary = await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/qcg-persistence",
        commit_sha=commit_sha, diff_files=diff_files,
    )
    assert summary.candidates_by_tier.get(ReviewEffortTier.LIGHT) == 1
    assert summary.candidates_by_tier.get(ReviewEffortTier.DEEP) == 1

    by_symbol = await _candidate_tiers(session_factory, repository_id=repository_id)
    assert by_symbol["can_withdraw"].effort_tier == ReviewEffortTier.LIGHT
    assert by_symbol["authorize_payment_result"].effort_tier == ReviewEffortTier.DEEP
    assert json.loads(by_symbol["can_withdraw"].effort_reasons) != []
    assert by_symbol["can_withdraw"].escalated is False

    async with session_factory() as session:
        run = (
            (await session.execute(select(ReviewRunModel).where(ReviewRunModel.repository_id == repository_id)))
            .scalars().one()
        )
    tiers_from_db = json.loads(run.candidates_by_tier)
    assert tiers_from_db.get("light") == 1
    assert tiers_from_db.get("deep") == 1
    # No findings returned by either role, so no critique was ever
    # mandated/selected -- critic_calls persisted as 0, not fabricated.
    assert run.critic_calls == 0


async def test_budget_reservation_is_safe_under_real_concurrency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required scenario: atomic budget reservation across concurrently
    reviewed candidates. Many well-separated candidates reviewed with
    real concurrency (max_concurrent_requests > 1) against a budget tight
    enough to force skips -- every candidate must be accounted for
    exactly once (reviewed or skipped, never both/neither), and the
    accepted spend must never run away unboundedly."""

    full_name = "test/qcg-concurrency"
    async with session_factory() as session:
        row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name="qcg-concurrency", full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = row.id

    root = Path("/tmp") / f"pf-qcg-concurrency-{uuid.uuid4().hex[:8]}"
    snapshot = materialize_fixture_repo(root, "ai_review_python", full_name=full_name)
    (snapshot.root_path / "src" / "generated.py").write_text(
        "\n".join(f"VALUE_{i} = {i}" for i in range(1, 10_001)) + "\n"
    )
    commit_sha = commit_all(snapshot.root_path, "add generated file")
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name
    )

    lines = [1 + i * 200 for i in range(20)]
    diff_files = [_diff_marking_lines("src/generated.py", lines)]

    reviewer = FakeLLMProvider(response_factory=lambda req: _NO_FINDINGS)
    # Tight enough to force real skips, generous enough that several
    # candidates succeed concurrently -- max_concurrent_requests > 1 so
    # asyncio.gather genuinely interleaves reservation attempts.
    config = ReviewConfig(max_candidates=20, max_total_input_tokens=2_000, max_concurrent_requests=8)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)

    summary = await service.review_local(
        repository_id=repository_id, root_path=snapshot.root_path, repository_full_name=full_name,
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )

    assert summary.candidates_skipped_budget > 0
    assert summary.candidate_count == (
        summary.candidates_reviewed + summary.candidates_skipped_budget + summary.candidates_failed
    )
    # Generous headroom (many candidates could be concurrently in-flight
    # before reconciliation lands) -- still a real, bounded ceiling, not
    # an unbounded runaway.
    assert summary.reviewer_usage.input_tokens <= config.max_total_input_tokens * 3


def max_theoretical_reviewer_calls(config: ReviewConfig) -> int:
    """Deterministic upper bound (spec section 20) on reviewer provider
    calls for one review run under a given :class:`ReviewConfig`: every
    candidate reviewed by both possible roles (tiering can only reduce
    this, never increase it -- see patchfrog.review.effort), each
    retried up to the configured ceiling."""

    roles = 2
    return config.max_candidates * roles * (1 + config.max_retries)


async def test_max_theoretical_reviewer_calls_bounds_actual_calls(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id, commit_sha, root_path = await _setup_light_and_deep_candidates(
        session_factory, full_name="test/qcg-call-bound"
    )
    diff_files = [_diff_marking_lines("src/billing.py", [14, 22])]

    reviewer = FakeLLMProvider(response_factory=lambda req: ProviderTransientError("always transiently fails"))
    config = ReviewConfig(max_candidates=5, max_retries=4, max_concurrent_requests=1)
    service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)

    await service.review_local(
        repository_id=repository_id, root_path=root_path, repository_full_name="test/qcg-call-bound",
        commit_sha=commit_sha, diff_files=diff_files, config=config,
    )

    assert len(reviewer.calls) <= max_theoretical_reviewer_calls(config)
