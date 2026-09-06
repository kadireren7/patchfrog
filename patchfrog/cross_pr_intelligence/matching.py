"""Deterministic derivation of
:class:`~patchfrog.cross_pr_intelligence.domain.CrossPROverlap`\\ s and
:class:`~patchfrog.cross_pr_intelligence.domain.CrossPRSignal`\\ s --
pure, synchronous, consuming only already-fetched peer surfaces
(:mod:`patchfrog.cross_pr_intelligence.queries`) and the current, in
-progress review's own already-computed changed surfaces (Change
Intelligence's own ``ChangeUnit.changed_candidates``, computed earlier
in the same run -- never re-derived here).
"""

from __future__ import annotations

import uuid

from patchfrog.cross_pr_intelligence.domain import (
    MAX_CROSS_PR_OVERLAPS,
    MAX_CROSS_PR_SIGNALS,
    CrossPROverlap,
    CrossPROverlapKind,
    CrossPRPeer,
    CrossPRReviewHint,
    CrossPRSignal,
)

#: The only mapping any v1 signal kind ever uses -- see the domain
#: module's own docstring for why `DEEPEN_CONTEXT`/
#: `INCREASE_CANDIDATE_PRIORITY` are reserved, never selected here.
_SIGNAL_HINT: dict[CrossPROverlapKind, CrossPRReviewHint] = {
    CrossPROverlapKind.SAME_CHANGED_SYMBOL: CrossPRReviewHint.REQUIRE_CRITIC,
}


def derive_cross_pr_overlaps(
    *,
    peers: tuple[CrossPRPeer, ...],
    peer_surfaces_by_review_run: dict[uuid.UUID, tuple[tuple[str, str], ...]],
    current_changed_surfaces: tuple[tuple[str, str], ...],
) -> tuple[CrossPROverlap, ...]:
    """One :class:`CrossPROverlap` for every ``(peer, surface)`` pair
    where the current PR's own changed surfaces and that peer's latest
    reviewed head's changed surfaces share the exact same
    ``(file_path, qualified_name)`` -- same-file-different-symbol never
    matches, since the key includes ``qualified_name``."""

    current_set = set(current_changed_surfaces)
    if not current_set:
        return ()

    overlaps: list[CrossPROverlap] = []
    for peer in peers:
        surfaces = peer_surfaces_by_review_run.get(peer.review_run_id, ())
        for file_path, qualified_name in surfaces:
            if (file_path, qualified_name) not in current_set:
                continue
            overlaps.append(
                CrossPROverlap(
                    peer=peer,
                    overlap_kind=CrossPROverlapKind.SAME_CHANGED_SYMBOL,
                    file_path=file_path,
                    qualified_name=qualified_name,
                )
            )
            if len(overlaps) >= MAX_CROSS_PR_OVERLAPS:
                return tuple(overlaps)
    return tuple(overlaps)


def derive_cross_pr_signals(overlaps: tuple[CrossPROverlap, ...]) -> tuple[CrossPRSignal, ...]:
    """One signal per distinct surface with at least one real overlap.
    Unlike Trajectory Intelligence's churn threshold, a single overlap
    is sufficient -- see :class:`~patchfrog.cross_pr_intelligence.domain.CrossPRSignal`'s
    own docstring for why."""

    by_surface: dict[tuple[str, str], list[CrossPROverlap]] = {}
    for overlap in overlaps:
        by_surface.setdefault((overlap.file_path, overlap.qualified_name), []).append(overlap)

    signals: list[CrossPRSignal] = []
    for (file_path, qualified_name), surface_overlaps in by_surface.items():
        peer_numbers = sorted({o.peer.github_pr_number for o in surface_overlaps})
        signal_kind = CrossPROverlapKind.SAME_CHANGED_SYMBOL
        evidence = (
            f"this exact surface is also changed in "
            f"{'PR' if len(peer_numbers) == 1 else 'PRs'} "
            f"{', '.join(f'#{n}' for n in peer_numbers)} in this same repository"
        )
        signals.append(
            CrossPRSignal(
                surface_file_path=file_path,
                surface_qualified_name=qualified_name,
                signal_kind=signal_kind,
                supporting_overlaps=tuple(surface_overlaps),
                review_hint=_SIGNAL_HINT[signal_kind],
                evidence=evidence,
            )
        )
        if len(signals) >= MAX_CROSS_PR_SIGNALS:
            break

    return tuple(signals)


def select_review_hint(
    signals: tuple[CrossPRSignal, ...], *, file_path: str, qualified_name: str | None
) -> CrossPRReviewHint:
    """The one deterministic hint a real current-PR candidate receives
    -- never an LLM decision. ``qualified_name=None`` never matches
    (module-region candidates carry no cross-PR signal in v1, same
    exclusion as candidate-identity everywhere else in this package)."""

    if qualified_name is None:
        return CrossPRReviewHint.NONE
    for signal in signals:
        if signal.surface_file_path == file_path and signal.surface_qualified_name == qualified_name:
            return signal.review_hint
    return CrossPRReviewHint.NONE
