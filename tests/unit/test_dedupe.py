from __future__ import annotations

from patchfrog.analysis.dedupe import deduplicate
from patchfrog.analysis.domain import Confidence, FindingCategory, RawFinding, Severity
from patchfrog.analysis.enrichment import EnrichedFinding
from patchfrog.domain.code import SourceSpan

_DEFAULT_SPAN = SourceSpan(10, 10, 1, 5)


def _enriched(
    *,
    analyzer: str,
    rule_id: str = "R1",
    category: FindingCategory = FindingCategory.CORRECTNESS,
    file_path: str = "a.py",
    span: SourceSpan = _DEFAULT_SPAN,
    symbol: str | None = None,
    severity: Severity = Severity.MEDIUM,
) -> EnrichedFinding:
    finding = RawFinding(
        rule_id=rule_id, category=category, title=rule_id, message="msg", severity=severity,
        confidence=Confidence.MEDIUM, file_path=file_path, span=span, source_analyzer=analyzer,
    )
    return EnrichedFinding(
        finding=finding, fingerprint=f"{analyzer}:{rule_id}:{file_path}:{span.start_line}",
        symbol_id=None, symbol_name=None, symbol_qualified_name=symbol,
        is_on_changed_line=False, is_in_changed_symbol=False,
    )


def test_same_analyzer_same_rule_same_location_merges() -> None:
    a = _enriched(analyzer="cppcheck", rule_id="nullPointer")
    b = _enriched(analyzer="cppcheck", rule_id="nullPointer")

    groups = deduplicate([a, b])

    assert len(groups) == 1
    assert len(groups[0].reported_by) == 2


def test_same_logical_finding_from_two_analyzers_merges() -> None:
    cppcheck_finding = _enriched(
        analyzer="cppcheck", rule_id="nullPointer", category=FindingCategory.MEMORY_SAFETY,
        span=SourceSpan(90, 91, 1, 5), symbol="cache_insert",
    )
    semgrep_finding = _enriched(
        analyzer="semgrep", rule_id="unchecked-nullable-return", category=FindingCategory.MEMORY_SAFETY,
        span=SourceSpan(91, 91, 1, 5), symbol="cache_insert",
    )

    groups = deduplicate([cppcheck_finding, semgrep_finding])

    assert len(groups) == 1
    assert set(groups[0].source_analyzers) == {"cppcheck", "semgrep"}


def test_same_line_but_unrelated_categories_do_not_merge() -> None:
    security_finding = _enriched(category=FindingCategory.SECURITY, analyzer="semgrep", span=SourceSpan(10, 10, 1, 5))
    style_finding = _enriched(category=FindingCategory.STYLE, analyzer="ruff", span=SourceSpan(10, 10, 1, 5))

    groups = deduplicate([security_finding, style_finding])

    assert len(groups) == 2


def test_overlapping_but_not_identical_ranges_merge() -> None:
    a = _enriched(analyzer="cppcheck", span=SourceSpan(10, 15, 1, 5))
    b = _enriched(analyzer="semgrep", span=SourceSpan(14, 20, 1, 5))

    groups = deduplicate([a, b])

    assert len(groups) == 1


def test_non_overlapping_ranges_in_same_file_and_category_do_not_merge() -> None:
    a = _enriched(analyzer="cppcheck", span=SourceSpan(10, 10, 1, 5))
    b = _enriched(analyzer="semgrep", span=SourceSpan(50, 50, 1, 5))

    groups = deduplicate([a, b])

    assert len(groups) == 2


def test_different_symbols_at_overlapping_line_ish_range_do_not_merge() -> None:
    a = _enriched(analyzer="cppcheck", span=SourceSpan(10, 10, 1, 5), symbol="FuncA")
    b = _enriched(analyzer="semgrep", span=SourceSpan(10, 10, 1, 5), symbol="FuncB")

    groups = deduplicate([a, b])

    assert len(groups) == 2


def test_same_rule_different_files_do_not_merge() -> None:
    a = _enriched(analyzer="ruff", rule_id="F401", file_path="a.py")
    b = _enriched(analyzer="ruff", rule_id="F401", file_path="b.py")

    groups = deduplicate([a, b])

    assert len(groups) == 2


def test_primary_pick_is_deterministic_by_severity_then_confidence_then_analyzer() -> None:
    low = _enriched(analyzer="cppcheck", severity=Severity.LOW)
    high = _enriched(analyzer="semgrep", severity=Severity.HIGH)

    groups = deduplicate([low, high])

    assert len(groups) == 1
    assert groups[0].primary.finding.source_analyzer == "semgrep"


def test_same_analyzer_different_rules_at_overlapping_location_do_not_merge() -> None:
    """Two genuinely distinct issues from one tool (e.g. ruff's F401 unused
    import and F811 redefinition on the same line) must not be silently
    collapsed into a single finding -- there's no independent second
    opinion corroborating a merge the way there is across analyzers."""

    unused_import = _enriched(analyzer="ruff", rule_id="F401", span=SourceSpan(10, 10, 1, 5))
    redefinition = _enriched(analyzer="ruff", rule_id="F811", span=SourceSpan(10, 10, 1, 5))

    groups = deduplicate([unused_import, redefinition])

    assert len(groups) == 2


def test_one_finding_with_symbol_one_without_can_still_merge() -> None:
    """A missing symbol on one side isn't itself disqualifying -- only an
    actual mismatch between two *known* symbols blocks a merge."""

    with_symbol = _enriched(analyzer="cppcheck", span=SourceSpan(10, 10, 1, 5), symbol="cache_insert")
    without_symbol = _enriched(analyzer="semgrep", span=SourceSpan(10, 10, 1, 5), symbol=None)

    groups = deduplicate([with_symbol, without_symbol])

    assert len(groups) == 1
