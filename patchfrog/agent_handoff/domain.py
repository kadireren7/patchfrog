"""Domain model for the Finding Handoff -- Milestone T (T1).

``FindingHandoff`` is the one, stable, bounded shape a coding agent ever
receives for one PatchFrog finding. It is a deterministic *projection* of
already-persisted state (:mod:`patchfrog.persistence.models.review`) --
never re-scored, never re-judged, never produced by a new provider call
(see :mod:`patchfrog.agent_handoff.service`).

**Deliberately excluded** (see ``validation/agent_handoff/latest-summary.md``
section 1.2/1.3 for the full audit reasoning): per-finding Contract/Intent/
Test/Historical/Trajectory/Cross-PR/Cross-Repo evidence (none of the
Intelligence layers persist evidence attributable to one specific finding
today -- only whole-review-run aggregate summaries exist), raw provider
confidence internals, token budgets, critic call counts, private prompt
content, any credential. ``executable_verification_evidence`` is kept as a
real, typed field rather than omitted -- it is honestly ``None`` for every
v1 finding (Executable Verification evidence is never durably linked to a
specific finding either), which keeps the wire shape stable for a future
milestone that does persist it, without ever fabricating a value today.

Current fix-attempt lifecycle state is deliberately **not** a field on this
type -- it lives in :mod:`patchfrog.fix_verification`, which itself depends
on this package (to resolve a handoff back to the finding it targets).
Making lifecycle state part of ``FindingHandoff`` would require the reverse
dependency and create an import cycle; :mod:`patchfrog.mcp`'s
``get_finding_handoff`` tool composes both independently instead.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity

#: A real, externally-consumed wire contract -- an MCP client parses and
#: may persist this shape outside PatchFrog's own process. Bump only for
#: an incompatible field add/remove/reinterpret, exactly like
#: VERIFIER_PROTOCOL_VERSION's own rule (never for a cosmetic/internal
#: change).
FINDING_HANDOFF_SCHEMA_VERSION = 1

#: Bounded, mirrors the sandbox's own excerpt-bounding philosophy
#: (patchfrog.executable_verification.domain) -- a handoff is evidence for
#: an agent to act on, never a log/context dump (spec Part R).
MAX_HANDOFF_EVIDENCE_ITEMS = 5
MAX_HANDOFF_TEXT_BYTES = 4000


@dataclass(frozen=True, slots=True)
class HandoffEvidenceSnippet:
    """One verbatim, already-validated evidence quote -- mirrors
    :class:`patchfrog.review.domain.ReviewEvidence` exactly (the same
    checked-against-real-source text a finding survived validation with),
    never a fresh excerpt pulled for the handoff."""

    file_path: str
    start_line: int
    end_line: int
    quoted_text: str


@dataclass(frozen=True, slots=True)
class FindingHandoff:
    """Bounded, structured, exact-head-bound evidence for one PatchFrog
    finding -- the only shape :mod:`patchfrog.mcp` ever hands a coding
    agent for a finding."""

    handoff_id: str
    schema_version: int

    repository_id: UUID
    repository_full_name: str
    review_run_id: UUID
    #: The commit this finding was identified on. Never mutated to a
    #: later head -- see :func:`compute_handoff_id` and Part G ("a
    #: handoff belongs to original_commit_sha").
    original_commit_sha: str

    finding_id: UUID
    category: FindingCategory
    severity: Severity
    confidence: Confidence

    title: str
    #: Identification -- the exact problematic condition.
    message: str
    #: The technical mechanism/root cause.
    reasoning_summary: str
    #: Realistic, code-grounded consequence -- nullable, never fabricated.
    impact: str | None
    #: An actionable remediation direction -- nullable, never fabricated.
    suggested_fix: str | None

    file_path: str
    start_line: int
    end_line: int
    qualified_name: str | None

    evidence: tuple[HandoffEvidenceSnippet, ...]

    #: A real, per-finding link to specific static-analysis findings
    #: (patchfrog.analysis.domain) -- unlike the LLM Intelligence layers,
    #: static findings are individually addressable by id.
    corroborated_by_static: bool
    static_finding_ids: tuple[UUID, ...]

    #: The critic's own reasoning_summary, when a critic verdict exists --
    #: never the critic's raw prompt or full verdict internals.
    critic_reasoning_summary: str | None

    #: Deterministically re-derived (never assumed from the original
    #: review) via patchfrog.executable_verification.eligibility --
    #: the test file path a fix attempt would be verified against, or
    #: None if this finding has no discoverable test-file relationship.
    suggested_verification_target: str | None

    #: See the module docstring -- always None in v1, kept as a typed
    #: field for wire-shape stability, never fabricated.
    executable_verification_evidence: str | None

    #: Whether this finding has already been published to the PR as a
    #: real GitHub review comment -- a small, directly useful bit of
    #: provenance (Part C: "publication reference if useful").
    published: bool
    github_review_id: int | None


def compute_handoff_id(
    *, repository_id: UUID, finding_id: UUID, review_run_id: UUID, original_commit_sha: str
) -> str:
    """Deterministic, stable handoff identity (Part F). Binds repository +
    finding + originating review run + exact commit -- two different
    findings can never collide, and this is never derived from mutable
    prose (title/message can be re-rendered without changing identity is
    not a goal here; the point is that the same finding always produces
    the same id, and no unrelated finding ever can)."""

    raw = "|".join((str(repository_id), str(finding_id), str(review_run_id), original_commit_sha))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def bound_text(text: str, *, max_bytes: int = MAX_HANDOFF_TEXT_BYTES) -> str:
    """Bounds one free-text field to ``max_bytes`` UTF-8 bytes, truncating
    on a safe boundary rather than raising -- a handoff must never fail to
    build because one field ran long (Part R: bounded, never unbounded)."""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "...(truncated)"
