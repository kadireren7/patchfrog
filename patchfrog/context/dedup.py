"""Context candidate deduplication / overlap suppression.

Runs after scoring (so "prefer the higher-ranked item" has a score to
compare) and before budgeting (so budget is never spent on something
about to be discarded anyway). Purely structural -- span/symbol
comparisons only, no content inspection needed, so it never requires
reading a file from disk.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.context.domain import ContextCandidate, ContextItemKind, ScoreComponent
from patchfrog.context.scoring import relationship_priority_index

#: Two same-file spans are treated as redundant once their overlap covers
#: this fraction of the smaller span -- conservative enough that e.g. a
#: caller's 5-line span fully inside a 200-line target isn't suppressed
#: (they're clearly not "the same content"), but two near-identical
#: regions are.
_OVERLAP_SUPPRESSION_RATIO = 0.6

#: A parent symbol's span *always* structurally contains everything
#: nested inside it -- including the target itself, whenever the target
#: is nested. That's expected, useful containment, not redundant
#: duplication, so overlap suppression never applies to it (exact-
#: duplicate suppression still would, in the degenerate case where a
#: "parent" and the target somehow share the identical span).
_EXEMPT_FROM_OVERLAP_SUPPRESSION = frozenset({ContextItemKind.PARENT_SYMBOL})


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: ContextCandidate
    score: float
    breakdown: tuple[ScoreComponent, ...]


@dataclass(frozen=True, slots=True)
class DedupResult:
    kept: tuple[ScoredCandidate, ...]
    dropped_duplicate: int
    dropped_overlap: int


def _sort_key(scored: ScoredCandidate) -> tuple[float, int, str, int, str]:
    c = scored.candidate
    return (
        -scored.score,
        relationship_priority_index(c.relationship),
        c.file_path,
        c.start_line,
        str(c.symbol_id) if c.symbol_id is not None else "",
    )


def _overlap_lines(a: ContextCandidate, b: ContextCandidate) -> int:
    return max(0, min(a.end_line, b.end_line) - max(a.start_line, b.start_line) + 1)


def deduplicate(candidates: list[ScoredCandidate]) -> DedupResult:
    ordered = sorted(candidates, key=_sort_key)

    kept: list[ScoredCandidate] = []
    dropped_duplicate = 0
    dropped_overlap = 0

    for scored in ordered:
        candidate = scored.candidate
        is_duplicate = False
        is_overlap = False

        for existing in kept:
            other = existing.candidate
            if other.file_path != candidate.file_path:
                continue

            if (
                other.start_line == candidate.start_line
                and other.end_line == candidate.end_line
                and other.symbol_id == candidate.symbol_id
            ):
                is_duplicate = True
                break

            if candidate.kind in _EXEMPT_FROM_OVERLAP_SUPPRESSION or other.kind in _EXEMPT_FROM_OVERLAP_SUPPRESSION:
                continue

            overlap = _overlap_lines(other, candidate)
            if overlap == 0:
                continue
            smaller_span = min(
                candidate.end_line - candidate.start_line + 1,
                other.end_line - other.start_line + 1,
            )
            if smaller_span > 0 and overlap / smaller_span >= _OVERLAP_SUPPRESSION_RATIO:
                is_overlap = True
                break

        if is_duplicate:
            dropped_duplicate += 1
            continue
        if is_overlap:
            dropped_overlap += 1
            continue
        kept.append(scored)

    return DedupResult(kept=tuple(kept), dropped_duplicate=dropped_duplicate, dropped_overlap=dropped_overlap)
