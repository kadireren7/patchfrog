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
    #: The actual 1-indexed line range ``content`` covers -- not
    #: necessarily ``[start_line, start_line + max_lines - 1]``: when
    #: trimming keeps a window around an anchor line (see
    #: :meth:`ContextSnippetService.extract`), the window can start well
    #: after the requested ``start_line``. Callers must use these, not
    #: recompute the range from the request, to report an item's true
    #: location.
    start_line: int
    end_line: int


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
        anchor_line: int | None = None,
    ) -> ExtractedSnippet | None:
        """Returns ``None`` (never raises) for any condition that makes a
        snippet unavailable rather than unsafe to extract from a file
        that does exist: missing file, deleted file, directory, binary
        content, or malformed encoding. Raises :class:`UnsafeSnippetPathError`
        only for a path that is actively unsafe (escapes the checkout
        root) -- that's a caller bug, never a normal "nothing here".

        When the requested range exceeds ``max_lines`` and ``anchor_line``
        is given, trimming keeps a ``max_lines``-sized window *containing*
        ``anchor_line`` -- biased toward ``start_line`` so a short lead-in
        (e.g. a function's signature) is naturally included whenever the
        window comfortably reaches back that far, but never at the cost of
        dropping the anchor itself. Without an anchor, trimming keeps the
        first ``max_lines`` lines of the range, as before.
        """

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
        window_start, window_end = start_line, clamped_end
        if clamped_end - start_line + 1 > max_lines:
            truncated = True
            anchor = anchor_line if anchor_line is not None else start_line
            anchor = min(max(anchor, start_line), clamped_end)  # clamp: a caller-supplied anchor could be stale
            window_start = max(start_line, anchor - max_lines // 2)
            window_end = window_start + max_lines - 1
            if window_end > clamped_end:
                window_end = clamped_end
                window_start = max(start_line, window_end - max_lines + 1)

        selected = lines[window_start - 1 : window_end]
        return ExtractedSnippet(
            content="\n".join(selected), truncated=truncated, start_line=window_start, end_line=window_end
        )
