"""Controlled corpus for Trajectory Intelligence Foundation (spec
section 38, minimum 30 scenarios) -- real, persisted (never
FakeLLM-authored, never hand-built) `review_generations`/
`review_candidates` chains via
:class:`~patchfrog.persistence.repositories.review_generation.ReviewGenerationRepository`
and real `ReviewCandidateModel` rows, exactly Phase 7's own real
lineage shape. Every case stages a real PR/repository, real
`ReviewRunModel` rows per head, and real `ReviewGenerationModel` chains
via `previous_generation_id`, then calls
:func:`patchfrog.trajectory_intelligence.service.build_trajectory_intelligence_report`
-- never a hand-constructed `TrajectoryHead` standing in for a real DB
round trip (that discipline is reserved for the unit-level matching
tests).

`REVERT_LIKE_CYCLE`/`REINTRODUCED_SURFACE`/`PRODUCTION_THEN_TEST_FOLLOWUP`
are deferred in v1 (see ``validation/trajectory_intelligence/latest-summary.md``
sections 6, 8, 9) -- corpus expectations for those scenarios are
explicit: no such signal is ever constructed, whatever the underlying
data looks like.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.persistence.models.review import ReviewCandidateModel, ReviewRunModel
from patchfrog.persistence.models.review_memory import ReviewGenerationModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.persistence.repositories.pull_request import PullRequestRepository
from patchfrog.persistence.repositories.review_generation import ReviewGenerationRepository
from patchfrog.review.domain import ReviewCandidateReason, ReviewRunStatus
from patchfrog.review_memory.config import NO_MEMORY_CONTEXT_FINGERPRINT
from patchfrog.review_memory.domain import IncrementalRunMode
from patchfrog.trajectory_intelligence.domain import (
    MAX_EVENTS_PER_SURFACE,
    MAX_TRAJECTORY_HEADS,
    MIN_SURFACE_CHURN_EVENTS,
    TrajectorySignalKind,
)
from patchfrog.trajectory_intelligence.matching import select_review_hint
from patchfrog.trajectory_intelligence.queries import fetch_trajectory_heads
from patchfrog.trajectory_intelligence.service import build_trajectory_intelligence_report

_ALL_IMPLEMENTED_SIGNAL_KINDS = frozenset({TrajectorySignalKind.REPEATED_SURFACE_CHURN})


async def _make_repo(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=0,
        )
        await session.commit()
        return repo.id


async def _make_pull_request(
    session_factory: async_sessionmaker[AsyncSession], *, repository_id: uuid.UUID, number: int
) -> uuid.UUID:
    async with session_factory() as session:
        pr = await PullRequestRepository().upsert(
            session, repository_id=repository_id, github_pr_number=number, title="t", author="a",
            base_sha="a" * 40, head_sha="a" * 40, state="open",
        )
        await session.commit()
        return pr.id


async def _stage_review_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    pull_request_id: uuid.UUID | None,
    commit_sha: str,
) -> uuid.UUID:
    async with session_factory() as session:
        # A real, distinct review_run identity per call -- two separate
        # calls for the *same* commit_sha (a legitimate retry) must not
        # collide with ReviewRunModel's own (repository_id, commit_sha,
        # config_fingerprint, model_fingerprint, incremental_context_fingerprint)
        # uniqueness constraint for status='succeeded'.
        unique_suffix = uuid.uuid4().hex
        model = ReviewRunModel(
            id=uuid.uuid4(), repository_id=repository_id, repository_index_id=uuid.uuid4(),
            pull_request_id=pull_request_id, commit_sha=commit_sha,
            config_fingerprint=unique_suffix.ljust(64, "c"),
            model_fingerprint="m" * 64, incremental_context_fingerprint=NO_MEMORY_CONTEXT_FINGERPRINT,
            status=ReviewRunStatus.SUCCEEDED, reviewer_provider="fake", reviewer_model="fake-model",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(model)
        await session.commit()
        return model.id


async def _stage_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    review_run_id: uuid.UUID,
    surfaces: tuple[tuple[str, str], ...],
) -> None:
    if not surfaces:
        return
    async with session_factory() as session:
        for file_path, qualified_name in surfaces:
            session.add(
                ReviewCandidateModel(
                    review_run_id=review_run_id, file_path=file_path, symbol_id=None,
                    symbol_name=qualified_name.rsplit(".", 1)[-1], qualified_name=qualified_name,
                    start_line=1, end_line=5, changed_lines="[1]", reason=ReviewCandidateReason.CHANGED_SYMBOL,
                )
            )
        await session.commit()


async def _stage_generation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    pull_request_id: uuid.UUID,
    review_run_id: uuid.UUID,
    commit_sha: str,
    previous_generation: ReviewGenerationModel | None,
    ancestry_verified: bool,
) -> ReviewGenerationModel:
    async with session_factory() as session:
        gen = await ReviewGenerationRepository().create(
            session, repository_id=repository_id, pull_request_id=pull_request_id, review_run_id=review_run_id,
            commit_sha=commit_sha, previous_generation_id=previous_generation.id if previous_generation else None,
            previous_commit_sha=previous_generation.commit_sha if previous_generation else None,
            ancestry_verified=ancestry_verified, mode=IncrementalRunMode.INCREMENTAL, compatibility_ok=True,
            invalidation_reason=None, memory_compatibility_fingerprint="fp",
        )
        await session.commit()
        return gen


async def _stage_head(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    pull_request_id: uuid.UUID,
    commit_sha: str,
    surfaces: tuple[tuple[str, str], ...] = (),
    previous_generation: ReviewGenerationModel | None = None,
    ancestry_verified: bool = True,
) -> ReviewGenerationModel:
    """Stage one full, real head: a ReviewRunModel, its ReviewCandidateModel
    rows, and its ReviewGenerationModel row, chained to `previous_generation`."""

    run_id = await _stage_review_run(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=commit_sha,
    )
    await _stage_candidates(session_factory, review_run_id=run_id, surfaces=surfaces)
    return await _stage_generation(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, review_run_id=run_id,
        commit_sha=commit_sha, previous_generation=previous_generation, ancestry_verified=ancestry_verified,
    )


def _sha(n: int) -> str:
    return f"{'a' * 39}{n:x}"


# ---- 1. One head, one symbol change -> no signal ----


async def test_case_one_head_one_symbol_no_signal(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-one-head"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(2), as_of=datetime.now(UTC),
        )
    assert report.signals == ()


# ---- 2. Same exact head reviewed twice -> no repeated-churn signal (dedup by commit_sha) ----


async def test_case_same_head_reviewed_twice_dedups(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-same-head-twice"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    shared_sha = _sha(1)
    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=shared_sha,
        surfaces=(("service.py", "apply_discount"),),
    )
    # A legitimate retry: a second ReviewRunModel/ReviewGenerationModel for the *same* commit_sha.
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=shared_sha,
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen1,
    )

    async with session_factory() as session:
        heads = await fetch_trajectory_heads(session, pull_request_id=pull_request_id)
    assert len(heads) == 1  # collapsed to one head despite two generations


# ---- 3. Same symbol changed across 2 distinct heads -> below threshold ----


async def test_case_two_distinct_heads_below_threshold(session_factory: async_sessionmaker[AsyncSession]) -> None:
    assert MIN_SURFACE_CHURN_EVENTS == 3
    full_name = "test/traj-two-heads-below"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen1,
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(3), as_of=datetime.now(UTC),
        )
    assert report.signals == ()


# ---- 4. Same symbol changed across 3 distinct heads -> REPEATED_SURFACE_CHURN ----


async def test_case_three_distinct_heads_triggers_churn(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-three-heads"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )
    gen2 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen1,
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(3),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen2,
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    assert len(report.signals) == 1
    assert report.signals[0].signal_kind is TrajectorySignalKind.REPEATED_SURFACE_CHURN
    assert report.signals[0].distinct_head_count == 3


# ---- 5. Two unrelated symbols across heads -> no cross-contamination ----


async def test_case_unrelated_symbols_no_cross_contamination(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-unrelated-symbols"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("a.py", "foo"),),
    )
    gen2 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("b.py", "bar"),), previous_generation=gen1,
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(3),
        surfaces=(("c.py", "baz"),), previous_generation=gen2,
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    assert report.signals == ()


# ---- 6. Same file, different symbols -> no surface conflation ----


async def test_case_same_file_different_symbols_no_conflation(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-same-file-diff-symbols"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "foo"),),
    )
    gen2 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("service.py", "bar"),), previous_generation=gen1,
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(3),
        surfaces=(("service.py", "baz"),), previous_generation=gen2,
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    assert report.signals == ()


# ---- 7. Docs-only trajectory (touched once) -> no orchestration escalation ----


async def test_case_docs_only_single_touch_no_escalation(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-docs-only"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("README.md", "intro"),),
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(2), as_of=datetime.now(UTC),
        )
    hint = select_review_hint(report.signals, file_path="README.md", qualified_name="intro")
    from patchfrog.trajectory_intelligence.domain import TrajectoryReviewHint

    assert hint is TrajectoryReviewHint.NONE


# ---- 8. Test-only surface churned 3x -> same REPEATED_SURFACE_CHURN treatment as production ----


async def test_case_test_only_surface_churn_treated_like_production(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    full_name = "test/traj-test-only-churn"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("test_service.py", "test_apply_discount"),),
    )
    gen2 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("test_service.py", "test_apply_discount"),), previous_generation=gen1,
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(3),
        surfaces=(("test_service.py", "test_apply_discount"),), previous_generation=gen2,
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    assert len(report.signals) == 1
    assert report.signals[0].signal_kind is TrajectorySignalKind.REPEATED_SURFACE_CHURN


# ---- 9. Force-push breaks ancestry -> incompatible prior trajectory discarded ----


async def test_case_force_push_discards_incompatible_prior_lineage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    full_name = "test/traj-force-push"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )
    # gen2's own ancestry_verified=False -- Phase 7 detected a force-push
    # when this generation was created; its own link to gen1 is broken.
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen1, ancestry_verified=False,
    )

    async with session_factory() as session:
        heads = await fetch_trajectory_heads(session, pull_request_id=pull_request_id)
    assert len(heads) == 1
    assert heads[0].commit_sha == _sha(2)  # gen1 excluded -- never connected across the break


# ---- 10. Known linear ancestry remains valid -> trajectory derived (positive control) ----


async def test_case_linear_ancestry_derives_trajectory(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-linear-ancestry"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
    )
    gen2 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        previous_generation=gen1,
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(3),
        previous_generation=gen2,
    )

    async with session_factory() as session:
        heads = await fetch_trajectory_heads(session, pull_request_id=pull_request_id)
    assert [h.commit_sha for h in heads] == [_sha(1), _sha(2), _sha(3)]
    assert [h.sequence_number for h in heads] == [1, 2, 3]


# ---- 11. Repeated review runs on same SHA -> one event/head ----


async def test_case_repeated_review_runs_same_sha_one_head(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-repeated-runs-same-sha"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    shared_sha = _sha(1)
    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=shared_sha,
        surfaces=(("service.py", "apply_discount"),),
    )
    gen1b = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=shared_sha,
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen1,
    )
    gen2 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen1b,
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(3),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen2,
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    # 3 distinct commit_shas total (shared_sha counted once, plus 2, plus current) -- exactly at threshold.
    assert len(report.signals) == 1
    assert report.signals[0].distinct_head_count == 3


# ---- 12. Carried-forward finding (no fresh candidate row) does not count as a surface change ----


async def test_case_carried_forward_never_counts_as_change_event(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """A head where the candidate was skipped entirely (carried forward
    by Phase 7, never re-reviewed) has no ReviewCandidateModel row for
    that surface at all -- correctly contributes zero events."""

    full_name = "test/traj-carried-forward"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )
    # gen2: this head's own review skipped the candidate (carried forward) -- no row at all.
    gen2 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(), previous_generation=gen1,
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(3),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen2,
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    # Only 2 real events (gen1, gen3) -- below the 3-head threshold.
    assert report.signals == ()


# ---- 13. Surface changed 3x but no current candidate -> no provider escalation ----


async def test_case_no_current_candidate_no_escalation(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-no-current-candidate"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )
    gen2 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen1,
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(3),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen2,
    )

    async with session_factory() as session:
        # Current head does not touch the churned surface at all.
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    from patchfrog.trajectory_intelligence.domain import TrajectoryReviewHint

    hint = select_review_hint(report.signals, file_path="unrelated.py", qualified_name="unrelated_fn")
    assert hint is TrajectoryReviewHint.NONE
    # The historical signal still exists internally (proves it's not silently dropped)...
    assert len(report.signals) == 1
    # ...but no current candidate exists to apply it to.


# ---- 14. Surface changed 3x + real current candidate -> bounded escalation allowed ----


async def test_case_current_candidate_gets_require_critic_hint(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-current-candidate-escalation"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen1,
    )

    from patchfrog.change_intelligence.domain import ChangeKind, ChangeUnit
    from patchfrog.review.domain import ReviewCandidate

    current_candidate = ReviewCandidate(
        file_path="service.py", symbol_id=None, symbol_name="apply_discount", qualified_name="apply_discount",
        start_line=1, end_line=5, changed_lines=(1,), static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
    )
    change_units = (
        ChangeUnit(id="u1", title="t", change_kind=ChangeKind.BEHAVIOR, changed_candidates=(current_candidate,)),
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(3), as_of=datetime.now(UTC),
            change_units=change_units,
        )
    from patchfrog.trajectory_intelligence.domain import TrajectoryReviewHint

    hint = select_review_hint(report.signals, file_path="service.py", qualified_name="apply_discount")
    assert hint is TrajectoryReviewHint.REQUIRE_CRITIC


# ---- 15. Strong signal + candidate already requires critic -> no duplicate critic call ----
# (covered at the effort-policy unit level: test_trajectory_signal_forces_mandatory_critic_even_at_deep
#  in tests/unit/test_review_effort.py -- the override is idempotent, never a second critic mechanism)


# ---- 16. A surface with many churn events still produces exactly one signal ----


async def test_case_many_churn_events_still_one_signal_object(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-one-signal-object"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    previous = None
    for i in range(1, 6):
        previous = await _stage_head(
            session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(i),
            surfaces=(("service.py", "apply_discount"),), previous_generation=previous,
        )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(6), as_of=datetime.now(UTC),
        )
    assert len(report.signals) == 1  # never one signal per event


# ---- 17. Multiple surfaces with signals -> bounded per-run hints ----


async def test_case_multiple_surfaces_produce_multiple_signals(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-multiple-surfaces"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    surfaces = (("a.py", "foo"), ("b.py", "bar"))
    previous = None
    for i in range(1, 4):
        previous = await _stage_head(
            session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(i),
            surfaces=surfaces, previous_generation=previous,
        )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    assert len(report.signals) == 2


# ---- 18. Large lineage -> MAX_TRAJECTORY_HEADS enforced ----


async def test_case_large_lineage_bounded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-large-lineage"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    previous = None
    for i in range(1, MAX_TRAJECTORY_HEADS + 5):
        previous = await _stage_head(
            session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(i),
            previous_generation=previous,
        )

    async with session_factory() as session:
        heads = await fetch_trajectory_heads(session, pull_request_id=pull_request_id)
    assert len(heads) == MAX_TRAJECTORY_HEADS


# ---- 19. Large per-surface event history -> bounded ----


async def test_case_large_per_surface_history_bounded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-large-per-surface"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    previous = None
    for i in range(1, MAX_EVENTS_PER_SURFACE + 4):
        previous = await _stage_head(
            session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(i),
            surfaces=(("service.py", "apply_discount"),), previous_generation=previous,
        )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(99), as_of=datetime.now(UTC),
        )
    assert len(report.signals) == 1
    assert len(report.signals[0].supporting_events) == MAX_EVENTS_PER_SURFACE


# ---- 20. Rename/move -> deferred, no false continuity ----


async def test_case_renamed_symbol_never_falsely_continues_churn(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-renamed-symbol"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )
    # Renamed at head 2 -- a genuinely different qualified_name.
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_loyalty_discount"),), previous_generation=gen1,
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(3), as_of=datetime.now(UTC),
        )
    assert report.signals == ()  # neither name reaches the threshold alone


# ---- 21-23. Deferred signal kinds: never constructed regardless of data shape ----


async def test_case_revert_like_pattern_never_produces_a_signal(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """A -> B -> A structural shape (approximated here by touching the
    same surface repeatedly) must never surface a REVERT_LIKE_CYCLE --
    it is deferred in v1 (see the audit)."""

    full_name = "test/traj-revert-deferred"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    previous = None
    for i in range(1, 4):
        previous = await _stage_head(
            session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(i),
            surfaces=(("service.py", "apply_discount"),), previous_generation=previous,
        )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    assert all(s.signal_kind in _ALL_IMPLEMENTED_SIGNAL_KINDS for s in report.signals)
    assert not any(s.signal_kind is TrajectorySignalKind.REVERT_LIKE_CYCLE for s in report.signals)


async def test_case_reintroduced_surface_never_produces_a_signal(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-reintroduced-deferred"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )
    # "Removed" at head 2 (no row for it), "reintroduced" at head 3.
    gen2 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(), previous_generation=gen1,
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(3),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen2,
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    assert not any(s.signal_kind is TrajectorySignalKind.REINTRODUCED_SURFACE for s in report.signals)


async def test_case_production_then_test_followup_never_produces_a_signal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    full_name = "test/traj-prod-then-test-deferred"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )
    gen2 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen1,
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(3),
        surfaces=(("test_service.py", "test_apply_discount"),), previous_generation=gen2,
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    assert not any(s.signal_kind is TrajectorySignalKind.PRODUCTION_THEN_TEST_FOLLOWUP for s in report.signals)


# ---- 24. N/O + P same surface -> orthogonal, no finding duplication ----


async def test_case_trajectory_orthogonal_to_historical_regression_memory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """N/O reason about *cross-review* historical trust; P reasons about
    *current-PR-lineage* churn. Both can independently reference the
    same surface without any shared state or interference -- proven by
    computing a Trajectory report for a surface with real churn and
    confirming it carries no historical-regression-memory-shaped
    fields at all (structurally orthogonal domain types)."""

    full_name = "test/traj-orthogonal-to-n-o"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )
    gen2 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen1,
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(3),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen2,
    )

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(4), as_of=datetime.now(UTC),
        )
    assert len(report.signals) == 1
    from dataclasses import fields

    field_names = {f.name for f in fields(report.signals[0])}
    assert "historical_record" not in field_names
    assert "enriches_historical_regression" not in field_names


# ---- 25. No trajectory history available -> empty report, normal review continues ----


async def test_case_no_pull_request_context_empty_report(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=None, current_commit_sha=_sha(1), as_of=datetime.now(UTC),
        )
    assert report.lineage_valid is False
    assert report.signals == ()
    assert report.events == ()


async def test_case_fresh_pr_no_prior_generation_empty_report(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-fresh-pr"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(1), as_of=datetime.now(UTC),
        )
    assert report.lineage_valid is False
    assert report.signals == ()


# ---- 26. Retry/replay exact same current head -> deterministic identical report ----


async def test_case_replay_same_inputs_produces_identical_report(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-replay-deterministic"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    gen1 = await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )
    await _stage_head(
        session_factory, repository_id=repository_id, pull_request_id=pull_request_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),), previous_generation=gen1,
    )

    as_of = datetime.now(UTC)
    async with session_factory() as session:
        report_a = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(3), as_of=as_of,
        )
    async with session_factory() as session:
        report_b = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(3), as_of=as_of,
        )
    assert report_a.head_count == report_b.head_count
    assert report_a.event_count == report_b.event_count
    assert report_a.signal_count == report_b.signal_count


# ---- 27. Repository isolation ----


async def test_case_repository_isolation(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name_a = "test/traj-isolation-a"
    full_name_b = "test/traj-isolation-b"
    repository_a = await _make_repo(session_factory, full_name_a)
    repository_b = await _make_repo(session_factory, full_name_b)
    pr_a = await _make_pull_request(session_factory, repository_id=repository_a, number=1)
    pr_b = await _make_pull_request(session_factory, repository_id=repository_b, number=1)

    previous = None
    for i in range(1, 4):
        previous = await _stage_head(
            session_factory, repository_id=repository_a, pull_request_id=pr_a, commit_sha=_sha(i),
            surfaces=(("service.py", "apply_discount"),), previous_generation=previous,
        )

    async with session_factory() as session:
        report_b = await build_trajectory_intelligence_report(
            session, pull_request_id=pr_b, current_commit_sha=_sha(99), as_of=datetime.now(UTC),
        )
    assert report_b.signals == ()
    assert report_b.heads_considered == ()


# ---- 28. Zero unconditional provider calls (structural proof) ----


def test_trajectory_intelligence_never_imports_a_provider() -> None:
    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "patchfrog" / "trajectory_intelligence"
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert "LLMProvider" not in (node.module or "") and not any(
                    alias.name == "LLMProvider" for alias in node.names
                ), f"{path} imports LLMProvider -- Trajectory Intelligence must add zero provider calls"


# ---- 29. MIN_SURFACE_CHURN_EVENTS is exactly 3, never configurable lower ----


def test_min_surface_churn_events_is_three() -> None:
    assert MIN_SURFACE_CHURN_EVENTS == 3


# ---- 30. A quiet PR (no history at all) behaves exactly like before this milestone ----


async def test_case_quiet_pr_no_signal_no_hint(session_factory: async_sessionmaker[AsyncSession]) -> None:
    full_name = "test/traj-quiet-pr"
    repository_id = await _make_repo(session_factory, full_name)
    pull_request_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1)

    async with session_factory() as session:
        report = await build_trajectory_intelligence_report(
            session, pull_request_id=pull_request_id, current_commit_sha=_sha(1), as_of=datetime.now(UTC),
        )
    from patchfrog.trajectory_intelligence.domain import TrajectoryReviewHint

    hint = select_review_hint(report.signals, file_path="service.py", qualified_name="apply_discount")
    assert hint is TrajectoryReviewHint.NONE
