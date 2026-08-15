"""Cppcheck adapter — C/C++ static analysis via structured XML output.

Cppcheck works directly on source files without needing a build system or
compilation database (unlike clang-tidy), so it's usable on essentially
any C/C++ checkout. Uses ``--xml`` (schema version 2), the most complete
structured output cppcheck offers.
"""

from __future__ import annotations

import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from patchfrog.analysis.analyzers.base import AnalyzerAvailability, AnalyzerDiscoveryResult
from patchfrog.analysis.domain import (
    AnalysisContext,
    AnalyzerCapabilities,
    AnalyzerExecutionStatus,
    AnalyzerResult,
    Confidence,
    FindingCategory,
    RawFinding,
    Severity,
)
from patchfrog.analysis.subprocess_sandbox import AnalyzerSubprocessError, run_sandboxed
from patchfrog.domain.code import Language, SourceSpan

_BINARY = "cppcheck"

#: cppcheck's own severities: error, warning, style, performance, portability,
#: information. Mapped conservatively — "error" is usually a real defect,
#: "style" is rarely more than a maintainability nit regardless of cppcheck
#: calling every one of these a flat "severity".
_SEVERITY_MAP = {
    "error": Severity.HIGH,
    "warning": Severity.MEDIUM,
    "performance": Severity.LOW,
    "portability": Severity.LOW,
    "style": Severity.INFO,
    "information": Severity.INFO,
}
_DEFAULT_SEVERITY = Severity.MEDIUM

#: cppcheck error IDs are the closest thing to a rule family; classify by
#: substring since IDs like "nullPointer"/"memleak"/"bufferAccessOutOfBounds"
#: are compound words, not a clean prefix scheme like ruff's.
_CATEGORY_KEYWORDS: tuple[tuple[str, FindingCategory], ...] = (
    ("null", FindingCategory.MEMORY_SAFETY),
    ("uninit", FindingCategory.MEMORY_SAFETY),
    ("memleak", FindingCategory.RESOURCE_MANAGEMENT),
    ("leak", FindingCategory.RESOURCE_MANAGEMENT),
    ("bufferaccessoutofbounds", FindingCategory.MEMORY_SAFETY),
    ("outofbounds", FindingCategory.MEMORY_SAFETY),
    ("overflow", FindingCategory.MEMORY_SAFETY),
    ("doublefree", FindingCategory.MEMORY_SAFETY),
    ("dealloc", FindingCategory.MEMORY_SAFETY),
    ("useafterfree", FindingCategory.MEMORY_SAFETY),
    ("race", FindingCategory.CONCURRENCY),
    ("deadlock", FindingCategory.CONCURRENCY),
    ("undefined", FindingCategory.UNDEFINED_BEHAVIOR),
    ("shift", FindingCategory.UNDEFINED_BEHAVIOR),
    ("signconversion", FindingCategory.CORRECTNESS),
    ("unused", FindingCategory.STYLE),  # unusedFunction, unusedVariable, unusedStructMember, ...
    ("style", FindingCategory.STYLE),
    ("performance", FindingCategory.PERFORMANCE),
    ("portability", FindingCategory.PORTABILITY),
)


def classify_error_id(error_id: str) -> FindingCategory:
    """Map a cppcheck error id (e.g. ``"nullPointer"``) to a normalized
    category by keyword. Standalone for direct unit testing."""

    lowered = error_id.lower()
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in lowered:
            return category
    return FindingCategory.UNKNOWN


class CppcheckAnalyzer:
    capabilities = AnalyzerCapabilities(
        name="cppcheck",
        languages=frozenset({Language.C, Language.CPP}),
        requires_compile_database=False,
        supports_file_scope=True,
        supports_repository_scope=True,
        supports_changed_files_only=True,
        supports_structured_output=True,
    )

    async def discover(self) -> AnalyzerDiscoveryResult:
        binary = shutil.which(_BINARY)
        if binary is None:
            return AnalyzerDiscoveryResult(
                availability=AnalyzerAvailability.UNAVAILABLE, reason="cppcheck binary not found on PATH"
            )
        try:
            result = await run_sandboxed([binary, "--version"], cwd=Path.cwd(), timeout_seconds=10)
        except AnalyzerSubprocessError as exc:
            return AnalyzerDiscoveryResult(availability=AnalyzerAvailability.UNAVAILABLE, reason=str(exc))
        if result.exit_code != 0:
            return AnalyzerDiscoveryResult(
                availability=AnalyzerAvailability.UNAVAILABLE, reason="cppcheck --version failed"
            )
        # "Cppcheck 2.13.0"
        version = result.stdout.strip().removeprefix("Cppcheck ")
        return AnalyzerDiscoveryResult(availability=AnalyzerAvailability.AVAILABLE, version=version)

    async def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        start = time.monotonic()
        discovery = await self.discover()
        if discovery.availability is AnalyzerAvailability.UNAVAILABLE:
            return AnalyzerResult(
                analyzer="cppcheck",
                version=None,
                status=AnalyzerExecutionStatus.UNSUPPORTED,
                duration_ms=0.0,
                error=discovery.reason,
            )
        version = discovery.version
        binary = shutil.which(_BINARY)
        assert binary is not None

        targets = _target_paths(context)
        if not targets:
            return AnalyzerResult(
                analyzer="cppcheck",
                version=version,
                status=AnalyzerExecutionStatus.SKIPPED,
                duration_ms=0.0,
                error="no C/C++ files in scope",
            )

        args = [binary, "--xml", "--xml-version=2", "--enable=warning,style,performance,portability", *targets]
        try:
            result = await run_sandboxed(
                args, cwd=context.checkout_path, timeout_seconds=context.config.timeout_seconds
            )
        except AnalyzerSubprocessError as exc:
            return AnalyzerResult(
                analyzer="cppcheck",
                version=version,
                status=AnalyzerExecutionStatus.FAILED,
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )

        duration_ms = (time.monotonic() - start) * 1000
        if result.timed_out:
            return AnalyzerResult(
                analyzer="cppcheck",
                version=version,
                status=AnalyzerExecutionStatus.TIMED_OUT,
                duration_ms=duration_ms,
            )
        # cppcheck writes its XML report to stderr, exit code 0 unless a
        # genuine internal failure (bad arguments, crash) occurred.
        if result.exit_code != 0:
            return AnalyzerResult(
                analyzer="cppcheck",
                version=version,
                status=AnalyzerExecutionStatus.FAILED,
                duration_ms=duration_ms,
                error=f"cppcheck exited {result.exit_code}: {result.stderr[:2000]}",
            )

        try:
            findings = parse_cppcheck_output(result.stderr, checkout_path=context.checkout_path)
        except ET.ParseError as exc:
            return AnalyzerResult(
                analyzer="cppcheck",
                version=version,
                status=AnalyzerExecutionStatus.FAILED,
                duration_ms=duration_ms,
                error=f"malformed cppcheck output: {exc}",
            )

        return AnalyzerResult(
            analyzer="cppcheck",
            version=version,
            status=AnalyzerExecutionStatus.SUCCEEDED,
            duration_ms=duration_ms,
            findings=tuple(findings),
            stdout_summary=f"{len(findings)} findings",
        )


def _target_paths(context: AnalysisContext) -> list[str]:
    if not (context.languages & {Language.C, Language.CPP}):
        return []
    if context.changed_files:
        return sorted(
            f for f in context.changed_files if Path(f).suffix in (".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx")
        )
    return ["."]


def parse_cppcheck_output(xml_text: str, *, checkout_path: Path) -> list[RawFinding]:
    """Parse cppcheck's ``--xml --xml-version=2`` report into findings.

    Standalone from :meth:`CppcheckAnalyzer.analyze` so adapter parsing
    can be unit-tested against a stored fixture without a live subprocess.
    """

    stripped = xml_text.strip()
    if not stripped:
        return []
    root = ET.fromstring(stripped)
    errors = root.find("errors")
    if errors is None:
        return []

    findings: list[RawFinding] = []
    for error in errors.findall("error"):
        error_id = error.get("id", "unknown")
        location = error.find("location")
        if location is None:
            continue  # a location-less error (e.g. a config/missingInclude notice) isn't a code finding

        file_attr = location.get("file", "")
        line = int(location.get("line", "1"))
        column = int(location.get("column", "0")) or 0

        raw_metadata = {}
        cwe = error.get("cwe")
        if cwe:
            raw_metadata["cwe"] = cwe

        findings.append(
            RawFinding(
                rule_id=error_id,
                category=classify_error_id(error_id),
                title=error_id,
                message=error.get("msg", ""),
                severity=_SEVERITY_MAP.get(error.get("severity", ""), _DEFAULT_SEVERITY),
                confidence=Confidence.MEDIUM,
                file_path=_relative_path(file_attr, checkout_path),
                span=SourceSpan(start_line=line, end_line=line, start_column=column, end_column=column),
                source_analyzer="cppcheck",
                raw_metadata=raw_metadata,
            )
        )
    return findings


def _relative_path(file_attr: str, checkout_path: Path) -> str:
    path = Path(file_attr)
    if not path.is_absolute():
        return file_attr
    try:
        return str(path.relative_to(checkout_path))
    except ValueError:
        return file_attr
