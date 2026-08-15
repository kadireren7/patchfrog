"""Maps a :class:`~patchfrog.domain.code.Language` to its parser.

This is the single place that knows all supported languages exist —
adding a new language means adding one parser module and one line here,
not touching every layer above.
"""

from __future__ import annotations

from patchfrog.domain.code import Language
from patchfrog.parsing.base import LanguageParser


class ParserRegistry:
    def __init__(self) -> None:
        self._parsers: dict[Language, LanguageParser] = {}

    def register(self, parser: LanguageParser) -> None:
        self._parsers[parser.language] = parser

    def get(self, language: Language) -> LanguageParser | None:
        return self._parsers.get(language)

    def supported_languages(self) -> frozenset[Language]:
        return frozenset(self._parsers.keys())


def default_registry() -> ParserRegistry:
    """The registry populated with all languages PatchFrog ships support for."""

    from patchfrog.parsing.c import CParser
    from patchfrog.parsing.cpp import CppParser
    from patchfrog.parsing.python import PythonParser

    registry = ParserRegistry()
    registry.register(PythonParser())
    registry.register(CParser())
    registry.register(CppParser())
    return registry
