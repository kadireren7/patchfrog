from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from patchfrog.analysis.analyzers.cppcheck import classify_error_id, parse_cppcheck_output
from patchfrog.analysis.domain import FindingCategory, Severity

FIXTURES = Path(__file__).parent.parent / "fixtures" / "analyzer_output"


def test_parses_stored_cppcheck_xml_fixture() -> None:
    xml_text = (FIXTURES / "cppcheck_output.xml").read_text()

    findings = parse_cppcheck_output(xml_text, checkout_path=Path("/repo"))

    # missingIncludeSystem has no <location> and must be skipped, not crash.
    assert len(findings) == 3

    null_pointer = findings[0]
    assert null_pointer.rule_id == "nullPointer"
    assert null_pointer.file_path == "src/cache.c"
    assert null_pointer.span.start_line == 42
    assert null_pointer.severity is Severity.HIGH
    assert null_pointer.category is FindingCategory.MEMORY_SAFETY
    assert null_pointer.raw_metadata["cwe"] == "476"

    style_finding = findings[2]
    assert style_finding.rule_id == "unusedFunction"
    assert style_finding.severity is Severity.INFO


def test_empty_output_returns_no_findings() -> None:
    assert parse_cppcheck_output("", checkout_path=Path("/repo")) == []


def test_malformed_xml_raises_for_caller_to_handle() -> None:
    with pytest.raises(ET.ParseError):
        parse_cppcheck_output("<not><valid", checkout_path=Path("/repo"))


@pytest.mark.parametrize(
    ("error_id", "expected_category"),
    [
        ("nullPointer", FindingCategory.MEMORY_SAFETY),
        ("memleak", FindingCategory.RESOURCE_MANAGEMENT),
        ("bufferAccessOutOfBounds", FindingCategory.MEMORY_SAFETY),
        ("doubleFree", FindingCategory.MEMORY_SAFETY),
        ("uninitvar", FindingCategory.MEMORY_SAFETY),
        ("unusedFunction", FindingCategory.STYLE),
        ("somethingCompletelyUnmapped", FindingCategory.UNKNOWN),
    ],
)
def test_classify_error_id_mapping(error_id: str, expected_category: FindingCategory) -> None:
    assert classify_error_id(error_id) is expected_category
