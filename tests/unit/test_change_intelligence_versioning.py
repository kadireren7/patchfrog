"""Pins the exact version bumps Change Intelligence Foundation made, so
an accidental revert of any of them is caught immediately -- same
pattern as tests/unit/test_review_orchestration_versioning.py."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CHANGE_INTELLIGENCE_VERSION
from patchfrog.review.config import (
    CONFIG_SCHEMA_VERSION,
    QUALITY_COST_POLICY_VERSION,
    REVIEW_ENGINE_VERSION,
    REVIEW_POLICY_VERSION,
    REVIEW_PROMPT_VERSION,
    ReviewModelIdentity,
)

#: The exact pre-Change-Intelligence versions (Quality + Cost Guard
#: baseline, main @ c9a7af7) -- frozen here as a fixed comparison point.
_PRE_CI_PROMPT_VERSION = 3
_PRE_CI_POLICY_VERSION = 4
_PRE_CI_ENGINE_VERSION = 3
_PRE_CI_CONFIG_SCHEMA_VERSION = 4
_PRE_CI_QUALITY_COST_POLICY_VERSION = 1


def test_review_prompt_version_bumped_for_change_intelligence_section() -> None:
    """A real prompt template shape change: the new optional
    `<change_intelligence>` user-prompt section (see
    patchfrog.review.prompt._build_user_prompt)."""

    assert REVIEW_PROMPT_VERSION > _PRE_CI_PROMPT_VERSION


def test_change_intelligence_version_introduced() -> None:
    assert CHANGE_INTELLIGENCE_VERSION == 1


def test_review_policy_version_unchanged_by_change_intelligence() -> None:
    """Change Intelligence never changes what survives to a final
    finding -- it only adds optional evidence text to the prompt."""

    assert REVIEW_POLICY_VERSION == _PRE_CI_POLICY_VERSION


def test_review_engine_version_unchanged_by_change_intelligence() -> None:
    """No change to call shape, retry/escalation rules, or execution
    architecture -- Change Intelligence is computed deterministically,
    in-process, with zero additional provider calls."""

    assert REVIEW_ENGINE_VERSION == _PRE_CI_ENGINE_VERSION


def test_config_schema_version_unchanged_by_change_intelligence() -> None:
    """No new/changed repository-controlled `.patchfrog.yml` field was
    introduced by this milestone."""

    assert CONFIG_SCHEMA_VERSION == _PRE_CI_CONFIG_SCHEMA_VERSION


def test_quality_cost_policy_version_unchanged_by_change_intelligence() -> None:
    """Quality + Cost Guard tiering itself is untouched -- Change
    Intelligence evidence text is already small enough (see
    patchfrog.change_intelligence.evidence's module docstring) to apply
    uniformly across tiers without needing its own policy dimension."""

    assert QUALITY_COST_POLICY_VERSION == _PRE_CI_QUALITY_COST_POLICY_VERSION


def test_pre_ci_model_identity_never_reused_post_ci() -> None:
    """The exact canonical-run-identity invariant this bump protects: a
    run fingerprinted under the pre-Change-Intelligence prompt version
    must produce a different ReviewModelIdentity fingerprint than a run
    under the current version, for otherwise-identical provider/model."""

    pre_ci = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=_PRE_CI_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    post_ci = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=REVIEW_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    assert pre_ci.fingerprint() != post_ci.fingerprint()
