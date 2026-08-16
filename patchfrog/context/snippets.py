"""Safe source-snippet extraction for context items.

Reuses :meth:`patchfrog.repository.snapshot.RepositorySnapshot.resolve_path`
for path containment (symlink-escape and ``../`` traversal safety) rather
than duplicating that logic -- the same guarantee Phase 2 indexing
depends on. Adds the bounds a *snippet* extractor specifically needs on
top: binary-content rejection, a hard byte cap per read, and line-range
validation against the file actually on disk.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchfrog.repository.snapshot import RepositorySnapshot

#: A single source file is never read past this many bytes -- generous
#: for any real source file, small enough to bound a pathological one
#: (e.g. a vendored minified blob) without a per-repository allowlist.
MAX_FILE_READ_BYTES = 2 * 1024 * 1024
_NULL_BYTE = b"\x00"


class UnsafeSnippetPathError(ValueError):
    """The requested path escapes the checkout root, or isn't a regular file."""


@dataclass(frozen=True, slots=True)
class ExtractedSnippet:
    content: str
    truncated: bool


class ContextSnippetService:
    """Extracts a bounded, safe source-line range from a repository snapshot."""

    def extract(
        self,
        snapshot: RepositorySnapshot,
        *,
        relative_path: str,
        start_line: int,
        end_line: int,
        max_lines: int,
    ) -> ExtractedSnippet | None:
        """Returns ``None`` (never raises) for any condition that makes a
        snippet unavailable rather than unsafe to extract from a file
        that does exist: missing file, deleted file, directory, binary
        content, or malformed encoding. Raises :class:`UnsafeSnippetPathError`
        only for a path that is actively unsafe (escapes the checkout
        root) -- that's a caller bug, never a normal "nothing here"."""

        try:
            # Fully resolves symlinks (see resolve_path's docstring), so a
            # symlink is only ever readable here if what it *actually*
            # points at is within the repository root -- one that escapes
            # is rejected below as unsafe, one that doesn't is
            # indistinguishable from a regular in-repo file at this point.
            path = snapshot.resolve_path(relative_path)
        except ValueError as exc:
            raise UnsafeSnippetPathError(str(exc)) from exc

        if not path.is_file():
            return None

        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > MAX_FILE_READ_BYTES:
            return None

        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if _NULL_BYTE in raw:
            return None  # binary content

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

        lines = text.splitlines()
        if start_line < 1 or start_line > len(lines):
            return None
        clamped_end = min(end_line, len(lines))
        if clamped_end < start_line:
            return None

        truncated = False
        if clamped_end - start_line + 1 > max_lines:
            clamped_end = start_line + max_lines - 1
            truncated = True

        selected = lines[start_line - 1 : clamped_end]
        return ExtractedSnippet(content="\n".join(selected), truncated=truncated)
