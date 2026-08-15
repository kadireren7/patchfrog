"""Language detection.

Mostly extension-based, with one deliberate exception: ``.h`` is
genuinely ambiguous between C and C++, so it falls back to a lightweight
content heuristic — the presence of unambiguous C++-only syntax
(``namespace``, ``class``, ``template<``, ``std::``, ``::`` qualified
names, access specifiers). This is a heuristic, not a guarantee; Phase 2
accepts that per its own scope ("acceptable in Phase 2 to use
repository/file context heuristics for headers").
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from patchfrog.domain.code import Language

_EXTENSION_LANGUAGE: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".c": Language.C,
    ".cc": Language.CPP,
    ".cpp": Language.CPP,
    ".cxx": Language.CPP,
    ".hh": Language.CPP,
    ".hpp": Language.CPP,
    ".hxx": Language.CPP,
}

_AMBIGUOUS_HEADER_EXTENSIONS = frozenset({".h"})

_CPP_ONLY_MARKERS = (
    re.compile(rb"\bnamespace\s+\w+"),
    re.compile(rb"\bclass\s+\w+"),
    re.compile(rb"\btemplate\s*<"),
    re.compile(rb"\bstd::"),
    re.compile(rb"::\s*\w"),
    re.compile(rb"\bpublic\s*:"),
    re.compile(rb"\bprivate\s*:"),
    re.compile(rb"\bprotected\s*:"),
)

_HEADER_SNIFF_BYTES = 8192


def detect_language(*, relative_path: str, content: bytes | None = None) -> Language | None:
    """Detect the language of a file, or ``None`` if unsupported/unrecognized.

    ``content`` is only consulted for the ambiguous ``.h`` extension; pass
    it when available for a more accurate C-vs-C++ header call.
    """

    suffix = PurePosixPath(relative_path).suffix.lower()

    if suffix in _EXTENSION_LANGUAGE:
        return _EXTENSION_LANGUAGE[suffix]

    if suffix in _AMBIGUOUS_HEADER_EXTENSIONS:
        return _detect_header_language(content)

    return None


def _detect_header_language(content: bytes | None) -> Language:
    if content is None:
        return Language.C
    sample = content[:_HEADER_SNIFF_BYTES]
    if any(marker.search(sample) for marker in _CPP_ONLY_MARKERS):
        return Language.CPP
    return Language.C
