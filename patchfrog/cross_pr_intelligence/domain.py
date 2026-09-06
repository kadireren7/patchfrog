"""Pure domain model for Cross-PR Intelligence -- no I/O, no LLM
(mirrors :mod:`patchfrog.trajectory_intelligence.domain`'s own role).

**Product principle: cross-PR overlap is never a finding.** This
package answers "is another PR in this same repository concurrently
touching the exact same surface?" -- never "whose change is wrong."
Overlap evidence may only ever *strengthen orchestration* (require
critic, reorder candidates) for a candidate that already exists; it is
never published as a standalone warning, never a developer-ranking or
blame signal. See ``validation/cross_pr_intelligence/latest-summary.md``
for the full audit behind every scope decision below.

Same-repository only (hard filter -- see
:mod:`patchfrog.cross_pr_intelligence.queries`). No GitHub API crawler,
no polling loop: every peer's eligibility and structural surfaces are
read from already-ingested webhook state and already-persisted review
history (audit sections 1-5).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

#: Bumped whenever peer-eligibility, overlap-detection, or
#: signal/hint-selection logic changes materially enough that a prior
#: report can no longer be considered equivalent to what re-running now
#: would produce. Independent of every other Intelligence package's own
#: version (this package only *reads* Phase 5/Phase 7's own already
#: -persisted rows for other PRs; it never reinterprets their rules).
CROSS_PR_INTELLIGENCE_VERSION = 1

#: Never scan every PR a repository has ever seen -- bounded to the
#: most recently active open peers (ordered by
#: ``PullRequestModel.updated_at`` descending) before any structural
#: comparison happens.
MAX_CROSS_PR_PEERS = 10

#: Bounds the changed-surface set read per peer's one latest reviewed
#: head -- never an unbounded per-peer candidate scan.
MAX_CROSS_PR_SURFACES_PER_PEER = 50

#: Bounds the total overlap list across every peer combined.
MAX_CROSS_PR_OVERLAPS = 20

#: Bounds the final signal list per run.
MAX_CROSS_PR_SIGNALS = 10


class CrossPROverlapKind(StrEnum):
    """Only `SAME_CHANGED_SYMBOL` is ever constructed in v1 -- see
    ``validation/cross_pr_intelligence/latest-summary.md`` section 6
    for why `SHARED_AFFECTED_SURFACE`/`SHARED_CONTRACT_SURFACE`/
    `CONTRACT_PRODUCER_CONSUMER_COLLISION` cannot be safely
    reconstructed: Change/Contract Intelligence's own affected-surface
    and contract-delta outputs are computed in-memory only, once per
    run, and never persisted -- there is no historical row to compare
    a peer's past review against. All three are kept on the enum for
    forward documentation only, exactly mirroring Milestone N's own
    never-constructed ``HistoricalMatchKind.SAME_FILE``."""

    #: Both the current PR and a peer's latest reviewed head directly
    #: change the exact same `(file_path, qualified_name)` symbol (a
    #: `ReviewCandidateModel` with `reason == CHANGED_SYMBOL` on both
    #: sides). Same-file-different-symbol never matches.
    SAME_CHANGED_SYMBOL = "same_changed_symbol"
    #: Deferred -- see audit section 6.
    SHARED_AFFECTED_SURFACE = "shared_affected_surface"
    #: Deferred -- see audit section 6.
    SHARED_CONTRACT_SURFACE = "shared_contract_surface"
    #: Deferred -- see audit section 6.
    CONTRACT_PRODUCER_CONSUMER_COLLISION = "contract_producer_consumer_collision"


class CrossPRReviewHint(StrEnum):
    """Deterministic from the strongest signal on a surface -- never an
    LLM decision. Only `REQUIRE_CRITIC` is ever selected by v1's single
    implemented overlap kind; `DEEPEN_CONTEXT`/`INCREASE_CANDIDATE_PRIORITY`
    are reserved for future overlap kinds and never selected today --
    mirrors :class:`patchfrog.trajectory_intelligence.domain.TrajectoryReviewHint`
    exactly. A candidate whose surface selects `REQUIRE_CRITIC` is also
    deterministically reordered to the front of the candidate list, as
    part of that same hint, not a second, independently-selected
    value."""

    NONE = "none"
    DEEPEN_CONTEXT = "deepen_context"
    REQUIRE_CRITIC = "require_critic"
    INCREASE_CANDIDATE_PRIORITY = "increase_candidate_priority"


@dataclass(frozen=True, slots=True)
class CrossPRPeer:
    """Another PR in the same repository with a valid, exact,
    non-stale latest reviewed head -- the only peers this package ever
    considers. ``github_pr_number`` is legitimate, non-sensitive
    -within-the-repository's-own-org evidence (the repository's own
    contributors already see every PR number in GitHub's UI) used only
    to build bounded per-candidate prompt evidence text -- never
    persisted to telemetry (see
    :mod:`patchfrog.cross_pr_intelligence.telemetry`)."""

    pull_request_id: UUID
    github_pr_number: int
    head_commit_sha: str
    review_run_id: UUID
    sequence_number: int


@dataclass(frozen=True, slots=True)
class CrossPROverlap:
    """One structural fact: this exact surface is also changed in a
    peer's reviewed head. Never raw source, never a full diff -- only
    the bounded identity needed to detect a pattern."""

    peer: CrossPRPeer
    overlap_kind: CrossPROverlapKind
    file_path: str
    qualified_name: str


@dataclass(frozen=True, slots=True)
class CrossPRSignal:
    """One deterministic pattern detected across a surface's own
    supporting overlaps. Never a numeric risk probability -- only a
    signal kind, the overlaps that produced it, and the one
    deterministic orchestration hint it selects.

    Unlike Trajectory Intelligence's `REPEATED_SURFACE_CHURN` (which
    requires >= `MIN_SURFACE_CHURN_EVENTS` distinct heads before a
    signal exists, since repeated single-PR edits are common enough at
    low counts to be noise), **a single real `SAME_CHANGED_SYMBOL`
    overlap is sufficient** -- two different PRs directly touching the
    exact same symbol concurrently is not noise at N=1; it is precisely
    the concurrent-change evidence this package exists to surface."""

    surface_file_path: str
    surface_qualified_name: str
    signal_kind: CrossPROverlapKind
    supporting_overlaps: tuple[CrossPROverlap, ...]
    review_hint: CrossPRReviewHint
    evidence: str

    @property
    def distinct_peer_count(self) -> int:
        return len({o.peer.pull_request_id for o in self.supporting_overlaps})


@dataclass(frozen=True, slots=True)
class CrossPRIntelligenceReport:
    """The complete, deterministic output for one review run. Never
    itself sent to an LLM in bulk -- only small, bounded per-candidate
    slices are (see
    :func:`patchfrog.cross_pr_intelligence.evidence.evidence_text_for_candidate`).
    Never rendered as a standalone user-facing section -- no
    ``story``/``summary`` field at all, mirroring Trajectory
    Intelligence's own precedent."""

    version: int
    peers_considered: tuple[CrossPRPeer, ...]
    overlaps: tuple[CrossPROverlap, ...]
    signals: tuple[CrossPRSignal, ...]

    @property
    def peer_count(self) -> int:
        return len(self.peers_considered)

    @property
    def overlap_count(self) -> int:
        return len(self.overlaps)

    @property
    def signal_count(self) -> int:
        return len(self.signals)

    @property
    def same_changed_symbol_count(self) -> int:
        return sum(1 for s in self.signals if s.signal_kind is CrossPROverlapKind.SAME_CHANGED_SYMBOL)

    @property
    def require_critic_count(self) -> int:
        return sum(1 for s in self.signals if s.review_hint is CrossPRReviewHint.REQUIRE_CRITIC)

    @property
    def deepen_context_count(self) -> int:
        return sum(1 for s in self.signals if s.review_hint is CrossPRReviewHint.DEEPEN_CONTEXT)
