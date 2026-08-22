"""Evidence, location, and taxonomy validation for raw reviewer output.

Nothing the provider returns is trusted until it passes here.
``output_config.format`` already guarantees the response is
schema-shaped JSON, but schema conformance says nothing about whether the
content is *true* -- a model can return perfectly well-formed JSON that
quotes text nobody wrote, points at a file it was never shown, or
misclassifies severity. This module is the deterministic gate between
"the provider proposed X" and "X is even eligible for the critic stage."

"The model may propose. PatchFrog decides what survives." -- this is
where that decision starts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.review.domain import (
    AIReviewFinding,
    ReviewEvidence,
    ValidatedFinding,
    ValidationOutcome,
)


class ResponseSchemaError(ValueError):
    """The raw provider response could not be parsed as the requested
    schema at all (malformed JSON, missing top-level ``findings`` key, or
    a finding entry too malformed to construct). Always a fatal,
    never-retried failure for the candidate that produced it -- retrying
    would just reproduce the same malformed response."""


def parse_findings(raw_json: str) -> list[AIReviewFinding]:
    """Parse the top-level structured response into
    :class:`~patchfrog.review.domain.AIReviewFinding` objects.

    Raises :class:`ResponseSchemaError` if the JSON doesn't parse or the
    top-level shape is wrong. Individual malformed *finding entries*
    within an otherwise-valid response are skipped (recorded via
    :func:`parse_and_validate_response`'s per-finding outcome) rather than
    failing the whole batch -- one bad entry among several good ones
    should not discard the good ones.
    """

    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ResponseSchemaError(f"response was not valid JSON: {exc}") from exc

    if not isinstance(payload, dict) or "findings" not in payload:
        raise ResponseSchemaError("response JSON missing top-level 'findings' array")

    raw_findings = payload["findings"]
    if not isinstance(raw_findings, list):
        raise ResponseSchemaError("'findings' was not an array")

    findings: list[AIReviewFinding] = []
    for entry in raw_findings:
        parsed = _parse_one_finding(entry)
        if parsed is not None:
            findings.append(parsed)
    return findings


def _parse_one_finding(entry: object) -> AIReviewFinding | None:
    if not isinstance(entry, dict):
        return None
    try:
        evidence = tuple(
            ReviewEvidence(
                file_path=str(e["file_path"]),
                start_line=int(e["start_line"]),
                end_line=int(e["end_line"]),
                quoted_text=str(e["quoted_text"]),
            )
            for e in entry.get("evidence", [])
            if isinstance(e, dict)
        )
        return AIReviewFinding(
            title=str(entry["title"]),
            message=str(entry["message"]),
            category=FindingCategory(entry["category"]),
            severity=Severity(entry["severity"]),
            confidence=Confidence(entry["confidence"]),
            file_path=str(entry["file_path"]),
            start_line=int(entry["start_line"]),
            end_line=int(entry["end_line"]),
            evidence=evidence,
            reasoning_summary=str(entry.get("reasoning_summary", "")),
            suggested_fix=(str(entry["suggested_fix"]) if entry.get("suggested_fix") else None),
            impact=(str(entry["impact"]) if entry.get("impact") else None),
        )
    except (KeyError, ValueError, TypeError):
        return None


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """Everything a finding is checked against -- the *exact* text that
    was actually sent to the provider, never a re-fetched or re-rendered
    copy, so a finding can only survive on evidence that provably reached
    the model."""

    allowed_file_paths: frozenset[str]
    context_text: str
    diff_excerpt: str


def validate_finding(finding: AIReviewFinding, *, context: ValidationContext) -> ValidatedFinding:
    """Deterministically validate one parsed finding.

    Order matters only for the ``detail`` message returned; a finding
    failing multiple checks still gets exactly one (the first-encountered)
    :class:`~patchfrog.review.domain.ValidationOutcome`.
    """

    if finding.start_line <= 0 or finding.end_line <= 0 or finding.start_line > finding.end_line:
        return ValidatedFinding(
            finding=finding,
            outcome=ValidationOutcome.HALLUCINATED_LOCATION,
            detail=f"invalid line range {finding.start_line}-{finding.end_line}",
        )

    if finding.file_path not in context.allowed_file_paths:
        return ValidatedFinding(
            finding=finding,
            outcome=ValidationOutcome.OUT_OF_SCOPE,
            detail=f"file_path {finding.file_path!r} was never shown to the model",
        )

    if not finding.message.strip():
        return ValidatedFinding(
            finding=finding, outcome=ValidationOutcome.INCOMPLETE_ANALYSIS, detail="message (identification) was empty"
        )

    if not finding.reasoning_summary.strip():
        return ValidatedFinding(
            finding=finding, outcome=ValidationOutcome.INCOMPLETE_ANALYSIS,
            detail="reasoning_summary (root cause) was empty",
        )

    if not finding.evidence:
        return ValidatedFinding(
            finding=finding, outcome=ValidationOutcome.HALLUCINATED_EVIDENCE, detail="no evidence supplied"
        )

    haystack = context.context_text + "\n" + context.diff_excerpt
    for e in finding.evidence:
        if e.file_path not in context.allowed_file_paths:
            return ValidatedFinding(
                finding=finding,
                outcome=ValidationOutcome.OUT_OF_SCOPE,
                detail=f"evidence file_path {e.file_path!r} was never shown to the model",
            )
        quoted = e.quoted_text.strip()
        if not quoted or quoted not in haystack:
            return ValidatedFinding(
                finding=finding,
                outcome=ValidationOutcome.HALLUCINATED_EVIDENCE,
                detail=f"quoted evidence does not appear verbatim in the shown context: {quoted[:120]!r}",
            )

    return ValidatedFinding(finding=finding, outcome=ValidationOutcome.VALID, detail="")


def parse_and_validate_response(
    raw_json: str, *, context: ValidationContext
) -> list[ValidatedFinding]:
    """Parse a raw provider response and validate every finding entry.

    Raises :class:`ResponseSchemaError` only for a top-level parse
    failure -- individual finding-level failures are returned as
    non-``VALID`` :class:`~patchfrog.review.domain.ValidatedFinding`
    entries so they can be persisted for audit rather than silently
    dropped.
    """

    findings = parse_findings(raw_json)
    return [validate_finding(f, context=context) for f in findings]
