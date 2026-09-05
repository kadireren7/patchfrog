"""Pins the exact version bumps Historical Regression Memory Foundation
made, so an accidental revert of any of them is caught immediately --
same pattern as tests/unit/test_test_intelligence_versioning.py."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CHANGE_INTELLIGENCE_VERSION
from patchfrog.contract_intelligence.domain import CONTRACT_INTELLIGENCE_VERSION
from patchfrog.historical_regression_memory.domain import HISTORICAL_REGRESSION_MEMORY_VERSION
from patchfrog.intent_verification.domain import INTENT_VERIFICATION_VERSION
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

#: The exact pre-Historical-Regression-Memory versions (Test
#: Intelligence Foundation baseline, main @ 62e155e) -- frozen here as
#: a fixed comparison point.
_PRE_HRM_PROMPT_VERSION = 7
_PRE_HRM_POLICY_VERSION = 4
_PRE_HRM_ENGINE_VERSION = 3
_PRE_HRM_CONFIG_SCHEMA_VERSION = 4
_PRE_HRM_QUALITY_COST_POLICY_VERSION = 1
_PRE_HRM_TELEMETRY_SCHEMA_VERSION = 5
_PRE_HRM_CHANGE_INTELLIGENCE_VERSION = 1
_PRE_HRM_CONTRACT_INTELLIGENCE_VERSION = 1
_PRE_HRM_INTENT_VERIFICATION_VERSION = 1
_PRE_HRM_TEST_INTELLIGENCE_VERSION = 1


def test_review_prompt_version_bumped_for_historical_regression_section() -> None:
    """A real prompt template shape change: the new optional
    `<historical_regression>` user-prompt section (see
    patchfrog.review.prompt._build_user_prompt)."""

    assert REVIEW_PROMPT_VERSION > _PRE_HRM_PROMPT_VERSION


def test_historical_regression_memory_version_introduced() -> None:
    assert HISTORICAL_REGRESSION_MEMORY_VERSION == 1


def test_telemetry_schema_version_bumped_for_historical_regression_memory() -> None:
    """ReviewTelemetrySnapshot gained the exported
    `historical_regression_memory` field -- a real JSON-shape change,
    bumped proactively (applying the Milestone J correction / K/L/M
    precedent, not repeating the original oversight)."""

    assert TELEMETRY_SCHEMA_VERSION > _PRE_HRM_TELEMETRY_SCHEMA_VERSION


def test_change_intelligence_version_unchanged() -> None:
    assert CHANGE_INTELLIGENCE_VERSION == _PRE_HRM_CHANGE_INTELLIGENCE_VERSION


def test_contract_intelligence_version_unchanged() -> None:
    assert CONTRACT_INTELLIGENCE_VERSION == _PRE_HRM_CONTRACT_INTELLIGENCE_VERSION


def test_intent_verification_version_unchanged() -> None:
    assert INTENT_VERIFICATION_VERSION == _PRE_HRM_INTENT_VERIFICATION_VERSION


def test_test_intelligence_version_unchanged() -> None:
    assert TEST_INTELLIGENCE_VERSION == _PRE_HRM_TEST_INTELLIGENCE_VERSION


def test_review_policy_version_unchanged() -> None:
    """Historical Regression Memory never changes what survives to a
    final finding -- it only adds optional evidence text to the prompt,
    exactly like Change/Contract/Intent/Test Intelligence before it."""

    assert REVIEW_POLICY_VERSION == _PRE_HRM_POLICY_VERSION


def test_review_engine_version_unchanged() -> None:
    """No change to call shape, retry/escalation rules, or execution
    architecture -- Historical Regression Memory's matching layer is
    computed deterministically, in-process, with zero LLM calls; its
    one bounded trust query never affects reviewer/critic call shape."""

    assert REVIEW_ENGINE_VERSION == _PRE_HRM_ENGINE_VERSION


def test_config_schema_version_unchanged() -> None:
    assert CONFIG_SCHEMA_VERSION == _PRE_HRM_CONFIG_SCHEMA_VERSION


def test_quality_cost_policy_version_unchanged() -> None:
    assert QUALITY_COST_POLICY_VERSION == _PRE_HRM_QUALITY_COST_POLICY_VERSION


def test_pre_hrm_model_identity_never_reused_post_hrm() -> None:
    """The exact canonical-run-identity invariant this bump protects: a
    run fingerprinted under the pre-Historical-Regression-Memory prompt
    version must produce a different ReviewModelIdentity fingerprint
    than a run under the current version, for otherwise-identical
    provider/model."""

    pre_hrm = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=_PRE_HRM_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    post_hrm = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=REVIEW_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    assert pre_hrm.fingerprint() != post_hrm.fingerprint()
