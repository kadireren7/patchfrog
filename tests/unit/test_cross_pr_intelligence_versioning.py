"""Pins the exact version bumps Cross-PR Intelligence Foundation made,
so an accidental revert of any of them is caught immediately -- same
pattern as tests/unit/test_trajectory_intelligence_versioning.py.

Like Trajectory Intelligence before it, this milestone *does* bump
``QUALITY_COST_POLICY_VERSION`` -- a second real Quality + Cost Guard
tiering-policy semantics change (a new structural signal that can raise
the provisional tier and force mandatory critic verification), not
merely a new optional prompt section."""

from __future__ import annotations

from patchfrog.change_intelligence.domain import CHANGE_INTELLIGENCE_VERSION
from patchfrog.contract_intelligence.domain import CONTRACT_INTELLIGENCE_VERSION
from patchfrog.cross_pr_intelligence.domain import CROSS_PR_INTELLIGENCE_VERSION
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

#: The exact pre-Cross-PR-Intelligence versions (Trajectory
#: Intelligence Foundation baseline, main @ 2cd9e1a) -- frozen here as a
#: fixed comparison point.
_PRE_Q_PROMPT_VERSION = 10
_PRE_Q_POLICY_VERSION = 4
_PRE_Q_ENGINE_VERSION = 3
_PRE_Q_CONFIG_SCHEMA_VERSION = 4
_PRE_Q_QUALITY_COST_POLICY_VERSION = 2
_PRE_Q_TELEMETRY_SCHEMA_VERSION = 8
_PRE_Q_CHANGE_INTELLIGENCE_VERSION = 1
_PRE_Q_CONTRACT_INTELLIGENCE_VERSION = 1
_PRE_Q_INTENT_VERIFICATION_VERSION = 1
_PRE_Q_TEST_INTELLIGENCE_VERSION = 1
_PRE_Q_HISTORICAL_REGRESSION_MEMORY_VERSION = 1
_PRE_Q_REPOSITORY_LEARNINGS_VERSION = 1
_PRE_Q_TRAJECTORY_INTELLIGENCE_VERSION = 1


def test_review_prompt_version_bumped_for_cross_pr_intelligence_section() -> None:
    """A real prompt template shape change: the new optional
    `<cross_pr_intelligence>` user-prompt section (see
    patchfrog.review.prompt._build_user_prompt)."""

    assert REVIEW_PROMPT_VERSION > _PRE_Q_PROMPT_VERSION


def test_cross_pr_intelligence_version_introduced() -> None:
    assert CROSS_PR_INTELLIGENCE_VERSION == 1


def test_telemetry_schema_version_bumped_for_cross_pr_intelligence() -> None:
    """ReviewTelemetrySnapshot gained the exported
    `cross_pr_intelligence` field -- a real JSON-shape change."""

    assert TELEMETRY_SCHEMA_VERSION > _PRE_Q_TELEMETRY_SCHEMA_VERSION


def test_quality_cost_policy_version_bumped_for_cross_pr_signal() -> None:
    """Like Trajectory Intelligence before it, this milestone changes
    tiering-policy semantics: ReviewEffortPolicy.decide_provisional
    gained a second real new structural signal
    (cross_pr_signal_present) that can raise the provisional tier and
    force mandatory critic verification."""

    assert QUALITY_COST_POLICY_VERSION > _PRE_Q_QUALITY_COST_POLICY_VERSION


def test_change_intelligence_version_unchanged() -> None:
    assert CHANGE_INTELLIGENCE_VERSION == _PRE_Q_CHANGE_INTELLIGENCE_VERSION


def test_contract_intelligence_version_unchanged() -> None:
    assert CONTRACT_INTELLIGENCE_VERSION == _PRE_Q_CONTRACT_INTELLIGENCE_VERSION


def test_intent_verification_version_unchanged() -> None:
    assert INTENT_VERIFICATION_VERSION == _PRE_Q_INTENT_VERIFICATION_VERSION


def test_test_intelligence_version_unchanged() -> None:
    assert TEST_INTELLIGENCE_VERSION == _PRE_Q_TEST_INTELLIGENCE_VERSION


def test_historical_regression_memory_version_unchanged() -> None:
    assert HISTORICAL_REGRESSION_MEMORY_VERSION == _PRE_Q_HISTORICAL_REGRESSION_MEMORY_VERSION


def test_repository_learnings_version_unchanged() -> None:
    assert REPOSITORY_LEARNINGS_VERSION == _PRE_Q_REPOSITORY_LEARNINGS_VERSION


def test_trajectory_intelligence_version_unchanged() -> None:
    """Cross-PR Intelligence reads Change Intelligence's own already
    -computed change_units directly -- it never reinterprets Trajectory
    Intelligence's own lineage-walking rules, so that package's version
    never bumps because of this milestone."""

    assert TRAJECTORY_INTELLIGENCE_VERSION == _PRE_Q_TRAJECTORY_INTELLIGENCE_VERSION


def test_review_policy_version_unchanged() -> None:
    """Cross-PR Intelligence never changes what survives to a final
    finding -- it only adds optional evidence text to the prompt and a
    bounded Quality + Cost Guard escalation signal for an already-real
    candidate."""

    assert REVIEW_POLICY_VERSION == _PRE_Q_POLICY_VERSION


def test_review_engine_version_unchanged() -> None:
    """No change to call shape, retry/escalation rules, or execution
    architecture -- Cross-PR Intelligence's matching layer is computed
    deterministically, in-process, with zero LLM calls and zero new
    GitHub API calls. Its one effect on execution (candidate ordering,
    critic mandatoriness) is expressed entirely through the existing
    Quality + Cost Guard mechanism, not a new engine-level call shape."""

    assert REVIEW_ENGINE_VERSION == _PRE_Q_ENGINE_VERSION


def test_config_schema_version_unchanged() -> None:
    assert CONFIG_SCHEMA_VERSION == _PRE_Q_CONFIG_SCHEMA_VERSION


def test_pre_q_model_identity_never_reused_post_q() -> None:
    """The exact canonical-run-identity invariant this bump protects: a
    run fingerprinted under the pre-Cross-PR-Intelligence prompt
    version must produce a different ReviewModelIdentity fingerprint
    than a run under the current version, for otherwise-identical
    provider/model."""

    pre_q = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=_PRE_Q_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=_PRE_Q_QUALITY_COST_POLICY_VERSION,
    )
    post_q = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=REVIEW_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
        quality_cost_policy_version=QUALITY_COST_POLICY_VERSION,
    )
    assert pre_q.fingerprint() != post_q.fingerprint()
