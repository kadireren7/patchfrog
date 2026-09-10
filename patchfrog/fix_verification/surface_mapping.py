"""Deterministic finding-surface mapping -- Milestone T (T3) security
correction, Blocker 2 / "LINE MAPPING".

Reuses the exact same content-hash-based matching principle
:mod:`patchfrog.review_memory.symbol_continuity` already established for
Phase 7 incremental review memory (never line numbers alone -- a symbol
that only moved lines, body unchanged, must still be recognized as the
*same* symbol) -- but computed directly via a single-file parse
(:class:`patchfrog.parsing.base.LanguageParser`) rather than requiring a
full repository re-index of the candidate commit, which a bounded,
per-finding fix-verification pass cannot justify.

**Deliberately narrower than the real indexed version**: this only ever
searches the *same file path* on the candidate side, never the whole
repository. A symbol that moved to a *different file* is reported
``UNMAPPABLE``, not found. This is an explicit, honest v1 scope limit,
not a silent gap -- ``UNMAPPABLE`` always resolves to ``INCONCLUSIVE``
in :mod:`patchfrog.fix_verification.service`, never a guessed location
and never a false ``FIXED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from patchfrog.domain.code import Language
from patchfrog.parsing.registry import default_registry
from patchfrog.repository.git import GitError, run_git


class SurfaceMappingStatus(StrEnum):
    #: Same qualified name, same file, identical body -- the flagged
    #: code has not changed at all, even though something else in the
    #: file did.
    UNCHANGED = "unchanged"
    #: Same qualified name, same file, different body -- safely mapped
    #: to a new line range in the same file.
    MODIFIED = "modified"
    #: Not found by identity, but exactly one candidate-side symbol in
    #: the same file shares the original's exact body -- moved/renamed
    #: within the file, safely mapped to its new line range.
    MOVED_OR_RENAMED = "moved_or_renamed"
    #: More than one same-file candidate shares the exact body --
    #: cannot pick one; never guessed.
    AMBIGUOUS = "ambiguous"
    #: No safe mapping at all (no qualified_name/language to key on, no
    #: parser for this language, original blob unreachable, candidate
    #: file deleted, or genuinely no match found in the same file).
    UNMAPPABLE = "unmappable"


@dataclass(frozen=True, slots=True)
class MappedSurface:
    status: SurfaceMappingStatus
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None

    @property
    def is_safely_mapped(self) -> bool:
        """Whether a fallback model may safely be shown code at this
        surface -- true for UNCHANGED/MODIFIED/MOVED_OR_RENAMED only."""

        return self.status in (
            SurfaceMappingStatus.UNCHANGED, SurfaceMappingStatus.MODIFIED, SurfaceMappingStatus.MOVED_OR_RENAMED,
        )


def map_finding_surface(
    *,
    candidate_checkout: Path,
    original_commit_sha: str,
    file_path: str,
    qualified_name: str | None,
    language: Language | None,
) -> MappedSurface:
    """Maps ``file_path``'s ``qualified_name`` symbol from
    ``original_commit_sha`` to its current location in
    ``candidate_checkout`` (which must already have ``original_commit_sha``
    reachable in its git history -- see
    :func:`patchfrog.repository.snapshot.RepositorySnapshotProvider.acquire`'s
    ``also_fetch``). Never raises -- any failure to establish a safe
    mapping returns ``UNMAPPABLE``."""

    if qualified_name is None or language is None:
        return MappedSurface(SurfaceMappingStatus.UNMAPPABLE)

    parser = default_registry().get(language)
    if parser is None:
        return MappedSurface(SurfaceMappingStatus.UNMAPPABLE)

    try:
        original_text = run_git(
            ["-C", str(candidate_checkout), "show", f"{original_commit_sha}:{file_path}"]
        )
    except GitError:
        return MappedSurface(SurfaceMappingStatus.UNMAPPABLE)

    original_parsed = parser.parse_file(relative_path=file_path, content=original_text.encode("utf-8"))
    original_symbol = next((s for s in original_parsed.symbols if s.qualified_name == qualified_name), None)
    if original_symbol is None:
        return MappedSurface(SurfaceMappingStatus.UNMAPPABLE)

    candidate_file = candidate_checkout / file_path
    if not candidate_file.is_file():
        return MappedSurface(SurfaceMappingStatus.UNMAPPABLE)

    candidate_parsed = parser.parse_file(relative_path=file_path, content=candidate_file.read_bytes())

    identity_match = next((s for s in candidate_parsed.symbols if s.qualified_name == qualified_name), None)
    if identity_match is not None:
        status = (
            SurfaceMappingStatus.UNCHANGED
            if identity_match.content_hash == original_symbol.content_hash
            else SurfaceMappingStatus.MODIFIED
        )
        return MappedSurface(
            status, file_path=file_path, start_line=identity_match.span.start_line,
            end_line=identity_match.span.end_line,
        )

    hash_candidates = [s for s in candidate_parsed.symbols if s.content_hash == original_symbol.content_hash]
    if len(hash_candidates) == 1:
        match = hash_candidates[0]
        return MappedSurface(
            SurfaceMappingStatus.MOVED_OR_RENAMED, file_path=file_path, start_line=match.span.start_line,
            end_line=match.span.end_line,
        )
    if len(hash_candidates) > 1:
        return MappedSurface(SurfaceMappingStatus.AMBIGUOUS)

    return MappedSurface(SurfaceMappingStatus.UNMAPPABLE)
