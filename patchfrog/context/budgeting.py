"""Greedy, deterministic context budgeting.

Candidates arrive already priority-sorted (see
:func:`patchfrog.context.dedup.deduplicate`, whose ``kept`` order *is*
the final ranking order) and are added while the token/line budget lasts.
The target itself gets a reserved minimum so a tight budget can never
displace it (see :attr:`~patchfrog.context.config.ContextConfig.target_reservation_fraction`);
everything else is subject to a per-item cap and a per-relationship
diversity cap so one high-scoring relationship kind can't consume the
whole bundle.

A candidate that doesn't fit is trimmed to what remains, never silently
dropped in favor of leaving budget unused -- large items are marked
``truncated`` rather than excluded outright, unless there's no room at
all.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.context.dedup import ScoredCandidate
from patchfrog.context.domain import ContextItem, ContextItemKind, ContextRelationship
from patchfrog.context.snippets import ContextSnippetService
from patchfrog.context.tokens import estimate_tokens
from patchfrog.repository.snapshot import RepositorySnapshot

_TARGET_KINDS = frozenset({ContextItemKind.TARGET_SYMBOL, ContextItemKind.TARGET_FILE_REGION})
_MIN_USEFUL_LINES = 1


@dataclass(frozen=True, slots=True)
class BudgetResult:
    items: tuple[ContextItem, ...]
    dropped_budget: int
    total_tokens: int
    total_lines: int


class ContextBudgeter:
    def __init__(self, *, snippet_service: ContextSnippetService | None = None) -> None:
        self._snippets = snippet_service or ContextSnippetService()

    def build(
        self,
        snapshot: RepositorySnapshot,
        *,
        kept: tuple[ScoredCandidate, ...],
        max_tokens: int,
        max_lines: int,
        max_tokens_per_item: int,
        max_lines_per_item: int,
        target_reservation_fraction: float,
        max_items_per_relationship: int,
        max_expansion_tokens: int | None = None,
        max_expansion_lines: int | None = None,
    ) -> BudgetResult:
        """``max_expansion_tokens``/``max_expansion_lines``, when given,
        additionally cap how much distance-2 ("expansion") candidates may
        consume *within* the existing ``max_tokens``/``max_lines``
        ceiling -- adaptive mode's bounded reservation for depth-2
        additions (see :class:`patchfrog.context.config.AdaptiveContextConfig`).
        ``None`` (the default -- fixed depth-1/depth-2 modes) means no
        separate cap: distance-2 candidates compete for the shared budget
        exactly as before this milestone existed."""

        remaining_tokens = max_tokens
        remaining_lines = max_lines
        expansion_tokens_remaining = max_expansion_tokens
        expansion_lines_remaining = max_expansion_lines
        target_line_cap = max(max_lines_per_item, int(max_lines * target_reservation_fraction))
        target_token_cap = max(max_tokens_per_item, int(max_tokens * target_reservation_fraction))

        items: list[ContextItem] = []
        dropped_budget = 0
        relationship_counts: dict[ContextRelationship, int] = {}

        for scored in kept:
            candidate = scored.candidate
            is_target = candidate.kind in _TARGET_KINDS
            is_expansion = candidate.distance >= 2

            if not is_target:
                count = relationship_counts.get(candidate.relationship, 0)
                if count >= max_items_per_relationship:
                    dropped_budget += 1
                    continue

            if remaining_lines < _MIN_USEFUL_LINES or remaining_tokens < 1:
                dropped_budget += 1
                continue
            if is_expansion and (
                (expansion_tokens_remaining is not None and expansion_tokens_remaining < 1)
                or (expansion_lines_remaining is not None and expansion_lines_remaining < _MIN_USEFUL_LINES)
            ):
                dropped_budget += 1
                continue

            line_cap = min(target_line_cap if is_target else max_lines_per_item, remaining_lines)
            token_cap = min(target_token_cap if is_target else max_tokens_per_item, remaining_tokens)
            if is_expansion:
                if expansion_lines_remaining is not None:
                    line_cap = min(line_cap, expansion_lines_remaining)
                if expansion_tokens_remaining is not None:
                    token_cap = min(token_cap, expansion_tokens_remaining)
            if line_cap < _MIN_USEFUL_LINES or token_cap < 1:
                dropped_budget += 1
                continue

            snippet = self._snippets.extract(
                snapshot,
                relative_path=candidate.file_path,
                start_line=candidate.start_line,
                end_line=candidate.end_line,
                max_lines=line_cap,
                anchor_line=candidate.anchor_line,
            )
            if snippet is None:
                dropped_budget += 1
                continue

            tokens = estimate_tokens(snippet.content)
            if tokens > token_cap:
                # Trim further, proportionally, to fit the token cap -- by
                # re-extracting with a stricter line cap rather than
                # slicing the already-extracted text, so the anchor-aware
                # windowing above is reused instead of duplicated: a naive
                # prefix-trim here could re-drop the very anchor line the
                # first pass took care to keep.
                current_lines = snippet.end_line - snippet.start_line + 1
                stricter_line_cap = max(1, int(current_lines * (token_cap / tokens)))
                snippet = self._snippets.extract(
                    snapshot,
                    relative_path=candidate.file_path,
                    start_line=candidate.start_line,
                    end_line=candidate.end_line,
                    max_lines=stricter_line_cap,
                    anchor_line=candidate.anchor_line,
                )
                if snippet is None:
                    dropped_budget += 1
                    continue
                tokens = estimate_tokens(snippet.content)

            line_count = snippet.end_line - snippet.start_line + 1
            if line_count < _MIN_USEFUL_LINES or tokens < 1 or not snippet.content:
                dropped_budget += 1
                continue

            items.append(
                ContextItem(
                    kind=candidate.kind,
                    file_path=candidate.file_path,
                    symbol_id=candidate.symbol_id,
                    symbol_name=candidate.symbol_name,
                    qualified_name=candidate.qualified_name,
                    start_line=snippet.start_line,
                    end_line=snippet.end_line,
                    content=snippet.content,
                    relationship=candidate.relationship,
                    distance=candidate.distance,
                    score=scored.score,
                    score_breakdown=scored.breakdown,
                    estimated_tokens=tokens,
                    reason=candidate.reason,
                    truncated=snippet.truncated,
                )
            )
            remaining_tokens -= tokens
            remaining_lines -= line_count
            if is_expansion:
                if expansion_tokens_remaining is not None:
                    expansion_tokens_remaining -= tokens
                if expansion_lines_remaining is not None:
                    expansion_lines_remaining -= line_count
            relationship_counts[candidate.relationship] = relationship_counts.get(candidate.relationship, 0) + 1

        total_tokens = sum(i.estimated_tokens for i in items)
        total_lines = sum((i.end_line - i.start_line + 1) for i in items)
        return BudgetResult(
            items=tuple(items), dropped_budget=dropped_budget, total_tokens=total_tokens, total_lines=total_lines
        )
