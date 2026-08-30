"""Typed specialist agent identity.

An :class:`AgentRole` is a fixed, structural property of every AI-generated
proposal -- attached at the moment a proposal is created, never inferred
later by parsing prompt text or model output. This is what lets
persistence, dedup, evaluation, and future telemetry all attribute a
proposal to the specialist that produced it without ambiguity.

v1 is deliberately narrow (see ``docs/agent-orchestration.md``): only two
roles. Do not add a role here without also updating
:mod:`patchfrog.review.agents.selection`, the role-specific prompts in
:mod:`patchfrog.review.prompt`, and the version bumps in
:mod:`patchfrog.review.config`.
"""

from __future__ import annotations

from enum import StrEnum


class AgentRole(StrEnum):
    """A specialist reviewer role. The critic/verifier is deliberately
    NOT a member of this enum -- it is a distinct second-stage check on
    an agent's proposal, not a peer specialist producing its own
    proposals (see :class:`patchfrog.review.critic.CriticService`)."""

    CORRECTNESS = "correctness"
    SECURITY = "security"
