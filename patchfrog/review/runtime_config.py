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
SUPPORTED_PROVIDERS = ("anthropic", "gemini")

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

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

    - `critic_model` omitted -> defaults to the same value as `model`
      (provider-neutral: never silently falls back to another
      provider's model).
    - `request_timeout_seconds` omitted -> a provider-appropriate
      default (30s, 120s for Gemini).

    Raises `ValueError` for an unsupported/unknown provider -- fails
    clearly rather than deferring to a confusing error deep inside
    provider construction.
    """

    provider = settings.review_provider
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"unsupported PATCHFROG_REVIEW_PROVIDER: {provider!r} "
            f"(supported: {', '.join(SUPPORTED_PROVIDERS)})"
        )

    model = settings.review_model if settings.review_model is not None else DEFAULT_MODEL
    critic_model = settings.review_critic_model if settings.review_critic_model is not None else model
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
