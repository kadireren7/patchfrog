"""Model Router domain types -- U3/U4.

:class:`ReviewRoutePlan` is the router's one output: which real
:class:`~patchfrog.review.provider.LLMProvider` instance serves each
:class:`~patchfrog.review.agents.roles.AgentRole`, and which (if any)
serves the critic -- plus a bounded, typed rationale
(:class:`RouteReason`), never freeform prose. It plugs directly into
:class:`patchfrog.review.orchestration.AgentOrchestrator`'s existing
``reviewer_providers: Mapping[AgentRole, LLMProvider]`` constructor
parameter and :class:`patchfrog.review.critic.CriticService`'s existing
``provider`` parameter -- no orchestration change, see
``validation/model_router_merge_readiness/latest-summary.md`` section 1.

**The router decides HOW existing review work is executed, never
WHETHER a finding is true** -- see :mod:`patchfrog.routing.router`'s own
module docstring for the full governing rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.provider import LLMProvider

#: Bumped only when this module's own durable semantic contract changes
#: materially (the shape/meaning of ReviewRoutePlan or RouteReason as
#: persisted/observed externally) -- never mechanically. See the final
#: report for this milestone's own bump/non-bump justification.
MODEL_ROUTER_VERSION = 1


class RouteReason(StrEnum):
    """Bounded, typed rationale for one routing decision -- never
    freeform prose, so a route plan can be asserted on in tests and
    logged as low-cardinality telemetry without risk of leaking
    unbounded text."""

    #: Exactly one provider has credentials configured -- diversity is
    #: structurally unavailable, not merely unused.
    SINGLE_PROVIDER_CONFIGURED = "single_provider_configured"
    #: The operator-preferred provider (``PATCHFROG_REVIEW_PROVIDER``)
    #: has credentials and is used for the reviewer role.
    PREFERRED_PROVIDER_AVAILABLE = "preferred_provider_available"
    #: The preferred provider had no credential configured, but an
    #: explicit, operator-configured fallback
    #: (``PATCHFROG_ROUTER_FALLBACK_PROVIDER``) did -- used instead.
    PREFERRED_PROVIDER_UNAVAILABLE_FALLBACK_USED = "preferred_provider_unavailable_fallback_used"
    #: The critic ran on a different provider family than the reviewer
    #: role(s) -- either explicitly configured
    #: (``PATCHFROG_ROUTER_CRITIC_PROVIDER``) or auto-selected because a
    #: second credentialed, structured-output-capable provider exists.
    CRITIC_FAMILY_DIVERSITY_USED = "critic_family_diversity_used"
    #: The critic ran on the same provider family as the reviewer --
    #: either because no other provider is configured, or because the
    #: operator explicitly pinned the critic to the reviewer's own
    #: family.
    CRITIC_SAME_FAMILY_NO_ALTERNATIVE = "critic_same_family_no_alternative"
    #: ``critic_enabled=False`` (repository/operator review-behavior
    #: config, see :mod:`patchfrog.review.config`) -- no critic route
    #: was computed at all.
    CRITIC_DISABLED_BY_CONFIG = "critic_disabled_by_config"


@dataclass(frozen=True, slots=True)
class ReviewRoutePlan:
    """The router's complete, bounded decision for one review run.
    Provider selection is decided once per run (never per candidate) --
    see the audit's own architectural-constraint note for why.

    **Two distinct fallback concepts, deliberately not conflated**
    (Milestone U runtime-failover correction):

    1. **Configuration-time provider-selection fallback**
       (``config_fallback_used``): the *preferred* provider
       (``PATCHFROG_REVIEW_PROVIDER``) had no credential configured at
       all, so a different provider was *selected* as primary before any
       call was ever made. Decided once, here, by
       :meth:`~patchfrog.routing.router.ModelRouter.route`.
    2. **Runtime execution failover**
       (``reviewer_fallback_providers``/``critic_fallback_provider``,
       gated by ``runtime_fallback_permitted``): the *selected* primary
       provider is credentialed and was used, but an actual bounded
       review call to it failed (after its own existing retry
       allowance) or returned a schema-invalid response -- see
       :meth:`patchfrog.review.orchestration.AgentOrchestrator._call_role`/
       ``_critique_one`` for exactly when this fires. Reuses the same
       operator-configured ``PATCHFROG_ROUTER_FALLBACK_PROVIDER`` as
       (1) -- one operator-configured backup provider, two distinct
       trigger points, never a second config surface. Bounded to
       exactly one runtime hop; never a chain, never back to the
       primary.
    """

    reviewer_providers: Mapping[AgentRole, LLMProvider]
    critic_provider: LLMProvider | None
    reviewer_provider_family: str
    critic_provider_family: str | None
    reasons: tuple[RouteReason, ...]
    #: True iff more than one credentialed, structured-output-capable
    #: provider was configured -- independent of whether diversity was
    #: actually *used* (an operator may pin the critic to the same
    #: family as the reviewer even when an alternative exists).
    diversity_available: bool
    diversity_used: bool
    #: Configuration-time fallback only -- see class docstring point 1.
    config_fallback_used: bool
    #: Runtime execution failover -- see class docstring point 2. Both
    #: ``None`` when no distinct, credentialed backup provider exists
    #: (single-provider deployments always have both ``None``).
    reviewer_fallback_providers: Mapping[AgentRole, LLMProvider] | None = None
    reviewer_runtime_fallback_family: str | None = None
    critic_fallback_provider: LLMProvider | None = None
    critic_runtime_fallback_family: str | None = None
    #: True iff at least one of the two runtime-fallback fields above is
    #: populated -- the single flag a caller checks before assuming
    #: runtime failover is possible at all this run.
    runtime_fallback_permitted: bool = False
    version: int = MODEL_ROUTER_VERSION
