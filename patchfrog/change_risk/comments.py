"""Conservative, language-aware "is this changed line only a comment?".

Errs toward "code": a false "this is code" costs one cheap provider
call; a false "this is only a comment" would skip review of a real
change. Rules:

- ``#`` is a comment only in hash-comment languages (Python, shell,
  YAML, TOML, Ruby). In C/C++ ``#`` is the preprocessor -- always code.
- Directive-like comments are code: shebangs, encoding declarations,
  ``# type:`` comments, ``//go:``/``// +build`` build tags,
  ``// @ts-``/``/// <reference`` and ``# syntax=`` directives.
- Python ``#`` lines are verified *exactly* with the standard-library
  tokenizer when the caller supplies the file's comment-line map
  (:func:`python_comment_lines` on head/base content). Without it, a
  diff-only parity rule applies: a ``#`` line counts only when the
  number of triple-quote delimiters before it *and* after it within its
  hunk are both even (a string opened or closed around it in view makes
  it code). The one case a diff alone cannot see -- a hunk entirely
  inside a multi-line string that opens before and closes after the
  hunk -- is why production callers supply the exact map.
- C-style ``*`` continuation lines only count inside a ``/* ... */``
  block this hunk itself opened; a hunk that starts mid-block is treated
  as code (``*ptr = 1;`` must never be mistaken for a comment).
- Unknown languages (including Markdown-less data files) are never
  comment-only.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import PurePosixPath

from patchfrog.diff.models import DiffFile, DiffLineType

_HASH_COMMENT_SUFFIXES = frozenset({".py", ".pyi", ".sh", ".bash", ".zsh", ".rb", ".yml", ".yaml", ".toml"})
_SLASH_COMMENT_SUFFIXES = frozenset(
    {".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".js", ".jsx", ".mjs", ".cjs", ".ts",
     ".tsx", ".java", ".kt", ".kts", ".go", ".rs", ".cs", ".swift", ".scala", ".php", ".dart"}
)
_HASH_DIRECTIVE_RE = re.compile(r"^#\s*(!|-\*-|.*coding[:=]|type:|syntax=|pyright:|mypy:)", re.IGNORECASE)
_SLASH_DIRECTIVE_RE = re.compile(r"^//\s*(go:|\+build|@ts-|/\s*<reference|#\s*sourcemappingurl|@flow)", re.IGNORECASE)
_TRIPLE_QUOTES = ('"""', "'''")


def comment_style(path: str) -> str | None:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _HASH_COMMENT_SUFFIXES:
        return "hash"
    if suffix in _SLASH_COMMENT_SUFFIXES:
        return "slash"
    return None


def _hash_line_is_comment(stripped: str) -> bool:
    return stripped.startswith("#") and not _HASH_DIRECTIVE_RE.match(stripped)


def is_python_path(path: str) -> bool:
    return path.lower().endswith((".py", ".pyi"))


def python_comment_lines(source: str) -> frozenset[int] | None:
    """1-based line numbers whose only token is a comment (exact, via the
    standard-library tokenizer -- never executes anything). ``None`` if
    the source does not tokenize (then the caller falls back to the
    conservative diff-only rule)."""

    comment_lines: set[int] = set()
    code_lines: set[int] = set()
    ignorable = {
        tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
        tokenize.ENCODING, tokenize.ENDMARKER,
    }
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                comment_lines.add(tok.start[0])
            elif tok.type not in ignorable:
                code_lines.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None
    return frozenset(comment_lines - code_lines)


def _triple_quote_count(text: str) -> int:
    return sum(text.count(q) for q in _TRIPLE_QUOTES)


def count_semantic_changed_lines(
    diff_file: DiffFile,
    *,
    head_comment_lines: frozenset[int] | None = None,
    base_comment_lines: frozenset[int] | None = None,
) -> tuple[int, bool]:
    """Return ``(semantic_changed_lines, comment_only)`` for one file.

    ``semantic_changed_lines`` counts changed (added or deleted) lines
    that are neither blank nor provably a comment. ``comment_only`` is
    ``True`` only when the file had at least one changed line and every
    changed line is blank or provably a comment.

    ``head_comment_lines``/``base_comment_lines`` (Python only): exact
    comment-line maps of the new/old file (see
    :func:`python_comment_lines`); when given they are authoritative for
    added/deleted lines respectively.
    """

    style = comment_style(diff_file.path)
    changed = 0
    semantic = 0
    python = style == "hash" and is_python_path(diff_file.path)

    for hunk in diff_file.hunks:
        in_block = False
        quote_counts = [_triple_quote_count(line.content) for line in hunk.lines]
        total_quotes = sum(quote_counts)
        quotes_before = 0
        for index, line in enumerate(hunk.lines):
            stripped = line.content.strip()
            is_comment = False
            if python:
                exact = (
                    head_comment_lines if line.line_type is DiffLineType.ADDITION
                    else base_comment_lines if line.line_type is DiffLineType.DELETION
                    else None
                )
                number = (
                    line.new_line_number if line.line_type is DiffLineType.ADDITION else line.old_line_number
                )
                if exact is not None and number is not None:
                    is_comment = number in exact and _hash_line_is_comment(stripped)
                else:
                    after = total_quotes - quotes_before - quote_counts[index]
                    is_comment = (
                        _hash_line_is_comment(stripped)
                        and quotes_before % 2 == 0
                        and after % 2 == 0
                    )
                quotes_before += quote_counts[index]
            elif style == "hash":
                is_comment = _hash_line_is_comment(stripped)
            elif style == "slash":
                if in_block:
                    is_comment = True
                    if "*/" in stripped:
                        in_block = False
                        is_comment = stripped.endswith("*/")
                elif stripped.startswith("//"):
                    is_comment = not _SLASH_DIRECTIVE_RE.match(stripped)
                elif stripped.startswith("/*"):
                    end = stripped.find("*/", 2)
                    if end == -1:
                        in_block = True
                        is_comment = True
                    else:
                        is_comment = stripped.endswith("*/") and end == len(stripped) - 2
            if line.line_type is DiffLineType.CONTEXT:
                continue
            changed += 1
            if stripped and not is_comment:
                semantic += 1

    return semantic, changed > 0 and semantic == 0 and style is not None


__all__ = ["comment_style", "count_semantic_changed_lines", "is_python_path", "python_comment_lines"]
