from __future__ import annotations

import uuid

from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason, TokenUsage


def _candidate(**overrides: object) -> ReviewCandidate:
    kwargs: dict[str, object] = {
        "file_path": "src/a.py",
        "symbol_id": None,
        "symbol_name": "f",
        "qualified_name": "f",
        "start_line": 1,
        "end_line": 10,
        "changed_lines": (2, 3),
        "static_finding_ids": (),
        "reason": ReviewCandidateReason.CHANGED_SYMBOL,
    }
    kwargs.update(overrides)
    return ReviewCandidate(**kwargs)  # type: ignore[arg-type]


def test_identical_candidates_have_identical_fingerprints() -> None:
    assert _candidate().fingerprint() == _candidate().fingerprint()


def test_different_file_path_changes_fingerprint() -> None:
    assert _candidate(file_path="a.py").fingerprint() != _candidate(file_path="b.py").fingerprint()


def test_different_symbol_id_changes_fingerprint() -> None:
    a = _candidate(symbol_id=uuid.uuid4())
    b = _candidate(symbol_id=uuid.uuid4())
    assert a.fingerprint() != b.fingerprint()


def test_fingerprint_excludes_changed_lines_and_reason() -> None:
    """Two candidates covering the same symbol span are the same logical
    unit of review even if diff churn changed which specific lines moved
    between requests -- changed_lines/reason are metadata, not identity."""

    a = _candidate(changed_lines=(2,), reason=ReviewCandidateReason.CHANGED_SYMBOL)
    b = _candidate(changed_lines=(2, 3, 4), reason=ReviewCandidateReason.STATIC_FINDING_EVIDENCE)
    assert a.fingerprint() == b.fingerprint()


def test_token_usage_addition() -> None:
    a = TokenUsage(input_tokens=10, output_tokens=5)
    b = TokenUsage(input_tokens=3, output_tokens=2)
    total = a + b
    assert total.input_tokens == 13
    assert total.output_tokens == 7
