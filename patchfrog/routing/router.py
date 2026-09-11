"""Model Router -- U3 (deterministic routing) / U4 (reviewer/critic
model-family diversity).

**Governing rule**: the Model Router decides *how* an already-scheduled
piece of review work is executed (which credentialed, structured-output-
capable provider serves the reviewer role(s) and the critic) -- it never
decides *whether* a finding is true, never creates a second review
engine, and never reads repository content to make that decision (see
Part J of this milestone's spec: routing must never be influenced by a
comment in the diff, a `.patchfrog.yml` field, or any other
repository-controlled input -- see
:data:`patchfrog.review.config.OPERATOR_ONLY_REVIEW_FIELDS`, extended by
nothing here since no new field name is needed). Every input to
:meth:`ModelRouter.route` is operator/deployment-controlled
(:class:`~patchfrog.config.settings.Settings`) or an already-computed,
run-level deterministic signal -- never an LLM's own opinion about which
model should review it.

Routes **once per review run**, before the per-candidate loop begins --
not once per candidate. See
``validation/model_router_merge_readiness/latest-summary.md`` section 1
for exactly why: :class:`~patchfrog.review.orchestration.AgentOrchestrator`
is itself constructed once per run, and the existing
:class:`~patchfrog.review.config.ReviewModelIdentity` provenance record
persisted onto ``ReviewRunModel`` is a run-level record. Varying provider
per candidate would need a materially larger, separate change (moving
orchestrator construction inside the per-candidate loop, plus a schema
change for per-finding provider provenance) -- an honest v1 scope
boundary, not a silent gap.

**The zero-LLM path** (Part T of the spec) is **not** decided here: an
empty candidate set means the caller (:mod:`patchfrog.review.service`)
never calls :meth:`ModelRouter.route` at all -- there is nothing to
route. This module has no "should I call an LLM" branch of its own to
avoid duplicating that upstream, already-existing decision.

**Two distinct fallback concepts** (Milestone U runtime-failover
correction -- see :class:`~patchfrog.routing.domain.ReviewRoutePlan`'s
own docstring for the full distinction):

1. **Configuration-time provider-selection fallback**: the preferred
   provider has no credential configured *at all* -> a different,
   credentialed provider is *selected* as primary before any call is
   ever made. Bounded to exactly one hop: preferred provider -> (if
   unavailable) one explicitly configured fallback provider -> (if
   that's also unavailable) a clear, actionable error, reusing
   :class:`~patchfrog.review.provider_factory.MissingProviderCredentialsError`
   rather than inventing a second "no provider" exception type callers
   would need to handle separately.
2. **Runtime execution failover**: the *selected* primary provider is
   credentialed and was used, but an actual bounded review call to it
   failed. This router only ever *computes* which provider is eligible
   to serve as that one-hop runtime fallback
   (``ReviewRoutePlan.reviewer_fallback_providers``/
   ``critic_fallback_provider``) -- it never itself makes or retries a
   provider call; that happens in
   :class:`patchfrog.review.orchestration.AgentOrchestrator`, which is
   what actually decides *when* to use the fallback this router names.

Both reuse the same single ``PATCHFROG_ROUTER_FALLBACK_PROVIDER``
setting -- one operator-configured backup provider, two distinct
trigger points, never a second config surface. There is no chain beyond
one hop and no retry-driven re-routing to a third provider in either
case -- see Part M.
"""

from __future__ import annotations

from collections.abc import Mapping

from patchfrog.config.settings import Settings
from patchfrog.review.agents.roles import AgentRole
from patchfrog.review.provider import LLMProvider
from patchfrog.review.provider_factory import MissingProviderCredentialsError, has_credentials
from patchfrog.review.providers.anthropic_provider import AnthropicLLMProvider
from patchfrog.review.providers.gemini_provider import GeminiLLMProvider
from patchfrog.review.providers.openai_provider import OpenAILLMProvider
from patchfrog.review.runtime_config import (
    DEFAULT_MODEL_BY_PROVIDER,
    SUPPORTED_PROVIDERS,
    ReviewRuntimeConfig,
)
from patchfrog.routing.capabilities import supports_structured_output
from patchfrog.routing.domain import ReviewRoutePlan, RouteReason


class NoProviderConfiguredError(MissingProviderCredentialsError):
    """No credentialed, structured-output-capable provider is configured
    at all -- a subclass of the existing
    :class:`~patchfrog.review.provider_factory.MissingProviderCredentialsError`
    (not a parallel exception type), so any existing call site that
    already handles that error (e.g. the CLI's ``--dry-run`` guard)
    handles this too, unchanged."""


def _build_provider(provider: str, model: str, *, settings: Settings, timeout_seconds: float) -> LLMProvider:
    if provider == "anthropic":
        return AnthropicLLMProvider(api_key=settings.anthropic_api_key, model=model, timeout_seconds=timeout_seconds)
    if provider == "gemini":
        return GeminiLLMProvider(api_key=settings.gemini_api_key, model=model, timeout_seconds=timeout_seconds)
    if provider == "openai":
        return OpenAILLMProvider(api_key=settings.openai_api_key, model=model, timeout_seconds=timeout_seconds)
    raise ValueError(f"unsupported provider: {provider!r}")  # pragma: no cover -- filtered out upstream


class ProviderNotAllowedByPolicyError(NoProviderConfiguredError):
    """Every credentialed, structured-output-capable provider was
    excluded by an explicit governance ``allowed_providers`` policy
    (Milestone Z, Z14) -- distinct from
    :class:`NoProviderConfiguredError` (no credential at all) so a
    caller/operator can tell "you forgot to set a credential" apart from
    "governance forbids every provider you've credentialed", but still a
    subclass of the existing :class:`~patchfrog.review.provider_factory.MissingProviderCredentialsError`
    so every pre-existing call site that already handles that error
    keeps working unchanged."""


class ModelRouter:
    """Computes one :class:`~patchfrog.routing.domain.ReviewRoutePlan`
    per review run from operator-controlled configuration alone.

    ``allowed_providers`` (Milestone Z, Z14): an optional governance-
    supplied allowlist. ``None`` (the default) means "no governance
    restriction" -- byte-for-byte identical behavior to every release
    before this milestone, for every self-hosted deployment with no
    Cloud governance configured. When given, it is applied to the
    credentialed-provider list *before* any selection happens (reviewer,
    critic, and runtime fallback all inherit the restriction
    automatically) -- **credential existence is never itself permission
    to use a provider** (the same rule Milestone U's own runtime
    fallback already established for ``PATCHFROG_ROUTER_FALLBACK_PROVIDER``,
    now generalized to governance)."""

    def __init__(self, *, settings: Settings, allowed_providers: frozenset[str] | None = None) -> None:
        self._settings = settings
        self._allowed_providers = allowed_providers

    def route(self, *, runtime_config: ReviewRuntimeConfig, critic_enabled: bool) -> ReviewRoutePlan:
        reasons: list[RouteReason] = []

        configured = [
            provider
            for provider in SUPPORTED_PROVIDERS
            if supports_structured_output(provider) and has_credentials(provider, settings=self._settings)
        ]
        if self._allowed_providers is not None:
            excluded_by_policy = [p for p in configured if p not in self._allowed_providers]
            configured = [p for p in configured if p in self._allowed_providers]
            if excluded_by_policy:
                reasons.append(RouteReason.PROVIDER_EXCLUDED_BY_POLICY)
            if not configured:
                raise ProviderNotAllowedByPolicyError(
                    "Every credentialed provider is excluded by the effective governance policy's "
                    f"allowed_providers={sorted(self._allowed_providers)!r}. Set a credential for an "
                    "allowed provider, or ask a workspace owner to widen the policy."
                )
        if not configured:
            raise NoProviderConfiguredError(
                "No configured provider has a credential set. Set at least one of "
                "ANTHROPIC_API_KEY/GEMINI_API_KEY/OPENAI_API_KEY (never in .patchfrog.yml) "
                "before running a real AI review. Use --dry-run to build candidates/context "
                "without calling a provider."
            )

        reviewer_family, config_fallback_used = self._select_reviewer_family(
            configured, preferred=runtime_config.provider, reasons=reasons
        )
        diversity_available = len(configured) > 1
        critic_family, diversity_used = self._select_critic_family(
            configured, reviewer_family, critic_enabled=critic_enabled, reasons=reasons
        )

        timeout_seconds = runtime_config.request_timeout_seconds

        def _model_for(family: str) -> str:
            if family == runtime_config.provider:
                return runtime_config.model
            return DEFAULT_MODEL_BY_PROVIDER[family]

        reviewer_provider = _build_provider(
            reviewer_family, _model_for(reviewer_family), settings=self._settings, timeout_seconds=timeout_seconds
        )
        reviewer_providers = {AgentRole.CORRECTNESS: reviewer_provider, AgentRole.SECURITY: reviewer_provider}

        critic_provider: LLMProvider | None = None
        if critic_family is not None:
            critic_model = runtime_config.critic_model if critic_family == reviewer_family else _model_for(critic_family)
            critic_provider = _build_provider(
                critic_family, critic_model, settings=self._settings, timeout_seconds=timeout_seconds
            )

        # Runtime execution failover (distinct from config_fallback_used
        # above -- see this module's own docstring). Eligible only when
        # the operator *explicitly* named PATCHFROG_ROUTER_FALLBACK_PROVIDER
        # and it is itself configured/credentialed and distinct from this
        # role's own primary -- never auto-selected from whatever else
        # merely happens to have a credential (see
        # _select_runtime_fallback_family's own docstring).
        runtime_fallback_family = self._select_runtime_fallback_family(configured, exclude=reviewer_family)
        reviewer_fallback_providers: Mapping[AgentRole, LLMProvider] | None = None
        if runtime_fallback_family is not None:
            fb_provider = _build_provider(
                runtime_fallback_family, _model_for(runtime_fallback_family),
                settings=self._settings, timeout_seconds=timeout_seconds,
            )
            reviewer_fallback_providers = {AgentRole.CORRECTNESS: fb_provider, AgentRole.SECURITY: fb_provider}

        critic_fallback_provider: LLMProvider | None = None
        critic_runtime_fallback_family: str | None = None
        if critic_family is not None:
            critic_runtime_fallback_family = self._select_runtime_fallback_family(configured, exclude=critic_family)
            if critic_runtime_fallback_family is not None:
                critic_fallback_provider = _build_provider(
                    critic_runtime_fallback_family, _model_for(critic_runtime_fallback_family),
                    settings=self._settings, timeout_seconds=timeout_seconds,
                )

        return ReviewRoutePlan(
            reviewer_providers=reviewer_providers,
            critic_provider=critic_provider,
            reviewer_provider_family=reviewer_family,
            critic_provider_family=critic_family,
            reasons=tuple(reasons),
            diversity_available=diversity_available,
            diversity_used=diversity_used,
            config_fallback_used=config_fallback_used,
            reviewer_fallback_providers=reviewer_fallback_providers,
            reviewer_runtime_fallback_family=runtime_fallback_family,
            critic_fallback_provider=critic_fallback_provider,
            critic_runtime_fallback_family=critic_runtime_fallback_family,
            runtime_fallback_permitted=reviewer_fallback_providers is not None or critic_fallback_provider is not None,
        )

    def _select_runtime_fallback_family(self, configured: list[str], *, exclude: str) -> str | None:
        """Final correction: runtime fallback is **never** auto-selected
        from whatever else happens to be configured/credentialed --
        having a credential for a provider is not, by itself, permission
        to use it as a fallback. Eligible only when the operator
        *explicitly* named it via ``PATCHFROG_ROUTER_FALLBACK_PROVIDER``,
        and it is actually configured and distinct from the role's own
        primary. No credential at all for the named provider, or no
        setting at all, both mean "no runtime fallback" -- identical to
        having none configured."""

        explicit = self._settings.router_fallback_provider
        if explicit is not None and explicit in configured and explicit != exclude:
            return explicit
        return None

    def _select_reviewer_family(
        self, configured: list[str], *, preferred: str, reasons: list[RouteReason]
    ) -> tuple[str, bool]:
        if preferred in configured:
            reasons.append(RouteReason.PREFERRED_PROVIDER_AVAILABLE)
            return preferred, False

        fallback = self._settings.router_fallback_provider
        if fallback is not None and fallback in configured:
            reasons.append(RouteReason.PREFERRED_PROVIDER_UNAVAILABLE_FALLBACK_USED)
            return fallback, True

        raise MissingProviderCredentialsError(
            f"PATCHFROG_REVIEW_PROVIDER={preferred!r} has no credential configured, and no usable "
            f"PATCHFROG_ROUTER_FALLBACK_PROVIDER is set either (configured providers: {configured}). "
            "Set the preferred provider's credential, or set PATCHFROG_ROUTER_FALLBACK_PROVIDER to "
            "one that has one."
        )

    def _select_critic_family(
        self, configured: list[str], reviewer_family: str, *, critic_enabled: bool, reasons: list[RouteReason]
    ) -> tuple[str | None, bool]:
        if not critic_enabled:
            reasons.append(RouteReason.CRITIC_DISABLED_BY_CONFIG)
            return None, False

        if len(configured) == 1:
            reasons.append(RouteReason.SINGLE_PROVIDER_CONFIGURED)
            return reviewer_family, False

        explicit = self._settings.router_critic_provider
        if explicit is not None and explicit in configured:
            diversity_used = explicit != reviewer_family
            reasons.append(RouteReason.CRITIC_FAMILY_DIVERSITY_USED if diversity_used else RouteReason.CRITIC_SAME_FAMILY_NO_ALTERNATIVE)
            return explicit, diversity_used

        other = next((provider for provider in configured if provider != reviewer_family), None)
        if other is not None:
            reasons.append(RouteReason.CRITIC_FAMILY_DIVERSITY_USED)
            return other, True

        reasons.append(RouteReason.CRITIC_SAME_FAMILY_NO_ALTERNATIVE)  # pragma: no cover -- unreachable when len>1
        return reviewer_family, False


__all__ = ["ModelRouter", "NoProviderConfiguredError"]
