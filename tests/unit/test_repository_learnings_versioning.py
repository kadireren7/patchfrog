"""Pins the exact version bumps Repository Learnings Foundation made,
so an accidental revert of any of them is caught immediately -- same
pattern as tests/unit/test_historical_regression_memory_versioning.py."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CHANGE_INTELLIGENCE_VERSION
from patchfrog.contract_intelligence.domain import CONTRACT_INTELLIGENCE_VERSION
from patchfrog.historical_regression_memory.domain import HISTORICAL_REGRESSION_MEMORY_VERSION
from patchfrog.intent_verification.domain import INTENT_VERIFICATION_VERSION
from patchfrog.repository_learnings.domain import REPOSITORY_LEARNINGS_VERSION
from patchfrog.review.config import (
    CONFIG_SCHEMA_VERSION,
    QUALITY_COST_POLICY_VERSION,
    REVIEW_ENGINE_VERSION,
    REVIEW_POLICY_VERSION,
    REVIEW_PROMPT_VERSION,
    ReviewModelIdentity,
)
from patchfrog.telemetry.domain import TELEMETRY_SCHEMA_VERSION
from patchfrog.test_intelligence.domain import TEST_INTELLIGENCE_VERSION

#: The exact pre-Repository-Learnings versions (Historical Regression
#: Memory Foundation baseline, main @ 5b42ea1) -- frozen here as a
#: fixed comparison point.
_PRE_RL_PROMPT_VERSION = 8
_PRE_RL_POLICY_VERSION = 4
_PRE_RL_ENGINE_VERSION = 3
_PRE_RL_CONFIG_SCHEMA_VERSION = 4
_PRE_RL_QUALITY_COST_POLICY_VERSION = 1
_PRE_RL_TELEMETRY_SCHEMA_VERSION = 6
_PRE_RL_CHANGE_INTELLIGENCE_VERSION = 1
_PRE_RL_CONTRACT_INTELLIGENCE_VERSION = 1
_PRE_RL_INTENT_VERIFICATION_VERSION = 1
_PRE_RL_TEST_INTELLIGENCE_VERSION = 1
_PRE_RL_HISTORICAL_REGRESSION_MEMORY_VERSION = 1


def test_review_prompt_version_bumped_for_repository_learning_section() -> None:
    """A real prompt template shape change: the new optional
    `<repository_learning>` user-prompt section (see
    patchfrog.review.prompt._build_user_prompt)."""

    assert REVIEW_PROMPT_VERSION > _PRE_RL_PROMPT_VERSION


def test_repository_learnings_version_introduced() -> None:
    assert REPOSITORY_LEARNINGS_VERSION == 1


def test_telemetry_schema_version_bumped_for_repository_learnings() -> None:
    """ReviewTelemetrySnapshot gained the exported
    `repository_learnings` field -- a real JSON-shape change, bumped
    proactively (applying the Milestone J correction / K/L/M/N
    precedent, not repeating the original oversight)."""

    assert TELEMETRY_SCHEMA_VERSION > _PRE_RL_TELEMETRY_SCHEMA_VERSION


def test_change_intelligence_version_unchanged() -> None:
    assert CHANGE_INTELLIGENCE_VERSION == _PRE_RL_CHANGE_INTELLIGENCE_VERSION


def test_contract_intelligence_version_unchanged() -> None:
    assert CONTRACT_INTELLIGENCE_VERSION == _PRE_RL_CONTRACT_INTELLIGENCE_VERSION


def test_intent_verification_version_unchanged() -> None:
    assert INTENT_VERIFICATION_VERSION == _PRE_RL_INTENT_VERIFICATION_VERSION


def test_test_intelligence_version_unchanged() -> None:
    assert TEST_INTELLIGENCE_VERSION == _PRE_RL_TEST_INTELLIGENCE_VERSION


def test_historical_regression_memory_version_unchanged() -> None:
    """Repository Learnings reuses N's trust model verbatim -- it never
    reinterprets N's own eligibility/temporal rules, so N's own version
    never bumps because of this milestone."""

    assert HISTORICAL_REGRESSION_MEMORY_VERSION == _PRE_RL_HISTORICAL_REGRESSION_MEMORY_VERSION


def test_review_policy_version_unchanged() -> None:
    """Repository Learnings never changes what survives to a final
    finding -- it only adds optional evidence text to the prompt,
    exactly like every other Intelligence layer before it."""

    assert REVIEW_POLICY_VERSION == _PRE_RL_POLICY_VERSION


def test_review_engine_version_unchanged() -> None:
    """No change to call shape, retry/escalation rules, or execution
    architecture -- Repository Learnings' matching layer is computed
    deterministically, in-process, with zero LLM calls and zero SQL
    queries of its own."""

    assert REVIEW_ENGINE_VERSION == _PRE_RL_ENGINE_VERSION


def test_config_schema_version_unchanged() -> None:
    assert CONFIG_SCHEMA_VERSION == _PRE_RL_CONFIG_SCHEMA_VERSION


def test_quality_cost_policy_version_unchanged() -> None:
    assert QUALITY_COST_POLICY_VERSION == _PRE_RL_QUALITY_COST_POLICY_VERSION


def test_pre_rl_model_identity_never_reused_post_rl() -> None:
    """The exact canonical-run-identity invariant this bump protects: a
    run fingerprinted under the pre-Repository-Learnings prompt version
    must produce a different ReviewModelIdentity fingerprint than a run
    under the current version, for otherwise-identical provider/model."""

    pre_rl = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=_PRE_RL_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    post_rl = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=REVIEW_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    assert pre_rl.fingerprint() != post_rl.fingerprint()
