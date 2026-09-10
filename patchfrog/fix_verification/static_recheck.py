"""Deterministic static-analyzer re-check -- Milestone T (T3), Part V step
3, security-corrected.

Reuses the exact analyzer adapter that originally corroborated a finding
(:mod:`patchfrog.analysis.analyzers`), invoked directly and scoped to
exactly one file -- never :class:`patchfrog.analysis.service.
StaticAnalysisService`'s full-repository indexing/persistence
orchestration (this is a bounded, ephemeral re-check for one finding, not
a new analysis run; see ``validation/agent_handoff/latest-summary.md``
section 1). Never a second static-analysis engine -- the same adapters,
the same rule ids, the same detection logic.

**Security correction**: the original version of this module returned a
bare ``bool | None`` and searched a fixed line window around the
*original* finding's own line numbers. The caller then treated "rule not
firing there" as a ``FIXED`` signal -- unsafe, because the rule may simply
not fire *at that particular checked surface* for reasons that have
nothing to do with the bug being fixed (the code moved, the file was
renamed, the analyzer's rule taxonomy doesn't map exactly). This module
now takes an already-computed
:class:`~patchfrog.fix_verification.surface_mapping.MappedSurface` instead
of guessing a line window, and returns a
:class:`StaticRecheckResult` that keeps "still fires" (strong) separate
from "does not fire at the safely mapped surface" (weak -- see the module
docstring of :mod:`patchfrog.fix_verification.domain`'s
``FixEvidenceDirection`` for why that distinction matters).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from patchfrog.analysis.analyzers.base import AnalyzerAvailability
from patchfrog.analysis.analyzers.registry import default_registry
from patchfrog.analysis.config import AnalysisConfig
from patchfrog.analysis.domain import AnalysisContext
from patchfrog.domain.code import Language
from patchfrog.fix_verification.surface_mapping import MappedSurface

#: A small window around the *mapped* (not original) surface -- allows
#: for off-by-one differences between a parser's symbol span and an
#: analyzer's own reported line, never a substitute for real mapping.
_NEAR_LINE_WINDOW = 2


class StaticRecheckStatus(StrEnum):
    #: The rule still fires at the safely mapped surface -- strong
    #: evidence the original condition still holds.
    STILL_PRESENT = "still_present"
    #: The analyzer ran cleanly and the rule does not fire at the safely
    #: mapped surface -- weak/supporting evidence only, never proof of a
    #: fix by itself (see the module docstring).
    ABSENT_AT_MAPPED_SURFACE = "absent_at_mapped_surface"
    #: No safe surface to check (unmapped/ambiguous mapping) -- never
    #: guessed at.
    INCONCLUSIVE = "inconclusive"
    #: The analyzer itself is unavailable on this host.
    UNAVAILABLE = "unavailable"


async def recheck_static_finding(
    *, checkout_path: Path, mapped_surface: MappedSurface, source_analyzer: str, rule_id: str, language: Language,
) -> StaticRecheckStatus:
    """Re-runs ``source_analyzer`` against ``mapped_surface.file_path`` in
    ``checkout_path`` and checks whether ``rule_id`` still fires at/near
    ``mapped_surface``'s current line range. Requires
    ``mapped_surface.is_safely_mapped`` -- an unmapped/ambiguous surface
    always returns ``INCONCLUSIVE`` without ever attempting a guess."""

    if not mapped_surface.is_safely_mapped:
        return StaticRecheckStatus.INCONCLUSIVE
    assert mapped_surface.file_path is not None
    assert mapped_surface.start_line is not None
    assert mapped_surface.end_line is not None

    analyzer = default_registry().get(source_analyzer)
    if analyzer is None:
        return StaticRecheckStatus.UNAVAILABLE

    discovery = await analyzer.discover()
    if discovery.availability != AnalyzerAvailability.AVAILABLE:
        return StaticRecheckStatus.UNAVAILABLE

    if not (checkout_path / mapped_surface.file_path).is_file():
        # Mapping already proved this file exists at the candidate head
        # (map_finding_surface requires it) -- this should not happen,
        # but never guess if it somehow does.
        return StaticRecheckStatus.INCONCLUSIVE

    context = AnalysisContext(
        repository_id=uuid4(),
        repository_index_id=uuid4(),
        commit_sha="",
        checkout_path=checkout_path,
        pull_request_number=None,
        changed_files=frozenset({mapped_surface.file_path}),
        changed_lines_by_file={},
        languages=frozenset({language}),
        config=AnalysisConfig(),
    )
    result = await analyzer.analyze(context)

    low = mapped_surface.start_line - _NEAR_LINE_WINDOW
    high = mapped_surface.end_line + _NEAR_LINE_WINDOW
    for finding in result.findings:
        if finding.rule_id != rule_id or finding.file_path != mapped_surface.file_path:
            continue
        if finding.span.start_line <= high and finding.span.end_line >= low:
            return StaticRecheckStatus.STILL_PRESENT
    return StaticRecheckStatus.ABSENT_AT_MAPPED_SURFACE


__all__ = ["StaticRecheckStatus", "recheck_static_finding"]
