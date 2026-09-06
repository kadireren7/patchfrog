"""Unit tests for :mod:`patchfrog.cross_pr_intelligence.matching` --
deterministic, pure derivation. Every case is a hand-built
:class:`~patchfrog.cross_pr_intelligence.domain.CrossPRPeer`, no
database, no LLM -- mirrors
tests/unit/test_trajectory_intelligence_matching.py's own discipline."""

from __future__ import annotations

import uuid

from patchfrog.cross_pr_intelligence.domain import (
    CrossPROverlapKind,
    CrossPRPeer,
    CrossPRReviewHint,
)
from patchfrog.cross_pr_intelligence.matching import (
    derive_cross_pr_overlaps,
    derive_cross_pr_signals,
    select_review_hint,
)


def _peer(*, pr_number: int, sequence_number: int = 1) -> CrossPRPeer:
    return CrossPRPeer(
        pull_request_id=uuid.uuid4(),
        github_pr_number=pr_number,
        head_commit_sha=f"commit-{pr_number}",
        review_run_id=uuid.uuid4(),
        sequence_number=sequence_number,
    )


def test_no_current_surfaces_no_overlaps() -> None:
    peer = _peer(pr_number=42)
    overlaps = derive_cross_pr_overlaps(
        peers=(peer,),
        peer_surfaces_by_review_run={peer.review_run_id: (("service.py", "process_payment"),)},
        current_changed_surfaces=(),
    )
    assert overlaps == ()
    assert derive_cross_pr_signals(overlaps) == ()


def test_no_peers_no_overlaps() -> None:
    overlaps = derive_cross_pr_overlaps(
        peers=(), peer_surfaces_by_review_run={}, current_changed_surfaces=(("service.py", "process_payment"),),
    )
    assert overlaps == ()


def test_same_changed_symbol_produces_overlap_and_signal() -> None:
    peer = _peer(pr_number=42)
    overlaps = derive_cross_pr_overlaps(
        peers=(peer,),
        peer_surfaces_by_review_run={peer.review_run_id: (("service.py", "process_payment"),)},
        current_changed_surfaces=(("service.py", "process_payment"),),
    )
    assert len(overlaps) == 1
    assert overlaps[0].overlap_kind is CrossPROverlapKind.SAME_CHANGED_SYMBOL
    assert overlaps[0].peer.github_pr_number == 42

    signals = derive_cross_pr_signals(overlaps)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.signal_kind is CrossPROverlapKind.SAME_CHANGED_SYMBOL
    assert signal.review_hint is CrossPRReviewHint.REQUIRE_CRITIC
    assert signal.distinct_peer_count == 1
    assert "#42" in signal.evidence


def test_single_overlap_is_sufficient_unlike_trajectory_churn() -> None:
    """Unlike Trajectory Intelligence's MIN_SURFACE_CHURN_EVENTS
    threshold, a single real cross-PR overlap must produce a signal --
    concurrent cross-author change is not noise at N=1."""

    peer = _peer(pr_number=7)
    overlaps = derive_cross_pr_overlaps(
        peers=(peer,),
        peer_surfaces_by_review_run={peer.review_run_id: (("a.py", "foo"),)},
        current_changed_surfaces=(("a.py", "foo"),),
    )
    assert len(overlaps) == 1
    assert len(derive_cross_pr_signals(overlaps)) == 1


def test_same_file_different_symbol_never_overlaps() -> None:
    peer = _peer(pr_number=42)
    overlaps = derive_cross_pr_overlaps(
        peers=(peer,),
        peer_surfaces_by_review_run={peer.review_run_id: (("service.py", "other_fn"),)},
        current_changed_surfaces=(("service.py", "process_payment"),),
    )
    assert overlaps == ()


def test_different_file_same_symbol_name_never_overlaps() -> None:
    peer = _peer(pr_number=42)
    overlaps = derive_cross_pr_overlaps(
        peers=(peer,),
        peer_surfaces_by_review_run={peer.review_run_id: (("other.py", "process_payment"),)},
        current_changed_surfaces=(("service.py", "process_payment"),),
    )
    assert overlaps == ()


def test_multiple_peers_on_same_surface_deduplicated_into_one_signal() -> None:
    peer_a = _peer(pr_number=10)
    peer_b = _peer(pr_number=11)
    overlaps = derive_cross_pr_overlaps(
        peers=(peer_a, peer_b),
        peer_surfaces_by_review_run={
            peer_a.review_run_id: (("service.py", "process_payment"),),
            peer_b.review_run_id: (("service.py", "process_payment"),),
        },
        current_changed_surfaces=(("service.py", "process_payment"),),
    )
    assert len(overlaps) == 2
    signals = derive_cross_pr_signals(overlaps)
    assert len(signals) == 1
    assert signals[0].distinct_peer_count == 2
    assert "#10" in signals[0].evidence
    assert "#11" in signals[0].evidence


def test_unrelated_surfaces_never_combine() -> None:
    peer = _peer(pr_number=1)
    overlaps = derive_cross_pr_overlaps(
        peers=(peer,),
        peer_surfaces_by_review_run={peer.review_run_id: (("a.py", "foo"), ("b.py", "bar"))},
        current_changed_surfaces=(("c.py", "baz"),),
    )
    assert overlaps == ()


def test_max_cross_pr_overlaps_bounded() -> None:
    from patchfrog.cross_pr_intelligence.domain import MAX_CROSS_PR_OVERLAPS

    peer = _peer(pr_number=1)
    many_surfaces = tuple((f"file_{i}.py", f"symbol_{i}") for i in range(MAX_CROSS_PR_OVERLAPS + 10))
    overlaps = derive_cross_pr_overlaps(
        peers=(peer,),
        peer_surfaces_by_review_run={peer.review_run_id: many_surfaces},
        current_changed_surfaces=many_surfaces,
    )
    assert len(overlaps) == MAX_CROSS_PR_OVERLAPS


def test_select_review_hint_none_for_unrelated_candidate() -> None:
    hint = select_review_hint((), file_path="service.py", qualified_name="process_payment")
    assert hint is CrossPRReviewHint.NONE


def test_select_review_hint_none_for_module_region_candidate() -> None:
    peer = _peer(pr_number=42)
    overlaps = derive_cross_pr_overlaps(
        peers=(peer,),
        peer_surfaces_by_review_run={peer.review_run_id: (("service.py", "process_payment"),)},
        current_changed_surfaces=(("service.py", "process_payment"),),
    )
    signals = derive_cross_pr_signals(overlaps)
    assert select_review_hint(signals, file_path="service.py", qualified_name=None) is CrossPRReviewHint.NONE


def test_select_review_hint_require_critic_for_matching_candidate() -> None:
    peer = _peer(pr_number=42)
    overlaps = derive_cross_pr_overlaps(
        peers=(peer,),
        peer_surfaces_by_review_run={peer.review_run_id: (("service.py", "process_payment"),)},
        current_changed_surfaces=(("service.py", "process_payment"),),
    )
    signals = derive_cross_pr_signals(overlaps)
    hint = select_review_hint(signals, file_path="service.py", qualified_name="process_payment")
    assert hint is CrossPRReviewHint.REQUIRE_CRITIC
