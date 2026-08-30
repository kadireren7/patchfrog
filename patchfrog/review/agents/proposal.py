"""Role-aware proposal state -- the shared currency specialist agents,
deterministic validation, cross-role dedup, and the critic all operate
on.

Replaces the pre-orchestration assumption of "one reviewer response per
candidate" with an explicit, typed, role-attributed proposal. Every
:class:`AgentProposal` traces back to exactly one
:class:`~patchfrog.review.agents.roles.AgentRole` and carries its own
deterministic validation outcome (see :mod:`patchfrog.review.validation`)
-- validation still runs independently per proposal, never shared or
skipped because a sibling proposal from the other role already passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.domain import CriticVerdict, TokenUsage, ValidatedFinding


@dataclass(frozen=True, slots=True)
class AgentProposal:
    """One specialist agent's proposal for one candidate, after
    deterministic validation and (if selected) critic review.

    ``suppressed_reason`` is set by cross-role dedup/contradiction
    handling (see :mod:`patchfrog.review.agents.cross_role`) -- ``None``
    means "not suppressed by cross-role logic" (it may still be rejected
    by validation, the critic, or confidence thresholding downstream;
    those are recorded on ``validated``/``critic_verdict`` respectively,
    not here).
    """

    role: AgentRole
    validated: ValidatedFinding
    critic_verdict: CriticVerdict | None = None
    reviewer_usage: TokenUsage = field(default_factory=TokenUsage)
    critic_usage: TokenUsage = field(default_factory=TokenUsage)
    suppressed_reason: str | None = None

    def with_critic_verdict(self, verdict: CriticVerdict | None, *, usage: TokenUsage) -> AgentProposal:
        return AgentProposal(
            role=self.role,
            validated=self.validated,
            critic_verdict=verdict,
            reviewer_usage=self.reviewer_usage,
            critic_usage=self.critic_usage + usage,
            suppressed_reason=self.suppressed_reason,
        )

    def suppressed(self, reason: str) -> AgentProposal:
        return AgentProposal(
            role=self.role,
            validated=self.validated,
            critic_verdict=self.critic_verdict,
            reviewer_usage=self.reviewer_usage,
            critic_usage=self.critic_usage,
            suppressed_reason=reason,
        )
