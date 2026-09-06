"""Pins the exact version bumps Executable Verification Foundation made,
so an accidental revert of any of them is caught immediately -- same
pattern as tests/unit/test_cross_repo_intelligence_versioning.py.

Unlike Trajectory/Cross-PR/Cross-Repo Intelligence, this milestone does
**not** bump ``QUALITY_COST_POLICY_VERSION`` -- verification never
influences ``ReviewEffortPolicy.decide_provisional``'s tiering signals
at all; it only enriches the *critic's own prompt* for a proposal that
is already being critiqued (see
``validation/executable_verification/latest-summary.md`` section 7).
It also does not bump ``REVIEW_POLICY_VERSION``: adding a new bounded
evidence *text* section (exactly like every other Intelligence
package's own critic/specialist evidence) is a prompt-shape change, not
a change to the deterministic validation/critic-selection rules
``REVIEW_POLICY_VERSION`` describes -- the same reasoning that already
kept it unchanged for J-R."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CHANGE_INTELLIGENCE_VERSION
from patchfrog.contract_intelligence.domain import CONTRACT_INTELLIGENCE_VERSION
from patchfrog.cross_pr_intelligence.domain import CROSS_PR_INTELLIGENCE_VERSION
from patchfrog.cross_repo_intelligence.domain import CROSS_REPO_INTELLIGENCE_VERSION
from patchfrog.executable_verification.domain import EXECUTABLE_VERIFICATION_VERSION
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
from patchfrog.trajectory_intelligence.domain import TRAJECTORY_INTELLIGENCE_VERSION

#: The exact pre-Executable-Verification versions (Cross-Repo
#: Intelligence Foundation baseline, main @ 63924f9) -- frozen here as a
#: fixed comparison point.
_PRE_S_PROMPT_VERSION = 12
_PRE_S_POLICY_VERSION = 4
_PRE_S_ENGINE_VERSION = 3
_PRE_S_CONFIG_SCHEMA_VERSION = 4
_PRE_S_QUALITY_COST_POLICY_VERSION = 4
_PRE_S_TELEMETRY_SCHEMA_VERSION = 10
_PRE_S_CHANGE_INTELLIGENCE_VERSION = 1
_PRE_S_CONTRACT_INTELLIGENCE_VERSION = 1
_PRE_S_INTENT_VERIFICATION_VERSION = 1
_PRE_S_TEST_INTELLIGENCE_VERSION = 1
_PRE_S_HISTORICAL_REGRESSION_MEMORY_VERSION = 1
_PRE_S_REPOSITORY_LEARNINGS_VERSION = 1
_PRE_S_TRAJECTORY_INTELLIGENCE_VERSION = 1
_PRE_S_CROSS_PR_INTELLIGENCE_VERSION = 1
_PRE_S_CROSS_REPO_INTELLIGENCE_VERSION = 1


def test_review_prompt_version_bumped_for_executable_verification_section() -> None:
    """A real prompt template shape change: the new optional
    `<executable_verification>` *critic-only* prompt section (see
    patchfrog.review.prompt.build_critic_prompt)."""

    assert REVIEW_PROMPT_VERSION > _PRE_S_PROMPT_VERSION


def test_executable_verification_version_introduced() -> None:
    assert EXECUTABLE_VERIFICATION_VERSION == 1


def test_telemetry_schema_version_bumped_for_executable_verification() -> None:
    """ReviewTelemetrySnapshot gained the exported
    `executable_verification` field -- a real JSON-shape change."""

    assert TELEMETRY_SCHEMA_VERSION > _PRE_S_TELEMETRY_SCHEMA_VERSION


def test_quality_cost_policy_version_unchanged() -> None:
    """Unlike Trajectory/Cross-PR/Cross-Repo Intelligence, verification
    never contributes a structural signal to
    ReviewEffortPolicy.decide_provisional -- it only enriches the
    critic's own prompt for an already-being-critiqued proposal."""

    assert QUALITY_COST_POLICY_VERSION == _PRE_S_QUALITY_COST_POLICY_VERSION


def test_change_intelligence_version_unchanged() -> None:
    assert CHANGE_INTELLIGENCE_VERSION == _PRE_S_CHANGE_INTELLIGENCE_VERSION


def test_contract_intelligence_version_unchanged() -> None:
    assert CONTRACT_INTELLIGENCE_VERSION == _PRE_S_CONTRACT_INTELLIGENCE_VERSION


def test_intent_verification_version_unchanged() -> None:
    assert INTENT_VERIFICATION_VERSION == _PRE_S_INTENT_VERIFICATION_VERSION


def test_test_intelligence_version_unchanged() -> None:
    """Executable Verification reuses
    patchfrog.test_intelligence.expectations.derive_test_surfaces
    directly -- it never reinterprets that package's own derivation
    rules, so its version never bumps because of this milestone."""

    assert TEST_INTELLIGENCE_VERSION == _PRE_S_TEST_INTELLIGENCE_VERSION


def test_historical_regression_memory_version_unchanged() -> None:
    assert HISTORICAL_REGRESSION_MEMORY_VERSION == _PRE_S_HISTORICAL_REGRESSION_MEMORY_VERSION


def test_repository_learnings_version_unchanged() -> None:
    assert REPOSITORY_LEARNINGS_VERSION == _PRE_S_REPOSITORY_LEARNINGS_VERSION


def test_trajectory_intelligence_version_unchanged() -> None:
    assert TRAJECTORY_INTELLIGENCE_VERSION == _PRE_S_TRAJECTORY_INTELLIGENCE_VERSION


def test_cross_pr_intelligence_version_unchanged() -> None:
    assert CROSS_PR_INTELLIGENCE_VERSION == _PRE_S_CROSS_PR_INTELLIGENCE_VERSION


def test_cross_repo_intelligence_version_unchanged() -> None:
    assert CROSS_REPO_INTELLIGENCE_VERSION == _PRE_S_CROSS_REPO_INTELLIGENCE_VERSION


def test_review_policy_version_unchanged() -> None:
    """A new bounded evidence *text* section is a prompt-shape change,
    not a change to what survives to a final finding -- the same
    reasoning that already kept this unchanged for J-R."""

    assert REVIEW_POLICY_VERSION == _PRE_S_POLICY_VERSION


def test_review_engine_version_unchanged() -> None:
    """No change to call shape, retry/escalation rules, or execution
    architecture -- the critic call shape is identical, just one
    additional optional prompt section."""

    assert REVIEW_ENGINE_VERSION == _PRE_S_ENGINE_VERSION


def test_config_schema_version_unchanged() -> None:
    assert CONFIG_SCHEMA_VERSION == _PRE_S_CONFIG_SCHEMA_VERSION


def test_pre_s_model_identity_never_reused_post_s() -> None:
    """The exact canonical-run-identity invariant this bump protects: a
    run fingerprinted under the pre-Executable-Verification prompt
    version must produce a different ReviewModelIdentity fingerprint
    than a run under the current version, for otherwise-identical
    provider/model."""

    pre_s = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=_PRE_S_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    post_s = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=REVIEW_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    assert pre_s.fingerprint() != post_s.fingerprint()
