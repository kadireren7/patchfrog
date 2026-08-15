"""Shared parser protocol and Tree-sitter node helpers.

Tree-sitter node handling (byte offsets, points, ``.text``) is
encapsulated here and in each language module — nothing outside
:mod:`patchfrog.parsing` should import ``tree_sitter`` directly.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol

from patchfrog.domain.code import Language, ParsedFile, SourceSpan

if TYPE_CHECKING:
    from tree_sitter import Node


class LanguageParser(Protocol):
    """A parser that turns raw file content into a :class:`ParsedFile`."""

    language: Language

    def parse_file(self, *, relative_path: str, content: bytes) -> ParsedFile: ...


def content_hash(data: bytes) -> str:
    """A stable content hash used for symbols and file-change detection."""

    return hashlib.sha256(data).hexdigest()


def node_text(node: Node | None) -> str:
    """Decode a node's source text, tolerating non-UTF-8 bytes."""

    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def span_of(node: Node) -> SourceSpan:
    """Build a 1-indexed-line :class:`SourceSpan` from a Tree-sitter node."""

    return SourceSpan(
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        start_column=node.start_point[1],
        end_column=node.end_point[1],
    )


def qualify(parent_qualified_name: str | None, name: str) -> str:
    """Join a parent qualified name and a child name with a dot."""

    return f"{parent_qualified_name}.{name}" if parent_qualified_name else name
