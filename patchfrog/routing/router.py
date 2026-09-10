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

**Fallback is bounded to exactly one hop**: preferred provider ->
(if unavailable) one explicitly configured fallback provider -> (if
that's also unavailable) a clear, actionable error, reusing
:class:`~patchfrog.review.provider_factory.MissingProviderCredentialsError`
rather than inventing a second "no provider" exception type callers
would need to handle separately. There is no chain beyond one fallback
and no retry-driven re-routing to a third provider -- see Part M.
"""

from __future__ import annotations

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


class ModelRouter:
    """Computes one :class:`~patchfrog.routing.domain.ReviewRoutePlan`
    per review run from operator-controlled configuration alone."""

    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings

    def route(self, *, runtime_config: ReviewRuntimeConfig, critic_enabled: bool) -> ReviewRoutePlan:
        reasons: list[RouteReason] = []

        configured = [
            provider
            for provider in SUPPORTED_PROVIDERS
            if supports_structured_output(provider) and has_credentials(provider, settings=self._settings)
        ]
        if not configured:
            raise NoProviderConfiguredError(
                "No configured provider has a credential set. Set at least one of "
                "ANTHROPIC_API_KEY/GEMINI_API_KEY/OPENAI_API_KEY (never in .patchfrog.yml) "
                "before running a real AI review. Use --dry-run to build candidates/context "
                "without calling a provider."
            )

        reviewer_family, fallback_used = self._select_reviewer_family(
            configured, preferred=runtime_config.provider, reasons=reasons
        )
        diversity_available = len(configured) > 1
        critic_family, diversity_used = self._select_critic_family(
            configured, reviewer_family, critic_enabled=critic_enabled, reasons=reasons
        )

        timeout_seconds = runtime_config.request_timeout_seconds
        reviewer_model = runtime_config.model if reviewer_family == runtime_config.provider else (
            DEFAULT_MODEL_BY_PROVIDER[reviewer_family]
        )
        reviewer_provider = _build_provider(
            reviewer_family, reviewer_model, settings=self._settings, timeout_seconds=timeout_seconds
        )
        reviewer_providers = {AgentRole.CORRECTNESS: reviewer_provider, AgentRole.SECURITY: reviewer_provider}

        critic_provider: LLMProvider | None = None
        if critic_family is not None:
            if critic_family == reviewer_family:
                critic_model = runtime_config.critic_model
            elif critic_family == runtime_config.provider:
                critic_model = runtime_config.model
            else:
                critic_model = DEFAULT_MODEL_BY_PROVIDER[critic_family]
            critic_provider = _build_provider(
                critic_family, critic_model, settings=self._settings, timeout_seconds=timeout_seconds
            )

        return ReviewRoutePlan(
            reviewer_providers=reviewer_providers,
            critic_provider=critic_provider,
            reviewer_provider_family=reviewer_family,
            critic_provider_family=critic_family,
            reasons=tuple(reasons),
            diversity_available=diversity_available,
            diversity_used=diversity_used,
            fallback_used=fallback_used,
        )

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
