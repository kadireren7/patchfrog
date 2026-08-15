"""Internal domain models for parsed source code structure.

This is the stable boundary between the Tree-sitter-based parsers
(:mod:`patchfrog.parsing`) and everything downstream (indexing,
intelligence, persistence). Nothing outside :mod:`patchfrog.parsing`
should ever touch a raw Tree-sitter node — parsers translate into these
models and nothing else crosses the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Language(StrEnum):
    """A source language PatchFrog can parse."""

    PYTHON = "python"
    C = "c"
    CPP = "cpp"


class SymbolKind(StrEnum):
    """The kind of a structural symbol extracted from source code.

    Only kinds a parser can extract reliably are populated — see each
    language parser's module docstring for exactly which kinds it
    produces.
    """

    MODULE = "module"
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    STRUCT = "struct"
    ENUM = "enum"
    UNION = "union"
    INTERFACE = "interface"
    VARIABLE = "variable"
    CONSTANT = "constant"
    TYPE_ALIAS = "type_alias"
    MACRO = "macro"


class ImportKind(StrEnum):
    """Whether an import/include target resolves within the repository."""

    LOCAL = "local"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A 1-indexed line/0-indexed column range within a file."""

    start_line: int
    end_line: int
    start_column: int
    end_column: int


@dataclass(frozen=True, slots=True)
class ParsedSymbol:
    """A structural symbol (function, class, struct, ...) found in a file."""

    name: str
    qualified_name: str
    kind: SymbolKind
    span: SourceSpan
    signature: str | None
    parent_qualified_name: str | None
    visibility: str | None
    content_hash: str


@dataclass(frozen=True, slots=True)
class ParsedImport:
    """A single `import`/`#include` statement found in a file."""

    raw_text: str
    target: str
    kind: ImportKind
    line: int


@dataclass(frozen=True, slots=True)
class ParsedCall:
    """A call site found in a file.

    Resolution to a concrete symbol (if any) happens later, in
    :mod:`patchfrog.intelligence.resolution` — a freshly parsed call only
    knows the textual callee name, never a resolved target.
    """

    callee_name: str
    caller_qualified_name: str | None
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """The full structural result of parsing a single source file."""

    path: str
    language: Language
    symbols: tuple[ParsedSymbol, ...] = field(default_factory=tuple)
    imports: tuple[ParsedImport, ...] = field(default_factory=tuple)
    calls: tuple[ParsedCall, ...] = field(default_factory=tuple)
    parse_errors: tuple[str, ...] = field(default_factory=tuple)
