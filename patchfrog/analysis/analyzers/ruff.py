"""Ruff adapter — Python linting via structured JSON output.

Ruff's own ``severity`` field in ``--output-format=json`` is almost always
``"error"`` regardless of how minor the rule is (a style nit and an
undefined-name bug are both reported as "error") — it reflects ruff's
internal diagnostic level, not a meaningful severity signal. PatchFrog
derives its own severity/category from the rule code's family instead
(see :data:`_CATEGORY_BY_PREFIX`), which is documented and tested here.
"""

from __future__ import annotations

import json
import shutil
import time
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

_BINARY = "ruff"

#: Longest-prefix-first: a rule code's alphabetic prefix identifies which
#: ruff-bundled linter family it came from. "E9" (pycodestyle syntax
#: errors) must be checked before the bare "E" (pycodestyle style) family.
_CATEGORY_BY_PREFIX: tuple[tuple[str, FindingCategory, Severity, Confidence], ...] = (
    ("E9", FindingCategory.CORRECTNESS, Severity.HIGH, Confidence.HIGH),  # syntax errors
    ("E722", FindingCategory.CORRECTNESS, Severity.MEDIUM, Confidence.HIGH),  # bare except (before bare "E")
    ("F821", FindingCategory.CORRECTNESS, Severity.HIGH, Confidence.HIGH),  # undefined name
    ("F", FindingCategory.CORRECTNESS, Severity.MEDIUM, Confidence.HIGH),  # pyflakes
    ("BLE", FindingCategory.CORRECTNESS, Severity.LOW, Confidence.MEDIUM),  # blind except (before bare "B")
    ("B", FindingCategory.CORRECTNESS, Severity.MEDIUM, Confidence.MEDIUM),  # flake8-bugbear
    ("ASYNC", FindingCategory.CONCURRENCY, Severity.MEDIUM, Confidence.MEDIUM),  # flake8-async (before bare "A")
    ("SIM", FindingCategory.MAINTAINABILITY, Severity.LOW, Confidence.MEDIUM),  # flake8-simplify (before bare "S")
    ("S", FindingCategory.SECURITY, Severity.HIGH, Confidence.MEDIUM),  # flake8-bandit
    ("PERF", FindingCategory.PERFORMANCE, Severity.LOW, Confidence.MEDIUM),  # perflint
    ("C90", FindingCategory.MAINTAINABILITY, Severity.LOW, Confidence.MEDIUM),  # mccabe complexity
    ("C4", FindingCategory.MAINTAINABILITY, Severity.LOW, Confidence.HIGH),  # comprehensions
    ("UP", FindingCategory.MAINTAINABILITY, Severity.LOW, Confidence.HIGH),  # pyupgrade
    ("PTH", FindingCategory.MAINTAINABILITY, Severity.LOW, Confidence.HIGH),  # use-pathlib
    ("RUF", FindingCategory.MAINTAINABILITY, Severity.LOW, Confidence.MEDIUM),  # ruff-native
    ("A", FindingCategory.MAINTAINABILITY, Severity.LOW, Confidence.HIGH),  # flake8-builtins
    ("N", FindingCategory.STYLE, Severity.INFO, Confidence.HIGH),  # pep8-naming
    ("D", FindingCategory.STYLE, Severity.INFO, Confidence.HIGH),  # pydocstyle
    ("I", FindingCategory.STYLE, Severity.INFO, Confidence.HIGH),  # isort
    ("W", FindingCategory.STYLE, Severity.INFO, Confidence.HIGH),  # pycodestyle warnings
    ("E", FindingCategory.STYLE, Severity.INFO, Confidence.HIGH),  # pycodestyle style
)
_DEFAULT_MAPPING = (FindingCategory.UNKNOWN, Severity.MEDIUM, Confidence.MEDIUM)

def classify_rule_code(code: str) -> tuple[FindingCategory, Severity, Confidence]:
    """Map a ruff rule code (e.g. ``"F401"``) to PatchFrog's normalized
    category/severity/confidence. Exposed standalone so the mapping table
    is directly unit-testable without invoking the subprocess."""

    for prefix, category, severity, confidence in _CATEGORY_BY_PREFIX:
        if code.startswith(prefix):
            return category, severity, confidence
    return _DEFAULT_MAPPING


class RuffAnalyzer:
    capabilities = AnalyzerCapabilities(
        name="ruff",
        languages=frozenset({Language.PYTHON}),
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
                availability=AnalyzerAvailability.UNAVAILABLE, reason="ruff binary not found on PATH"
            )
        try:
            result = await run_sandboxed([binary, "--version"], cwd=Path.cwd(), timeout_seconds=10)
        except AnalyzerSubprocessError as exc:
            return AnalyzerDiscoveryResult(availability=AnalyzerAvailability.UNAVAILABLE, reason=str(exc))
        if result.exit_code != 0:
            return AnalyzerDiscoveryResult(
                availability=AnalyzerAvailability.UNAVAILABLE, reason="ruff --version failed"
            )
        version = result.stdout.strip().removeprefix("ruff ")
        return AnalyzerDiscoveryResult(availability=AnalyzerAvailability.AVAILABLE, version=version)

    async def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        start = time.monotonic()
        discovery = await self.discover()
        if discovery.availability is AnalyzerAvailability.UNAVAILABLE:
            return AnalyzerResult(
                analyzer="ruff",
                version=None,
                status=AnalyzerExecutionStatus.UNSUPPORTED,
                duration_ms=0.0,
                error=discovery.reason,
            )
        version = discovery.version
        binary = shutil.which(_BINARY)
        assert binary is not None  # discover() already confirmed this

        targets = _target_paths(context)
        if not targets:
            return AnalyzerResult(
                analyzer="ruff",
                version=version,
                status=AnalyzerExecutionStatus.SKIPPED,
                duration_ms=0.0,
                error="no Python files in scope",
            )

        args = [binary, "check", "--output-format=json", "--no-cache", "--exit-zero", *targets]
        try:
            result = await run_sandboxed(
                args, cwd=context.checkout_path, timeout_seconds=context.config.timeout_seconds
            )
        except AnalyzerSubprocessError as exc:
            return AnalyzerResult(
                analyzer="ruff",
                version=version,
                status=AnalyzerExecutionStatus.FAILED,
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )

        duration_ms = (time.monotonic() - start) * 1000
        if result.timed_out:
            return AnalyzerResult(
                analyzer="ruff", version=version, status=AnalyzerExecutionStatus.TIMED_OUT, duration_ms=duration_ms
            )
        if result.exit_code != 0:
            return AnalyzerResult(
                analyzer="ruff",
                version=version,
                status=AnalyzerExecutionStatus.FAILED,
                duration_ms=duration_ms,
                error=f"ruff exited {result.exit_code}: {result.stderr[:2000]}",
            )

        try:
            findings = parse_ruff_output(result.stdout, checkout_path=context.checkout_path)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return AnalyzerResult(
                analyzer="ruff",
                version=version,
                status=AnalyzerExecutionStatus.FAILED,
                duration_ms=duration_ms,
                error=f"malformed ruff output: {exc}",
            )

        return AnalyzerResult(
            analyzer="ruff",
            version=version,
            status=AnalyzerExecutionStatus.SUCCEEDED,
            duration_ms=duration_ms,
            findings=tuple(findings),
            stdout_summary=f"{len(findings)} findings",
        )


def _target_paths(context: AnalysisContext) -> list[str]:
    if Language.PYTHON not in context.languages:
        return []
    if context.changed_files:
        return sorted(f for f in context.changed_files if f.endswith(".py"))
    return ["."]


def parse_ruff_output(stdout: str, *, checkout_path: Path) -> list[RawFinding]:
    """Parse ``ruff check --output-format=json`` output into findings.

    Standalone from :meth:`RuffAnalyzer.analyze` so adapter parsing can be
    unit-tested against stored JSON fixtures without a live subprocess.
    """

    stripped = stdout.strip()
    if not stripped:
        return []
    entries = json.loads(stripped)
    findings: list[RawFinding] = []
    for entry in entries:
        code = entry["code"]
        if code is None:
            continue  # syntax-error-only entries can omit a rule code
        category, severity, confidence = classify_rule_code(code)
        absolute_path = Path(entry["filename"])
        try:
            relative_path = str(absolute_path.relative_to(checkout_path))
        except ValueError:
            relative_path = str(absolute_path)

        findings.append(
            RawFinding(
                rule_id=code,
                category=category,
                title=entry.get("name", code),
                message=entry["message"],
                severity=severity,
                confidence=confidence,
                file_path=relative_path,
                span=SourceSpan(
                    start_line=entry["location"]["row"],
                    end_line=entry["end_location"]["row"],
                    start_column=entry["location"]["column"],
                    end_column=entry["end_location"]["column"],
                ),
                source_analyzer="ruff",
                raw_metadata={"url": entry.get("url")} if entry.get("url") else {},
            )
        )
    return findings
