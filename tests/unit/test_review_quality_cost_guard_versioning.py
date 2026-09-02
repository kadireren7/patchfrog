"""Pins the exact version bumps the Quality + Cost Guard (Milestone F)
made, so an accidental revert of any of them is caught immediately --
same pattern as tests/unit/test_review_orchestration_versioning.py."""

from __future__ import annotations

from patchfrog.review.config import (
    CONFIG_SCHEMA_VERSION,
    QUALITY_COST_POLICY_VERSION,
    REVIEW_ENGINE_VERSION,
    REVIEW_POLICY_VERSION,
    REVIEW_PROMPT_VERSION,
    ReviewModelIdentity,
)

#: The exact end-of-Agent-Orchestration-v1 versions (Milestone D
#: baseline, main @ 034ab4d29a886ccd7832880a555f9aaa7741d6eb) -- frozen
#: here as a fixed comparison point.
_PRE_GUARD_CONFIG_SCHEMA_VERSION = 3
_PRE_GUARD_POLICY_VERSION = 3
_PRE_GUARD_ENGINE_VERSION = 2


def test_config_schema_version_bumped_for_shared_output_budget_semantics() -> None:
    assert CONFIG_SCHEMA_VERSION > _PRE_GUARD_CONFIG_SCHEMA_VERSION


def test_review_policy_version_bumped_for_tiered_critic_expectation() -> None:
    assert REVIEW_POLICY_VERSION > _PRE_GUARD_POLICY_VERSION


def test_review_engine_version_bumped_for_tiered_execution() -> None:
    assert REVIEW_ENGINE_VERSION > _PRE_GUARD_ENGINE_VERSION


# No prompt text changed for the Quality + Cost Guard itself -- tiering
# only changed which roles run and how strictly the critic verifies,
# never the prompt templates themselves (spec sections 30/37), so
# REVIEW_PROMPT_VERSION stayed at the Agent Orchestration v1 value (3)
# through the end of this milestone. A later milestone (Change
# Intelligence Foundation) legitimately bumped it again, to 4, for a
# real prompt template change (the new optional `<change_intelligence>`
# section); see tests/unit/test_review_orchestration_versioning.py's own
# comment for why the historical "still 3" pin that used to live here is
# retired rather than kept permanently false.


def test_quality_cost_policy_version_introduced() -> None:
    assert QUALITY_COST_POLICY_VERSION == 1


def test_pre_guard_model_identity_never_reused_post_guard() -> None:
    """The exact canonical-run-identity invariant these bumps protect: a
    run fingerprinted under the pre-guard prompt/policy/engine versions
    must produce a different ReviewModelIdentity fingerprint than a run
    under the current (Quality + Cost Guard) versions, for otherwise-
    identical provider/model."""

    pre_guard = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=REVIEW_PROMPT_VERSION,
        policy_version=_PRE_GUARD_POLICY_VERSION,
        engine_version=_PRE_GUARD_ENGINE_VERSION,
        quality_cost_policy_version=0,
    )
    current = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=REVIEW_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    assert pre_guard.fingerprint() != current.fingerprint()
