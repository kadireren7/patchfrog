"""Deterministic symbol continuity matching between two index versions.

Classifies every symbol from the previous review's structural index
against the current one as unchanged / modified / added / deleted /
moved / renamed / ambiguous -- never via an LLM (see the module
docstring of :mod:`patchfrog.review_memory`), and never via line numbers
alone (a symbol that only moved lines, body unchanged, must still be
recognized as the *same* symbol).

Matching, in priority order, all exact/structural, never fuzzy:

1. Same (effective file path, qualified name, kind) -- the effective
   path applies git's own rename detection
   (:func:`patchfrog.indexing.incremental.compute_change_set`, reused
   directly, never re-derived) so a renamed file's symbols still match
   by their qualified name in the new path. Identical ``content_hash``
   -> UNCHANGED (even if the line range moved -- see below); different
   hash -> MODIFIED.
2. No match by name/kind/path, but exactly one current symbol (same
   kind) anywhere shares the previous symbol's exact ``content_hash``
   -> MOVED (same qualified name) or RENAMED (different qualified name).
   Content-hash equality is exact-body equality, about as strong a
   deterministic rename signal as exists without an LLM.
3. More than one such content-hash match -> AMBIGUOUS. Precision over
   token savings (per spec): callers must treat this the same as a
   fresh symbol needing review, never guess which candidate is "right".
4. No match at all -> DELETED (or ADDED, from the current side).

A previous symbol whose file was deleted (not renamed) is always
DELETED, never left to the generic no-match path, so the persisted
reason is always specific.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from patchfrog.domain.code import SymbolKind
from patchfrog.indexing.models import ChangeSet, FileChangeType
from patchfrog.review_memory.domain import (
    SymbolContinuityResult,
    SymbolContinuityStatus,
    SymbolSnapshot,
)


@dataclass(frozen=True, slots=True)
class RenameMap:
    old_to_new: dict[str, str]
    deleted_paths: frozenset[str]


def build_rename_map(file_changes: ChangeSet) -> RenameMap:
    old_to_new: dict[str, str] = {}
    deleted: set[str] = set()
    for change in file_changes.changes:
        if change.change_type is FileChangeType.RENAMED and change.previous_path is not None:
            old_to_new[change.previous_path] = change.path
        elif change.change_type is FileChangeType.DELETED:
            deleted.add(change.path)
    return RenameMap(old_to_new=old_to_new, deleted_paths=frozenset(deleted))


def match_symbols(
    *,
    previous_symbols: Sequence[SymbolSnapshot],
    current_symbols: Sequence[SymbolSnapshot],
    file_changes: ChangeSet,
) -> tuple[SymbolContinuityResult, ...]:
    rename_map = build_rename_map(file_changes)

    by_identity: dict[tuple[str, str, SymbolKind], SymbolSnapshot] = {
        (s.file_path, s.qualified_name, s.kind): s for s in current_symbols
    }
    by_content_hash: dict[tuple[str, SymbolKind], list[SymbolSnapshot]] = {}
    for s in current_symbols:
        by_content_hash.setdefault((s.content_hash, s.kind), []).append(s)

    consumed: set[UUID] = set()
    results: list[SymbolContinuityResult] = []

    for prev in previous_symbols:
        effective_path = rename_map.old_to_new.get(prev.file_path, prev.file_path)

        if effective_path in rename_map.deleted_paths:
            results.append(
                SymbolContinuityResult(
                    status=SymbolContinuityStatus.DELETED, previous=prev, current=None,
                    reason=f"file deleted: {prev.file_path}",
                )
            )
            continue

        identity_match = by_identity.get((effective_path, prev.qualified_name, prev.kind))
        if identity_match is not None:
            consumed.add(identity_match.id)
            if identity_match.content_hash == prev.content_hash:
                results.append(
                    SymbolContinuityResult(
                        status=SymbolContinuityStatus.UNCHANGED, previous=prev, current=identity_match,
                        reason="same qualified name, same file, identical body",
                    )
                )
            else:
                results.append(
                    SymbolContinuityResult(
                        status=SymbolContinuityStatus.MODIFIED, previous=prev, current=identity_match,
                        reason="same qualified name, same file, body changed",
                    )
                )
            continue

        hash_candidates = [
            c for c in by_content_hash.get((prev.content_hash, prev.kind), []) if c.id not in consumed
        ]
        if len(hash_candidates) == 1:
            candidate = hash_candidates[0]
            consumed.add(candidate.id)
            if candidate.qualified_name == prev.qualified_name:
                results.append(
                    SymbolContinuityResult(
                        status=SymbolContinuityStatus.MOVED, previous=prev, current=candidate,
                        reason="same qualified name and body, different location",
                    )
                )
            else:
                results.append(
                    SymbolContinuityResult(
                        status=SymbolContinuityStatus.RENAMED, previous=prev, current=candidate,
                        reason="identical body, unique match under a different qualified name",
                    )
                )
            continue
        if len(hash_candidates) > 1:
            results.append(
                SymbolContinuityResult(
                    status=SymbolContinuityStatus.AMBIGUOUS, previous=prev, current=None,
                    reason=f"{len(hash_candidates)} current symbols share this exact body -- cannot pick one",
                )
            )
            continue

        results.append(
            SymbolContinuityResult(
                status=SymbolContinuityStatus.DELETED, previous=prev, current=None,
                reason="no current symbol matches by identity or by exact body",
            )
        )

    for cur in current_symbols:
        if cur.id in consumed:
            continue
        results.append(
            SymbolContinuityResult(
                status=SymbolContinuityStatus.ADDED, previous=None, current=cur,
                reason="no previous symbol matches by identity or by exact body",
            )
        )

    return tuple(results)
