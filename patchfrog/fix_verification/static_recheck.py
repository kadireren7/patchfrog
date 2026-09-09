"""Deterministic static-analyzer re-check -- Milestone T (T3), Part V step 3.

Reuses the exact analyzer adapter that originally corroborated a finding
(:mod:`patchfrog.analysis.analyzers`), invoked directly and scoped to
exactly one file -- never :class:`patchfrog.analysis.service.
StaticAnalysisService`'s full-repository indexing/persistence
orchestration (this is a bounded, ephemeral re-check for one finding, not
a new analysis run; see ``validation/agent_handoff/latest-summary.md``
section 1). Never a second static-analysis engine -- the same adapters,
the same rule ids, the same detection logic.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from patchfrog.analysis.analyzers.base import AnalyzerAvailability
from patchfrog.analysis.analyzers.registry import default_registry
from patchfrog.analysis.config import AnalysisConfig
from patchfrog.analysis.domain import AnalysisContext
from patchfrog.domain.code import Language

#: A generous window either side of the original finding's line range --
#: a rule firing a few lines away after nearby edits is still the same
#: underlying issue; a rule firing somewhere unrelated in the file is not.
_NEAR_LINE_WINDOW = 3


async def recheck_static_finding(
    *,
    checkout_path: Path,
    file_path: str,
    source_analyzer: str,
    rule_id: str,
    original_start_line: int,
    original_end_line: int,
    language: Language,
) -> bool | None:
    """Returns ``True`` if ``rule_id`` still fires at/near the original
    location in ``checkout_path``'s current copy of ``file_path``,
    ``False`` if the analyzer ran cleanly and it does not, or ``None`` if
    the analyzer itself is unavailable/unsupported on this host (never
    guessed at -- the caller must treat ``None`` as "no signal", not as
    either outcome)."""

    analyzer = default_registry().get(source_analyzer)
    if analyzer is None:
        return None

    discovery = await analyzer.discover()
    if discovery.availability != AnalyzerAvailability.AVAILABLE:
        return None

    if not (checkout_path / file_path).is_file():
        # The file no longer exists at this head -- the specific rule
        # cannot be firing on it, but this is not proof of a fix either
        # (the code may have moved); the caller decides how to weigh this.
        return False

    context = AnalysisContext(
        repository_id=uuid4(),
        repository_index_id=uuid4(),
        commit_sha="",
        checkout_path=checkout_path,
        pull_request_number=None,
        changed_files=frozenset({file_path}),
        changed_lines_by_file={},
        languages=frozenset({language}),
        config=AnalysisConfig(),
    )
    result = await analyzer.analyze(context)

    low = original_start_line - _NEAR_LINE_WINDOW
    high = original_end_line + _NEAR_LINE_WINDOW
    for finding in result.findings:
        if finding.rule_id != rule_id or finding.file_path != file_path:
            continue
        if finding.span.start_line <= high and finding.span.end_line >= low:
            return True
    return False


__all__ = ["recheck_static_finding"]
