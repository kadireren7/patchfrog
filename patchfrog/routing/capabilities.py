"""U2: a minimal, honest provider-capability registry.

Deliberately encodes only the one capability the Model Router actually
needs to route safely: whether a provider's :class:`~patchfrog.review.provider.LLMProvider`
adapter guarantees bounded, schema-conforming structured output (every
adapter in this codebase does -- see each adapter's own module
docstring). It does **not** encode reasoning strength, cost tier, or
context-window size as static per-vendor facts: those are marketing
claims that go stale the moment a vendor ships a new model, not
capabilities PatchFrog's own code can verify or needs in order to route
a request safely (see the combined U+V spec's own "do NOT encode
marketing claims as facts" instruction). An operator who wants a
specific provider to serve a specific role configures that directly
(``PATCHFROG_REVIEW_PROVIDER``/``PATCHFROG_ROUTER_CRITIC_PROVIDER`` --
see :mod:`patchfrog.routing.router`), rather than PatchFrog guessing
which vendor is "stronger" from a hardcoded table.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider: str
    #: Whether this provider's adapter enforces bounded, schema-
    #: conforming structured output (never free-form text parsing) --
    #: the one property every review-pipeline consumer of an
    #: :class:`~patchfrog.review.provider.LLMProvider` actually relies
    #: on. All three currently-supported adapters satisfy this by
    #: construction (see each provider module's own docstring); a
    #: capability-less provider is not something this codebase can add
    #: without also changing :mod:`patchfrog.review.provider`'s
    #: contract, so this is never expected to be False for a real entry
    #: below -- it exists so a hypothetical future adapter that could
    #: only do free-form text has somewhere explicit to declare that,
    #: rather than the router assuming every registered provider
    #: qualifies.
    structured_output: bool


_REGISTRY: dict[str, ProviderCapabilities] = {
    "anthropic": ProviderCapabilities(provider="anthropic", structured_output=True),
    "gemini": ProviderCapabilities(provider="gemini", structured_output=True),
    "openai": ProviderCapabilities(provider="openai", structured_output=True),
}


def capabilities_for(provider: str) -> ProviderCapabilities | None:
    """``None`` for an unregistered provider name -- never a fabricated
    default capability set."""

    return _REGISTRY.get(provider)


def supports_structured_output(provider: str) -> bool:
    capabilities = capabilities_for(provider)
    return capabilities is not None and capabilities.structured_output
