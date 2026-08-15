"""Maps analyzer names to adapters.

Single place that knows the full set of analyzers PatchFrog ships —
adding a new one means one adapter module and one line here.
"""

from __future__ import annotations

from patchfrog.analysis.analyzers.base import Analyzer


class AnalyzerRegistry:
    def __init__(self) -> None:
        self._analyzers: dict[str, Analyzer] = {}

    def register(self, name: str, analyzer: Analyzer) -> None:
        self._analyzers[name] = analyzer

    def get(self, name: str) -> Analyzer | None:
        return self._analyzers.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._analyzers.keys())

    def all(self) -> dict[str, Analyzer]:
        return dict(self._analyzers)


def default_registry() -> AnalyzerRegistry:
    """The registry populated with every analyzer PatchFrog ships support for."""

    from patchfrog.analysis.analyzers.clang_tidy import ClangTidyAnalyzer
    from patchfrog.analysis.analyzers.cppcheck import CppcheckAnalyzer
    from patchfrog.analysis.analyzers.ruff import RuffAnalyzer
    from patchfrog.analysis.analyzers.semgrep import SemgrepAnalyzer

    registry = AnalyzerRegistry()
    registry.register("ruff", RuffAnalyzer())
    registry.register("cppcheck", CppcheckAnalyzer())
    registry.register("clang_tidy", ClangTidyAnalyzer())
    registry.register("semgrep", SemgrepAnalyzer())
    return registry
