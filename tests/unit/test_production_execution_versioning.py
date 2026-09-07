"""Pins the exact version decisions Milestone S6 (Production Execution
Enablement) made -- same pattern as
tests/unit/test_executable_verification_versioning.py. S6 introduces
exactly one new version constant (VERIFIER_PROTOCOL_VERSION, a genuinely
new, independently-versioned wire contract) and changes none of the
others -- see validation/production_execution/latest-summary.md section 9
for the full reasoning behind each."""

from __future__ import annotations

from patchfrog.executable_verification.domain import EXECUTABLE_VERIFICATION_VERSION
from patchfrog.executable_verification.protocol import VERIFIER_PROTOCOL_VERSION
from patchfrog.review.config import (
    QUALITY_COST_POLICY_VERSION,
    REVIEW_ENGINE_VERSION,
    REVIEW_POLICY_VERSION,
    REVIEW_PROMPT_VERSION,
)
from patchfrog.telemetry.domain import TELEMETRY_SCHEMA_VERSION

#: The exact pre-S6 versions (Milestone S security-correction baseline,
#: main @ 4f37ac5) -- frozen here as a fixed comparison point.
_PRE_S6_EXECUTABLE_VERIFICATION_VERSION = 1
_PRE_S6_PROMPT_VERSION = 13
_PRE_S6_TELEMETRY_SCHEMA_VERSION = 11
_PRE_S6_QUALITY_COST_POLICY_VERSION = 4
_PRE_S6_ENGINE_VERSION = 3
_PRE_S6_POLICY_VERSION = 4


def test_verifier_protocol_version_introduced() -> None:
    assert VERIFIER_PROTOCOL_VERSION == 1


def test_executable_verification_version_unchanged() -> None:
    """The execution/report semantic contract is untouched -- S6 only
    changes which process calls execute_against_snapshot, never what a
    VerificationOutcome asserts."""

    assert EXECUTABLE_VERIFICATION_VERSION == _PRE_S6_EXECUTABLE_VERIFICATION_VERSION


def test_review_prompt_version_unchanged() -> None:
    assert REVIEW_PROMPT_VERSION == _PRE_S6_PROMPT_VERSION


def test_telemetry_schema_version_unchanged() -> None:
    assert TELEMETRY_SCHEMA_VERSION == _PRE_S6_TELEMETRY_SCHEMA_VERSION


def test_quality_cost_policy_version_unchanged() -> None:
    assert QUALITY_COST_POLICY_VERSION == _PRE_S6_QUALITY_COST_POLICY_VERSION


def test_review_engine_version_unchanged() -> None:
    assert REVIEW_ENGINE_VERSION == _PRE_S6_ENGINE_VERSION


def test_review_policy_version_unchanged() -> None:
    assert REVIEW_POLICY_VERSION == _PRE_S6_POLICY_VERSION
