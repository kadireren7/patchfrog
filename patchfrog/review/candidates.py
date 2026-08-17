"""Deterministic, symbol-centered review candidate generation.

Every candidate traces back to something structural -- a changed line
mapped to its containing symbol via Phase 2's repository intelligence, or
(when no symbol covers the changed lines, e.g. module-level statements) a
contiguous changed-line region within one file. Candidates are never
generated per-line and never chosen by an LLM's own judgment about what
looks interesting -- selection and prioritization happen entirely in this
module, before any provider call is made.

Static findings are attached to a candidate as evidence (see
:mod:`patchfrog.review.prompt`) when their location falls inside the
candidate's span; a static finding never creates a candidate on its own
that changed lines don't already justify -- static findings are hints for
review, not an independent trigger.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.diff.models import DiffFile
from patchfrog.intelligence.queries import RepositoryQueryService
from patchfrog.persistence.models.analysis import FindingModel
from patchfrog.review.domain import ReviewCandidate, ReviewCandidateReason, StaticFindingSummary

#: A module-region candidate (no containing symbol) is capped to this many
#: lines so a giant file with scattered top-level changes never becomes a
#: single unbounded candidate.
_MAX_MODULE_REGION_LINES = 60


@dataclass(frozen=True, slots=True)
class _ChangedLine:
    file_path: str
    line: int


@dataclass(slots=True)
class _SymbolGroup:
    symbol_name: str
    qualified_name: str
    start_line: int
    end_line: int
    lines: list[int]


class ReviewCandidateGenerator:
    def __init__(self, *, query_service: RepositoryQueryService | None = None) -> None:
        self._queries = query_service or RepositoryQueryService()

    async def generate(
        self,
        session: AsyncSession,
        *,
        repository_index_id: UUID,
        diff_files: list[DiffFile],
        static_findings: list[FindingModel],
        max_candidates: int,
    ) -> tuple[ReviewCandidate, ...]:
        changed_lines = _extract_added_lines(diff_files)
        if not changed_lines:
            return ()

        symbol_groups: dict[tuple[str, UUID], _SymbolGroup] = {}
        module_region_lines: dict[str, list[int]] = {}

        for cl in changed_lines:
            symbol = await self._queries.symbol_for_changed_line(
                session, repository_index_id=repository_index_id, relative_path=cl.file_path, line=cl.line
            )
            if symbol is None:
                module_region_lines.setdefault(cl.file_path, []).append(cl.line)
                continue
            key = (cl.file_path, symbol.id)
            group = symbol_groups.setdefault(
                key,
                _SymbolGroup(
                    symbol_name=symbol.name,
                    qualified_name=symbol.qualified_name,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    lines=[],
                ),
            )
            group.lines.append(cl.line)

        candidates: list[ReviewCandidate] = []
        for (file_path, symbol_id), group in symbol_groups.items():
            candidates.append(
                ReviewCandidate(
                    file_path=file_path,
                    symbol_id=symbol_id,
                    symbol_name=group.symbol_name,
                    qualified_name=group.qualified_name,
                    start_line=group.start_line,
                    end_line=group.end_line,
                    changed_lines=tuple(sorted(group.lines)),
                    static_finding_ids=(),
                    reason=ReviewCandidateReason.CHANGED_SYMBOL,
                )
            )

        for file_path, lines in module_region_lines.items():
            for region in _cluster_lines(sorted(lines), max_span=_MAX_MODULE_REGION_LINES):
                candidates.append(
                    ReviewCandidate(
                        file_path=file_path,
                        symbol_id=None,
                        symbol_name=None,
                        qualified_name=None,
                        start_line=region[0],
                        end_line=region[-1],
                        changed_lines=tuple(region),
                        static_finding_ids=(),
                        reason=ReviewCandidateReason.CHANGED_MODULE_REGION,
                    )
                )

        candidates = _attach_static_findings(candidates, static_findings)
        candidates = _prioritize(candidates)
        return tuple(candidates[:max_candidates])


def _extract_added_lines(diff_files: list[DiffFile]) -> list[_ChangedLine]:
    result: list[_ChangedLine] = []
    for diff_file in diff_files:
        for line in diff_file.added_lines:
            if line.new_line_number is not None:
                result.append(_ChangedLine(file_path=diff_file.path, line=line.new_line_number))
    return result


def _cluster_lines(lines: list[int], *, max_span: int) -> list[list[int]]:
    """Group sorted line numbers into contiguous-ish clusters, splitting
    whenever a gap exceeds ``max_span`` or a cluster's own span would
    exceed it -- deterministic, single pass, no external dependency."""

    if not lines:
        return []
    clusters: list[list[int]] = [[lines[0]]]
    for line in lines[1:]:
        current = clusters[-1]
        if line - current[0] <= max_span:
            current.append(line)
        else:
            clusters.append([line])
    return clusters


def _attach_static_findings(
    candidates: list[ReviewCandidate], static_findings: list[FindingModel]
) -> list[ReviewCandidate]:
    if not static_findings:
        return candidates

    result: list[ReviewCandidate] = []
    for candidate in candidates:
        matched_ids = tuple(
            f.id
            for f in static_findings
            if f.file_path == candidate.file_path
            and not (f.end_line < candidate.start_line or f.start_line > candidate.end_line)
        )
        if matched_ids:
            result.append(
                ReviewCandidate(
                    file_path=candidate.file_path,
                    symbol_id=candidate.symbol_id,
                    symbol_name=candidate.symbol_name,
                    qualified_name=candidate.qualified_name,
                    start_line=candidate.start_line,
                    end_line=candidate.end_line,
                    changed_lines=candidate.changed_lines,
                    static_finding_ids=matched_ids,
                    reason=candidate.reason,
                )
            )
        else:
            result.append(candidate)
    return result


def _prioritize(candidates: list[ReviewCandidate]) -> list[ReviewCandidate]:
    """Deterministic priority order: candidates corroborated by a static
    finding first, then by how much changed (more changed lines first),
    then a total, stable tie-break on (file_path, start_line, end_line) so
    the same diff always produces the same order regardless of dict/set
    iteration order upstream."""

    return sorted(
        candidates,
        key=lambda c: (
            0 if c.static_finding_ids else 1,
            -len(c.changed_lines),
            c.file_path,
            c.start_line,
            c.end_line,
        ),
    )


def summarize_static_finding(finding: FindingModel) -> StaticFindingSummary:
    return StaticFindingSummary(
        finding_id=finding.id,
        rule_id=finding.rule_id,
        category=FindingCategory(finding.category),
        severity=Severity(finding.severity),
        confidence=Confidence(finding.confidence),
        title=finding.title,
        message=finding.message,
        start_line=finding.start_line,
        end_line=finding.end_line,
        source_analyzer=finding.source_analyzer,
    )
