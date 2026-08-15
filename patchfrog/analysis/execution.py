"""Concurrent analyzer execution.

Each selected analyzer is an independent subprocess with its own timeout
and no shared mutable state, so they run concurrently. One analyzer
raising is never allowed to reach here — every :class:`Analyzer.analyze`
implementation is contractually required to catch its own failures and
return a failed :class:`~patchfrog.analysis.domain.AnalyzerResult` — but
this still guards defensively with ``return_exceptions=True`` and
converts any escaped exception into a ``FAILED`` result, so a defect in
one adapter can never take down the whole analysis run.
"""

from __future__ import annotations

import asyncio

import structlog

from patchfrog.analysis.analyzers.base import Analyzer
from patchfrog.analysis.domain import AnalysisContext, AnalyzerExecutionStatus, AnalyzerResult

logger = structlog.get_logger(__name__)


async def run_analyzers(
    analyzers: dict[str, Analyzer], context: AnalysisContext
) -> dict[str, AnalyzerResult]:
    names = list(analyzers.keys())
    raw_results = await asyncio.gather(
        *(analyzers[name].analyze(context) for name in names), return_exceptions=True
    )

    results: dict[str, AnalyzerResult] = {}
    for name, outcome in zip(names, raw_results, strict=True):
        if isinstance(outcome, BaseException):
            logger.error("analyzer_raised_unexpectedly", analyzer=name, error=str(outcome))
            results[name] = AnalyzerResult(
                analyzer=name,
                version=None,
                status=AnalyzerExecutionStatus.FAILED,
                duration_ms=0.0,
                error=f"analyzer adapter raised unexpectedly: {outcome}",
            )
        else:
            results[name] = outcome
    return results
