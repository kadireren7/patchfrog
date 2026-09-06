"""Pure domain model for Cross-Repo Intelligence -- no I/O, no LLM
(mirrors :mod:`patchfrog.cross_pr_intelligence.domain`'s own role).

**Product principle: proven dependency/contract evidence across
repository boundaries, never a repository-discovery engine.** This
package answers "does another *explicitly-linked* repository depend on
a contract the current PR just changed?" -- never "which repositories
look related." No relationship is ever inferred from repository names,
shared organization, shared language, matching directory names,
similar symbols, package-name coincidence, GitHub topics, embeddings,
or README text similarity. See
``validation/cross_repo_intelligence/latest-summary.md`` for the full
audit behind every scope decision below.

**Explicit relationships only, registered by a trusted operator path,
never from `.patchfrog.yml` or any other PR-influenced source.** A
malicious PR must never be able to expand PatchFrog's repository
access scope -- see latest-summary.md section 12 for the confirmed
architectural reason `.patchfrog.yml`-based registration is unsafe
(it is read from the PR's own untrusted head commit).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

#: Bumped whenever peer-eligibility, contract-key matching, or
#: signal/hint-selection logic changes materially enough that a prior
#: report can no longer be considered equivalent to what re-running now
#: would produce.
CROSS_REPO_INTELLIGENCE_VERSION = 1

#: Never scan every relation a repository has ever had -- bounded to
#: the most recently created active relations, applied as a SQL LIMIT.
MAX_CROSS_REPO_PEERS = 4

#: Bounds the total relation-row read in the one bounded query (never a
#: per-peer secondary query -- v1's signal kind needs none, see
#: latest-summary.md section 8).
MAX_CROSS_REPO_CONTRACTS_PER_QUERY = 20

#: Bounds the final signal list per run.
MAX_CROSS_REPO_SIGNALS = 8


class RepositoryRelationKind(StrEnum):
    """Only `EXPLICIT_SHARED_CONTRACT` is ever constructed in v1 -- see
    ``validation/cross_repo_intelligence/latest-summary.md`` sections
    3-4 for why `PACKAGE_DEPENDENCY`/`SUBMODULE` cannot be safely
    supported: PatchFrog parses no package manifests and no submodule
    metadata anywhere today. `API_CONSUMER`/`SCHEMA_CONSUMER` are
    reserved for a future, more granular taxonomy but are not
    distinguished from `EXPLICIT_SHARED_CONTRACT` in v1.
    `MANUAL_EXPLICIT` is reserved for a future relation with no
    contract-key identity at all (not needed by v1's one signal kind,
    which always requires a contract key). All are kept on the enum for
    forward documentation only, exactly mirroring
    `CrossPROverlapKind`'s own deferred members."""

    EXPLICIT_SHARED_CONTRACT = "explicit_shared_contract"
    PACKAGE_DEPENDENCY = "package_dependency"
    SUBMODULE = "submodule"
    API_CONSUMER = "api_consumer"
    SCHEMA_CONSUMER = "schema_consumer"
    MANUAL_EXPLICIT = "manual_explicit"


class RepositoryRelationProvenance(StrEnum):
    """How a :class:`RepositoryRelationKind` row was established.
    **Only `OPERATOR_CLI` is ever written in v1** -- the sole safe,
    trusted-path registration mechanism (see latest-summary.md section
    12). The rest are reserved for future relation kinds that would
    need their own separately-audited trust source; never "inferred",
    never an LLM."""

    OPERATOR_CLI = "operator_cli"
    PACKAGE_MANIFEST_MAPPING = "package_manifest_mapping"
    SUBMODULE = "submodule"
    API_CONTRACT_REGISTRATION = "api_contract_registration"


class CrossRepoSignalKind(StrEnum):
    """Only `CROSS_REPO_CONTRACT_CHANGE` is ever constructed in v1 --
    see latest-summary.md sections 3, 10-11 for why
    `CROSS_REPO_PACKAGE_DEPENDENCY_IMPACT` is deferred (no reliable
    package-to-repository mapping exists)."""

    #: The current PR's own Contract Intelligence delta at a registered
    #: `RepositoryContractKey` matches an active, authorized
    #: `EXPLICIT_SHARED_CONTRACT` relation naming a peer as consumer.
    CROSS_REPO_CONTRACT_CHANGE = "cross_repo_contract_change"
    #: Deferred -- see latest-summary.md section 3/11.
    CROSS_REPO_PACKAGE_DEPENDENCY_IMPACT = "cross_repo_package_dependency_impact"


class CrossRepoReviewHint(StrEnum):
    """Deterministic from the strongest signal on a surface -- never an
    LLM decision. Only `REQUIRE_CRITIC` is ever selected by v1's single
    implemented signal kind; `DEEPEN_CONTEXT`/`INCREASE_CANDIDATE_PRIORITY`
    are reserved for future signal kinds -- mirrors
    `patchfrog.cross_pr_intelligence.domain.CrossPRReviewHint` exactly."""

    NONE = "none"
    DEEPEN_CONTEXT = "deepen_context"
    REQUIRE_CRITIC = "require_critic"
    INCREASE_CANDIDATE_PRIORITY = "increase_candidate_priority"


@dataclass(frozen=True, slots=True)
class CrossRepoPeer:
    """Another, explicitly-linked repository, authorized under the same
    GitHub App installation as the current repository. ``full_name`` is
    legitimate, non-sensitive-within-the-installation's-own-scope
    evidence (an operator who registered this relation already knows
    both repository names) used only to build bounded per-candidate
    prompt evidence text -- never persisted to telemetry (see
    :mod:`patchfrog.cross_repo_intelligence.telemetry`).

    Deliberately carries **no** peer-review-state fields (no latest
    reviewed head, no review run id): v1's only signal kind needs none
    -- the relation registration itself is the proof of dependency, not
    a live re-derivation from the peer's current source (see
    latest-summary.md section 8). A future signal kind that needs live
    peer structural comparison would need new cross-repository indexing
    infrastructure this milestone deliberately does not introduce."""

    repository_id: UUID
    full_name: str
    relation_kind: RepositoryRelationKind
    contract_key: str


@dataclass(frozen=True, slots=True)
class CrossRepoOverlap:
    """One structural fact: the current PR's contract change at this
    exact surface is registered under a contract key an authorized peer
    explicitly consumes."""

    peer: CrossRepoPeer
    signal_kind: CrossRepoSignalKind
    file_path: str
    qualified_name: str


@dataclass(frozen=True, slots=True)
class CrossRepoSignal:
    """One deterministic pattern detected across a surface's own
    supporting overlaps. Never a numeric risk probability -- only a
    signal kind, the overlaps that produced it, and the one
    deterministic orchestration hint it selects.

    A single real overlap is sufficient (mirrors
    :class:`patchfrog.cross_pr_intelligence.domain.CrossPRSignal`'s own
    reasoning) -- an explicit, operator-registered cross-repository
    dependency is never noise at N=1."""

    surface_file_path: str
    surface_qualified_name: str
    signal_kind: CrossRepoSignalKind
    supporting_overlaps: tuple[CrossRepoOverlap, ...]
    review_hint: CrossRepoReviewHint
    evidence: str

    @property
    def distinct_peer_count(self) -> int:
        return len({o.peer.repository_id for o in self.supporting_overlaps})


@dataclass(frozen=True, slots=True)
class CrossRepoIntelligenceReport:
    """The complete, deterministic output for one review run. Never
    itself sent to an LLM in bulk -- only small, bounded per-candidate
    slices are (see
    :func:`patchfrog.cross_repo_intelligence.evidence.evidence_text_for_candidate`).
    Never rendered as a standalone user-facing section -- no
    ``story``/``summary`` field at all, mirroring Trajectory/Cross-PR
    Intelligence's own precedent."""

    version: int
    peers_considered: tuple[CrossRepoPeer, ...]
    overlaps: tuple[CrossRepoOverlap, ...]
    signals: tuple[CrossRepoSignal, ...]

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
    def contract_change_count(self) -> int:
        return sum(1 for s in self.signals if s.signal_kind is CrossRepoSignalKind.CROSS_REPO_CONTRACT_CHANGE)

    @property
    def require_critic_count(self) -> int:
        return sum(1 for s in self.signals if s.review_hint is CrossRepoReviewHint.REQUIRE_CRITIC)

    @property
    def deepen_context_count(self) -> int:
        return sum(1 for s in self.signals if s.review_hint is CrossRepoReviewHint.DEEPEN_CONTEXT)
