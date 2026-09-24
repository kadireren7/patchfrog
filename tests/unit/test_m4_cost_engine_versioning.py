"""Pins the exact version decisions of M4 (Ultra-Low-Cost Review Engine)
-- same pattern as the other ``test_*_versioning.py`` files. See
validation/m4_m5_cost_engine_dependency_discovery/latest-summary.md."""

from __future__ import annotations

from patchfrog.change_risk import CHANGE_RISK_POLICY_VERSION
from patchfrog.review.config import (
    CONFIG_SCHEMA_VERSION,
    QUALITY_COST_POLICY_VERSION,
    REVIEW_ENGINE_VERSION,
    REVIEW_POLICY_VERSION,
    REVIEW_PROMPT_VERSION,
)
from patchfrog.review.cost_policy import REVIEW_COST_POLICY_VERSION
from patchfrog.telemetry.domain import TELEMETRY_SCHEMA_VERSION

_PRE_M4_PROMPT_VERSION = 13
_PRE_M4_TELEMETRY_SCHEMA_VERSION = 11


def test_new_m4_versions_introduced() -> None:
    assert CHANGE_RISK_POLICY_VERSION == 1
    assert REVIEW_COST_POLICY_VERSION == 1


def test_prompt_version_bumped_for_the_single_pass_prompt() -> None:
    assert REVIEW_PROMPT_VERSION == _PRE_M4_PROMPT_VERSION + 1


def test_telemetry_schema_bumped_for_the_cost_section() -> None:
    assert TELEMETRY_SCHEMA_VERSION == _PRE_M4_TELEMETRY_SCHEMA_VERSION + 1


def test_deliberate_non_bumps() -> None:
    # Engine: cost-aware identity is carried by the cost-policy fingerprint.
    assert REVIEW_ENGINE_VERSION == 3
    # Validation/critic/confidence survival rules are untouched.
    assert REVIEW_POLICY_VERSION == 4
    # Per-candidate effort tiering is untouched.
    assert QUALITY_COST_POLICY_VERSION == 4
    # No new .patchfrog.yml field: cost policy is operator-only.
    assert CONFIG_SCHEMA_VERSION == 5
