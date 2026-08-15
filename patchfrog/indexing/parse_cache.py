"""JSON (de)serialization for the content-addressed parse cache.

Isolated from :mod:`patchfrog.persistence` so the wire format for a
cached :class:`~patchfrog.domain.code.ParsedFile` lives next to the
domain model it serializes, not next to the SQL table that happens to
store it.
"""

from __future__ import annotations

import json
from typing import Any

from patchfrog.domain.code import (
    ImportKind,
    Language,
    ParsedCall,
    ParsedFile,
    ParsedImport,
    ParsedSymbol,
    SourceSpan,
    SymbolKind,
)


def serialize_parsed_file(parsed_file: ParsedFile) -> str:
    """Serialize everything except ``path`` — the cache is keyed by content, not location."""

    payload: dict[str, Any] = {
        "symbols": [_symbol_to_dict(s) for s in parsed_file.symbols],
        "imports": [_import_to_dict(i) for i in parsed_file.imports],
        "calls": [_call_to_dict(c) for c in parsed_file.calls],
        "parse_errors": list(parsed_file.parse_errors),
    }
    return json.dumps(payload)


def deserialize_parsed_file(*, relative_path: str, language: Language, payload: str) -> ParsedFile:
    data = json.loads(payload)
    return ParsedFile(
        path=relative_path,
        language=language,
        symbols=tuple(_symbol_from_dict(s) for s in data["symbols"]),
        imports=tuple(_import_from_dict(i) for i in data["imports"]),
        calls=tuple(_call_from_dict(c) for c in data["calls"]),
        parse_errors=tuple(data["parse_errors"]),
    )


def _symbol_to_dict(symbol: ParsedSymbol) -> dict[str, Any]:
    return {
        "name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "kind": symbol.kind.value,
        "span": {
            "start_line": symbol.span.start_line,
            "end_line": symbol.span.end_line,
            "start_column": symbol.span.start_column,
            "end_column": symbol.span.end_column,
        },
        "signature": symbol.signature,
        "parent_qualified_name": symbol.parent_qualified_name,
        "visibility": symbol.visibility,
        "content_hash": symbol.content_hash,
    }


def _symbol_from_dict(data: dict[str, Any]) -> ParsedSymbol:
    span = data["span"]
    return ParsedSymbol(
        name=data["name"],
        qualified_name=data["qualified_name"],
        kind=SymbolKind(data["kind"]),
        span=SourceSpan(
            start_line=span["start_line"],
            end_line=span["end_line"],
            start_column=span["start_column"],
            end_column=span["end_column"],
        ),
        signature=data["signature"],
        parent_qualified_name=data["parent_qualified_name"],
        visibility=data["visibility"],
        content_hash=data["content_hash"],
    )


def _import_to_dict(imp: ParsedImport) -> dict[str, Any]:
    return {
        "raw_text": imp.raw_text,
        "target": imp.target,
        "kind": imp.kind.value,
        "line": imp.line,
    }


def _import_from_dict(data: dict[str, Any]) -> ParsedImport:
    return ParsedImport(
        raw_text=data["raw_text"],
        target=data["target"],
        kind=ImportKind(data["kind"]),
        line=data["line"],
    )


def _call_to_dict(call: ParsedCall) -> dict[str, Any]:
    return {
        "callee_name": call.callee_name,
        "caller_qualified_name": call.caller_qualified_name,
        "line": call.line,
        "column": call.column,
    }


def _call_from_dict(data: dict[str, Any]) -> ParsedCall:
    return ParsedCall(
        callee_name=data["callee_name"],
        caller_qualified_name=data["caller_qualified_name"],
        line=data["line"],
        column=data["column"],
    )
