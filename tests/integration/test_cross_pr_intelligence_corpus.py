"""Controlled corpus for Cross-PR Intelligence Foundation (minimum 30
scenarios) -- real, persisted `pull_requests`/`review_generations`/
`review_candidates` rows via
:class:`~patchfrog.persistence.repositories.pull_request.PullRequestRepository`
and :class:`~patchfrog.persistence.repositories.review_generation.ReviewGenerationRepository`,
never a hand-built `CrossPRPeer` standing in for a real DB round trip
(that discipline is reserved for the unit-level matching tests in
``tests/unit/test_cross_pr_intelligence_matching.py``).

`SHARED_AFFECTED_SURFACE`/`SHARED_CONTRACT_SURFACE`/
`CONTRACT_PRODUCER_CONSUMER_COLLISION` are deferred in v1 (see
``validation/cross_pr_intelligence/latest-summary.md`` section 6) --
only `SAME_CHANGED_SYMBOL` is ever constructed, whatever the underlying
data looks like.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.change_intelligence.domain import ChangeKind, ChangeUnit
from patchfrog.cross_pr_intelligence.domain import (
    MAX_CROSS_PR_OVERLAPS,
    MAX_CROSS_PR_PEERS,
    MAX_CROSS_PR_SURFACES_PER_PEER,
    CrossPROverlapKind,
    CrossPRReviewHint,
)
from patchfrog.cross_pr_intelligence.matching import select_review_hint
from patchfrog.cross_pr_intelligence.service import build_cross_pr_intelligence_report
from patchfrog.persistence.models.review import ReviewCandidateModel, ReviewRunModel
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.persistence.repositories.pull_request import PullRequestRepository
from patchfrog.persistence.repositories.review_generation import ReviewGenerationRepository
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason, ReviewRunStatus
from patchfrog.review_memory.config import NO_MEMORY_CONTEXT_FINGERPRINT
from patchfrog.review_memory.domain import IncrementalRunMode


async def _make_repo(session_factory: async_sessionmaker[AsyncSession], full_name: str) -> uuid.UUID:
    async with session_factory() as session:
        repo = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner="test", name=full_name.split("/")[-1], full_name=full_name, installation_id=0,
        )
        await session.commit()
        return repo.id


async def _make_pull_request(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    number: int,
    head_sha: str,
    state: str = "open",
) -> uuid.UUID:
    async with session_factory() as session:
        pr = await PullRequestRepository().upsert(
            session, repository_id=repository_id, github_pr_number=number, title="t", author="a",
            base_sha="a" * 40, head_sha=head_sha, state=state,
        )
        await session.commit()
        return pr.id


async def _stage_review(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository_id: uuid.UUID,
    pull_request_id: uuid.UUID,
    commit_sha: str,
    surfaces: tuple[tuple[str, str], ...],
    include_module_region: bool = False,
) -> None:
    """Stage one full, real reviewed head for a PR: a ReviewRunModel,
    its ReviewCandidateModel rows, and one ReviewGenerationModel
    (sequence_number=1, no prior generation -- a peer only ever needs
    its one latest reviewed head)."""

    async with session_factory() as session:
        unique_suffix = uuid.uuid4().hex
        run = ReviewRunModel(
            id=uuid.uuid4(), repository_id=repository_id, repository_index_id=uuid.uuid4(),
            pull_request_id=pull_request_id, commit_sha=commit_sha,
            config_fingerprint=unique_suffix.ljust(64, "c"),
            model_fingerprint="m" * 64, incremental_context_fingerprint=NO_MEMORY_CONTEXT_FINGERPRINT,
            status=ReviewRunStatus.SUCCEEDED, reviewer_provider="fake", reviewer_model="fake-model",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()

        for file_path, qualified_name in surfaces:
            session.add(
                ReviewCandidateModel(
                    review_run_id=run.id, file_path=file_path, symbol_id=None,
                    symbol_name=qualified_name.rsplit(".", 1)[-1], qualified_name=qualified_name,
                    start_line=1, end_line=5, changed_lines="[1]", reason=ReviewCandidateReason.CHANGED_SYMBOL,
                )
            )
        if include_module_region:
            session.add(
                ReviewCandidateModel(
                    review_run_id=run.id, file_path="module_region.py", symbol_id=None,
                    symbol_name=None, qualified_name=None,
                    start_line=1, end_line=5, changed_lines="[1]", reason=ReviewCandidateReason.CHANGED_SYMBOL,
                )
            )
        await session.commit()
        run_id = run.id

    async with session_factory() as session:
        await ReviewGenerationRepository().create(
            session, repository_id=repository_id, pull_request_id=pull_request_id, review_run_id=run_id,
            commit_sha=commit_sha, previous_generation_id=None, previous_commit_sha=None,
            ancestry_verified=True, mode=IncrementalRunMode.INCREMENTAL, compatibility_ok=True,
            invalidation_reason=None, memory_compatibility_fingerprint="fp",
        )
        await session.commit()


def _change_unit(surfaces: tuple[tuple[str, str], ...]) -> ChangeUnit:
    candidates = tuple(
        ReviewCandidate(
            file_path=file_path, symbol_id=None, symbol_name=qualified_name.rsplit(".", 1)[-1],
            qualified_name=qualified_name, start_line=1, end_line=5, changed_lines=(1,),
            static_finding_ids=(), reason=ReviewCandidateReason.CHANGED_SYMBOL,
        )
        for file_path, qualified_name in surfaces
    )
    return ChangeUnit(id="unit-1", title="t", change_kind=ChangeKind.BEHAVIOR, changed_candidates=candidates)


def _sha(n: int) -> str:
    return f"{'a' * 39}{n:x}"


# ---- 1. No peers exist -> empty report ----


async def test_case_no_peers_empty_report(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-no-peers")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert report.peers_considered == ()
    assert report.overlaps == ()
    assert report.signals == ()


# ---- 2. One open peer with a real SAME_CHANGED_SYMBOL overlap ----


async def test_case_one_open_peer_same_changed_symbol_overlap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-basic-overlap")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert len(report.peers_considered) == 1
    assert len(report.overlaps) == 1
    assert report.overlaps[0].overlap_kind is CrossPROverlapKind.SAME_CHANGED_SYMBOL
    assert len(report.signals) == 1
    assert report.signals[0].review_hint is CrossPRReviewHint.REQUIRE_CRITIC
    hint = select_review_hint(report.signals, file_path="service.py", qualified_name="apply_discount")
    assert hint is CrossPRReviewHint.REQUIRE_CRITIC


# ---- 3. Peer changes a different symbol in the same file -> no overlap ----


async def test_case_same_file_different_symbol_no_overlap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-same-file-diff-symbol")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(("service.py", "other_function"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert len(report.peers_considered) == 1
    assert report.overlaps == ()
    assert report.signals == ()


# ---- 4. Closed peer is excluded ----


async def test_case_closed_peer_excluded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-closed-peer")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=2, head_sha=_sha(2), state="closed",
    )
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert report.peers_considered == ()
    assert report.overlaps == ()


# ---- 5. Merged peer is excluded ----


async def test_case_merged_peer_excluded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-merged-peer")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=2, head_sha=_sha(2), state="merged",
    )
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert report.peers_considered == ()


# ---- 6. Stale peer head (head_sha no longer matches latest reviewed generation) is excluded ----


async def test_case_stale_peer_head_excluded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-stale-peer")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    # Peer's *persisted* head_sha has moved past what was last reviewed.
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(99))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert report.peers_considered == ()


# ---- 7. Peer in a different repository is excluded even with an identical symbol name (hard filter) ----


async def test_case_different_repository_never_a_peer(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_a = await _make_repo(session_factory, "test/cpr-repo-a")
    repository_b = await _make_repo(session_factory, "test/cpr-repo-b")
    current_id = await _make_pull_request(session_factory, repository_id=repository_a, number=1, head_sha=_sha(1))
    other_repo_pr = await _make_pull_request(session_factory, repository_id=repository_b, number=1, head_sha=_sha(2))
    await _stage_review(
        session_factory, repository_id=repository_b, pull_request_id=other_repo_pr, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_a, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert report.peers_considered == ()
    assert report.overlaps == ()


# ---- 8. More than MAX_CROSS_PR_PEERS open peers -> bounded ----


async def test_case_peer_count_bounded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-peer-bound")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    for i in range(MAX_CROSS_PR_PEERS + 5):
        peer_id = await _make_pull_request(
            session_factory, repository_id=repository_id, number=100 + i, head_sha=_sha(100 + i),
        )
        await _stage_review(
            session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(100 + i),
            surfaces=((f"file_{i}.py", f"symbol_{i}"),),
        )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id, change_units=(),
        )
    assert len(report.peers_considered) == MAX_CROSS_PR_PEERS


# ---- 9. Peer with more than MAX_CROSS_PR_SURFACES_PER_PEER changed surfaces -> bounded ----


async def test_case_peer_surfaces_bounded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-surface-bound")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    many_surfaces = tuple((f"file_{i}.py", f"symbol_{i}") for i in range(MAX_CROSS_PR_SURFACES_PER_PEER + 10))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=many_surfaces,
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit(many_surfaces),),
        )
    assert len(report.overlaps) <= MAX_CROSS_PR_SURFACES_PER_PEER


# ---- 10. Multiple peers overlapping the same current-PR surface -> deduplicated into one signal ----


async def test_case_multiple_peers_same_surface_one_signal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-multi-peer-dedup")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_a = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    peer_b = await _make_pull_request(session_factory, repository_id=repository_id, number=3, head_sha=_sha(3))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_a, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_b, commit_sha=_sha(3),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert len(report.peers_considered) == 2
    assert len(report.overlaps) == 2
    assert len(report.signals) == 1
    assert report.signals[0].distinct_peer_count == 2


# ---- 11. pull_request_id=None (local review) -> empty report ----


async def test_case_no_pull_request_id_empty_report(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-no-pr-id")

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=None,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert report.peers_considered == ()
    assert report.overlaps == ()
    assert report.signals == ()


# ---- 12. Peer's only match is a module-region candidate (qualified_name=None) -> excluded ----


async def test_case_peer_module_region_candidate_excluded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-module-region")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(), include_module_region=True,
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("module_region.py", "irrelevant"),)),),
        )
    assert report.overlaps == ()


# ---- 13. Peer never reviewed (no generation at all) -> excluded ----


async def test_case_peer_never_reviewed_excluded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-never-reviewed")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert report.peers_considered == ()


# ---- 14. Current PR has no changed surfaces -> no overlaps even with peers present ----


async def test_case_current_pr_no_changed_surfaces_no_overlaps(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-current-no-surfaces")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id, change_units=(),
        )
    assert len(report.peers_considered) == 1
    assert report.overlaps == ()


# ---- 15. The current PR is never considered its own peer ----


async def test_case_current_pr_never_its_own_peer(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-self-exclusion")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=current_id, commit_sha=_sha(1),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert report.peers_considered == ()
    assert report.overlaps == ()


# ---- 16. One peer overlaps, one doesn't -> both counted as peers, only one signal ----


async def test_case_one_overlapping_one_not_both_counted_as_peers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-mixed-peers")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    overlapping_peer = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    unrelated_peer = await _make_pull_request(session_factory, repository_id=repository_id, number=3, head_sha=_sha(3))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=overlapping_peer, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=unrelated_peer, commit_sha=_sha(3),
        surfaces=(("unrelated.py", "unrelated_fn"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert len(report.peers_considered) == 2
    assert len(report.overlaps) == 1
    assert len(report.signals) == 1


# ---- 17. Reopened-then-closed-then-open-again peer (state reflects latest webhook, not history) ----


async def test_case_reopened_peer_is_eligible(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-reopened")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(
        session_factory, repository_id=repository_id, number=2, head_sha=_sha(2), state="closed",
    )
    # Reopened -- state flips back to open (a real ingest() upsert would do this).
    async with session_factory() as session:
        await PullRequestRepository().upsert(
            session, repository_id=repository_id, github_pr_number=2, title="t", author="a",
            base_sha="a" * 40, head_sha=_sha(2), state="open",
        )
        await session.commit()
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert len(report.peers_considered) == 1


# ---- 18. Peer overlapping on more than one surface -> multiple overlaps, multiple signals ----


async def test_case_peer_overlapping_multiple_surfaces(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-multi-surface")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    surfaces = (("service.py", "apply_discount"), ("billing.py", "charge_customer"))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2), surfaces=surfaces,
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id, change_units=(_change_unit(surfaces),),
        )
    assert len(report.overlaps) == 2
    assert len(report.signals) == 2


# ---- 19. Unrelated file names never coincidentally match ----


async def test_case_unrelated_files_no_overlap(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-unrelated-files")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(("a.py", "foo"), ("b.py", "bar")),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("c.py", "baz"),)),),
        )
    assert report.overlaps == ()


# ---- 20. Overlap count bounded across many overlapping peers ----


async def test_case_overlap_count_bounded_across_peers(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-overlap-bound")
    current_surfaces = tuple((f"file_{i}.py", f"symbol_{i}") for i in range(MAX_CROSS_PR_OVERLAPS + 10))
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    for i in range(MAX_CROSS_PR_PEERS):
        peer_id = await _make_pull_request(
            session_factory, repository_id=repository_id, number=100 + i, head_sha=_sha(100 + i),
        )
        await _stage_review(
            session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(100 + i),
            surfaces=current_surfaces,
        )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit(current_surfaces),),
        )
    assert len(report.overlaps) == MAX_CROSS_PR_OVERLAPS


# ---- 21. Signal evidence text names the peer's PR number, not its author ----


async def test_case_signal_evidence_names_pr_number_not_author(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-evidence-wording")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=99, head_sha=_sha(2))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert "#99" in report.signals[0].evidence
    assert "author" not in report.signals[0].evidence.lower()


# ---- 22. Peer eligibility never depends on matching title/author alone ----


async def test_case_title_author_alone_never_creates_overlap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A peer sharing the current PR's title/author but touching an
    unrelated surface must never overlap -- only real structural
    symbol identity counts."""

    repository_id = await _make_repo(session_factory, "test/cpr-title-author")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(("unrelated_file.py", "unrelated_symbol"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert report.overlaps == ()


# ---- 23. Zero unconditional provider calls (structural proof) ----


def test_cross_pr_intelligence_never_imports_a_provider() -> None:
    import ast
    from pathlib import Path as _Path

    package_dir = _Path(__file__).parent.parent.parent / "patchfrog" / "cross_pr_intelligence"
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert "LLMProvider" not in (node.module or "") and not any(
                    alias.name == "LLMProvider" for alias in node.names
                ), f"{path} imports LLMProvider -- Cross-PR Intelligence must add zero provider calls"


# ---- 24. Deferred overlap kinds are never constructed ----


def test_deferred_overlap_kinds_never_constructed() -> None:
    """SHARED_AFFECTED_SURFACE/SHARED_CONTRACT_SURFACE/
    CONTRACT_PRODUCER_CONSUMER_COLLISION are kept on the enum for
    forward documentation only -- the matching module's own hint table
    only ever maps SAME_CHANGED_SYMBOL."""

    from patchfrog.cross_pr_intelligence.matching import _SIGNAL_HINT

    assert set(_SIGNAL_HINT.keys()) == {CrossPROverlapKind.SAME_CHANGED_SYMBOL}


# ---- 25. Peer ordering prefers most-recently-active when bounding ----


async def test_case_most_recently_active_peer_kept_when_bounded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When more open peers exist than MAX_CROSS_PR_PEERS, the most
    recently updated ones are kept -- proven by staging one peer,
    letting it be the *oldest* by re-touching every later peer's
    updated_at via a fresh upsert, then confirming a specific
    early peer's overlap is dropped once the bound is exceeded by
    strictly-newer peers."""

    repository_id = await _make_repo(session_factory, "test/cpr-recency-order")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))

    oldest_peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=oldest_peer_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )

    for i in range(MAX_CROSS_PR_PEERS):
        peer_id = await _make_pull_request(
            session_factory, repository_id=repository_id, number=200 + i, head_sha=_sha(200 + i),
        )
        await _stage_review(
            session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(200 + i),
            surfaces=((f"file_{i}.py", f"symbol_{i}"),),
        )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert len(report.peers_considered) == MAX_CROSS_PR_PEERS
    kept_ids = {p.pull_request_id for p in report.peers_considered}
    assert oldest_peer_id not in kept_ids


# ---- 26. Empty change_units with peers present never crashes ----


async def test_case_empty_change_units_never_crashes(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-empty-change-units")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2),
        surfaces=(("service.py", "apply_discount"),),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id, change_units=(),
        )
    assert report.overlaps == ()


# ---- 27. Version is stamped on every report ----


async def test_case_report_stamped_with_current_version(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from patchfrog.cross_pr_intelligence.domain import CROSS_PR_INTELLIGENCE_VERSION

    repository_id = await _make_repo(session_factory, "test/cpr-version-stamp")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id, change_units=(),
        )
    assert report.version == CROSS_PR_INTELLIGENCE_VERSION


# ---- 28. A peer's own review_run_id with zero candidates never crashes or overlaps ----


async def test_case_peer_review_run_with_zero_candidates(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_id = await _make_repo(session_factory, "test/cpr-zero-candidates")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2), surfaces=(),
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id,
            change_units=(_change_unit((("service.py", "apply_discount"),)),),
        )
    assert len(report.peers_considered) == 1
    assert report.overlaps == ()


# ---- 29. Two peers in two different repositories never interfere with each other's bound ----


async def test_case_peer_bound_is_per_repository(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repository_a = await _make_repo(session_factory, "test/cpr-repo-bound-a")
    repository_b = await _make_repo(session_factory, "test/cpr-repo-bound-b")
    current_a = await _make_pull_request(session_factory, repository_id=repository_a, number=1, head_sha=_sha(1))

    for i in range(MAX_CROSS_PR_PEERS):
        peer_id = await _make_pull_request(
            session_factory, repository_id=repository_b, number=300 + i, head_sha=_sha(300 + i),
        )
        await _stage_review(
            session_factory, repository_id=repository_b, pull_request_id=peer_id, commit_sha=_sha(300 + i),
            surfaces=((f"file_{i}.py", f"symbol_{i}"),),
        )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_a, pull_request_id=current_a, change_units=(),
        )
    assert report.peers_considered == ()


# ---- 30. Signal count bounded across many distinct overlapping surfaces ----


async def test_case_signal_count_bounded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from patchfrog.cross_pr_intelligence.domain import MAX_CROSS_PR_SIGNALS

    repository_id = await _make_repo(session_factory, "test/cpr-signal-bound")
    current_id = await _make_pull_request(session_factory, repository_id=repository_id, number=1, head_sha=_sha(1))
    peer_id = await _make_pull_request(session_factory, repository_id=repository_id, number=2, head_sha=_sha(2))
    surfaces = tuple((f"file_{i}.py", f"symbol_{i}") for i in range(MAX_CROSS_PR_SIGNALS + 5))
    await _stage_review(
        session_factory, repository_id=repository_id, pull_request_id=peer_id, commit_sha=_sha(2), surfaces=surfaces,
    )

    async with session_factory() as session:
        report = await build_cross_pr_intelligence_report(
            session, repository_id=repository_id, pull_request_id=current_id, change_units=(_change_unit(surfaces),),
        )
    assert len(report.signals) <= MAX_CROSS_PR_SIGNALS
