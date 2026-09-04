"""Pins the exact version bumps Test Intelligence Foundation made, so an
accidental revert of any of them is caught immediately -- same pattern as
tests/unit/test_intent_verification_versioning.py."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CHANGE_INTELLIGENCE_VERSION
from patchfrog.contract_intelligence.domain import CONTRACT_INTELLIGENCE_VERSION
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

#: The exact pre-Test-Intelligence versions (Intent Verification
#: Foundation baseline, main @ 84c334a) -- frozen here as a fixed
#: comparison point.
_PRE_TI_PROMPT_VERSION = 6
_PRE_TI_POLICY_VERSION = 4
_PRE_TI_ENGINE_VERSION = 3
_PRE_TI_CONFIG_SCHEMA_VERSION = 4
_PRE_TI_QUALITY_COST_POLICY_VERSION = 1
_PRE_TI_TELEMETRY_SCHEMA_VERSION = 4
_PRE_TI_CHANGE_INTELLIGENCE_VERSION = 1
_PRE_TI_CONTRACT_INTELLIGENCE_VERSION = 1
_PRE_TI_INTENT_VERIFICATION_VERSION = 1


def test_review_prompt_version_bumped_for_test_intelligence_section() -> None:
    """A real prompt template shape change: the new optional
    `<test_intelligence>` user-prompt section (see
    patchfrog.review.prompt._build_user_prompt)."""

    assert REVIEW_PROMPT_VERSION > _PRE_TI_PROMPT_VERSION


def test_test_intelligence_version_introduced() -> None:
    assert TEST_INTELLIGENCE_VERSION == 1


def test_telemetry_schema_version_bumped_for_test_intelligence() -> None:
    """ReviewTelemetrySnapshot gained the exported `test_intelligence`
    field -- a real JSON-shape change, bumped proactively (applying the
    Milestone J correction / Milestone K/L precedent, not repeating the
    original oversight)."""

    assert TELEMETRY_SCHEMA_VERSION > _PRE_TI_TELEMETRY_SCHEMA_VERSION


def test_change_intelligence_version_unchanged_by_test_intelligence() -> None:
    assert CHANGE_INTELLIGENCE_VERSION == _PRE_TI_CHANGE_INTELLIGENCE_VERSION


def test_contract_intelligence_version_unchanged_by_test_intelligence() -> None:
    assert CONTRACT_INTELLIGENCE_VERSION == _PRE_TI_CONTRACT_INTELLIGENCE_VERSION


def test_intent_verification_version_unchanged_by_test_intelligence() -> None:
    assert INTENT_VERIFICATION_VERSION == _PRE_TI_INTENT_VERIFICATION_VERSION


def test_review_policy_version_unchanged_by_test_intelligence() -> None:
    """Test Intelligence never changes what survives to a final finding
    -- it only adds optional evidence text to the prompt, exactly like
    Change/Contract/Intent Intelligence before it."""

    assert REVIEW_POLICY_VERSION == _PRE_TI_POLICY_VERSION


def test_review_engine_version_unchanged_by_test_intelligence() -> None:
    """No change to call shape, retry/escalation rules, or execution
    architecture -- Test Intelligence is computed deterministically,
    in-process, with zero I/O and zero additional provider calls."""

    assert REVIEW_ENGINE_VERSION == _PRE_TI_ENGINE_VERSION


def test_config_schema_version_unchanged_by_test_intelligence() -> None:
    assert CONFIG_SCHEMA_VERSION == _PRE_TI_CONFIG_SCHEMA_VERSION


def test_quality_cost_policy_version_unchanged_by_test_intelligence() -> None:
    assert QUALITY_COST_POLICY_VERSION == _PRE_TI_QUALITY_COST_POLICY_VERSION


def test_pre_ti_model_identity_never_reused_post_ti() -> None:
    """The exact canonical-run-identity invariant this bump protects: a
    run fingerprinted under the pre-Test-Intelligence prompt version
    must produce a different ReviewModelIdentity fingerprint than a run
    under the current version, for otherwise-identical provider/model."""

    pre_ti = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=_PRE_TI_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    post_ti = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=REVIEW_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    assert pre_ti.fingerprint() != post_ti.fingerprint()
