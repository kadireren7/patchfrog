"""clang-tidy adapter — C/C++ static analysis via compile-database-driven checks.

Two deliberate constraints, both required by the Phase 3 spec:

1. **Never runs without a compilation database.** clang-tidy without
   ``compile_commands.json`` mostly produces noise (unresolved includes,
   guessed-wrong flags) rather than useful findings, and PatchFrog must
   never *generate* one by executing the repository's build system
   (that would mean running untrusted repository code). If no
   compilation database is found at a conventional location, this
   analyzer reports :attr:`AnalyzerExecutionStatus.SKIPPED` with a clear
   reason instead of running anyway.

2. **Parses clang-tidy's standard diagnostic line format**
   (``file:line:col: severity: message [check-name]``) rather than its
   ``-export-fixes`` YAML export. That export is the more "structured"
   option on paper, but it locates diagnostics by byte offset, not
   line/column — converting that back requires re-reading and
   byte-counting through each source file, which is both slower and a
   source of its own bugs (multi-byte UTF-8 offset arithmetic). The
   plain diagnostic line format is a stable, strictly-defined grammar
   clang has used for decades and gives line/column directly.
"""

from __future__ import annotations

import re
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

_BINARY = "clang-tidy"

_COMPILE_DB_LOCATIONS = ("compile_commands.json", "build/compile_commands.json")

_DIAGNOSTIC_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): (?P<severity>error|warning): "
    r"(?P<message>.*?)(?: \[(?P<check>[\w,.\-]+)\])?$"
)

_SEVERITY_MAP = {"error": Severity.HIGH, "warning": Severity.MEDIUM}
_DEFAULT_SEVERITY = Severity.MEDIUM

#: clang-tidy check names are namespaced ("bugprone-use-after-move",
#: "clang-analyzer-core.NullDereference", ...) — the namespace prefix is a
#: reliable category signal, unlike cppcheck's compound-word ids.
#: Matched lowercase against a lowercased check name — clang-analyzer
#: checker names mix case inconsistently in their subcategory part (e.g.
#: "core.NullDereference" but "core.uninitialized.Assign"), so comparing
#: case-insensitively avoids hardcoding (and inevitably mismatching) that
#: casing rather than tracking it accurately.
_CATEGORY_BY_CHECK_PREFIX: tuple[tuple[str, FindingCategory], ...] = (
    ("clang-analyzer-core.uninitialized", FindingCategory.MEMORY_SAFETY),
    ("clang-analyzer-core.null", FindingCategory.MEMORY_SAFETY),
    ("clang-analyzer-cplusplus.newdelete", FindingCategory.MEMORY_SAFETY),
    ("clang-analyzer-unix.malloc", FindingCategory.MEMORY_SAFETY),
    ("clang-analyzer-security", FindingCategory.SECURITY),
    ("clang-analyzer", FindingCategory.UNDEFINED_BEHAVIOR),
    ("bugprone-use-after-move", FindingCategory.MEMORY_SAFETY),
    ("bugprone-dangling", FindingCategory.MEMORY_SAFETY),
    ("bugprone-", FindingCategory.CORRECTNESS),
    ("cert-", FindingCategory.SECURITY),
    ("security-", FindingCategory.SECURITY),
    ("concurrency-", FindingCategory.CONCURRENCY),
    ("performance-", FindingCategory.PERFORMANCE),
    ("portability-", FindingCategory.PORTABILITY),
    ("readability-", FindingCategory.MAINTAINABILITY),
    ("modernize-", FindingCategory.MAINTAINABILITY),
    ("misc-", FindingCategory.MAINTAINABILITY),
    ("cppcoreguidelines-", FindingCategory.MAINTAINABILITY),
)


def classify_check_name(check_name: str) -> FindingCategory:
    """Map a clang-tidy check name to a normalized category. Standalone
    for direct unit testing."""

    lowered = check_name.lower()
    for prefix, category in _CATEGORY_BY_CHECK_PREFIX:
        if lowered.startswith(prefix):
            return category
    return FindingCategory.UNKNOWN


def find_compilation_database(checkout_path: Path) -> Path | None:
    """Locate a compile database at a conventional path, if present.

    Never generated — only ever discovered. See the module docstring.
    """

    for relative in _COMPILE_DB_LOCATIONS:
        candidate = checkout_path / relative
        if candidate.is_file():
            return candidate.parent
    return None


class ClangTidyAnalyzer:
    capabilities = AnalyzerCapabilities(
        name="clang_tidy",
        languages=frozenset({Language.C, Language.CPP}),
        requires_compile_database=True,
        supports_file_scope=True,
        supports_repository_scope=False,
        supports_changed_files_only=True,
        supports_structured_output=False,
    )

    async def discover(self) -> AnalyzerDiscoveryResult:
        binary = shutil.which(_BINARY)
        if binary is None:
            return AnalyzerDiscoveryResult(
                availability=AnalyzerAvailability.UNAVAILABLE, reason="clang-tidy binary not found on PATH"
            )
        try:
            result = await run_sandboxed([binary, "--version"], cwd=Path.cwd(), timeout_seconds=10)
        except AnalyzerSubprocessError as exc:
            return AnalyzerDiscoveryResult(availability=AnalyzerAvailability.UNAVAILABLE, reason=str(exc))
        if result.exit_code != 0:
            return AnalyzerDiscoveryResult(
                availability=AnalyzerAvailability.UNAVAILABLE, reason="clang-tidy --version failed"
            )
        version_match = re.search(r"version\s+([\d.]+)", result.stdout)
        version = version_match.group(1) if version_match else result.stdout.strip()
        return AnalyzerDiscoveryResult(availability=AnalyzerAvailability.AVAILABLE, version=version)

    async def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        start = time.monotonic()
        discovery = await self.discover()
        if discovery.availability is AnalyzerAvailability.UNAVAILABLE:
            return AnalyzerResult(
                analyzer="clang_tidy",
                version=None,
                status=AnalyzerExecutionStatus.UNSUPPORTED,
                duration_ms=0.0,
                error=discovery.reason,
            )
        version = discovery.version
        binary = shutil.which(_BINARY)
        assert binary is not None

        compile_db_dir = find_compilation_database(context.checkout_path)
        if compile_db_dir is None:
            return AnalyzerResult(
                analyzer="clang_tidy",
                version=version,
                status=AnalyzerExecutionStatus.SKIPPED,
                duration_ms=0.0,
                error="no compile_commands.json found; clang-tidy requires a compilation "
                "database and PatchFrog never generates one by running the repository's build",
            )

        targets = _target_paths(context)
        if not targets:
            return AnalyzerResult(
                analyzer="clang_tidy",
                version=version,
                status=AnalyzerExecutionStatus.SKIPPED,
                duration_ms=0.0,
                error="no C/C++ source files in scope",
            )

        args = [binary, "-p", str(compile_db_dir), "--quiet", *targets]
        try:
            result = await run_sandboxed(
                args, cwd=context.checkout_path, timeout_seconds=context.config.timeout_seconds
            )
        except AnalyzerSubprocessError as exc:
            return AnalyzerResult(
                analyzer="clang_tidy",
                version=version,
                status=AnalyzerExecutionStatus.FAILED,
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )

        duration_ms = (time.monotonic() - start) * 1000
        if result.timed_out:
            return AnalyzerResult(
                analyzer="clang_tidy",
                version=version,
                status=AnalyzerExecutionStatus.TIMED_OUT,
                duration_ms=duration_ms,
            )
        # clang-tidy exits non-zero whenever it emits any warning/error —
        # that's expected and not itself a run failure.

        findings = parse_clang_tidy_output(result.stdout, checkout_path=context.checkout_path)

        return AnalyzerResult(
            analyzer="clang_tidy",
            version=version,
            status=AnalyzerExecutionStatus.SUCCEEDED,
            duration_ms=duration_ms,
            findings=tuple(findings),
            stdout_summary=f"{len(findings)} findings",
        )


def _target_paths(context: AnalysisContext) -> list[str]:
    if not (context.languages & {Language.C, Language.CPP}):
        return []
    suffixes = (".c", ".cc", ".cpp", ".cxx")  # headers alone can't be compiled as translation units
    if context.changed_files:
        return sorted(f for f in context.changed_files if Path(f).suffix in suffixes)
    return []  # clang-tidy has no whole-repository mode without an explicit file list


def parse_clang_tidy_output(stdout: str, *, checkout_path: Path) -> list[RawFinding]:
    """Parse clang-tidy's standard diagnostic text output into findings.

    Standalone from :meth:`ClangTidyAnalyzer.analyze` so adapter parsing
    can be unit-tested against stored fixture text without a live
    subprocess. See the module docstring for why this format (not
    ``-export-fixes`` YAML) is used.
    """

    findings: list[RawFinding] = []
    for line in stdout.splitlines():
        match = _DIAGNOSTIC_RE.match(line)
        if match is None:
            continue  # note: lines, source snippets, and `^~~~` carets are not diagnostics
        check_name = match.group("check") or "clang-tidy"
        findings.append(
            RawFinding(
                rule_id=check_name,
                category=classify_check_name(check_name),
                title=check_name,
                message=match.group("message"),
                severity=_SEVERITY_MAP.get(match.group("severity"), _DEFAULT_SEVERITY),
                confidence=Confidence.MEDIUM,
                file_path=_relative_path(match.group("file"), checkout_path),
                span=SourceSpan(
                    start_line=int(match.group("line")),
                    end_line=int(match.group("line")),
                    start_column=int(match.group("col")),
                    end_column=int(match.group("col")),
                ),
                source_analyzer="clang_tidy",
            )
        )
    return findings


def _relative_path(file_str: str, checkout_path: Path) -> str:
    path = Path(file_str)
    if not path.is_absolute():
        return file_str
    try:
        return str(path.relative_to(checkout_path))
    except ValueError:
        return file_str
