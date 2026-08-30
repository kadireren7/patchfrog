"""Required scenario 22: the review run/model identity for Agent
Orchestration v1 must differ from the pre-orchestration single-reviewer
engine version, so a prior canonical run is never silently reused as if
it already went through orchestration.

Also pins the exact version bumps (spec section 14) so an accidental
revert of any of them is caught immediately."""

from __future__ import annotations

from patchfrog.review.config import (
    REVIEW_ENGINE_VERSION,
    REVIEW_POLICY_VERSION,
    REVIEW_PROMPT_VERSION,
    ReviewModelIdentity,
)

#: The exact pre-orchestration versions (Milestone C baseline, main @
#: 5609311b911412cbffb1f755e53a0ce0dd0fbb08) -- frozen here as a fixed
#: comparison point, not re-derived from the current module, so this
#: test actually catches a version bump being silently reverted.
_PRE_ORCHESTRATION_PROMPT_VERSION = 2
_PRE_ORCHESTRATION_POLICY_VERSION = 2
_PRE_ORCHESTRATION_ENGINE_VERSION = 1


def test_review_prompt_version_bumped_for_role_scoped_prompts() -> None:
    assert REVIEW_PROMPT_VERSION > _PRE_ORCHESTRATION_PROMPT_VERSION


def test_review_policy_version_bumped_for_new_acceptance_policies() -> None:
    assert REVIEW_POLICY_VERSION > _PRE_ORCHESTRATION_POLICY_VERSION


def test_review_engine_version_bumped_for_orchestration_architecture() -> None:
    assert REVIEW_ENGINE_VERSION > _PRE_ORCHESTRATION_ENGINE_VERSION


# Agent Orchestration v1 itself introduced no new/changed repository-
# controlled `.patchfrog.yml` fields, so CONFIG_SCHEMA_VERSION was NOT
# bumped for it (spec section 14: "Do not bump CONFIG_SCHEMA_VERSION
# unless repository config semantics actually change") -- it stayed at
# the Milestone C value (3) through the end of this milestone. A later
# milestone (Quality + Cost Guard) legitimately bumped it again for an
# unrelated reason; see tests/unit/test_review_quality_cost_guard_versioning.py
# for that pin. The historical "still 3" pin that used to live here is
# retired rather than kept permanently false.


def test_pre_orchestration_model_identity_never_reused_post_orchestration() -> None:
    """The exact canonical-run-identity invariant this bump protects:
    a run fingerprinted under the old prompt/policy/engine versions must
    produce a different ReviewModelIdentity fingerprint than a run under
    the current (orchestration) versions, for otherwise-identical
    provider/model."""

    pre_orchestration = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=_PRE_ORCHESTRATION_PROMPT_VERSION,
        policy_version=_PRE_ORCHESTRATION_POLICY_VERSION,
        engine_version=_PRE_ORCHESTRATION_ENGINE_VERSION,
    )
    current = ReviewModelIdentity(
        reviewer_provider="anthropic", reviewer_model="claude-opus-5",
        critic_provider="anthropic", critic_model="claude-opus-5",
        prompt_version=REVIEW_PROMPT_VERSION,
        policy_version=REVIEW_POLICY_VERSION,
        engine_version=REVIEW_ENGINE_VERSION,
    )
    assert pre_orchestration.fingerprint() != current.fingerprint()
