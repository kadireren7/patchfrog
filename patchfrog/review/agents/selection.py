"""Deterministic specialist-agent selection.

Which specialist roles run for a given candidate is a decision PatchFrog
makes structurally -- never an LLM "router" deciding which LLM to call
(that would just be another unbounded, non-deterministic provider call).
:class:`AgentSelectionPolicy` is a pure function of already-available
structural data: the candidate itself and the static findings already
attached to it.

v1 honesty note: there is not yet enough reliable structural signal to
confidently *skip* the Security agent for an arbitrary candidate, so the
conservative fallback still selects it. What this policy buys is: (a) an
explicit, auditable *reason* for every selection decision (never "it just
always runs"), and (b) a single seam future milestones can tighten
without touching the orchestrator itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import ReviewCandidate, StaticFindingSummary

#: Substrings (checked case-insensitively against the candidate's file
#: path, qualified name, and symbol name) that deterministically suggest
#: security relevance -- authentication/authorization, secrets, unsafe
#: input/process/network sinks, and common trust-boundary keywords. This
#: is a coarse, explainable heuristic, not a classifier -- see the module
#: docstring.
_SECURITY_SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "auth", "login", "logout", "session", "token", "password", "passwd",
    "secret", "credential", "permission", "acl", "role", "privilege",
    "crypto", "cipher", "hash", "sign", "verify", "sanitiz", "escape",
    "sql", "query", "shell", "exec", "eval", "subprocess", "command",
    "deserialize", "pickle", "template", "render", "upload", "download",
    "url", "http", "request", "cors", "csrf", "xss", "injection",
    "path", "filename", "file_path", "webhook", "signature",
)


class AgentSelectionReason(StrEnum):
    """Why a role was selected for a candidate -- always persisted-worthy
    audit information, never just "because"."""

    #: Correctness is the broad default specialist for every candidate.
    DEFAULT = "default"
    #: A static finding already attached to this candidate is in the
    #: security category -- concrete corroboration that this location
    #: warrants a security-specialist pass.
    STATIC_SECURITY_CORROBORATION = "static_security_corroboration"
    #: The candidate's file/symbol name matches a security-sensitive
    #: naming heuristic (see ``_SECURITY_SENSITIVE_KEYWORDS``).
    SECURITY_SENSITIVE_NAMING = "security_sensitive_naming"
    #: No reliable signal either way -- v1 conservative fallback: still
    #: run the security specialist rather than risk a false negative.
    CONSERVATIVE_FALLBACK = "conservative_fallback"


@dataclass(frozen=True, slots=True)
class AgentSelectionDecision:
    role: AgentRole
    reason: AgentSelectionReason


class AgentSelectionPolicy:
    """Deterministic, side-effect-free role selection. See module
    docstring for why this always currently selects both roles in
    practice, and why that's still worth encapsulating here."""

    def select(
        self, candidate: ReviewCandidate, *, static_findings: tuple[StaticFindingSummary, ...]
    ) -> tuple[AgentSelectionDecision, ...]:
        decisions = [AgentSelectionDecision(role=AgentRole.CORRECTNESS, reason=AgentSelectionReason.DEFAULT)]

        if any(f.category.value == "security" for f in static_findings):
            decisions.append(
                AgentSelectionDecision(
                    role=AgentRole.SECURITY, reason=AgentSelectionReason.STATIC_SECURITY_CORROBORATION
                )
            )
            return tuple(decisions)

        haystack = " ".join(
            filter(None, (candidate.file_path, candidate.qualified_name, candidate.symbol_name))
        ).lower()
        if any(keyword in haystack for keyword in _SECURITY_SENSITIVE_KEYWORDS):
            decisions.append(
                AgentSelectionDecision(
                    role=AgentRole.SECURITY, reason=AgentSelectionReason.SECURITY_SENSITIVE_NAMING
                )
            )
            return tuple(decisions)

        decisions.append(
            AgentSelectionDecision(role=AgentRole.SECURITY, reason=AgentSelectionReason.CONSERVATIVE_FALLBACK)
        )
        return tuple(decisions)
