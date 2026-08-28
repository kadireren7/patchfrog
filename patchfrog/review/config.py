"""AI Reviewer configuration and effective-toolchain identity.

Mirrors :mod:`patchfrog.analysis.config` + :mod:`patchfrog.analysis.toolchain`'s
split for the static analysis engine: :class:`ReviewConfig` captures
configuration *intent* (loaded from an optional ``review:`` section in
``.patchfrog.yml``, same untrusted-repo-content safety rules as analysis
config), while :class:`ReviewModelIdentity` captures the *effective*
toolchain a run actually used -- provider, model, prompt version, and
review-policy version. Both fingerprints together form a review run's
persisted identity (see :mod:`patchfrog.persistence.repositories.review_run`),
so a model swap, a provider swap, a prompt-template edit, or a
validation/critic/confidence-aggregation rule change each invalidate
reuse of a prior canonical run -- exactly the toolchain-awareness bug
fixed for the static analysis engine in Phase 3.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from patchfrog.analysis.domain import Confidence

logger = structlog.get_logger(__name__)

_CONFIG_FILENAMES = (".patchfrog.yml", ".patchfrog.yaml")

#: Bumped whenever ReviewConfig's own shape/semantics change -- including a
#: change to how an *omitted* field's effective value is computed, even
#: when the field list itself doesn't change (see the critic_model/
#: request_timeout_seconds effective-default fix below: two YAML configs
#: that look identical to an older PatchFrog version can now produce a
#: materially different effective config, so any prior canonical run must
#: never be silently reused across this version boundary).
CONFIG_SCHEMA_VERSION = 2

#: Bumped whenever patchfrog.review.prompt's system/user prompt templates
#: change materially enough that a prior run's proposals can no longer be
#: considered equivalent to what re-running now would produce.
REVIEW_PROMPT_VERSION = 2

#: Bumped whenever patchfrog.review.validation / patchfrog.review.critic /
#: patchfrog.review.confidence's rules for what survives to a final
#: finding change materially.
REVIEW_POLICY_VERSION = 2

#: Bumped whenever candidate generation/selection (patchfrog.review.candidates)
#: changes materially.
REVIEW_ENGINE_VERSION = 1

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_CRITIC_MODEL = "claude-opus-5"
DEFAULT_MAX_CANDIDATES = 40
DEFAULT_MAX_INPUT_TOKENS_PER_CANDIDATE = 12_000
DEFAULT_MAX_OUTPUT_TOKENS_PER_CANDIDATE = 4_096
DEFAULT_MAX_TOTAL_INPUT_TOKENS = 400_000
DEFAULT_MAX_CONCURRENT_REQUESTS = 4
DEFAULT_MIN_FINAL_CONFIDENCE: Confidence = Confidence.MEDIUM
DEFAULT_MAX_RETRIES = 2
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

#: Per-provider effective timeout used only when a repository's
#: `.patchfrog.yml` omits `request_timeout_seconds` entirely. Anthropic
#: keeps the 30s general default (unchanged). Gemini's default
#: ("AUTOMATIC") thinking behavior is slower and far more variable --
#: live validation observed real 504 DEADLINE_EXCEEDED failures at 30s
#: and single calls up to ~144s -- so an explicit, more generous default
#: applies automatically for `provider: gemini` alone, without requiring
#: every Gemini-configured repository to remember to raise it by hand.
#: An explicitly-configured `request_timeout_seconds` always wins over
#: this table, for either provider (see `_apply_effective_defaults`).
_DEFAULT_TIMEOUT_SECONDS_BY_PROVIDER: dict[str, float] = {
    "gemini": 120.0,
}


class ReviewConfig(BaseModel):
    """Effective AI-review configuration for one review run.

    ``provider``/``model`` here are configuration *intent* -- what the
    caller asked for. The provider adapter's own
    :class:`~patchfrog.review.provider.ProviderIdentity`, captured at call
    time, is the *effective* identity folded into
    :class:`ReviewModelIdentity` below; the two are expected to agree, but
    only the latter participates in run-identity fingerprinting, so a
    provider/adapter behavior change is still caught even if the
    configured strings didn't change.
    """

    model_config = ConfigDict(extra="ignore")

    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    critic_enabled: bool = True
    critic_model: str = DEFAULT_CRITIC_MODEL
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_input_tokens_per_candidate: int = DEFAULT_MAX_INPUT_TOKENS_PER_CANDIDATE
    max_output_tokens_per_candidate: int = DEFAULT_MAX_OUTPUT_TOKENS_PER_CANDIDATE
    max_total_input_tokens: int = DEFAULT_MAX_TOTAL_INPUT_TOKENS
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS
    min_final_confidence: Confidence = DEFAULT_MIN_FINAL_CONFIDENCE
    max_retries: int = DEFAULT_MAX_RETRIES
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS

    @model_validator(mode="after")
    def _apply_effective_defaults(self) -> ReviewConfig:
        """Fill in provider-coherent effective values for fields the
        caller *omitted* -- never for a field explicitly supplied, even
        when the explicit value happens to equal a class default.

        ``model_fields_set`` (populated by pydantic during construction,
        both for direct keyword construction and for
        ``model_validate(review_section)`` in :func:`load_review_config`)
        is exactly "which field names were actually present in the
        input" -- the only reliable way to distinguish "omitted" from
        "explicitly set to the default-looking value", which comparing
        against ``DEFAULT_CRITIC_MODEL``/``DEFAULT_REQUEST_TIMEOUT_SECONDS``
        cannot do (a user may deliberately choose exactly that string).

        Mutating the fields here (rather than exposing a separate
        "effective config" object) means every reader --
        :meth:`fingerprint`, :mod:`patchfrog.review.provider_factory`,
        anything else that reads ``config.critic_model`` /
        ``config.request_timeout_seconds`` -- automatically sees the
        correct effective value with no change needed at the read site;
        config normalization is the single boundary, not scattered
        provider-aware branches.
        """

        if "critic_model" not in self.model_fields_set:
            # Same reviewer/critic model unless the caller says otherwise
            # -- provider-neutral (an Anthropic config with an explicit
            # non-default `model` and no `critic_model` gets the same
            # coherent behavior, not just Gemini).
            self.critic_model = self.model
        if "request_timeout_seconds" not in self.model_fields_set:
            self.request_timeout_seconds = _DEFAULT_TIMEOUT_SECONDS_BY_PROVIDER.get(
                self.provider, DEFAULT_REQUEST_TIMEOUT_SECONDS
            )
        return self

    def fingerprint(self) -> str:
        """A deterministic fingerprint of *configuration intent* --
        deliberately excludes anything about what actually ran (see
        :class:`ReviewModelIdentity`, folded in separately by the
        caller)."""

        payload = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "provider": self.provider,
            "model": self.model,
            "critic_enabled": self.critic_enabled,
            "critic_model": self.critic_model,
            "max_candidates": self.max_candidates,
            "max_input_tokens_per_candidate": self.max_input_tokens_per_candidate,
            "max_output_tokens_per_candidate": self.max_output_tokens_per_candidate,
            "max_total_input_tokens": self.max_total_input_tokens,
            "min_final_confidence": self.min_final_confidence.value,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class ReviewModelIdentity(BaseModel):
    """The *effective* toolchain identity for one review run: the actual
    provider/model that served requests (reviewer and, if enabled,
    critic), plus PatchFrog's own prompt/policy/engine versions.

    Two runs with identical :class:`ReviewConfig` intent can still have a
    different effective identity -- e.g. PatchFrog ships a prompt-template
    fix, or the configured model alias now resolves to a different
    snapshot server-side -- and canonical-run reuse must treat those as
    distinct, exactly like
    :class:`patchfrog.analysis.toolchain.ToolchainSnapshot` does for the
    static analysis engine.
    """

    model_config = ConfigDict(extra="ignore")

    reviewer_provider: str
    reviewer_model: str
    critic_provider: str | None
    critic_model: str | None
    prompt_version: int = REVIEW_PROMPT_VERSION
    policy_version: int = REVIEW_POLICY_VERSION
    engine_version: int = REVIEW_ENGINE_VERSION

    def fingerprint(self) -> str:
        payload = {
            "reviewer_provider": self.reviewer_provider,
            "reviewer_model": self.reviewer_model,
            "critic_provider": self.critic_provider,
            "critic_model": self.critic_model,
            "prompt_version": self.prompt_version,
            "policy_version": self.policy_version,
            "engine_version": self.engine_version,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


EnabledSetting = bool | Literal["auto"]

#: How ``load_review_config`` handles a *malformed* (present but
#: unparsable/invalid) config file. ``"defaults"`` -- the historical,
#: dry-run/CLI-preview-friendly behavior -- silently falls back to
#: :class:`ReviewConfig` defaults after logging a warning. ``"raise"``
#: instead raises :class:`MalformedReviewConfigError`, so a caller that
#: represents a real, persisted review run (the CLI's real run and the
#: production Celery task alike -- see :mod:`patchfrog.review.service`)
#: can turn a bad committed config into a visible, queryable failed run
#: rather than one that silently proceeded on defaults. A genuinely
#: *missing* file is never "malformed" -- that always means defaults,
#: under either mode.
OnMalformed = Literal["defaults", "raise"]


class MalformedReviewConfigError(ValueError):
    """Raised by :func:`load_review_config` when ``on_malformed="raise"``
    and the repository's ``.patchfrog.yml``/``.patchfrog.yaml`` exists but
    could not be parsed or validated (bad YAML, non-mapping shape, or a
    ``review:`` section that fails :class:`ReviewConfig` validation).

    Carries the file's raw text so a caller can derive a stable,
    content-addressed identity for the failure -- see
    :func:`patchfrog.review.service._malformed_config_fingerprint` --
    so retrying against the *same* bad content doesn't need a fresh
    identity, but fixing the file does.
    """

    def __init__(self, message: str, *, path: Path, raw_text: str) -> None:
        super().__init__(message)
        self.path = path
        self.raw_text = raw_text


def load_review_config(repository_root: Path, *, on_malformed: OnMalformed = "defaults") -> ReviewConfig:
    """Load the ``review:`` section of ``.patchfrog.yml``/``.patchfrog.yaml``
    from a repository root.

    Same untrusted-input safety rules as
    :func:`patchfrog.analysis.config.load_analysis_config`: the file is
    only ever parsed with ``yaml.safe_load``, and a genuinely *missing*
    file always falls back to defaults regardless of ``on_malformed``.
    Credentials are never read from this file -- see
    :mod:`patchfrog.config.settings` for the environment-only
    ``ANTHROPIC_API_KEY``.
    """

    for filename in _CONFIG_FILENAMES:
        path = repository_root / filename
        if not path.is_file():
            continue

        try:
            raw_text = path.read_text()
        except OSError as exc:
            return _malformed(path, "", f"unreadable: {exc}", on_malformed)

        try:
            raw = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            return _malformed(path, raw_text, f"invalid YAML: {exc}", on_malformed)

        if raw is None:
            return ReviewConfig()
        if not isinstance(raw, dict):
            return _malformed(path, raw_text, "top-level content is not a mapping", on_malformed)

        review_section = raw.get("review", {})
        if not isinstance(review_section, dict):
            return _malformed(path, raw_text, "'review' section is not a mapping", on_malformed)

        # Never allow a credential-shaped key in the repo-controlled file
        # to be silently accepted and ignored without at least a warning
        # -- credentials are environment-only (see module docstring).
        for forbidden in ("api_key", "credentials", "token", "secret"):
            if forbidden in review_section:
                logger.warning(
                    "review_config_credential_field_ignored", path=str(path), field=forbidden
                )

        try:
            return ReviewConfig.model_validate(review_section)
        except Exception as exc:
            return _malformed(path, raw_text, f"invalid 'review' section: {exc}", on_malformed)

    return ReviewConfig()


def _malformed(path: Path, raw_text: str, detail: str, on_malformed: OnMalformed) -> ReviewConfig:
    logger.warning("review_config_malformed", path=str(path), detail=detail, on_malformed=on_malformed)
    if on_malformed == "raise":
        raise MalformedReviewConfigError(f"{path}: {detail}", path=path, raw_text=raw_text)
    return ReviewConfig()
