from __future__ import annotations

import json

import pytest

from patchfrog.review.domain import ValidationOutcome
from patchfrog.review.validation import (
    ResponseSchemaError,
    ValidationContext,
    parse_and_validate_response,
    parse_findings,
)

_VALID_RESPONSE = json.dumps(
    {
        "findings": [
            {
                "title": "Inverted comparison",
                "message": "amount >= balance allows withdrawing more than the balance.",
                "category": "correctness",
                "severity": "high",
                "confidence": "high",
                "file_path": "src/billing.py",
                "start_line": 2,
                "end_line": 3,
                "evidence": [
                    {
                        "file_path": "src/billing.py",
                        "start_line": 3,
                        "end_line": 3,
                        "quoted_text": "return amount >= balance",
                    }
                ],
                "reasoning_summary": "The comparison is backwards.",
                "suggested_fix": "return balance >= amount",
            }
        ]
    }
)

_CONTEXT_TEXT = "# src/billing.py\ndef can_withdraw(balance, amount):\n    return amount >= balance\n"


def _context() -> ValidationContext:
    return ValidationContext(
        allowed_file_paths=frozenset({"src/billing.py"}), context_text=_CONTEXT_TEXT, diff_excerpt=""
    )


def test_valid_finding_passes_validation() -> None:
    results = parse_and_validate_response(_VALID_RESPONSE, context=_context())
    assert len(results) == 1
    assert results[0].outcome == ValidationOutcome.VALID


def test_empty_findings_array_is_valid() -> None:
    results = parse_and_validate_response(json.dumps({"findings": []}), context=_context())
    assert results == []


def test_malformed_json_raises_schema_error() -> None:
    with pytest.raises(ResponseSchemaError):
        parse_findings("{not valid json")


def test_missing_findings_key_raises_schema_error() -> None:
    with pytest.raises(ResponseSchemaError):
        parse_findings(json.dumps({"wrong_key": []}))


def test_findings_not_a_list_raises_schema_error() -> None:
    with pytest.raises(ResponseSchemaError):
        parse_findings(json.dumps({"findings": "not a list"}))


def test_malformed_individual_finding_is_skipped_not_fatal() -> None:
    payload = {
        "findings": [
            {"title": "missing required fields"},
            json.loads(_VALID_RESPONSE)["findings"][0],
        ]
    }
    findings = parse_findings(json.dumps(payload))
    assert len(findings) == 1  # the malformed entry is silently skipped, not fatal


def test_out_of_scope_file_path_is_rejected() -> None:
    payload = json.loads(_VALID_RESPONSE)
    payload["findings"][0]["file_path"] = "src/other_file_never_shown.py"
    payload["findings"][0]["evidence"][0]["file_path"] = "src/other_file_never_shown.py"
    results = parse_and_validate_response(json.dumps(payload), context=_context())
    assert results[0].outcome == ValidationOutcome.OUT_OF_SCOPE


def test_hallucinated_evidence_is_rejected() -> None:
    payload = json.loads(_VALID_RESPONSE)
    payload["findings"][0]["evidence"][0]["quoted_text"] = "this text was never in the context at all"
    results = parse_and_validate_response(json.dumps(payload), context=_context())
    assert results[0].outcome == ValidationOutcome.HALLUCINATED_EVIDENCE


def test_no_evidence_is_rejected() -> None:
    payload = json.loads(_VALID_RESPONSE)
    payload["findings"][0]["evidence"] = []
    results = parse_and_validate_response(json.dumps(payload), context=_context())
    assert results[0].outcome == ValidationOutcome.HALLUCINATED_EVIDENCE


def test_invalid_line_range_is_rejected() -> None:
    payload = json.loads(_VALID_RESPONSE)
    payload["findings"][0]["start_line"] = 10
    payload["findings"][0]["end_line"] = 3
    results = parse_and_validate_response(json.dumps(payload), context=_context())
    assert results[0].outcome == ValidationOutcome.HALLUCINATED_LOCATION


def test_zero_line_is_rejected() -> None:
    payload = json.loads(_VALID_RESPONSE)
    payload["findings"][0]["start_line"] = 0
    payload["findings"][0]["end_line"] = 0
    results = parse_and_validate_response(json.dumps(payload), context=_context())
    assert results[0].outcome == ValidationOutcome.HALLUCINATED_LOCATION


def test_evidence_quoted_from_diff_excerpt_is_accepted() -> None:
    payload = json.loads(_VALID_RESPONSE)
    payload["findings"][0]["evidence"][0]["quoted_text"] = "+3: return amount >= balance"
    context = ValidationContext(
        allowed_file_paths=frozenset({"src/billing.py"}),
        context_text="unrelated context, no match here",
        diff_excerpt="+3: return amount >= balance",
    )
    results = parse_and_validate_response(json.dumps(payload), context=context)
    assert results[0].outcome == ValidationOutcome.VALID


def test_invalid_category_enum_drops_the_finding() -> None:
    payload = json.loads(_VALID_RESPONSE)
    payload["findings"][0]["category"] = "not_a_real_category"
    findings = parse_findings(json.dumps(payload))
    assert findings == []
