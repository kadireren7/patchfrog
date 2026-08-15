"""Conservative cross-analyzer finding deduplication.

Groups :class:`~patchfrog.analysis.enrichment.EnrichedFinding` instances
that are almost certainly the same underlying issue — e.g. cppcheck and
semgrep both flagging the same null-pointer dereference — into one
:class:`DeduplicatedFinding`, while preserving every source instance as
provenance (``reported_by``).

Two findings are only ever merged when *all* of the following hold:

- same file
- same normalized category (a security finding and a style finding at
  the same line are not "the same issue")
- overlapping line ranges
- if *both* have a known containing symbol, it must be the same symbol
  (one known, one unknown doesn't block a merge — overlapping lines in
  the same file/category is still decent evidence)

Grouping is computed as connected components over the "is a duplicate of"
relation (pairwise, not a strict equivalence relation on its own) rather
than greedy first-match merging, so the result doesn't depend on input
order. When genuinely uncertain, findings are left separate — duplicates
are preferred over an incorrect merge.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.analysis.enrichment import EnrichedFinding
from patchfrog.domain.code import SourceSpan


@dataclass(frozen=True, slots=True)
class DeduplicatedFinding:
    primary: EnrichedFinding
    reported_by: tuple[EnrichedFinding, ...]

    @property
    def source_analyzers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f.finding.source_analyzer for f in self.reported_by))


def deduplicate(findings: list[EnrichedFinding]) -> list[DeduplicatedFinding]:
    groups = _connected_components(findings)
    return [_build_group(group) for group in groups]


def _connected_components(findings: list[EnrichedFinding]) -> list[list[EnrichedFinding]]:
    n = len(findings)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        root_i, root_j = find(i), find(j)
        if root_i != root_j:
            parent[root_j] = root_i

    for i in range(n):
        for j in range(i + 1, n):
            if _is_duplicate_pair(findings[i], findings[j]):
                union(i, j)

    groups: dict[int, list[EnrichedFinding]] = {}
    for i, finding in enumerate(findings):
        groups.setdefault(find(i), []).append(finding)
    return list(groups.values())


def _is_duplicate_pair(a: EnrichedFinding, b: EnrichedFinding) -> bool:
    if a.finding.file_path != b.finding.file_path:
        return False
    if a.finding.category != b.finding.category:
        return False
    if not _spans_overlap(a.finding.span, b.finding.span):
        return False
    if a.finding.source_analyzer == b.finding.source_analyzer and a.finding.rule_id != b.finding.rule_id:
        # The same tool reporting two different rules at an overlapping
        # location is almost always two distinct real issues (e.g. ruff's
        # F401 "unused import" and F811 "redefinition" on one line), not
        # one issue seen twice -- unlike the cross-analyzer case, there's
        # no independent second opinion here to corroborate a merge.
        return False
    return not (
        a.symbol_qualified_name is not None
        and b.symbol_qualified_name is not None
        and a.symbol_qualified_name != b.symbol_qualified_name
    )


def _spans_overlap(a: SourceSpan, b: SourceSpan) -> bool:
    return a.start_line <= b.end_line and b.start_line <= a.end_line


def _build_group(group: list[EnrichedFinding]) -> DeduplicatedFinding:
    # Deterministic "primary" pick: highest severity first (severities are
    # ordered critical > high > medium > low > info as declared on the
    # enum), then most confident, then lexicographic source analyzer name
    # as a final tiebreaker so the choice never depends on input order.
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    confidence_rank = {"high": 0, "medium": 1, "low": 2}

    def sort_key(f: EnrichedFinding) -> tuple[int, int, str]:
        return (
            severity_rank[f.finding.severity.value],
            confidence_rank[f.finding.confidence.value],
            f.finding.source_analyzer,
        )

    ordered = sorted(group, key=sort_key)
    return DeduplicatedFinding(primary=ordered[0], reported_by=tuple(ordered))
