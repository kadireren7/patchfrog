"""Unit tests for :mod:`patchfrog.intent_verification.extraction` -- the
deterministic sufficiency gate (spec section 5) and claim extraction
(spec sections 2/3/6/7). No I/O, no database, no LLM."""

from __future__ import annotations

from patchfrog.intent_verification.domain import IntentSourceKind, IntentStrength
from patchfrog.intent_verification.extraction import (
    extract_claims_from_pr_metadata,
    is_intent_evidence_sufficient,
    normalize_intent_text,
)


def test_spec_sufficient_examples_pass() -> None:
    assert is_intent_evidence_sufficient("Prevent duplicate webhook processing")
    assert is_intent_evidence_sufficient(
        "Retries currently create a second payment. This PR should make payment creation idempotent."
    )
    assert is_intent_evidence_sufficient("Reject expired sessions after logout")
    assert is_intent_evidence_sufficient("Allow reconnect attempts to use configurable retry limits")


def test_spec_insufficient_examples_fail() -> None:
    for text in ("fix", "cleanup", "changes", "WIP", "refactor stuff", "try again"):
        assert not is_intent_evidence_sufficient(text), text


def test_empty_and_short_text_insufficient() -> None:
    assert not is_intent_evidence_sufficient("")
    assert not is_intent_evidence_sufficient("   ")
    assert not is_intent_evidence_sufficient("Fix bug")
    assert not is_intent_evidence_sufficient("update stuff")


def test_case_insensitive_placeholder_match() -> None:
    assert not is_intent_evidence_sufficient("WIP")
    assert not is_intent_evidence_sufficient("Wip")
    assert not is_intent_evidence_sufficient("CLEANUP")


def test_normalize_collapses_whitespace_and_bounds_length() -> None:
    assert normalize_intent_text("  Prevent   duplicate\n\nwebhook   processing  ") == (
        "Prevent duplicate webhook processing"
    )
    long_text = "Prevent " + "x" * 1000
    assert len(normalize_intent_text(long_text)) <= 500


def test_extract_claims_title_and_body_both_sufficient() -> None:
    claims = extract_claims_from_pr_metadata(
        title="Prevent duplicate webhook processing",
        body="Retries currently create a second payment.",
    )
    assert len(claims) == 2
    assert claims[0].source.source_kind is IntentSourceKind.PR_TITLE
    assert claims[1].source.source_kind is IntentSourceKind.PR_BODY
    assert all(c.strength is IntentStrength.EXPLICIT for c in claims)


def test_extract_claims_vague_title_sufficient_body() -> None:
    """Spec section 29 case 13: "Meaningful body, vague title -> body may
    establish usable intent"."""

    claims = extract_claims_from_pr_metadata(
        title="fix stuff", body="This PR should make payment creation idempotent to prevent duplicates."
    )
    assert len(claims) == 1
    assert claims[0].source.source_kind is IntentSourceKind.PR_BODY


def test_extract_claims_no_body_meaningful_title_only() -> None:
    """Spec section 29 case 12: "No PR body, meaningful title only ->
    still usable"."""

    claims = extract_claims_from_pr_metadata(title="Prevent duplicate webhook processing", body=None)
    assert len(claims) == 1
    assert claims[0].source.source_kind is IntentSourceKind.PR_TITLE


def test_extract_claims_no_metadata_is_a_no_op() -> None:
    """Spec section 29 case 14: "Metadata absent -> no-op"."""

    assert extract_claims_from_pr_metadata(title=None, body=None) == ()
    assert extract_claims_from_pr_metadata(title="", body="") == ()


def test_extract_claims_both_vague_produces_nothing() -> None:
    assert extract_claims_from_pr_metadata(title="fix stuff", body="WIP") == ()


def test_claim_id_is_deterministic() -> None:
    claims1 = extract_claims_from_pr_metadata(title="Prevent duplicate webhook processing", body=None)
    claims2 = extract_claims_from_pr_metadata(title="Prevent duplicate webhook processing", body=None)
    assert claims1[0].id == claims2[0].id


def test_claim_normalized_statement_is_never_a_paraphrase() -> None:
    """Spec section 6: preserving the sanitized explicit statement
    verbatim is an acceptable, not a fallback, design."""

    claims = extract_claims_from_pr_metadata(title="Prevent duplicate webhook processing", body=None)
    assert claims[0].normalized_statement == "Prevent duplicate webhook processing"
