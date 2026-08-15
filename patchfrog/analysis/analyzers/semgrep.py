"""Semgrep adapter — rule-based pattern matching via structured JSON output.

Uses PatchFrog's own small bundled ruleset
(``analyzers/semgrep_rules/patchfrog-rules.yml``) rather than downloading
rules from the Semgrep registry at runtime — analysis stays deterministic
and never depends on network access. ``--metrics=off`` disables Semgrep's
own telemetry call for the same reason.
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

_BINARY = "semgrep"

BUNDLED_RULES_PATH = Path(__file__).parent / "semgrep_rules" / "patchfrog-rules.yml"

_SEVERITY_MAP = {
    "ERROR": Severity.HIGH,
    "WARNING": Severity.MEDIUM,
    "INFO": Severity.LOW,
}
_DEFAULT_SEVERITY = Severity.MEDIUM

_CONFIDENCE_MAP = {"HIGH": Confidence.HIGH, "MEDIUM": Confidence.MEDIUM, "LOW": Confidence.LOW}
_DEFAULT_CONFIDENCE = Confidence.MEDIUM


class SemgrepAnalyzer:
    capabilities = AnalyzerCapabilities(
        name="semgrep",
        languages=frozenset({Language.PYTHON, Language.C, Language.CPP}),
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
                availability=AnalyzerAvailability.UNAVAILABLE, reason="semgrep binary not found on PATH"
            )
        try:
            result = await run_sandboxed([binary, "--version"], cwd=Path.cwd(), timeout_seconds=15)
        except AnalyzerSubprocessError as exc:
            return AnalyzerDiscoveryResult(availability=AnalyzerAvailability.UNAVAILABLE, reason=str(exc))
        if result.exit_code != 0:
            return AnalyzerDiscoveryResult(
                availability=AnalyzerAvailability.UNAVAILABLE, reason="semgrep --version failed"
            )
        return AnalyzerDiscoveryResult(
            availability=AnalyzerAvailability.AVAILABLE, version=result.stdout.strip()
        )

    async def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        start = time.monotonic()
        discovery = await self.discover()
        if discovery.availability is AnalyzerAvailability.UNAVAILABLE:
            return AnalyzerResult(
                analyzer="semgrep",
                version=None,
                status=AnalyzerExecutionStatus.UNSUPPORTED,
                duration_ms=0.0,
                error=discovery.reason,
            )
        version = discovery.version
        binary = shutil.which(_BINARY)
        assert binary is not None

        if not (context.languages & self.capabilities.languages):
            return AnalyzerResult(
                analyzer="semgrep",
                version=version,
                status=AnalyzerExecutionStatus.SKIPPED,
                duration_ms=0.0,
                error="no supported languages in scope",
            )

        targets = sorted(context.changed_files) if context.changed_files else ["."]
        args = [
            binary,
            "--config",
            str(BUNDLED_RULES_PATH),
            "--json",
            "--metrics=off",
            "--quiet",
            *targets,
        ]
        try:
            result = await run_sandboxed(
                args, cwd=context.checkout_path, timeout_seconds=context.config.timeout_seconds
            )
        except AnalyzerSubprocessError as exc:
            return AnalyzerResult(
                analyzer="semgrep",
                version=version,
                status=AnalyzerExecutionStatus.FAILED,
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )

        duration_ms = (time.monotonic() - start) * 1000
        if result.timed_out:
            return AnalyzerResult(
                analyzer="semgrep",
                version=version,
                status=AnalyzerExecutionStatus.TIMED_OUT,
                duration_ms=duration_ms,
            )
        # semgrep exits 1 when findings exist under --config with certain
        # blocking rules; only >1 indicates a genuine execution failure.
        if result.exit_code not in (0, 1):
            return AnalyzerResult(
                analyzer="semgrep",
                version=version,
                status=AnalyzerExecutionStatus.FAILED,
                duration_ms=duration_ms,
                error=f"semgrep exited {result.exit_code}: {result.stderr[:2000]}",
            )

        try:
            findings = parse_semgrep_output(result.stdout, checkout_path=context.checkout_path)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return AnalyzerResult(
                analyzer="semgrep",
                version=version,
                status=AnalyzerExecutionStatus.FAILED,
                duration_ms=duration_ms,
                error=f"malformed semgrep output: {exc}",
            )

        return AnalyzerResult(
            analyzer="semgrep",
            version=version,
            status=AnalyzerExecutionStatus.SUCCEEDED,
            duration_ms=duration_ms,
            findings=tuple(findings),
            stdout_summary=f"{len(findings)} findings",
        )


def parse_semgrep_output(stdout: str, *, checkout_path: Path) -> list[RawFinding]:
    """Parse ``semgrep --json`` output into findings.

    Standalone from :meth:`SemgrepAnalyzer.analyze` so adapter parsing can
    be unit-tested against stored JSON fixtures without a live subprocess.
    """

    stripped = stdout.strip()
    if not stripped:
        return []
    payload = json.loads(stripped)
    findings: list[RawFinding] = []
    for entry in payload.get("results", []):
        extra = entry["extra"]
        metadata = extra.get("metadata", {})
        category_raw = metadata.get("category", "unknown")
        try:
            category = FindingCategory(category_raw)
        except ValueError:
            category = FindingCategory.UNKNOWN

        severity = _SEVERITY_MAP.get(extra.get("severity", ""), _DEFAULT_SEVERITY)
        confidence = _CONFIDENCE_MAP.get(metadata.get("confidence", ""), _DEFAULT_CONFIDENCE)

        absolute_path = Path(entry["path"])
        try:
            relative_path = str(absolute_path.relative_to(checkout_path))
        except ValueError:
            relative_path = str(absolute_path)

        rule_id = entry["check_id"].rsplit(".", 1)[-1]  # strip the config-path-derived prefix
        raw_metadata = {"cwe": metadata["cwe"]} if "cwe" in metadata else {}

        findings.append(
            RawFinding(
                rule_id=rule_id,
                category=category,
                title=rule_id,
                message=extra["message"],
                severity=severity,
                confidence=confidence,
                file_path=relative_path,
                span=SourceSpan(
                    start_line=entry["start"]["line"],
                    end_line=entry["end"]["line"],
                    start_column=entry["start"]["col"],
                    end_column=entry["end"]["col"],
                ),
                source_analyzer="semgrep",
                raw_metadata=raw_metadata,
            )
        )
    return findings
