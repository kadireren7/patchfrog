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
    ) -> BudgetResult:
        remaining_tokens = max_tokens
        remaining_lines = max_lines
        target_line_cap = max(max_lines_per_item, int(max_lines * target_reservation_fraction))
        target_token_cap = max(max_tokens_per_item, int(max_tokens * target_reservation_fraction))

        items: list[ContextItem] = []
        dropped_budget = 0
        relationship_counts: dict[ContextRelationship, int] = {}

        for scored in kept:
            candidate = scored.candidate
            is_target = candidate.kind in _TARGET_KINDS

            if not is_target:
                count = relationship_counts.get(candidate.relationship, 0)
                if count >= max_items_per_relationship:
                    dropped_budget += 1
                    continue

            if remaining_lines < _MIN_USEFUL_LINES or remaining_tokens < 1:
                dropped_budget += 1
                continue

            line_cap = min(target_line_cap if is_target else max_lines_per_item, remaining_lines)
            token_cap = min(target_token_cap if is_target else max_tokens_per_item, remaining_tokens)
            if line_cap < _MIN_USEFUL_LINES or token_cap < 1:
                dropped_budget += 1
                continue

            snippet = self._snippets.extract(
                snapshot,
                relative_path=candidate.file_path,
                start_line=candidate.start_line,
                end_line=candidate.end_line,
                max_lines=line_cap,
            )
            if snippet is None:
                dropped_budget += 1
                continue

            content = snippet.content
            truncated = snippet.truncated
            tokens = estimate_tokens(content)
            if tokens > token_cap:
                # Trim further, proportionally, to fit the token cap --
                # a second, stricter line-count pass rather than mid-line
                # truncation, so a trimmed item never ends on a partial line.
                lines = content.split("\n")
                target_line_count = max(1, int(len(lines) * (token_cap / tokens)))
                content = "\n".join(lines[:target_line_count])
                tokens = estimate_tokens(content)
                truncated = True

            line_count = content.count("\n") + 1 if content else 0
            if line_count < _MIN_USEFUL_LINES or tokens < 1:
                dropped_budget += 1
                continue

            items.append(
                ContextItem(
                    kind=candidate.kind,
                    file_path=candidate.file_path,
                    symbol_id=candidate.symbol_id,
                    symbol_name=candidate.symbol_name,
                    qualified_name=candidate.qualified_name,
                    start_line=candidate.start_line,
                    end_line=min(candidate.end_line, candidate.start_line + line_count - 1),
                    content=content,
                    relationship=candidate.relationship,
                    distance=candidate.distance,
                    score=scored.score,
                    score_breakdown=scored.breakdown,
                    estimated_tokens=tokens,
                    reason=candidate.reason,
                    truncated=truncated,
                )
            )
            remaining_tokens -= tokens
            remaining_lines -= line_count
            relationship_counts[candidate.relationship] = relationship_counts.get(candidate.relationship, 0) + 1

        total_tokens = sum(i.estimated_tokens for i in items)
        total_lines = sum((i.end_line - i.start_line + 1) for i in items)
        return BudgetResult(
            items=tuple(items), dropped_budget=dropped_budget, total_tokens=total_tokens, total_lines=total_lines
        )
