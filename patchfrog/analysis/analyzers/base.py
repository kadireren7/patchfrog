"""Shared analyzer protocol and discovery model.

Every concrete adapter (:mod:`patchfrog.analysis.analyzers.ruff`, etc.)
implements :class:`Analyzer`. Nothing outside this package should need to
know a given analyzer's command-line shape or native output format —
:meth:`Analyzer.analyze` always returns the normalized
:class:`~patchfrog.analysis.domain.AnalyzerResult`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from patchfrog.analysis.domain import AnalysisContext, AnalyzerCapabilities, AnalyzerResult


class AnalyzerAvailability(StrEnum):
    """Whether an analyzer's binary is actually usable in this environment."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class AnalyzerDiscoveryResult:
    """The result of checking whether an analyzer can run here, right now."""

    availability: AnalyzerAvailability
    version: str | None = None
    reason: str | None = None


class Analyzer(Protocol):
    """A static analyzer adapter."""

    capabilities: AnalyzerCapabilities

    async def discover(self) -> AnalyzerDiscoveryResult:
        """Check whether this analyzer's binary/toolchain is usable here.

        Must never raise — an analyzer that can't be discovered is
        represented as :attr:`AnalyzerAvailability.UNAVAILABLE`, not an
        exception, so a missing optional tool never crashes PatchFrog.
        """
        ...

    async def analyze(self, context: AnalysisContext) -> AnalyzerResult:
        """Run this analyzer and return normalized results.

        Must never raise for an analyzer-side failure (non-zero exit,
        timeout, malformed output, ...) — those are represented in the
        returned :class:`AnalyzerResult`'s ``status``/``error`` fields, so
        one analyzer's failure can never take down the whole analysis run.
        """
        ...
