"""Unit tests for :mod:`patchfrog.cross_repo_intelligence.matching` --
deterministic, pure derivation. Every case is a hand-built
:class:`~patchfrog.cross_repo_intelligence.domain.CrossRepoPeer`, no
database, no LLM -- mirrors
tests/unit/test_cross_pr_intelligence_matching.py's own discipline."""

from __future__ import annotations

import uuid

from patchfrog.cross_repo_intelligence.domain import (
    CrossRepoOverlap,
    CrossRepoPeer,
    CrossRepoReviewHint,
    CrossRepoSignalKind,
    RepositoryRelationKind,
)
from patchfrog.cross_repo_intelligence.matching import derive_cross_repo_signals, select_review_hint


def _peer(*, full_name: str, contract_key: str = "payments.capture:v1") -> CrossRepoPeer:
    return CrossRepoPeer(
        repository_id=uuid.uuid4(), full_name=full_name,
        relation_kind=RepositoryRelationKind.EXPLICIT_SHARED_CONTRACT, contract_key=contract_key,
    )


def _overlap(*, peer: CrossRepoPeer, file_path: str, qualified_name: str) -> CrossRepoOverlap:
    return CrossRepoOverlap(
        peer=peer, signal_kind=CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE,
        file_path=file_path, qualified_name=qualified_name,
    )


def test_no_overlaps_no_signals() -> None:
    assert derive_cross_repo_signals(()) == ()


def test_single_overlap_is_sufficient() -> None:
    peer = _peer(full_name="org/service-b")
    overlap = _overlap(peer=peer, file_path="capture.py", qualified_name="capture_payment")
    signals = derive_cross_repo_signals((overlap,))
    assert len(signals) == 1
    signal = signals[0]
    assert signal.signal_kind is CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE
    assert signal.review_hint is CrossRepoReviewHint.REQUIRE_CRITIC
    assert signal.distinct_peer_count == 1
    assert "org/service-b" in signal.evidence
    assert "payments.capture:v1" in signal.evidence


def test_multiple_peers_same_surface_one_signal() -> None:
    peer_a = _peer(full_name="org/service-b")
    peer_b = _peer(full_name="org/service-c")
    overlaps = (
        _overlap(peer=peer_a, file_path="capture.py", qualified_name="capture_payment"),
        _overlap(peer=peer_b, file_path="capture.py", qualified_name="capture_payment"),
    )
    signals = derive_cross_repo_signals(overlaps)
    assert len(signals) == 1
    assert signals[0].distinct_peer_count == 2
    assert "org/service-b" in signals[0].evidence
    assert "org/service-c" in signals[0].evidence


def test_unrelated_surfaces_never_combine() -> None:
    peer = _peer(full_name="org/service-b")
    overlaps = (
        _overlap(peer=peer, file_path="a.py", qualified_name="foo"),
        _overlap(peer=peer, file_path="b.py", qualified_name="bar"),
    )
    signals = derive_cross_repo_signals(overlaps)
    assert len(signals) == 2


def test_max_cross_repo_signals_bounded() -> None:
    from patchfrog.cross_repo_intelligence.domain import MAX_CROSS_REPO_SIGNALS

    peer = _peer(full_name="org/service-b")
    overlaps = tuple(
        _overlap(peer=peer, file_path=f"file_{i}.py", qualified_name=f"symbol_{i}")
        for i in range(MAX_CROSS_REPO_SIGNALS + 5)
    )
    signals = derive_cross_repo_signals(overlaps)
    assert len(signals) == MAX_CROSS_REPO_SIGNALS


def test_select_review_hint_none_for_unrelated_candidate() -> None:
    hint = select_review_hint((), file_path="capture.py", qualified_name="capture_payment")
    assert hint is CrossRepoReviewHint.NONE


def test_select_review_hint_none_for_module_region_candidate() -> None:
    peer = _peer(full_name="org/service-b")
    overlap = _overlap(peer=peer, file_path="capture.py", qualified_name="capture_payment")
    signals = derive_cross_repo_signals((overlap,))
    assert select_review_hint(signals, file_path="capture.py", qualified_name=None) is CrossRepoReviewHint.NONE


def test_select_review_hint_require_critic_for_matching_candidate() -> None:
    peer = _peer(full_name="org/service-b")
    overlap = _overlap(peer=peer, file_path="capture.py", qualified_name="capture_payment")
    signals = derive_cross_repo_signals((overlap,))
    hint = select_review_hint(signals, file_path="capture.py", qualified_name="capture_payment")
    assert hint is CrossRepoReviewHint.REQUIRE_CRITIC
