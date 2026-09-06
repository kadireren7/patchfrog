"""Deterministic derivation of
:class:`~patchfrog.cross_repo_intelligence.domain.CrossRepoSignal`\\ s
from already-fetched overlaps
(:mod:`patchfrog.cross_repo_intelligence.queries`) -- pure, synchronous.
"""

from __future__ import annotations

from patchfrog.cross_repo_intelligence.domain import (
    MAX_CROSS_REPO_SIGNALS,
    CrossRepoOverlap,
    CrossRepoReviewHint,
    CrossRepoSignal,
    CrossRepoSignalKind,
)

#: The only mapping any v1 signal kind ever uses -- see the domain
#: module's own docstring for why `DEEPEN_CONTEXT`/
#: `INCREASE_CANDIDATE_PRIORITY` are reserved, never selected here.
_SIGNAL_HINT: dict[CrossRepoSignalKind, CrossRepoReviewHint] = {
    CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE: CrossRepoReviewHint.REQUIRE_CRITIC,
}


def derive_cross_repo_signals(overlaps: tuple[CrossRepoOverlap, ...]) -> tuple[CrossRepoSignal, ...]:
    """One signal per distinct surface with at least one real overlap.
    A single, explicit, operator-registered cross-repository dependency
    is sufficient -- see
    :class:`~patchfrog.cross_repo_intelligence.domain.CrossRepoSignal`'s
    own docstring for why."""

    by_surface: dict[tuple[str, str], list[CrossRepoOverlap]] = {}
    for overlap in overlaps:
        by_surface.setdefault((overlap.file_path, overlap.qualified_name), []).append(overlap)

    signals: list[CrossRepoSignal] = []
    for (file_path, qualified_name), surface_overlaps in by_surface.items():
        peer_names = sorted({o.peer.full_name for o in surface_overlaps})
        signal_kind = CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE
        contract_keys = sorted({o.peer.contract_key for o in surface_overlaps})
        evidence = (
            f"this exact contract ({', '.join(contract_keys)}) is explicitly registered as consumed by "
            f"{'repository' if len(peer_names) == 1 else 'repositories'} {', '.join(peer_names)}"
        )
        signals.append(
            CrossRepoSignal(
                surface_file_path=file_path, surface_qualified_name=qualified_name, signal_kind=signal_kind,
                supporting_overlaps=tuple(surface_overlaps), review_hint=_SIGNAL_HINT[signal_kind],
                evidence=evidence,
            )
        )
        if len(signals) >= MAX_CROSS_REPO_SIGNALS:
            break

    return tuple(signals)


def select_review_hint(
    signals: tuple[CrossRepoSignal, ...], *, file_path: str, qualified_name: str | None
) -> CrossRepoReviewHint:
    """The one deterministic hint a real current-PR candidate receives
    -- never an LLM decision. ``qualified_name=None`` never matches
    (module-region candidates carry no cross-repo signal in v1)."""

    if qualified_name is None:
        return CrossRepoReviewHint.NONE
    for signal in signals:
        if signal.surface_file_path == file_path and signal.surface_qualified_name == qualified_name:
            return signal.review_hint
    return CrossRepoReviewHint.NONE
