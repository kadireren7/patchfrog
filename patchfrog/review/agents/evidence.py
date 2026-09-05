"""The shared evidence package every specialist agent reviews from.

Built exactly once per candidate (see
:meth:`patchfrog.review.orchestration.AgentOrchestrator.review_candidate`),
using the existing Context Engine exactly as-is -- no per-agent context
rebuild, no adaptive multi-hop (that is a later milestone; see
``docs/agent-orchestration.md``). Every specialist agent for one
candidate receives this same, already-redacted package, which is what
makes agent outputs comparable, reproducible, and cheap: one context
build serves every role, not one per role.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from patchfrog.review.domain import ReviewCandidate, StaticFindingSummary


@dataclass(frozen=True, slots=True)
class CandidateEvidencePackage:
    """Everything a specialist agent is shown for one candidate --
    already redacted, already scoped. ``allowed_file_paths`` is the exact
    set deterministic validation checks every agent's claimed
    ``file_path``/evidence locations against (see
    :mod:`patchfrog.review.validation`), so an agent can never cite a
    file it wasn't shown regardless of which role produced the claim."""

    candidate: ReviewCandidate
    context_text: str
    diff_excerpt: str
    static_findings: tuple[StaticFindingSummary, ...]
    allowed_file_paths: frozenset[str]
    context_bundle_id: UUID | None
    #: Bounded (see :mod:`patchfrog.change_intelligence.evidence`) evidence
    #: from the Change Intelligence Foundation -- empty string when this
    #: candidate has no missing-companion evidence tied to it. Already
    #: small enough (at most a few lines, one candidate's own companions
    #: only) to apply uniformly across effort tiers without threatening
    #: the Quality + Cost Guard's LIGHT-tier budget.
    change_intelligence_text: str = ""
    #: Bounded (see :mod:`patchfrog.contract_intelligence.evidence`)
    #: evidence from Contract & Blast Radius Intelligence -- empty
    #: string unless this exact candidate is the source of a real,
    #: structurally-detected contract delta. Same size discipline as
    #: ``change_intelligence_text`` above.
    contract_intelligence_text: str = ""
    #: Bounded (see :mod:`patchfrog.intent_verification.evidence`)
    #: evidence from Intent Verification -- empty string unless this
    #: exact candidate is part of a ChangeUnit mapped to a real,
    #: sufficiently-explicit intent claim. Same size discipline as
    #: ``change_intelligence_text``/``contract_intelligence_text`` above.
    intent_verification_text: str = ""
    #: Bounded (see :mod:`patchfrog.test_intelligence.evidence`) evidence
    #: from Test Intelligence -- empty string unless this exact candidate
    #: is the source file of a real, structurally-detected test gap. Same
    #: size discipline as ``change_intelligence_text``/
    #: ``contract_intelligence_text``/``intent_verification_text`` above.
    test_intelligence_text: str = ""
    #: Bounded (see :mod:`patchfrog.historical_regression_memory.evidence`)
    #: evidence from Historical Regression Memory -- empty string unless
    #: this exact candidate matches a real, trusted historical finding.
    #: Same size discipline as ``change_intelligence_text``/
    #: ``contract_intelligence_text``/``intent_verification_text``/
    #: ``test_intelligence_text`` above.
    historical_regression_text: str = ""
