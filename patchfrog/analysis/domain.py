"""Internal domain models for the static analysis engine.

This is the stable boundary below which analyzer-specific output formats
never cross — every adapter (:mod:`patchfrog.analysis.analyzers`)
translates its tool's native output into :class:`RawFinding` and nothing
else leaves the adapter layer. Everything downstream (deduplication,
enrichment, persistence, future query/review layers) operates on these
types only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from patchfrog.analysis.config import AnalysisConfig
from patchfrog.domain.code import Language, SourceSpan


class Severity(StrEnum):
    """PatchFrog-owned normalized severity — never an analyzer's raw string."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Confidence(StrEnum):
    """How confident the detection itself is, independent of severity."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingCategory(StrEnum):
    """A conservative taxonomy. Use UNKNOWN rather than forcing a fake fit."""

    CORRECTNESS = "correctness"
    SECURITY = "security"
    MEMORY_SAFETY = "memory_safety"
    RESOURCE_MANAGEMENT = "resource_management"
    CONCURRENCY = "concurrency"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    STYLE = "style"
    PORTABILITY = "portability"
    UNDEFINED_BEHAVIOR = "undefined_behavior"
    API_MISUSE = "api_misuse"
    UNKNOWN = "unknown"


class AnalyzerExecutionStatus(StrEnum):
    """Outcome of running one analyzer once."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    UNSUPPORTED = "unsupported"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class RawFinding:
    """One finding as reported by a single analyzer, pre-dedup/pre-enrichment.

    ``raw_metadata`` is for small, bounded, genuinely useful extra fields an
    adapter wants to preserve (e.g. a CWE id) — never a dump of the tool's
    full raw output.
    """

    rule_id: str
    category: FindingCategory
    title: str
    message: str
    severity: Severity
    confidence: Confidence
    file_path: str
    span: SourceSpan
    source_analyzer: str
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AnalyzerCapabilities:
    """Static metadata about what an analyzer can do — consulted once at
    selection time rather than scattered through conditionals elsewhere."""

    name: str
    languages: frozenset[Language]
    requires_compile_database: bool
    supports_file_scope: bool
    supports_repository_scope: bool
    supports_changed_files_only: bool
    supports_structured_output: bool


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    """Explicit context handed to every analyzer adapter — never globals."""

    repository_id: UUID
    repository_index_id: UUID
    commit_sha: str
    checkout_path: Path
    pull_request_number: int | None
    changed_files: frozenset[str]
    changed_lines_by_file: dict[str, frozenset[int]]
    languages: frozenset[Language]
    config: AnalysisConfig


@dataclass(frozen=True, slots=True)
class AnalyzerResult:
    """The outcome of running one analyzer once, before normalization."""

    analyzer: str
    version: str | None
    status: AnalyzerExecutionStatus
    duration_ms: float
    findings: tuple[RawFinding, ...] = field(default_factory=tuple)
    stdout_summary: str | None = None
    error: str | None = None


class AnalysisRunResultStatus(StrEnum):
    """See :class:`~patchfrog.persistence.models.analysis.AnalysisRunStatus`
    for the exact SUCCEEDED/PARTIAL/FAILED semantics."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AnalysisRunSummary:
    """Metrics for one completed (or failed) analysis run."""

    status: AnalysisRunResultStatus
    analyzers_succeeded: int
    analyzers_failed: int
    analyzers_skipped: int
    raw_findings_count: int
    findings_count: int
    duration_ms: float
    reused_existing_run: bool
