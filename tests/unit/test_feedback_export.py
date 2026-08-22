"""Unit coverage for :mod:`patchfrog.feedback.export`'s redaction rules
(Phase 9 spec section 27): no actor login, no reply body, no raw
evidence/source text is ever present in an export record -- only a hash
of the evidence and plain feedback counts/signals."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.feedback.assessment import compute_finding_assessment
from patchfrog.feedback.export import _record_for_summary
from patchfrog.persistence.models.review import AIFindingModel


def _finding_model(finding_id: uuid.UUID) -> AIFindingModel:
    model = AIFindingModel(
        id=finding_id,
        review_run_id=uuid.uuid4(),
        proposal_id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        title="t",
        message="the secret is interpolated into the log line",
        category=FindingCategory.SECURITY,
        severity=Severity.HIGH,
        confidence=Confidence.HIGH,
        file_path="a.py",
        start_line=1,
        end_line=1,
        evidence="[\"secret_value_that_must_never_leak\"]",
        reasoning_summary="r",
    )
    return model


def test_export_record_never_contains_raw_evidence_text() -> None:
    finding_id = uuid.uuid4()
    summary = compute_finding_assessment(finding_id, [])
    record = _record_for_summary(summary, finding=_finding_model(finding_id), reply_bodies=None)
    dumped = repr(record)
    assert "secret_value_that_must_never_leak" not in dumped
    assert "evidence_hash" in record
    assert record["evidence_hash"] != "secret_value_that_must_never_leak"


def test_export_record_never_contains_an_actor_login_field() -> None:
    finding_id = uuid.uuid4()
    summary = compute_finding_assessment(finding_id, [])
    record = _record_for_summary(summary, finding=_finding_model(finding_id), reply_bodies=None)
    assert "actor" not in record
    assert "login" not in record
    assert "username" not in record


def test_reply_bodies_absent_by_default() -> None:
    finding_id = uuid.uuid4()
    summary = compute_finding_assessment(finding_id, [])
    record = _record_for_summary(summary, finding=_finding_model(finding_id), reply_bodies=None)
    assert "reply_bodies" not in record


def test_finding_metadata_missing_is_represented_as_none_not_an_error() -> None:
    finding_id = uuid.uuid4()
    summary = compute_finding_assessment(finding_id, [])
    record = _record_for_summary(summary, finding=None, reply_bodies=None)
    assert record["category"] is None
    assert record["severity"] is None
    assert record["evidence_hash"] is None
