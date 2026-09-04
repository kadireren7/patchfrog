"""AI Reviewer configuration and effective-toolchain identity.

Mirrors :mod:`patchfrog.analysis.config` + :mod:`patchfrog.analysis.toolchain`'s
split for the static analysis engine: :class:`ReviewConfig` captures
repository-controlled review *behavior* intent (loaded from an optional
``review:`` section in ``.patchfrog.yml``, same untrusted-repo-content
safety rules as analysis config), while :class:`ReviewModelIdentity`
captures the *effective* toolchain a run actually used -- provider,
model, prompt version, and review-policy version. Both fingerprints
together form a review run's persisted identity (see
:mod:`patchfrog.persistence.repositories.review_run`), so a model swap, a
provider swap, a prompt-template edit, or a validation/critic/confidence-
aggregation rule change each invalidate reuse of a prior canonical run --
exactly the toolchain-awareness bug fixed for the static analysis engine
in Phase 3.

Provider/model selection is deliberately **not** part of this module's
repository-controlled config -- see :mod:`patchfrog.review.runtime_config`
for the operator/deployment-controlled ``ReviewRuntimeConfig`` that owns
provider, model, critic model, and request timeout. A repository cannot
choose or influence PatchFrog's AI provider; see
:func:`load_review_config`'s explicit rejection of those fields below.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict

from patchfrog.analysis.domain import Confidence

logger = structlog.get_logger(__name__)

_CONFIG_FILENAMES = (".patchfrog.yml", ".patchfrog.yaml")

#: Bumped whenever ReviewConfig's own shape/semantics change -- including a
#: change to how an *omitted* field's effective value is computed, even
#: when the field list itself doesn't change. Bumped to 4 for the Quality
#: + Cost Guard (Milestone F): `max_output_tokens_per_candidate`'s
#: *effective* repo-facing meaning changes materially -- previously each
#: selected role independently received the full configured value;
#: now it is a shared candidate-level ceiling deterministically split
#: across whichever roles the effort tier selects (see
#: patchfrog.review.orchestration.AgentOrchestrator). A repository that
#: set this field under the old semantics would silently get a
#: different effective per-role budget under the new one, so any prior
#: canonical run must never be silently reused across this version
#: boundary. (Previously bumped to 3 because `provider`/`model`/
#: `critic_model`/`request_timeout_seconds` were removed from
#: repository-controlled config entirely -- see module docstring.)
CONFIG_SCHEMA_VERSION = 4

#: Bumped whenever patchfrog.review.prompt's system/user prompt templates
#: change materially enough that a prior run's proposals can no longer be
#: considered equivalent to what re-running now would produce. Bumped to
#: 3 for Agent Orchestration v1: the single general-purpose reviewer
#: system prompt was replaced by two role-scoped specialist prompts
#: (Correctness, Security -- see patchfrog.review.prompt.build_agent_prompt)
#: with materially different scope instructions each. NOT bumped for the
#: Quality + Cost Guard (Milestone F) -- no prompt text changed; tiering
#: only changes which roles run and how strictly the critic verifies,
#: never the prompt templates themselves. Bumped to 4 for Change
#: Intelligence Foundation: a new optional `<change_intelligence>`
#: user-prompt section (patchfrog.review.prompt._build_user_prompt),
#: populated from patchfrog.change_intelligence.evidence when a
#: candidate has real missing-companion evidence -- the template shape
#: itself changed even though it's empty (and thus byte-identical to
#: before) for most candidates. Bumped to 5 for Contract & Blast Radius
#: Intelligence: a second new optional `<contract_intelligence>`
#: user-prompt section, populated from
#: patchfrog.contract_intelligence.evidence only for the exact candidate
#: that is the source of a real, structurally-detected contract delta --
#: empty (and thus byte-identical) for every other candidate, but the
#: template shape itself changed again. Bumped to 6 for Intent
#: Verification Foundation: a third new optional `<intent_verification>`
#: user-prompt section, populated from
#: patchfrog.intent_verification.evidence only for the exact candidate
#: that is part of a mapped ChangeUnit for a real, sufficiently-explicit
#: intent claim -- empty for every other candidate (including every
#: candidate on a PR with no usable PR title/body intent at all).
REVIEW_PROMPT_VERSION = 6

#: Bumped whenever patchfrog.review.validation / patchfrog.review.critic /
#: patchfrog.review.confidence's rules for what survives to a final
#: finding change materially. Bumped to 4 for the Quality + Cost Guard
#: (Milestone F): tier-driven bounded escalation and `CriticExpectation`
#: (patchfrog.review.effort) change what can survive to a final finding
#: -- a LIGHT-tier candidate now critiques even less than today's
#: selective policy for otherwise-unremarkable proposals, while a
#: DEEP/escalated candidate now makes critic verification mandatory,
#: bypassing that selective policy entirely. (Previously bumped to 3 for
#: Agent Orchestration v1: selective critic verification and cross-role
#: duplicate merge / contradiction suppression.)
REVIEW_POLICY_VERSION = 4

#: Bumped whenever the review *execution engine* changes materially --
#: originally scoped to candidate generation/selection
#: (patchfrog.review.candidates) alone, broadened for Agent Orchestration
#: v1 (one general-purpose reviewer call per candidate became
#: deterministic role selection -> concurrent specialist calls ->
#: cross-role dedup/contradiction handling -> selective critic
#: verification). Bumped to 3 for the Quality + Cost Guard (Milestone F):
#: every candidate's role selection, context budget, output-token
#: budget, and retry allowance are now tier-driven
#: (patchfrog.review.effort.ReviewEffortPolicy) rather than uniform --
#: a materially different call shape per candidate than the prior
#: engine version ever produced.
REVIEW_ENGINE_VERSION = 3

#: Independent version for the Quality + Cost Guard's own tiering policy
#: (patchfrog.review.effort) -- deliberately separate from
#: REVIEW_ENGINE_VERSION/REVIEW_POLICY_VERSION so a future change to only
#: the tiering thresholds/signals themselves (e.g. adjusting
#: `_LARGE_CHANGED_SYMBOL_LINES`) can invalidate canonical-run reuse
#: without needing a broader engine- or policy-version bump.
QUALITY_COST_POLICY_VERSION = 1

DEFAULT_MAX_CANDIDATES = 40
DEFAULT_MAX_INPUT_TOKENS_PER_CANDIDATE = 12_000
DEFAULT_MAX_OUTPUT_TOKENS_PER_CANDIDATE = 4_096
DEFAULT_MAX_TOTAL_INPUT_TOKENS = 400_000
DEFAULT_MAX_CONCURRENT_REQUESTS = 4
DEFAULT_MIN_FINAL_CONFIDENCE: Confidence = Confidence.MEDIUM
DEFAULT_MAX_RETRIES = 2

#: Fields that select PatchFrog's AI provider/model/timeout -- an
#: operator/deployment concern, never a repository one (see
#: :mod:`patchfrog.review.runtime_config`). A repository's
#: `.patchfrog.yml` setting any of these has zero effect on which
#: provider/model actually runs; :func:`load_review_config` explicitly
#: detects and rejects them rather than silently dropping them via
#: `extra="ignore"`.
OPERATOR_ONLY_REVIEW_FIELDS = ("provider", "model", "critic_model", "request_timeout_seconds")


class ReviewConfig(BaseModel):
    """Repository-controlled AI-review *behavior* configuration for one
    review run.

    Deliberately does **not** include provider/model/critic model/request
    timeout -- those are operator/deployment-controlled runtime concerns
    (see :class:`patchfrog.review.runtime_config.ReviewRuntimeConfig`).
    Only behavior a repository may legitimately tune lives here: how many
    candidates to review, token/concurrency budgets, confidence
    thresholds, and retry counts.
    """

    model_config = ConfigDict(extra="ignore")

    critic_enabled: bool = True
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_input_tokens_per_candidate: int = DEFAULT_MAX_INPUT_TOKENS_PER_CANDIDATE
    max_output_tokens_per_candidate: int = DEFAULT_MAX_OUTPUT_TOKENS_PER_CANDIDATE
    max_total_input_tokens: int = DEFAULT_MAX_TOTAL_INPUT_TOKENS
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS
    min_final_confidence: Confidence = DEFAULT_MIN_FINAL_CONFIDENCE
    max_retries: int = DEFAULT_MAX_RETRIES

    def fingerprint(self) -> str:
        """A deterministic fingerprint of repository-controlled review
        *behavior* intent -- deliberately excludes provider/model/timeout
        (operator-controlled, see :mod:`patchfrog.review.runtime_config`)
        and anything about what actually ran (see
        :class:`ReviewModelIdentity`, folded in separately by the
        caller)."""

        payload = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "critic_enabled": self.critic_enabled,
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
    #: See :data:`QUALITY_COST_POLICY_VERSION`.
    quality_cost_policy_version: int = QUALITY_COST_POLICY_VERSION

    def fingerprint(self) -> str:
        payload = {
            "reviewer_provider": self.reviewer_provider,
            "reviewer_model": self.reviewer_model,
            "critic_provider": self.critic_provider,
            "critic_model": self.critic_model,
            "prompt_version": self.prompt_version,
            "policy_version": self.policy_version,
            "engine_version": self.engine_version,
            "quality_cost_policy_version": self.quality_cost_policy_version,
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

        operator_only_present = sorted(
            field for field in OPERATOR_ONLY_REVIEW_FIELDS if field in review_section
        )
        if operator_only_present:
            # Provider/model/critic model/timeout are operator/deployment
            # concerns, never repository ones (see module docstring) -- a
            # repository cannot use `.patchfrog.yml` to influence which
            # AI provider or model actually runs. A real review attempt
            # (`on_malformed="raise"`) must fail loudly and actionably
            # rather than silently proceeding as if the fields had never
            # been set; a preview/default resolution just warns and
            # strips them, since `ReviewConfig` doesn't have these fields
            # at all any more and `extra="ignore"` would otherwise drop
            # them with no visible trace.
            logger.warning(
                "review_config_operator_only_fields_ignored",
                path=str(path),
                fields=operator_only_present,
            )
            if on_malformed == "raise":
                raise MalformedReviewConfigError(
                    f"{path}: review.{', review.'.join(operator_only_present)} "
                    "are no longer repository-controlled. Remove these fields from "
                    "'.patchfrog.yml' and configure the PatchFrog runtime/operator "
                    "instead (see docs/deployment.md).",
                    path=path,
                    raw_text=raw_text,
                )
            review_section = {
                key: value
                for key, value in review_section.items()
                if key not in OPERATOR_ONLY_REVIEW_FIELDS
            }

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
