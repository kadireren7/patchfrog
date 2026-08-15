from __future__ import annotations

from patchfrog.analysis.domain import Confidence, FindingCategory, RawFinding, Severity
from patchfrog.analysis.fingerprint import compute_fingerprint
from patchfrog.domain.code import SourceSpan


def _finding(**overrides: object) -> RawFinding:
    defaults: dict[str, object] = {
        "rule_id": "F401",
        "category": FindingCategory.CORRECTNESS,
        "title": "unused-import",
        "message": "`os` imported but unused",
        "severity": Severity.MEDIUM,
        "confidence": Confidence.HIGH,
        "file_path": "src/cache.py",
        "span": SourceSpan(start_line=10, end_line=10, start_column=1, end_column=5),
        "source_analyzer": "ruff",
    }
    defaults.update(overrides)
    return RawFinding(**defaults)  # type: ignore[arg-type]


def test_identical_finding_produces_identical_fingerprint() -> None:
    a = compute_fingerprint(_finding(), symbol_qualified_name="Cache.get")
    b = compute_fingerprint(_finding(), symbol_qualified_name="Cache.get")
    assert a == b


def test_different_rule_id_produces_different_fingerprint() -> None:
    a = compute_fingerprint(_finding(rule_id="F401"), symbol_qualified_name=None)
    b = compute_fingerprint(_finding(rule_id="F841"), symbol_qualified_name=None)
    assert a != b


def test_different_file_produces_different_fingerprint() -> None:
    a = compute_fingerprint(_finding(file_path="a.py"), symbol_qualified_name=None)
    b = compute_fingerprint(_finding(file_path="b.py"), symbol_qualified_name=None)
    assert a != b


def test_different_analyzer_produces_different_fingerprint() -> None:
    a = compute_fingerprint(_finding(source_analyzer="ruff"), symbol_qualified_name=None)
    b = compute_fingerprint(_finding(source_analyzer="semgrep"), symbol_qualified_name=None)
    assert a != b


def test_symbol_anchored_fingerprint_survives_line_shift_within_same_symbol() -> None:
    """A finding that moves a few lines within the same containing symbol
    (e.g. because unrelated code was inserted above it) keeps the same
    fingerprint when a symbol anchor is available."""

    at_line_10 = compute_fingerprint(
        _finding(span=SourceSpan(10, 10, 1, 5)), symbol_qualified_name="Cache.get"
    )
    at_line_14 = compute_fingerprint(
        _finding(span=SourceSpan(14, 14, 1, 5)), symbol_qualified_name="Cache.get"
    )
    assert at_line_10 == at_line_14


def test_without_symbol_anchor_a_line_shift_changes_the_fingerprint() -> None:
    """Without symbol context to anchor to, the exact line is the only
    available identity — a genuine limitation, not a bug: this documents
    the expected (not ideal) behavior for module-level findings."""

    at_line_10 = compute_fingerprint(_finding(span=SourceSpan(10, 10, 1, 5)), symbol_qualified_name=None)
    at_line_14 = compute_fingerprint(_finding(span=SourceSpan(14, 14, 1, 5)), symbol_qualified_name=None)
    assert at_line_10 != at_line_14


def test_moving_to_a_different_symbol_changes_the_fingerprint() -> None:
    a = compute_fingerprint(_finding(), symbol_qualified_name="Cache.get")
    b = compute_fingerprint(_finding(), symbol_qualified_name="Cache.set")
    assert a != b


def test_unrelated_finding_elsewhere_does_not_affect_this_ones_fingerprint() -> None:
    """A change to a completely different, unrelated finding must never
    alter this finding's fingerprint — fingerprints are computed purely
    from a finding's own fields, with no cross-finding state."""

    before = compute_fingerprint(_finding(), symbol_qualified_name="Cache.get")
    # Simulate "unrelated finding elsewhere" by computing a second,
    # unrelated fingerprint in between -- must not perturb globals/caches.
    _ = compute_fingerprint(_finding(file_path="unrelated.py", rule_id="X999"), symbol_qualified_name="Other.thing")
    after = compute_fingerprint(_finding(), symbol_qualified_name="Cache.get")
    assert before == after
