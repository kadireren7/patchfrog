"""Controlled corpus for Repository Learnings Foundation (spec section
40, minimum 25 scenarios) -- mirrors
tests/integration/test_historical_regression_memory_corpus.py's own
discipline exactly: real git repository, real indexing, real
diff-driven :class:`~patchfrog.review.domain.ReviewCandidate`
generation for the *current* side where needed, and always real,
persisted (never FakeLLM-authored, never hand-built) historical state
via :func:`~patchfrog.historical_regression_memory.queries.fetch_trusted_historical_records`
(reused from Milestone N directly, never re-implemented) for the
*historical/trust* side.

Every case that needs the trust side stages one or more real
``ReviewRunModel``/``ReviewCandidateModel``/``AIFindingProposalModel``/
``AIFindingModel`` row chains plus real ``FeedbackEventModel`` rows and
a real recompute -- exactly Phase 9's own real pipeline.

The mandatory Milestone N vs O distinction (spec's own explicit
requirement) is proven directly: :func:`test_case_single_trusted_event_n_may_fire_o_must_not`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.change_intelligence.domain import ChangeIntelligenceReport
from patchfrog.change_intelligence.service import build_change_intelligence_report
from patchfrog.feedback.domain import (
    ActorIdentity,
    ExplicitCommand,
    FeedbackEvent,
    FeedbackEventType,
    FeedbackSource,
    SignalStrength,
)
from patchfrog.feedback.queries import recompute_and_persist_all
from patchfrog.historical_regression_memory.domain import HistoricalMatchKind
from patchfrog.historical_regression_memory.queries import fetch_trusted_historical_records
from patchfrog.historical_regression_memory.service import build_historical_regression_report
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.review import (
    AIFindingModel,
    AIFindingProposalModel,
    ReviewCandidateModel,
    ReviewRunModel,
)
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.persistence.repositories.feedback import FeedbackEventRepository
from patchfrog.persistence.repositories.repository_index import RepositoryIndexRepository
from patchfrog.repository_learnings.domain import (
    MAX_LEARNINGS_PER_RUN,
    MAX_SUPPORTING_EVENTS_PER_LEARNING,
    MIN_SUPPORTING_EVENTS,
    RepositoryLearningApplicationStatus,
    RepositoryLearningPatternKind,
)
from patchfrog.repository_learnings.matching import derive_repository_learnings
from patchfrog.repository_learnings.service import build_repository_learnings_report
from patchfrog.review.candidates import ReviewCandidateGenerator
from patchfrog.review.domain import (
    ProposalStatus,
    ReviewCandidate,
    ReviewCandidateReason,
    ReviewRunStatus,
)
from patchfrog.review.local_diff import diff_against_base
from tests.support.git_repo import commit_all, init_git_repo

_README = "# scratch repo\n"


async def _make_repo(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=0,
        )
        await session.commit()
        return repo.id


def _setup_base(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "README.md").write_text(_README)
    init_git_repo(root)
    return root


async def _index_and_group(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    root: Path,
    full_name: str,
    base_sha: str,
) -> ChangeIntelligenceReport:
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    diff_files = diff_against_base(root, base_sha)
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        candidates: list[ReviewCandidate] = list(
            await ReviewCandidateGenerator().generate(
                session, repository_index_id=index.id, diff_files=diff_files, static_findings=[], max_candidates=40,
            )
        )
        change_report = await build_change_intelligence_report(session, candidates=candidates)
    return change_report


async def _stage_historical_finding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    repository_index_id: uuid.UUID,
    file_path: str,
    qualified_name: str | None,
    category: FindingCategory = FindingCategory.CORRECTNESS,
    title: str = "forgot idempotency key on retry",
    commit_sha: str | None = None,
) -> uuid.UUID:
    """Real persisted rows -- identical discipline to Milestone N's own
    ``_stage_historical_finding``. Never a hand-constructed
    ``HistoricalRegressionRecord`` standing in for a real DB round trip."""

    sha = commit_sha or uuid.uuid4().hex[:40].ljust(40, "0")
    async with session_factory() as session:
        run = ReviewRunModel(
            id=uuid.uuid4(), repository_id=repository_id, repository_index_id=repository_index_id,
            commit_sha=sha, config_fingerprint="c" * 64, model_fingerprint="m" * 64,
            incremental_context_fingerprint="i" * 64, status=ReviewRunStatus.SUCCEEDED,
            reviewer_provider="fake", reviewer_model="fake-model",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()

        candidate = ReviewCandidateModel(
            id=uuid.uuid4(), review_run_id=run.id, file_path=file_path, symbol_id=None,
            symbol_name=qualified_name.rsplit(".", 1)[-1] if qualified_name else None,
            qualified_name=qualified_name, start_line=1, end_line=5, changed_lines="[1]",
            reason=ReviewCandidateReason.CHANGED_SYMBOL,
        )
        session.add(candidate)
        await session.flush()

        proposal = AIFindingProposalModel(
            id=uuid.uuid4(), review_run_id=run.id, candidate_id=candidate.id, title=title,
            message="a real historical defect", category=category, severity=Severity.MEDIUM,
            confidence=Confidence.HIGH, file_path=file_path, start_line=1, end_line=5, evidence="[]",
            reasoning_summary="root cause", status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        await session.flush()

        finding = AIFindingModel(
            id=uuid.uuid4(), review_run_id=run.id, proposal_id=proposal.id, candidate_id=candidate.id, title=title,
            message="a real historical defect", category=category, severity=Severity.MEDIUM,
            confidence=Confidence.HIGH, file_path=file_path, start_line=1, end_line=5, evidence="[]",
            reasoning_summary="root cause",
        )
        session.add(finding)
        await session.commit()
        return finding.id


async def _stage_feedback(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    finding_id: uuid.UUID,
    command: ExplicitCommand,
) -> None:
    async with session_factory() as session:
        event = FeedbackEvent(
            repository_id=repository_id, pull_request_id=None, review_run_id=None, publication_id=None,
            review_publication_comment_id=None, finding_id=finding_id, github_review_id=None,
            github_comment_id=None, event_type=FeedbackEventType.EXPLICIT_COMMAND, source=FeedbackSource.REPLY_SYNC,
            external_event_id=f"cmd:{finding_id}:{command.value}:{uuid.uuid4().hex[:8]}",
            raw_signal=f"/patchfrog {command.value}", normalized_signal=command.value,
            signal_strength=SignalStrength.STRONG, actor=ActorIdentity(login="developer", is_bot=False),
            occurred_at=datetime.now(UTC),
        )
        await FeedbackEventRepository().create_if_new(session, event=event)
        await session.commit()

    async with session_factory() as session:
        await recompute_and_persist_all(session, repository_id=repository_id)
        await session.commit()


async def _index_once(
    session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, root: Path, full_name: str
) -> uuid.UUID:
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
    if index is not None:
        return index.id
    await RepositoryIndexingService(session_factory=session_factory).index_local_repository(
        repository_id=repository_id, root_path=root, repository_full_name=full_name
    )
    async with session_factory() as session:
        index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        return index.id


async def _stage_and_trust(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    repository_index_id: uuid.UUID,
    file_path: str,
    qualified_name: str | None,
    command: ExplicitCommand = ExplicitCommand.FIXED,
    category: FindingCategory = FindingCategory.CORRECTNESS,
) -> uuid.UUID:
    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=repository_index_id,
        file_path=file_path, qualified_name=qualified_name, category=category,
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=command)
    return finding_id


# ---- 1. Single trusted event -> N may fire, O must not (mandatory) ----


async def test_case_single_trusted_event_n_may_fire_o_must_not(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-single-event"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )

    async with session_factory() as session:
        n_report = await build_historical_regression_report(
            session, repository_id=repository_id, as_of=datetime.now(UTC),
        )
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )

    # N's own trust query legitimately returns the one record.
    assert len(trusted_records) == 1
    # N itself never needs a current PR to be considered a valid report.
    assert n_report.trusted_records_considered == trusted_records
    # But O must never construct a learning from it alone.
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert learnings == ()


# ---- 2. Two independent trusted events, different review runs -> learning activates ----


async def test_case_two_independent_events_activate_a_learning(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-two-independent"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.USEFUL,
    )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert len(learnings) == 1
    assert learnings[0].support_count == 2
    assert learnings[0].pattern.anchor_qualified_name == "apply_discount"
    assert learnings[0].pattern.pattern_kind is RepositoryLearningPatternKind.REPEATED_SAME_SURFACE_REGRESSION


# ---- 3. Duplicate feedback on the SAME finding counts once ----


async def test_case_duplicate_feedback_on_same_finding_counts_once(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-duplicate-feedback"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)
    await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.USEFUL)

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    assert len(trusted_records) == 1  # N's own query already collapses this
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert learnings == ()


# ---- 4. Two findings, same review run -> independence fails ----


async def test_case_two_findings_same_review_run_never_satisfies_independence(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-same-run"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    shared_sha = uuid.uuid4().hex[:40].ljust(40, "0")
    async with session_factory() as session:
        run = ReviewRunModel(
            id=uuid.uuid4(), repository_id=repository_id, repository_index_id=index_id,
            commit_sha=shared_sha, config_fingerprint="c" * 64, model_fingerprint="m" * 64,
            incremental_context_fingerprint="i" * 64, status=ReviewRunStatus.SUCCEEDED,
            reviewer_provider="fake", reviewer_model="fake-model",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        run_id = run.id

        finding_ids: list[uuid.UUID] = []
        for i in range(2):
            candidate = ReviewCandidateModel(
                id=uuid.uuid4(), review_run_id=run_id, file_path="pricing.py", symbol_id=None,
                symbol_name="apply_discount", qualified_name="apply_discount", start_line=1, end_line=5,
                changed_lines="[1]", reason=ReviewCandidateReason.CHANGED_SYMBOL,
            )
            session.add(candidate)
            await session.flush()
            proposal = AIFindingProposalModel(
                id=uuid.uuid4(), review_run_id=run_id, candidate_id=candidate.id, title=f"defect {i}",
                message="a real historical defect", category=FindingCategory.CORRECTNESS, severity=Severity.MEDIUM,
                confidence=Confidence.HIGH, file_path="pricing.py", start_line=1, end_line=5, evidence="[]",
                reasoning_summary="root cause", status=ProposalStatus.ACCEPTED,
            )
            session.add(proposal)
            await session.flush()
            finding = AIFindingModel(
                id=uuid.uuid4(), review_run_id=run_id, proposal_id=proposal.id, candidate_id=candidate.id,
                title=f"defect {i}", message="a real historical defect", category=FindingCategory.CORRECTNESS,
                severity=Severity.MEDIUM, confidence=Confidence.HIGH, file_path="pricing.py", start_line=1,
                end_line=5, evidence="[]", reasoning_summary="root cause",
            )
            session.add(finding)
            await session.flush()
            finding_ids.append(finding.id)
        await session.commit()

    for finding_id in finding_ids:
        await _stage_feedback(session_factory, repository_id=repository_id, finding_id=finding_id, command=ExplicitCommand.FIXED)

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    assert len(trusted_records) == 2  # two distinct findings, both trusted
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert learnings == ()  # but only one independent review run backs them


# ---- 5. Three independent events -> activated_at is the 2nd earliest, not the 3rd ----


async def test_case_three_independent_events_activation_is_minimum_support_set(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-three-events"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    for _ in range(3):
        await _stage_and_trust(
            session_factory, repository_id=repository_id, repository_index_id=index_id,
            file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
        )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert len(learnings) == 1
    learning = learnings[0]
    assert learning.support_count == 3
    # activated_at is the 2nd (MIN_SUPPORTING_EVENTS-th) earliest, never the most recent.
    assert learning.activated_at != learning.last_observed_at


# ---- 6. False-positive on one supporting finding drops it below threshold ----


async def test_case_false_positive_on_one_finding_drops_below_threshold(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-fp-drops-below"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    fp_finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(
        session_factory, repository_id=repository_id, finding_id=fp_finding_id, command=ExplicitCommand.FALSE_POSITIVE
    )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    assert len(trusted_records) == 1  # the FP finding is excluded by N's own fail-closed rule
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert learnings == ()


# ---- 7. Invalidation: a learning that WAS active stops being derived once support drops ----


async def test_case_invalidation_falls_out_of_live_rederivation(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-invalidation"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    finding_a = await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.USEFUL,
    )

    async with session_factory() as session:
        trusted_before = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    learnings_before = derive_repository_learnings(trusted_records=trusted_before, repository_id=repository_id)
    assert len(learnings_before) == 1

    # A developer later marks the FIRST finding a false positive.
    await _stage_feedback(
        session_factory, repository_id=repository_id, finding_id=finding_a, command=ExplicitCommand.FALSE_POSITIVE
    )

    async with session_factory() as session:
        trusted_after = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    learnings_after = derive_repository_learnings(trusted_records=trusted_after, repository_id=repository_id)
    assert learnings_after == ()  # no explicit "retire" call needed -- falls out of live re-derivation


# ---- 8. True temporal-leakage replay: T1/T2/T3/T4 (mirrors N's own proof) ----


async def test_case_true_temporal_leakage_replay_never_sees_future_support(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-temporal-replay"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    # T1: one trusted event exists.
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )

    # T2: capture a review boundary -- only 1 event exists as of now.
    as_of_t2 = datetime.now(UTC)
    async with session_factory() as session:
        trusted_t2 = await fetch_trusted_historical_records(session, repository_id=repository_id, as_of=as_of_t2)
    assert derive_repository_learnings(trusted_records=trusted_t2, repository_id=repository_id) == ()

    # T3: a second, independent trusted event, strictly after T2.
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.USEFUL,
    )

    # Replay the EXACT SAME boundary as T2 -- must still see only 1 event.
    async with session_factory() as session:
        trusted_replay = await fetch_trusted_historical_records(session, repository_id=repository_id, as_of=as_of_t2)
    assert derive_repository_learnings(trusted_records=trusted_replay, repository_id=repository_id) == ()

    # T4: a genuinely later boundary sees both.
    as_of_t4 = datetime.now(UTC)
    async with session_factory() as session:
        trusted_t4 = await fetch_trusted_historical_records(session, repository_id=repository_id, as_of=as_of_t4)
    learnings_t4 = derive_repository_learnings(trusted_records=trusted_t4, repository_id=repository_id)
    assert len(learnings_t4) == 1


# ---- 9. Repository isolation: events in another repo never contribute ----


async def test_case_repository_isolation(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name_a = "test/rl-isolation-a"
    full_name_b = "test/rl-isolation-b"
    root_a = _setup_base(tmp_path / "a")
    (root_a / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root_a, "base")
    repository_a = await _make_repo(session_factory, full_name_a)
    index_a = await _index_once(session_factory, repository_id=repository_a, root=root_a, full_name=full_name_a)

    root_b = _setup_base(tmp_path / "b")
    (root_b / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root_b, "base")
    repository_b = await _make_repo(session_factory, full_name_b)
    await _index_once(session_factory, repository_id=repository_b, root=root_b, full_name=full_name_b)

    # Repo A gets two independent trusted events; repo B gets none.
    await _stage_and_trust(
        session_factory, repository_id=repository_a, repository_index_id=index_a,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_a, repository_index_id=index_a,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.USEFUL,
    )

    async with session_factory() as session:
        trusted_b = await fetch_trusted_historical_records(session, repository_id=repository_b, as_of=datetime.now(UTC))
    assert trusted_b == ()
    assert derive_repository_learnings(trusted_records=trusted_b, repository_id=repository_b) == ()


# ---- 10. Bounds: MAX_SUPPORTING_EVENTS_PER_LEARNING never exceeded ----


async def test_case_supporting_evidence_bounded(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-bounded-evidence"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    for _ in range(MAX_SUPPORTING_EVENTS_PER_LEARNING + 3):
        await _stage_and_trust(
            session_factory, repository_id=repository_id, repository_index_id=index_id,
            file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
        )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert len(learnings) == 1
    assert len(learnings[0].supporting_evidence) == MAX_SUPPORTING_EVENTS_PER_LEARNING
    assert learnings[0].support_count == MAX_SUPPORTING_EVENTS_PER_LEARNING + 3  # true count, never truncated


# ---- 11. Bounds: MAX_LEARNINGS_PER_RUN never exceeded ----


async def test_case_learnings_per_run_bounded(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-bounded-learnings"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def x():\n    return 1\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    for i in range(MAX_LEARNINGS_PER_RUN + 3):
        symbol = f"surface_{i}"
        await _stage_and_trust(
            session_factory, repository_id=repository_id, repository_index_id=index_id,
            file_path="pricing.py", qualified_name=symbol, command=ExplicitCommand.FIXED,
        )
        await _stage_and_trust(
            session_factory, repository_id=repository_id, repository_index_id=index_id,
            file_path="pricing.py", qualified_name=symbol, command=ExplicitCommand.USEFUL,
        )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC), limit=500
        )
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert len(learnings) == MAX_LEARNINGS_PER_RUN


# ---- 12. Full pipeline: current PR touches the learned surface -> real application ----


async def test_case_current_pr_touches_learned_surface_produces_real_application(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-full-pipeline-touch"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.USEFUL,
    )

    (root / "pricing.py").write_text(
        "def apply_discount(order):\n    if order['loyalty_years'] > 5:\n        return order['total'] * 0.9\n"
        "    return order['total']\n"
    )
    commit_all(root, "touch apply_discount again")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        n_report = await build_historical_regression_report(
            session, repository_id=repository_id, as_of=datetime.now(UTC), change_units=change_report.change_units,
        )
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )

    o_report = build_repository_learnings_report(
        repository_id=repository_id, trusted_records=trusted_records,
        change_units=change_report.change_units, historical_candidates=n_report.candidates,
    )
    assert o_report.learning_count == 1
    assert len(o_report.applications) == 1
    application = o_report.applications[0]
    assert application.status is RepositoryLearningApplicationStatus.UNSATISFIED
    assert application.current_qualified_name == "apply_discount"
    # N also fires on this exact same-symbol surface -- O must enrich, never duplicate.
    assert any(c.match_kind is HistoricalMatchKind.SAME_SYMBOL for c in n_report.candidates)
    assert not application.stands_alone
    assert "Repository learning" in o_report.repository_learning_story


# ---- 13. Full pipeline: current PR does NOT touch the learned surface -> no application ----


async def test_case_current_pr_does_not_touch_learned_surface_no_application(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-full-pipeline-no-touch"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "shipping.py").write_text("def compute_shipping(order):\n    return 5\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.USEFUL,
    )

    (root / "shipping.py").write_text("def compute_shipping(order):\n    return 10\n")
    commit_all(root, "touch a totally different surface")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )

    o_report = build_repository_learnings_report(
        repository_id=repository_id, trusted_records=trusted_records, change_units=change_report.change_units,
    )
    assert o_report.learning_count == 1  # the learning still exists...
    assert o_report.applications == ()  # ...but nothing in the current PR is relevant to it
    assert o_report.repository_learning_story == ""


# ---- 14. Full pipeline: renamed symbol never falls back to a match ----


async def test_case_renamed_symbol_never_matched(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-renamed-symbol"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.USEFUL,
    )

    (root / "pricing.py").write_text("def apply_loyalty_discount(order):\n    return order['total']\n")
    commit_all(root, "rename apply_discount")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )

    o_report = build_repository_learnings_report(
        repository_id=repository_id, trusted_records=trusted_records, change_units=change_report.change_units,
    )
    assert o_report.applications == ()


# ---- 15. Test-only PR touching a learned surface never triggers an application ----


async def test_case_test_only_pr_never_triggers_application(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-test-only"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n    assert apply_discount({'total': 1}) == 1\n"
    )
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.USEFUL,
    )

    (root / "test_pricing.py").write_text(
        "from pricing import apply_discount\n\n\ndef test_apply_discount():\n"
        "    assert apply_discount({'total': 1}) == 1\n    assert apply_discount({'total': 2}) == 2\n"
    )
    commit_all(root, "extend the test only")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )

    o_report = build_repository_learnings_report(
        repository_id=repository_id, trusted_records=trusted_records, change_units=change_report.change_units,
    )
    assert o_report.applications == ()


# ---- 16. Security-category repeated finding names the pattern SECURITY ----


async def test_case_security_category_repeated_finding(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-security-category"
    root = _setup_base(tmp_path)
    (root / "auth.py").write_text("def check_token(token):\n    return True\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="auth.py", qualified_name="check_token", command=ExplicitCommand.FIXED,
        category=FindingCategory.SECURITY,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="auth.py", qualified_name="check_token", command=ExplicitCommand.USEFUL,
        category=FindingCategory.SECURITY,
    )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert len(learnings) == 1
    assert learnings[0].pattern.finding_category is FindingCategory.SECURITY


# ---- 17. Different files/symbols never cross-contaminate independence counting ----


async def test_case_different_surfaces_never_cross_contaminate(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-no-cross-contamination"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    (root / "shipping.py").write_text("def compute_shipping(order):\n    return 5\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="shipping.py", qualified_name="compute_shipping", command=ExplicitCommand.FIXED,
    )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert learnings == ()  # each surface has only 1 independent event of its own


# ---- 18. Ignore feedback on one supporting finding also drops below threshold ----


async def test_case_ignore_on_one_finding_drops_below_threshold(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-ignore-drops-below"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    ignored_finding_id = await _stage_historical_finding(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount",
    )
    await _stage_feedback(
        session_factory, repository_id=repository_id, finding_id=ignored_finding_id, command=ExplicitCommand.IGNORE
    )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    assert len(trusted_records) == 1
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert learnings == ()


# ---- 19. No qualified name (module-level finding) never participates ----


async def test_case_no_qualified_name_never_participates(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-no-qualified-name"
    root = _setup_base(tmp_path)
    (root / "config.py").write_text("VALUE = 1\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="config.py", qualified_name=None, command=ExplicitCommand.FIXED,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="config.py", qualified_name=None, command=ExplicitCommand.USEFUL,
    )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert learnings == ()


# ---- 20. Two events with only FIXED (never USEFUL) still satisfies the gate ----


async def test_case_two_fixed_events_activate(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-two-fixed"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    for _ in range(2):
        await _stage_and_trust(
            session_factory, repository_id=repository_id, repository_index_id=index_id,
            file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
        )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert len(learnings) == 1


# ---- 21. Two events with only USEFUL (never FIXED) still satisfies the gate ----


async def test_case_two_useful_events_activate(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-two-useful"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    for _ in range(2):
        await _stage_and_trust(
            session_factory, repository_id=repository_id, repository_index_id=index_id,
            file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.USEFUL,
        )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert len(learnings) == 1


# ---- 22. Zero trusted records -> zero learnings, zero applications, empty report ----


async def test_case_zero_trusted_records_empty_report(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-empty"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    assert trusted_records == ()
    o_report = build_repository_learnings_report(repository_id=repository_id, trusted_records=trusted_records)
    assert o_report.learning_count == 0
    assert o_report.applications == ()
    assert o_report.repository_learning_story == ""


# ---- 23. Application status is always UNSATISFIED for the only implemented pattern kind ----


async def test_case_application_status_always_unsatisfied(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-always-unsatisfied"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    base_sha = commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.USEFUL,
    )

    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total'] * 0.5\n")
    commit_all(root, "touch again")
    change_report = await _index_and_group(
        session_factory, repository_id=repository_id, root=root, full_name=full_name, base_sha=base_sha
    )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    o_report = build_repository_learnings_report(
        repository_id=repository_id, trusted_records=trusted_records, change_units=change_report.change_units,
    )
    assert len(o_report.applications) == 1
    assert all(a.status is RepositoryLearningApplicationStatus.UNSATISFIED for a in o_report.applications)
    assert not any(
        a.status in (RepositoryLearningApplicationStatus.SATISFIED, RepositoryLearningApplicationStatus.INSUFFICIENT_EVIDENCE)
        for a in o_report.applications
    )


# ---- 24. Companion/contract/test learning kinds are never constructed (deferred, v1 scope) ----


async def test_case_deferred_pattern_kinds_never_constructed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    full_name = "test/rl-deferred-kinds"
    root = _setup_base(tmp_path)
    (root / "pricing.py").write_text("def apply_discount(order):\n    return order['total']\n")
    commit_all(root, "base")
    repository_id = await _make_repo(session_factory, full_name)
    index_id = await _index_once(session_factory, repository_id=repository_id, root=root, full_name=full_name)

    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.FIXED,
    )
    await _stage_and_trust(
        session_factory, repository_id=repository_id, repository_index_id=index_id,
        file_path="pricing.py", qualified_name="apply_discount", command=ExplicitCommand.USEFUL,
    )

    async with session_factory() as session:
        trusted_records = await fetch_trusted_historical_records(
            session, repository_id=repository_id, as_of=datetime.now(UTC)
        )
    learnings = derive_repository_learnings(trusted_records=trusted_records, repository_id=repository_id)
    assert len(learnings) == 1
    assert all(
        learning.pattern.pattern_kind is RepositoryLearningPatternKind.REPEATED_SAME_SURFACE_REGRESSION
        for learning in learnings
    )


# ---- 25. MIN_SUPPORTING_EVENTS is exactly 2, never configurable lower ----


def test_case_min_supporting_events_is_two() -> None:
    assert MIN_SUPPORTING_EVENTS == 2


# ---- 26. Zero LLM provider calls anywhere in the package (structural proof) ----


def test_repository_learnings_never_imports_a_provider() -> None:
    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "patchfrog" / "repository_learnings"
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert "LLMProvider" not in (node.module or "") and not any(
                    alias.name == "LLMProvider" for alias in node.names
                ), f"{path} imports LLMProvider -- Repository Learnings must add zero provider calls"
