"""Pins the exact version bumps Cross-Repo Intelligence Foundation
made, so an accidental revert of any of them is caught immediately --
same pattern as tests/unit/test_cross_pr_intelligence_versioning.py.

Like Cross-PR Intelligence before it, this milestone *does* bump
``QUALITY_COST_POLICY_VERSION`` -- a third real Quality + Cost Guard
tiering-policy semantics change (a new structural signal that can raise
the provisional tier and force mandatory critic verification), not
merely a new optional prompt section."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CHANGE_INTELLIGENCE_VERSION
from patchfrog.contract_intelligence.domain import CONTRACT_INTELLIGENCE_VERSION
from patchfrog.cross_pr_intelligence.domain import CROSS_PR_INTELLIGENCE_VERSION
from patchfrog.cross_repo_intelligence.domain import CROSS_REPO_INTELLIGENCE_VERSION
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

#: The exact pre-Cross-Repo-Intelligence versions (Cross-PR
#: Intelligence Foundation baseline, main @ ed61f11) -- frozen here as
#: a fixed comparison point.
_PRE_R_PROMPT_VERSION = 11
_PRE_R_POLICY_VERSION = 4
_PRE_R_ENGINE_VERSION = 3
_PRE_R_CONFIG_SCHEMA_VERSION = 4
_PRE_R_QUALITY_COST_POLICY_VERSION = 3
_PRE_R_TELEMETRY_SCHEMA_VERSION = 9
_PRE_R_CHANGE_INTELLIGENCE_VERSION = 1
_PRE_R_CONTRACT_INTELLIGENCE_VERSION = 1
_PRE_R_INTENT_VERIFICATION_VERSION = 1
_PRE_R_TEST_INTELLIGENCE_VERSION = 1
_PRE_R_HISTORICAL_REGRESSION_MEMORY_VERSION = 1
_PRE_R_REPOSITORY_LEARNINGS_VERSION = 1
_PRE_R_TRAJECTORY_INTELLIGENCE_VERSION = 1
_PRE_R_CROSS_PR_INTELLIGENCE_VERSION = 1


def test_review_prompt_version_bumped_for_cross_repo_intelligence_section() -> None:
    """A real prompt template shape change: the new optional
    `<cross_repo_intelligence>` user-prompt section (see
    patchfrog.review.prompt._build_user_prompt)."""

    assert REVIEW_PROMPT_VERSION > _PRE_R_PROMPT_VERSION


def test_cross_repo_intelligence_version_introduced() -> None:
    assert CROSS_REPO_INTELLIGENCE_VERSION == 1


def test_telemetry_schema_version_bumped_for_cross_repo_intelligence() -> None:
    """ReviewTelemetrySnapshot gained the exported
    `cross_repo_intelligence` field -- a real JSON-shape change."""

    assert TELEMETRY_SCHEMA_VERSION > _PRE_R_TELEMETRY_SCHEMA_VERSION


def test_quality_cost_policy_version_bumped_for_cross_repo_signal() -> None:
    """Like Trajectory and Cross-PR Intelligence before it, this
    milestone changes tiering-policy semantics: ReviewEffortPolicy.
    decide_provisional gained a third real new structural signal
    (cross_repo_signal_present) that can raise the provisional tier and
    force mandatory critic verification."""

    assert QUALITY_COST_POLICY_VERSION > _PRE_R_QUALITY_COST_POLICY_VERSION


def test_change_intelligence_version_unchanged() -> None:
    assert CHANGE_INTELLIGENCE_VERSION == _PRE_R_CHANGE_INTELLIGENCE_VERSION


def test_contract_intelligence_version_unchanged() -> None:
    """Cross-Repo Intelligence reads Contract Intelligence's own
    already-computed deltas directly -- it never reinterprets that
    package's own signature-diffing rules, so its version never bumps
    because of this milestone."""

    assert CONTRACT_INTELLIGENCE_VERSION == _PRE_R_CONTRACT_INTELLIGENCE_VERSION


def test_intent_verification_version_unchanged() -> None:
    assert INTENT_VERIFICATION_VERSION == _PRE_R_INTENT_VERIFICATION_VERSION


def test_test_intelligence_version_unchanged() -> None:
    assert TEST_INTELLIGENCE_VERSION == _PRE_R_TEST_INTELLIGENCE_VERSION


def test_historical_regression_memory_version_unchanged() -> None:
    assert HISTORICAL_REGRESSION_MEMORY_VERSION == _PRE_R_HISTORICAL_REGRESSION_MEMORY_VERSION


def test_repository_learnings_version_unchanged() -> None:
    assert REPOSITORY_LEARNINGS_VERSION == _PRE_R_REPOSITORY_LEARNINGS_VERSION


def test_trajectory_intelligence_version_unchanged() -> None:
    assert TRAJECTORY_INTELLIGENCE_VERSION == _PRE_R_TRAJECTORY_INTELLIGENCE_VERSION


def test_cross_pr_intelligence_version_unchanged() -> None:
    """Cross-Repo Intelligence never reuses Cross-PR Intelligence's own
    peer-discovery logic -- different trust boundary, different
    identity model -- so that package's version never bumps because of
    this milestone."""

    assert CROSS_PR_INTELLIGENCE_VERSION == _PRE_R_CROSS_PR_INTELLIGENCE_VERSION


def test_review_policy_version_unchanged() -> None:
    """Cross-Repo Intelligence never changes what survives to a final
    finding -- it only adds optional evidence text to the prompt and a
    bounded Quality + Cost Guard escalation signal for an already-real
    candidate."""

    assert REVIEW_POLICY_VERSION == _PRE_R_POLICY_VERSION


def test_review_engine_version_unchanged() -> None:
    """No change to call shape, retry/escalation rules, or execution
    architecture -- Cross-Repo Intelligence's matching layer is computed
    deterministically, in-process, with zero LLM calls and zero new
    GitHub API calls. Its one effect on execution (candidate ordering,
    critic mandatoriness) is expressed entirely through the existing
    Quality + Cost Guard mechanism, not a new engine-level call shape."""

    assert REVIEW_ENGINE_VERSION == _PRE_R_ENGINE_VERSION


def test_config_schema_version_unchanged() -> None:
    assert CONFIG_SCHEMA_VERSION == _PRE_R_CONFIG_SCHEMA_VERSION


def test_pre_r_model_identity_never_reused_post_r() -> None:
    """The exact canonical-run-identity invariant this bump protects: a
    run fingerprinted under the pre-Cross-Repo-Intelligence prompt
    version must produce a different ReviewModelIdentity fingerprint
    than a run under the current version, for otherwise-identical
    provider/model."""

    pre_r = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=_PRE_R_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=_PRE_R_QUALITY_COST_POLICY_VERSION,
    )
    post_r = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=REVIEW_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    assert pre_r.fingerprint() != post_r.fingerprint()
