"""Pins the exact version bumps Contract & Blast Radius Intelligence
made, so an accidental revert of any of them is caught immediately --
same pattern as tests/unit/test_change_intelligence_versioning.py."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CHANGE_INTELLIGENCE_VERSION
from patchfrog.contract_intelligence.domain import CONTRACT_INTELLIGENCE_VERSION
from patchfrog.review.config import (
    CONFIG_SCHEMA_VERSION,
    QUALITY_COST_POLICY_VERSION,
    REVIEW_ENGINE_VERSION,
    REVIEW_POLICY_VERSION,
    REVIEW_PROMPT_VERSION,
    ReviewModelIdentity,
)
from patchfrog.telemetry.domain import TELEMETRY_SCHEMA_VERSION

#: The exact pre-Contract-Intelligence versions (Change Intelligence
#: Foundation baseline, main @ 7734603) -- frozen here as a fixed
#: comparison point.
_PRE_CK_PROMPT_VERSION = 4
_PRE_CK_POLICY_VERSION = 4
_PRE_CK_ENGINE_VERSION = 3
_PRE_CK_CONFIG_SCHEMA_VERSION = 4
_PRE_CK_QUALITY_COST_POLICY_VERSION = 1
_PRE_CK_TELEMETRY_SCHEMA_VERSION = 2
_PRE_CK_CHANGE_INTELLIGENCE_VERSION = 1


def test_review_prompt_version_bumped_for_contract_intelligence_section() -> None:
    """A real prompt template shape change: the new optional
    `<contract_intelligence>` user-prompt section (see
    patchfrog.review.prompt._build_user_prompt)."""

    assert REVIEW_PROMPT_VERSION > _PRE_CK_PROMPT_VERSION


def test_contract_intelligence_version_introduced() -> None:
    assert CONTRACT_INTELLIGENCE_VERSION == 1


def test_telemetry_schema_version_bumped_for_contract_intelligence() -> None:
    """ReviewTelemetrySnapshot gained the exported `contract_intelligence`
    field -- a real JSON-shape change (see
    tests/unit/test_telemetry_reporting.py for the direct export-shape
    proof), so this milestone bumps the schema version itself rather
    than repeating Milestone J's initial oversight."""

    assert TELEMETRY_SCHEMA_VERSION > _PRE_CK_TELEMETRY_SCHEMA_VERSION


def test_change_intelligence_version_unchanged_by_contract_intelligence() -> None:
    """Change Intelligence's own grouping/affected-surface/companion-
    heuristic logic is untouched -- the new CONTRACT_CONSUMER_NOT_UPDATED
    CompanionReasonCode member is produced by a different package reusing
    the type, never by patchfrog.change_intelligence itself."""

    assert CHANGE_INTELLIGENCE_VERSION == _PRE_CK_CHANGE_INTELLIGENCE_VERSION


def test_review_policy_version_unchanged_by_contract_intelligence() -> None:
    """Contract Intelligence never changes what survives to a final
    finding -- it only adds optional evidence text to the prompt,
    exactly like Change Intelligence before it."""

    assert REVIEW_POLICY_VERSION == _PRE_CK_POLICY_VERSION


def test_review_engine_version_unchanged_by_contract_intelligence() -> None:
    """No change to call shape, retry/escalation rules, or execution
    architecture -- Contract Intelligence is computed deterministically,
    in-process (plus one bounded, read-only base-commit file fetch),
    with zero additional provider calls."""

    assert REVIEW_ENGINE_VERSION == _PRE_CK_ENGINE_VERSION


def test_config_schema_version_unchanged_by_contract_intelligence() -> None:
    """No new/changed repository-controlled `.patchfrog.yml` field was
    introduced by this milestone."""

    assert CONFIG_SCHEMA_VERSION == _PRE_CK_CONFIG_SCHEMA_VERSION


def test_quality_cost_policy_version_unchanged_by_contract_intelligence() -> None:
    """Quality + Cost Guard tiering itself is untouched -- Contract
    Intelligence evidence text is already small enough (see
    patchfrog.contract_intelligence.evidence's own bounds) to apply
    uniformly across tiers without needing its own policy dimension."""

    assert QUALITY_COST_POLICY_VERSION == _PRE_CK_QUALITY_COST_POLICY_VERSION


def test_pre_ck_model_identity_never_reused_post_ck() -> None:
    """The exact canonical-run-identity invariant this bump protects: a
    run fingerprinted under the pre-Contract-Intelligence prompt version
    must produce a different ReviewModelIdentity fingerprint than a run
    under the current version, for otherwise-identical provider/model."""

    pre_ck = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=_PRE_CK_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    post_ck = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=REVIEW_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    assert pre_ck.fingerprint() != post_ck.fingerprint()
