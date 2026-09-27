"""M9.8: evidence/patch integrity check -- a `MigrationEvidenceBundle`
computed for one upstream change / patch / repository state must never be
silently treated as evidence for a different one."""

from __future__ import annotations

import pytest

from patchfrog.migration.domain import (
    GeneratedPatch,
    PatchOrigin,
    SafetyGateResult,
    StepOutcome,
    StepResult,
)
from patchfrog.migration_pr.integrity import MigrationPRIntegrityError, verify_evidence_integrity
from patchfrog.migration_verification.domain import (
    EvidenceStrength,
    MigrationEvidenceBundle,
    MigrationVerificationPlan,
    ResidualRisk,
    VerificationCoverage,
    VerificationOutcome,
)
from patchfrog.migration_verification.evidence import build_evidence_bundle
from patchfrog.upstream.domain import ExternalChangeEvent
from patchfrog.upstream.events import build_contract_change, load_contract_file
from patchfrog.upstream.hints import EMPTY_HINTS
from tests.support.upstream_cases import CASES_ROOT

DEMO_VERIFIED = CASES_ROOT / "demo_verified"


def _event() -> ExternalChangeEvent:
    return build_contract_change(
        load_contract_file(DEMO_VERIFIED / "old.yaml"), load_contract_file(DEMO_VERIFIED / "new.yaml"),
        hints=EMPTY_HINTS,
    )


def _patch(unified_diff: str = "diff") -> GeneratedPatch:
    return GeneratedPatch(
        origin=PatchOrigin.DETERMINISTIC, unified_diff=unified_diff, modified_files=("app/x.py",),
        new_contents={"app/x.py": "content"}, step_results=(StepResult("s1", StepOutcome.APPLIED, "d"),),
        safety=(SafetyGateResult("g", True, "ok"),),
    )


def _bundle(*, change_fingerprint: str, patch_fingerprint: str, repository_head_sha: str) -> MigrationEvidenceBundle:
    plan = MigrationVerificationPlan(change_fingerprint, patch_fingerprint, (), (), ())
    coverage = VerificationCoverage((), (), (), (), ())
    return build_evidence_bundle(
        plan=plan, repository="demo", repository_head_sha=repository_head_sha, step_evidence=(), contract_checks=(),
        baseline_comparisons=(), coverage=coverage, evidence_strength=EvidenceStrength.NONE,
        residual_risk=ResidualRisk.LOW, outcome=VerificationOutcome.VERIFIED, outcome_reasons=("ok",),
    )


def test_matching_evidence_passes() -> None:
    event = _event()
    patch = _patch()
    bundle = _bundle(change_fingerprint=event.fingerprint, patch_fingerprint=patch.fingerprint, repository_head_sha="deadbeef")
    verify_evidence_integrity(event=event, patch=patch, bundle=bundle, base_commit_sha="deadbeef")


def test_mismatched_change_fingerprint_raises() -> None:
    event = _event()
    patch = _patch()
    bundle = _bundle(change_fingerprint="wrong" * 12, patch_fingerprint=patch.fingerprint, repository_head_sha="deadbeef")
    with pytest.raises(MigrationPRIntegrityError, match="upstream change"):
        verify_evidence_integrity(event=event, patch=patch, bundle=bundle, base_commit_sha="deadbeef")


def test_mismatched_patch_fingerprint_raises() -> None:
    event = _event()
    patch = _patch()
    bundle = _bundle(change_fingerprint=event.fingerprint, patch_fingerprint="wrong" * 12, repository_head_sha="deadbeef")
    with pytest.raises(MigrationPRIntegrityError, match="patch"):
        verify_evidence_integrity(event=event, patch=patch, bundle=bundle, base_commit_sha="deadbeef")


def test_evidence_bundle_for_one_patch_is_never_reused_for_another() -> None:
    event = _event()
    patch_a = _patch("diff a")
    patch_b = _patch("diff b")
    assert patch_a.fingerprint != patch_b.fingerprint
    bundle_for_a = _bundle(change_fingerprint=event.fingerprint, patch_fingerprint=patch_a.fingerprint, repository_head_sha="deadbeef")
    with pytest.raises(MigrationPRIntegrityError):
        verify_evidence_integrity(event=event, patch=patch_b, bundle=bundle_for_a, base_commit_sha="deadbeef")


def test_mismatched_repository_head_sha_raises() -> None:
    event = _event()
    patch = _patch()
    bundle = _bundle(change_fingerprint=event.fingerprint, patch_fingerprint=patch.fingerprint, repository_head_sha="old-sha")
    with pytest.raises(MigrationPRIntegrityError, match="repository head"):
        verify_evidence_integrity(event=event, patch=patch, bundle=bundle, base_commit_sha="new-sha")


def test_no_patch_requires_empty_patch_fingerprint() -> None:
    event = _event()
    bundle = _bundle(change_fingerprint=event.fingerprint, patch_fingerprint="", repository_head_sha="deadbeef")
    verify_evidence_integrity(event=event, patch=None, bundle=bundle, base_commit_sha="deadbeef")


def test_no_patch_but_bundle_claims_a_patch_raises() -> None:
    event = _event()
    bundle = _bundle(change_fingerprint=event.fingerprint, patch_fingerprint="somepatch" * 8, repository_head_sha="deadbeef")
    with pytest.raises(MigrationPRIntegrityError, match="patch"):
        verify_evidence_integrity(event=event, patch=None, bundle=bundle, base_commit_sha="deadbeef")
