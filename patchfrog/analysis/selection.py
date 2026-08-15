"""Analyzer selection.

Consults each analyzer's static :class:`~patchfrog.analysis.domain.AnalyzerCapabilities`
(language overlap) and the run's :class:`~patchfrog.analysis.config.AnalysisConfig`
(explicit enable/disable) exactly once, here — nothing downstream needs to
re-derive "should this analyzer even be considered" from scattered
conditionals. Whether a *selected* analyzer is actually usable right now
(binary present, compile database available, ...) is a separate concern,
resolved by each adapter's own ``discover()``/``analyze()`` at execution
time — selection only asks "is this worth attempting."
"""

from __future__ import annotations

from patchfrog.analysis.analyzers.base import Analyzer
from patchfrog.analysis.analyzers.registry import AnalyzerRegistry
from patchfrog.analysis.config import AnalysisConfig
from patchfrog.domain.code import Language


def select_analyzers(
    registry: AnalyzerRegistry, *, config: AnalysisConfig, languages: frozenset[Language]
) -> dict[str, Analyzer]:
    selected: dict[str, Analyzer] = {}
    for name, analyzer in registry.all().items():
        if config.wants(name) is False:
            continue
        if not (analyzer.capabilities.languages & languages):
            continue
        selected[name] = analyzer
    return selected
