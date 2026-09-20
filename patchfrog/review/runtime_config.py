"""Operator/deployment-controlled AI provider runtime configuration.

:class:`ReviewRuntimeConfig` is the trust boundary counterpart to
:mod:`patchfrog.review.config`'s repository-controlled
:class:`~patchfrog.review.config.ReviewConfig`: it owns *which AI
provider/model actually runs* -- something a reviewed repository must
never be able to choose (a malicious or merely careless
``.patchfrog.yml`` could otherwise force a more expensive model, a
different critic, or route traffic to an unintended provider). It is
resolved exclusively from :class:`patchfrog.config.settings.Settings`
(environment variables / secret manager), never from any repository
file.

Self-hosted operators choose provider/model via the
``PATCHFROG_REVIEW_*`` environment variables below. A future PatchFrog
Cloud is expected to resolve this same object from its own internal
routing instead of raw environment variables, without any repository
ever needing to change.
"""

from __future__ import annotations

from pydantic import BaseModel

from patchfrog.config.settings import Settings

#: The only providers `ReviewRuntimeConfig`/`provider_factory` currently
#: support. Kept here (rather than duplicated in `provider_factory`) so
#: both the CLI dry-run path (which never constructs a provider) and
#: `provider_factory` (which does) validate against the same list.
SUPPORTED_PROVIDERS = ("anthropic", "gemini", "openai")

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

#: Milestone U (Model Router, family diversity): the default model name
#: for a provider *other than* the operator's configured
#: `PATCHFROG_REVIEW_PROVIDER`/`PATCHFROG_REVIEW_MODEL` pair -- used only
#: when the router selects a different provider family for the critic
#: role (`PATCHFROG_ROUTER_CRITIC_PROVIDER`, see
#: `patchfrog.routing.router`) and the operator did not also set
#: `PATCHFROG_REVIEW_CRITIC_MODEL` to a model name valid for that other
#: family. Never used for the reviewer role or a single-provider setup --
#: those always use `DEFAULT_MODEL`/the operator's own explicit value,
#: unchanged from before this milestone.
DEFAULT_MODEL_BY_PROVIDER: dict[str, str] = {
    "anthropic": DEFAULT_MODEL,
    "gemini": "gemini-3.6-flash",
    "openai": "gpt-6-astra",
}

#: Per-provider effective timeout used only when the operator omits
#: `PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS` entirely. Anthropic keeps
#: the 30s general default (unchanged). Gemini's default ("AUTOMATIC")
#: thinking behavior is slower and far more variable -- live validation
#: observed real 504 DEADLINE_EXCEEDED failures at 30s and single calls
#: up to ~144s -- so a more generous default applies automatically for
#: `PATCHFROG_REVIEW_PROVIDER=gemini` alone. An explicitly-configured
#: timeout always wins over this table, for either provider.
_DEFAULT_TIMEOUT_SECONDS_BY_PROVIDER: dict[str, float] = {
    "gemini": 120.0,
}

#: Conservative model-name family prefixes, after stripping a
#: `models/`-prefixed resource-path form (see
#: patchfrog.review.providers.gemini_provider's own docstring on why that
#: form is legitimate) -- never an exhaustive per-model list, which would
#: go stale the moment either vendor ships a new model name. The single
#: source of truth for this check (also used by `patchfrog.ops.doctor`'s
#: advisory report) -- exists to catch a real, previously-live production
#: bug: PATCHFROG_REVIEW_PROVIDER=openai with PATCHFROG_REVIEW_MODEL left
#: unset (or copy-pasted from an Anthropic example) silently resolving to
#: `claude-opus-5` and 404ing against OpenAI's own API on the first real
#: review.
MODEL_FAMILY_PREFIX: dict[str, str] = {"anthropic": "claude-", "gemini": "gemini-", "openai": "gpt-"}
_MODEL_RESOURCE_PREFIX = "models/"


def model_matches_provider_family(provider: str, model: str) -> bool:
    """`True` unless `model` looks like a *different, known* provider's
    model name -- conservative by design: an unlisted/future model
    family (neither `provider`'s own expected prefix nor any other known
    one) is never rejected, since this check's only job is catching the
    exact, previously-live misconfiguration above, not acting as an
    exhaustive per-model allowlist."""

    expected_prefix = MODEL_FAMILY_PREFIX.get(provider)
    if expected_prefix is None:
        return True
    normalized = model.removeprefix(_MODEL_RESOURCE_PREFIX)
    if normalized.startswith(expected_prefix):
        return True
    other_families = [p for p, prefix in MODEL_FAMILY_PREFIX.items() if p != provider and normalized.startswith(prefix)]
    return not other_families


class ReviewRuntimeConfig(BaseModel):
    """The effective, operator-controlled provider/model/timeout for AI
    review. Never loaded from `.patchfrog.yml` or any other
    repository-controlled input -- see module docstring.
    """

    provider: str
    model: str
    critic_model: str
    request_timeout_seconds: float


def resolve_review_runtime_config(settings: Settings) -> ReviewRuntimeConfig:
    """Resolve the operator's effective AI provider runtime configuration
    from trusted `Settings` (environment variables), applying the exact
    same "omitted vs. explicit" effective-default semantics previously
    used for `.patchfrog.yml`'s (now-removed) `review.critic_model` /
    `review.request_timeout_seconds` fields:

    - `model`/`critic_model` omitted -> defaults to *this provider's own*
      model (`DEFAULT_MODEL_BY_PROVIDER[provider]`) -- never a flat,
      provider-oblivious default, which is exactly how a real production
      incident sent `claude-opus-5` to OpenAI's API (404) after
      `PATCHFROG_REVIEW_PROVIDER=openai` was configured without also
      setting `PATCHFROG_REVIEW_MODEL`.
    - `request_timeout_seconds` omitted -> a provider-appropriate
      default (30s, 120s for Gemini).

    Raises `ValueError` for an unsupported/unknown provider, or for an
    *explicitly* configured `PATCHFROG_REVIEW_MODEL`/
    `PATCHFROG_REVIEW_CRITIC_MODEL` that looks like a different, known
    provider's model name -- fails clearly rather than silently sending
    a mismatched model to the wrong provider's API. A defaulted model
    (operator didn't set one) is always correct by construction and
    never reaches this check.
    """

    provider = settings.review_provider
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"unsupported PATCHFROG_REVIEW_PROVIDER: {provider!r} "
            f"(supported: {', '.join(SUPPORTED_PROVIDERS)})"
        )

    provider_default_model = DEFAULT_MODEL_BY_PROVIDER.get(provider, DEFAULT_MODEL)

    if settings.review_model is not None:
        if not model_matches_provider_family(provider, settings.review_model):
            raise ValueError(
                f"PATCHFROG_REVIEW_MODEL={settings.review_model!r} does not look like a {provider!r} "
                f"model, but PATCHFROG_REVIEW_PROVIDER={provider!r}. Set a model name valid for that "
                "provider, or unset PATCHFROG_REVIEW_MODEL to use the provider's own default "
                f"({provider_default_model!r})."
            )
        model = settings.review_model
    else:
        model = provider_default_model

    if settings.review_critic_model is not None:
        if not model_matches_provider_family(provider, settings.review_critic_model):
            raise ValueError(
                f"PATCHFROG_REVIEW_CRITIC_MODEL={settings.review_critic_model!r} does not look like a "
                f"{provider!r} model, but PATCHFROG_REVIEW_PROVIDER={provider!r}. Set a model name valid "
                "for that provider, or unset PATCHFROG_REVIEW_CRITIC_MODEL to use the reviewer model."
            )
        critic_model = settings.review_critic_model
    else:
        critic_model = model

    request_timeout_seconds = (
        settings.review_request_timeout_seconds
        if settings.review_request_timeout_seconds is not None
        else _DEFAULT_TIMEOUT_SECONDS_BY_PROVIDER.get(provider, DEFAULT_REQUEST_TIMEOUT_SECONDS)
    )

    return ReviewRuntimeConfig(
        provider=provider,
        model=model,
        critic_model=critic_model,
        request_timeout_seconds=request_timeout_seconds,
    )
